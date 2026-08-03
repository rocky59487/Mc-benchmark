"""Tests for static mod inspection.

Builds real jars on the fly rather than mocking zipfile, because the thing being
tested is whether we can read what mod authors actually ship.
"""

from __future__ import annotations

import json
import struct
import zipfile
from pathlib import Path

import pytest

from mcbench.inspect import (
    Severity,
    inspect_mods,
    mixin_targets,
    read_jar,
    read_jars,
)


def class_naming(targets: list[str]) -> bytes:
    """A minimal class file whose constant pool contains the given strings."""
    entries = [b"\x01" + struct.pack(">H", len(t)) + t.encode() for t in targets]
    return (
        b"\xca\xfe\xba\xbe"
        + struct.pack(">HH", 0, 65)
        + struct.pack(">H", len(entries) + 1)
        + b"".join(entries)
    )


def fabric_jar(path: Path, meta: dict, mixin_classes: dict[str, list[str]] | None = None):
    with zipfile.ZipFile(path, "w") as archive:
        archive.writestr("fabric.mod.json", json.dumps({"schemaVersion": 1, **meta}))
        for name, targets in (mixin_classes or {}).items():
            archive.writestr(f"mixin/{name}.class", class_naming(targets))
    return path


class TestConstantPoolScanning:
    def test_extracts_minecraft_references(self):
        data = class_naming([
            "Lnet/minecraft/client/Minecraft;",
            "Lnet/minecraft/world/level/Level;",
            "Ljava/lang/String;",
        ])
        targets = mixin_targets(data)
        assert "net/minecraft/client/Minecraft" in targets
        assert "net/minecraft/world/level/Level" in targets
        assert not any("java/lang" in t for t in targets)

    def test_ignores_non_class_data(self):
        assert mixin_targets(b"not a class file at all") == set()
        assert mixin_targets(b"") == set()

    def test_survives_a_truncated_class(self):
        data = class_naming(["Lnet/minecraft/client/Minecraft;"])[:12]
        assert isinstance(mixin_targets(data), set)

    def test_handles_long_and_double_taking_two_pool_slots(self):
        """A genuine JVM spec quirk; getting it wrong desynchronises the walk."""
        entries = [
            b"\x01" + struct.pack(">H", 32) + b"Lnet/minecraft/client/Minecraft;",
            b"\x05" + b"\x00" * 8,  # CONSTANT_Long, consumes two slots
            b"\x01" + struct.pack(">H", 30) + b"Lnet/minecraft/world/entity/;",
        ]
        data = (
            b"\xca\xfe\xba\xbe" + struct.pack(">HH", 0, 65)
            + struct.pack(">H", 5) + b"".join(entries)
        )
        targets = mixin_targets(data)
        assert "net/minecraft/client/Minecraft" in targets


class TestFabricMetadata:
    def test_reads_the_basics(self, tmp_path):
        path = fabric_jar(tmp_path / "a.jar", {
            "id": "testmod", "version": "1.2.3", "name": "Test",
            "environment": "client", "license": "MIT",
            "depends": {"fabricloader": ">=0.16.0"},
            "breaks": {"othermod": "*"},
        })
        meta = read_jar(path)
        assert meta.mod_id == "testmod"
        assert meta.version == "1.2.3"
        assert meta.loader == "fabric"
        assert meta.client_only
        assert meta.depends["fabricloader"] == ">=0.16.0"
        assert meta.breaks["othermod"] == "*"

    def test_list_valued_version_ranges_are_joined(self):
        from mcbench.inspect import _version_spec

        assert _version_spec([">=1.0", "<2.0"]) == ">=1.0 || <2.0"

    def test_an_unreadable_jar_is_reported_not_raised(self, tmp_path):
        path = tmp_path / "broken.jar"
        path.write_bytes(b"definitely not a zip")
        meta = read_jar(path)
        assert meta.errors
        assert meta.mod_id == "broken"

    def test_a_jar_without_mod_metadata_is_reported(self, tmp_path):
        path = tmp_path / "plain.jar"
        with zipfile.ZipFile(path, "w") as archive:
            archive.writestr("README.txt", "hello")
        meta = read_jar(path)
        assert any("no recognised mod metadata" in e for e in meta.errors)


