"""Tests for static mod inspection.

Builds real jars on the fly rather than mocking zipfile, because the thing being
tested is whether we can read what mod authors actually ship.
"""

from __future__ import annotations

import json
import os
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


def annotated_class(targets: list[str]) -> bytes:
    """A complete class file carrying ``@Mixin(Foo.class, ...)``.

    Built by hand because the point of the reader under test is that it works
    on what mod authors ship without a bytecode library in the dependency tree.
    """
    strings = [
        "Lorg/spongepowered/asm/mixin/Mixin;",   # 1
        "RuntimeVisibleAnnotations",             # 2
        "value",                                 # 3
        "java/lang/Object",                      # 4
    ]
    descriptors = [f"L{t};" for t in targets]
    strings.extend(descriptors)

    pool = b"".join(
        b"\x01" + struct.pack(">H", len(s)) + s.encode() for s in strings
    )
    # One CONSTANT_Class pointing at "java/lang/Object", for this_class/super.
    class_index = len(strings) + 1
    pool += b"\x07" + struct.pack(">H", 4)

    # element_value: an array of class constants, one per target.
    element = b"[" + struct.pack(">H", len(descriptors))
    for offset in range(len(descriptors)):
        element += b"c" + struct.pack(">H", 5 + offset)

    # annotation: type_index, num_element_value_pairs, then (name_index, value)
    annotation = (
        struct.pack(">H", 1)        # type_index -> the @Mixin descriptor
        + struct.pack(">H", 1)      # one element-value pair
        + struct.pack(">H", 3)      # element name_index -> "value"
        + element
    )
    annotations = struct.pack(">H", 1) + annotation
    attribute = (
        struct.pack(">H", 2) + struct.pack(">I", len(annotations)) + annotations
    )

    return (
        b"\xca\xfe\xba\xbe"
        + struct.pack(">HH", 0, 65)
        + struct.pack(">H", class_index + 1)
        + pool
        + struct.pack(">H", 0x0021)          # access_flags
        + struct.pack(">H", class_index)     # this_class
        + struct.pack(">H", class_index)     # super_class
        + struct.pack(">H", 0)               # interfaces
        + struct.pack(">H", 0)               # fields
        + struct.pack(">H", 0)               # methods
        + struct.pack(">H", 1)               # attributes
        + attribute
    )


def fabric_jar(
    path: Path,
    meta: dict,
    mixin_classes: dict[str, list[str]] | None = None,
    *,
    config: dict | None = None,
    config_name: str = "mod.mixins.json",
    annotated: dict[str, list[str]] | None = None,
    nested: dict[str, Path] | None = None,
):
    with zipfile.ZipFile(path, "w") as archive:
        if config is not None:
            meta = {**meta, "mixins": [config_name]}
            archive.writestr(config_name, json.dumps(config))
        archive.writestr("fabric.mod.json", json.dumps({"schemaVersion": 1, **meta}))
        for name, targets in (mixin_classes or {}).items():
            archive.writestr(f"mixin/{name}.class", class_naming(targets))
        for name, targets in (annotated or {}).items():
            archive.writestr(f"{name}.class", annotated_class(targets))
        for name, source in (nested or {}).items():
            archive.writestr(f"META-INF/jars/{name}", Path(source).read_bytes())
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

    def test_a_module_carried_in_the_jar_satisfies_the_dependency(self, tmp_path):
        """Jar-in-Jar is how a mod runs without the whole API.

        Sodium bundles the five Fabric API modules it depends on and needs no
        Fabric API installed. Asking only whether the aggregate id was present
        reported those five as a missing dependency, at ERROR, for a mod set
        that starts — on the most widely installed mod in the ecosystem.
        """
        a = fabric_jar(tmp_path / "a.jar", {
            "id": "a", "version": "1",
            "depends": {
                "fabric-renderer-api-v1": "*",
                "fabric-resource-loader-v0": "*",
            },
        })
        # What read_jars sees for a bundled module: the id in its own right,
        # with no aggregate fabric-api anywhere.
        one = fabric_jar(tmp_path / "renderer.jar", {
            "id": "fabric-renderer-api-v1", "version": "3.4.1",
        })
        two = fabric_jar(tmp_path / "loader.jar", {
            "id": "fabric-resource-loader-v0", "version": "1.3.1",
        })
        result = inspect_mods(read_jars([a, one, two]))
        assert "missing_dependency" not in {f.code for f in result.findings}

    def test_a_module_that_is_absent_is_still_reported(self, tmp_path):
        # The collapse into one finding still has to happen for the ones that
        # really are missing, or the fix above would just silence the check.
        a = fabric_jar(tmp_path / "a.jar", {
            "id": "a", "version": "1",
            "depends": {
                "fabric-renderer-api-v1": "*",
                "fabric-rendering-fluids-v1": "*",
            },
        })
        one = fabric_jar(tmp_path / "renderer.jar", {
            "id": "fabric-renderer-api-v1", "version": "3.4.1",
        })
        result = inspect_mods(read_jars([a, one]))
        missing = [f for f in result.findings if f.code == "missing_dependency"]
        assert len(missing) == 1
        assert "1 of its modules" in missing[0].detail
        assert "fabric-rendering-fluids-v1" in missing[0].detail
        assert "fabric-renderer-api-v1" not in missing[0].detail

    def test_a_bundled_module_is_still_version_checked(self, tmp_path):
        # Falling through to the ordinary dependency path means the range is
        # checked too, which the aggregate branch never did.
        a = fabric_jar(tmp_path / "a.jar", {
            "id": "a", "version": "1",
            "depends": {"fabric-renderer-api-v1": ">=9.0.0"},
        })
        one = fabric_jar(tmp_path / "renderer.jar", {
            "id": "fabric-renderer-api-v1", "version": "3.4.1",
        })
        result = inspect_mods(read_jars([a, one]))
        assert "version_conflict" in {f.code for f in result.findings}

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
        # No @Mixin annotation could be read from these, so the finding is the
        # weaker footprint one — kept as a distinct code so a reader can tell a
        # declared target from a class a mixin merely mentions.
        overlap = next(
            f for f in result.findings if f.code == "mixin_footprint_overlap"
        )
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


def _real_jar() -> Path | None:
    """A real shipped mod to test against, if one is available.

    Set MCBENCH_SAMPLE_JAR to a mod jar to enable these. Absent, they skip:
    the jar is not redistributable (docs/LICENSING.md), so it cannot live in
    the repository.
    """
    raw = os.environ.get("MCBENCH_SAMPLE_JAR")
    if not raw:
        return None
    try:
        path = Path(raw)
        return path if path.is_file() else None
    except OSError:
        return None


REAL_JAR = _real_jar()


@pytest.mark.skipif(REAL_JAR is None, reason="set MCBENCH_SAMPLE_JAR to enable")
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