class TestBukkitMetadata:
    def test_reads_plugin_yml(self, tmp_path):
        path = tmp_path / "plug.jar"
        with zipfile.ZipFile(path, "w") as archive:
            archive.writestr("plugin.yml", (
                "name: MyPlugin\nversion: 3.1.4\n"
                "main: com.example.Main\n"
                "depend:\n  - Vault\n  - WorldEdit\n"
            ))
        meta = read_jar(path)
        assert meta.loader == "bukkit"
        assert meta.mod_id == "MyPlugin"
        assert meta.version == "3.1.4"
        assert "Vault" in meta.depends
        assert "WorldEdit" in meta.depends

    def test_reads_inline_list_form(self, tmp_path):
        path = tmp_path / "plug2.jar"
        with zipfile.ZipFile(path, "w") as archive:
            archive.writestr("plugin.yml", "name: P\nversion: 1\ndepend: [Vault]\n")
        assert "Vault" in read_jar(path).depends


class TestNeoForgeMetadata:
    def test_reads_mods_toml_including_incompatibilities(self, tmp_path):
        path = tmp_path / "nf.jar"
        with zipfile.ZipFile(path, "w") as archive:
            archive.writestr("META-INF/neoforge.mods.toml", """
license = "MIT"
[[mods]]
modId = "examplemod"
version = "1.0.0"
displayName = "Example"

[[dependencies.examplemod]]
modId = "neoforge"
type = "required"
versionRange = "[21,)"

[[dependencies.examplemod]]
modId = "badmod"
type = "incompatible"
versionRange = "*"
""")
        meta = read_jar(path)
        assert meta.loader == "neoforge"
        assert meta.mod_id == "examplemod"
        assert "neoforge" in meta.depends
        assert "badmod" in meta.breaks


class TestAnalysis:
    def test_detects_a_declared_conflict(self, tmp_path):
        a = fabric_jar(tmp_path / "a.jar", {"id": "a", "version": "1", "breaks": {"b": "*"}})
        b = fabric_jar(tmp_path / "b.jar", {"id": "b", "version": "1"})
        result = inspect_mods(read_jars([a, b]))
        codes = {f.code for f in result.findings}
        assert "declared_conflict" in codes
        assert not result.ok

    def test_a_declared_break_against_an_absent_mod_is_not_a_finding(self, tmp_path):
        a = fabric_jar(tmp_path / "a.jar", {"id": "a", "version": "1", "breaks": {"ghost": "*"}})
        result = inspect_mods(read_jars([a]))
        assert "declared_conflict" not in {f.code for f in result.findings}

    def test_detects_a_missing_dependency(self, tmp_path):
        a = fabric_jar(tmp_path / "a.jar", {
            "id": "a", "version": "1", "depends": {"missinglib": ">=2"}
        })
        result = inspect_mods(read_jars([a]))
        assert "missing_dependency" in {f.code for f in result.findings}

    def test_ambient_dependencies_are_not_missing(self, tmp_path):
        a = fabric_jar(tmp_path / "a.jar", {
            "id": "a", "version": "1",
            "depends": {"minecraft": "*", "java": ">=21", "fabricloader": "*"},
        })
        result = inspect_mods(read_jars([a]))
        assert "missing_dependency" not in {f.code for f in result.findings}

    def test_fabric_api_modules_collapse_into_one_finding(self, tmp_path):
        """Six module dependencies are one absent artefact.

        Reporting them separately makes a single real problem look like six, and
        a tool that cries wolf gets ignored.
        """
        a = fabric_jar(tmp_path / "a.jar", {
            "id": "a", "version": "1",
            "depends": {
                "fabric-rendering-fluids-v1": ">=2.0.0",
                "fabric-resource-loader-v0": "*",
                "fabric-block-getter-api-v2": "*",
            },
        })
        result = inspect_mods(read_jars([a]))
        missing = [f for f in result.findings if f.code == "missing_dependency"]
        assert len(missing) == 1
        assert "Fabric API" in missing[0].summary
        assert "3 of its modules" in missing[0].detail

    def test_fabric_api_present_satisfies_its_modules(self, tmp_path):
        a = fabric_jar(tmp_path / "a.jar", {
            "id": "a", "version": "1", "depends": {"fabric-resource-loader-v0": "*"},
        })
        api = fabric_jar(tmp_path / "api.jar", {"id": "fabric-api", "version": "0.100"})
        result = inspect_mods(read_jars([a, api]))
        assert "missing_dependency" not in {f.code for f in result.findings}

    def test_detects_duplicate_ids(self, tmp_path):
        a = fabric_jar(tmp_path / "one.jar", {"id": "same", "version": "1"})
        b = fabric_jar(tmp_path / "two.jar", {"id": "same", "version": "2"})
        result = inspect_mods(read_jars([a, b]))
        assert "duplicate_id" in {f.code for f in result.findings}

    def test_provides_satisfies_a_dependency(self, tmp_path):
        a = fabric_jar(tmp_path / "a.jar", {"id": "a", "version": "1", "depends": {"lib": "*"}})
        b = fabric_jar(tmp_path / "b.jar", {"id": "b", "version": "1", "provides": ["lib"]})
        result = inspect_mods(read_jars([a, b]))
        assert "missing_dependency" not in {f.code for f in result.findings}


class TestMixinOverlap:
    def test_reports_classes_touched_by_two_mods(self, tmp_path):
        a = fabric_jar(tmp_path / "a.jar", {"id": "a", "version": "1"}, {
            "M": ["Lnet/minecraft/client/Minecraft;", "Lnet/minecraft/client/Camera;"],
        })
        b = fabric_jar(tmp_path / "b.jar", {"id": "b", "version": "1"}, {
            "M": ["Lnet/minecraft/client/Camera;"],
        })
        result = inspect_mods(read_jars([a, b]))
        assert "net/minecraft/client/Camera" in result.overlaps
        assert set(result.overlaps["net/minecraft/client/Camera"]) == {"a", "b"}
        # Only one mod touches Minecraft, so it is not contended.
        assert "net/minecraft/client/Minecraft" not in result.overlaps

    def test_overlap_is_informational_not_an_error(self, tmp_path):
        """Mods routinely coexist on the same class; overlap is a lead, not a verdict."""
        a = fabric_jar(tmp_path / "a.jar", {"id": "a", "version": "1"}, {
            "M": ["Lnet/minecraft/client/Camera;"]})
        b = fabric_jar(tmp_path / "b.jar", {"id": "b", "version": "1"}, {
            "M": ["Lnet/minecraft/client/Camera;"]})
        result = inspect_mods(read_jars([a, b]))
        overlap = next(f for f in result.findings if f.code == "mixin_overlap")
        assert overlap.severity is Severity.INFO
        assert result.ok

    def test_hotspots_rank_by_contention(self, tmp_path):
        jars = []
        for name in "abc":
            jars.append(fabric_jar(tmp_path / f"{name}.jar", {"id": name, "version": "1"}, {
                "M": ["Lnet/minecraft/client/Camera;"]}))
        jars.append(fabric_jar(tmp_path / "d.jar", {"id": "d", "version": "1"}, {
            "M": ["Lnet/minecraft/client/Camera;", "Lnet/minecraft/world/level/Level;"]}))
        jars.append(fabric_jar(tmp_path / "e.jar", {"id": "e", "version": "1"}, {
            "M": ["Lnet/minecraft/world/level/Level;"]}))
        result = inspect_mods(read_jars(jars))
        top = result.hotspots(limit=1)
        assert top[0][0] == "net/minecraft/client/Camera"
        assert len(top[0][1]) == 4

    def test_threshold_is_configurable(self, tmp_path):
        a = fabric_jar(tmp_path / "a.jar", {"id": "a", "version": "1"}, {
            "M": ["Lnet/minecraft/client/Camera;"]})
        b = fabric_jar(tmp_path / "b.jar", {"id": "b", "version": "1"}, {
            "M": ["Lnet/minecraft/client/Camera;"]})
        result = inspect_mods(read_jars([a, b]), overlap_threshold=3)
        assert result.overlaps == {}


REAL_JAR = Path(
    "/root/.claude/uploads/185810f2-003c-50db-9b8b-cdb95e052274/"
    "f789e4ae-sodiumfabric0.9.1mc26.1.2.jar"
)


@pytest.mark.skipif(not REAL_JAR.exists(), reason="sample jar not present")
class TestAgainstARealMod:
    """Verifies against a real shipped mod rather than only fixtures."""

    def test_reads_sodium(self):
        meta = read_jar(REAL_JAR)
        assert meta.mod_id == "sodium"
        assert meta.loader == "fabric"
        assert meta.client_only
        assert meta.errors == ()

    def test_finds_its_declared_incompatibilities(self):
        # Sodium ships a substantial breaks block; reading it is the cheapest
        # conflict detection available.
        meta = read_jar(REAL_JAR)
        assert len(meta.breaks) > 10
        assert "embeddium" in meta.breaks

    def test_extracts_a_realistic_mixin_footprint(self):
        meta = read_jar(REAL_JAR)
        assert len(meta.mixin_targets) > 50
        assert all(t.startswith("net/minecraft/") for t in meta.mixin_targets)
