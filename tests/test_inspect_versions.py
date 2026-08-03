"""Version-aware inspection, mixin configuration reading, and nested jars.

Covers the two ways presence-only checking was wrong in opposite directions —
certifying a pack that cannot load, and rejecting one that is fine — plus the
mixin scan that selected classes by filename and the bundled dependencies it
reported as missing.
"""

from __future__ import annotations

import zipfile
from pathlib import Path

import pytest
from test_inspect import annotated_class, class_naming, fabric_jar

from mcbench.inspect import (
    InspectionTarget,
    annotated_mixin_targets,
    inspect_mods,
    read_jar,
    read_jars,
)
from mcbench.versions import Dialect, Satisfaction, satisfies


def codes(result) -> set[str]:
    return {f.code for f in result.findings}


def forge_jar(path: Path, *, mod_id: str, version: str, dependencies: str = "") -> Path:
    toml = f"""
modLoader = "javafml"
loaderVersion = "[47,)"
license = "MIT"
[[mods]]
modId = "{mod_id}"
version = "{version}"
{dependencies}
"""
    with zipfile.ZipFile(path, "w") as archive:
        archive.writestr("META-INF/mods.toml", toml)
    return path


class TestVersionRanges:
    @pytest.mark.parametrize(
        ("installed", "spec", "expected"),
        [
            ("1", ">=2", Satisfaction.VIOLATED),
            ("2.0.1", ">=2", Satisfaction.SATISFIED),
            ("1.2.4", "1.2", Satisfaction.SATISFIED),
            ("1.3.0", "~1.2.3", Satisfaction.VIOLATED),
            ("1.9.9", "^1.2.3", Satisfaction.SATISFIED),
            ("2.0.0", "^1.2.3", Satisfaction.VIOLATED),
            ("1.5", ">=1.0 <2.0", Satisfaction.SATISFIED),
            ("2.5", ">=1.0 <2.0", Satisfaction.VIOLATED),
            ("3.1", ">=1.0 <2.0 || >=3.0", Satisfaction.SATISFIED),
            ("garbage", ">=1.0", Satisfaction.UNKNOWN),
        ],
    )
    def test_fabric(self, installed, spec, expected):
        assert satisfies(installed, spec, dialect=Dialect.FABRIC) is expected

    @pytest.mark.parametrize(
        ("installed", "spec", "expected"),
        [
            ("1.5", "[1.0,2.0)", Satisfaction.SATISFIED),
            ("2.0", "[1.0,2.0)", Satisfaction.VIOLATED),
            ("2.0", "[1.0,2.0]", Satisfaction.SATISFIED),
            ("0.9", "[1.0,)", Satisfaction.VIOLATED),
            # Forge reads a bare version as a minimum, not an equality.
            ("5.0", "1.0", Satisfaction.SATISFIED),
        ],
    )
    def test_maven(self, installed, spec, expected):
        assert satisfies(installed, spec, dialect=Dialect.MAVEN) is expected

    def test_build_metadata_is_not_precedence(self):
        """Fabric mods encode the game version after '+'; ordering by it would
        rank mods by which Minecraft they target rather than by their release."""
        assert satisfies("0.92.0+1.20.1", ">=0.90.0") is Satisfaction.SATISFIED

    def test_bukkit_has_no_range_syntax_to_check(self):
        assert satisfies("1.0", ">=2", dialect=Dialect.BUKKIT) is Satisfaction.UNKNOWN


class TestDependencyVersions:
    def test_an_unsatisfied_range_is_an_error(self, tmp_path):
        """`lib >=2` with `lib 1` present: previously certified as fine."""
        app = fabric_jar(tmp_path / "app.jar", {
            "id": "app", "version": "1", "depends": {"lib": ">=2"},
        })
        lib = fabric_jar(tmp_path / "lib.jar", {"id": "lib", "version": "1"})
        result = inspect_mods(read_jars([app, lib]))
        assert "version_conflict" in codes(result)
        assert not result.ok

    def test_a_satisfied_range_is_clean(self, tmp_path):
        app = fabric_jar(tmp_path / "app.jar", {
            "id": "app", "version": "1", "depends": {"lib": ">=2"},
        })
        lib = fabric_jar(tmp_path / "lib.jar", {"id": "lib", "version": "2.3.1"})
        result = inspect_mods(read_jars([app, lib]))
        assert "version_conflict" not in codes(result)
        assert result.ok

    def test_an_undecidable_range_warns_rather_than_passing(self, tmp_path):
        app = fabric_jar(tmp_path / "app.jar", {
            "id": "app", "version": "1", "depends": {"lib": ">=2"},
        })
        lib = fabric_jar(tmp_path / "lib.jar", {"id": "lib", "version": "SNAPSHOT"})
        result = inspect_mods(read_jars([app, lib]))
        assert "undecidable_dependency" in codes(result)
        # A warning, not an error: it may well be fine, and we do not know.
        assert result.ok

    def test_an_unchecked_constraint_names_the_option_that_would_check_it(
        self, tmp_path
    ):
        """The remedy has to be about this constraint, not a fixed sentence.

        Every unchecked ambient dependency used to say "pass
        --minecraft-version and --loader-version", including on a run where
        both were supplied and on constraints neither of them pins.
        """
        app = fabric_jar(tmp_path / "app.jar", {
            "id": "app", "version": "1",
            "depends": {"fabricloader": ">=0.16", "java": ">=21"},
        })
        result = inspect_mods(read_jars([app]))
        details = {
            f.mods[1]: f.detail for f in result.findings
            if f.code == "unchecked_dependency"
        }
        assert "--loader-version" in details["fabricloader"]
        assert "--minecraft-version" not in details["fabricloader"]
        # Nothing pins the Java version, so it must not suggest a flag.
        assert "--" not in details["java"]
        assert "java" in details["java"]

    def test_the_target_minecraft_version_is_checked(self, tmp_path):
        app = fabric_jar(tmp_path / "app.jar", {
            "id": "app", "version": "1", "depends": {"minecraft": ">=1.21"},
        })
        against_old = inspect_mods(
            read_jars([app]),
            target=InspectionTarget(loader="fabric", minecraft_version="1.20.1"),
        )
        assert "version_conflict" in codes(against_old)

        against_new = inspect_mods(
            read_jars([app]),
            target=InspectionTarget(loader="fabric", minecraft_version="1.21.1"),
        )
        assert "version_conflict" not in codes(against_new)

    def test_without_a_target_the_constraint_is_recorded_not_verified(self, tmp_path):
        app = fabric_jar(tmp_path / "app.jar", {
            "id": "app", "version": "1", "depends": {"minecraft": ">=1.21"},
        })
        result = inspect_mods(read_jars([app]))
        assert "unchecked_dependency" in codes(result)

    def test_forge_ranges_are_read_as_maven(self, tmp_path):
        app = forge_jar(
            tmp_path / "app.jar", mod_id="app", version="1",
            dependencies="""
[[dependencies.app]]
modId = "lib"
type = "required"
versionRange = "[2.0,3.0)"
""",
        )
        lib = forge_jar(tmp_path / "lib.jar", mod_id="lib", version="1.9")
        result = inspect_mods(
            read_jars([app, lib]), target=InspectionTarget(loader="forge")
        )
        assert "version_conflict" in codes(result)


class TestConflictVersions:
    def test_an_out_of_range_conflict_is_not_reported(self, tmp_path):
        """`breaks lib <2` with `lib 3` installed: previously an error."""
        app = fabric_jar(tmp_path / "app.jar", {
            "id": "app", "version": "1", "breaks": {"lib": "<2"},
        })
        lib = fabric_jar(tmp_path / "lib.jar", {"id": "lib", "version": "3.0"})
        result = inspect_mods(read_jars([app, lib]))
        assert "declared_conflict" not in codes(result)
        assert result.ok

    def test_an_in_range_conflict_is_an_error(self, tmp_path):
        app = fabric_jar(tmp_path / "app.jar", {
            "id": "app", "version": "1", "breaks": {"lib": "<2"},
        })
        lib = fabric_jar(tmp_path / "lib.jar", {"id": "lib", "version": "1.4"})
        result = inspect_mods(read_jars([app, lib]))
        assert "declared_conflict" in codes(result)
        assert not result.ok


class TestFabricApiIsNotAmbient:
    def test_a_missing_fabric_api_is_reported(self, tmp_path):
        """The one thing most Fabric packs need was the one thing never checked."""
        app = fabric_jar(tmp_path / "app.jar", {
            "id": "app", "version": "1", "depends": {"fabric-api": "*"},
        })
        result = inspect_mods(read_jars([app]))
        assert "missing_dependency" in codes(result)
        assert not result.ok

    def test_a_provisioning_guarantee_satisfies_it(self, tmp_path):
        app = fabric_jar(tmp_path / "app.jar", {
            "id": "app", "version": "1", "depends": {"fabric-api": "*"},
        })
        result = inspect_mods(
            read_jars([app]),
            target=InspectionTarget(loader="fabric", provisioned=frozenset({"fabric-api"})),
        )
        assert result.ok


class TestFailClosed:
    def test_an_unreadable_jar_is_an_error(self, tmp_path):
        broken = tmp_path / "broken.jar"
        broken.write_bytes(b"this is not a zip archive")
        result = inspect_mods(read_jars([broken]))
        assert not result.ok
        assert any(f.code == "unreadable" for f in result.errors)

    def test_a_jar_with_no_descriptor_is_an_error(self, tmp_path):
        empty = tmp_path / "empty.jar"
        with zipfile.ZipFile(empty, "w") as archive:
            archive.writestr("README", "nothing to declare")
        result = inspect_mods(read_jars([empty]))
        assert not result.ok

    def test_malformed_metadata_is_an_error(self, tmp_path):
        bad = tmp_path / "bad.jar"
        with zipfile.ZipFile(bad, "w") as archive:
            archive.writestr("fabric.mod.json", "{not json")
        result = inspect_mods(read_jars([bad]))
        assert not result.ok
        assert any("malformed" in f.summary for f in result.errors)


class TestDeclaredMixins:
    def test_an_unusually_named_mixin_is_found(self, tmp_path):
        """A declared mixin outside a package called 'mixin'.

        The old scan selected classes whose path contained "mixin", so this one
        was invisible — and invisible in the contention ranking that decides
        where an operator points a bisect.
        """
        jar = fabric_jar(
            tmp_path / "a.jar",
            {"id": "a", "version": "1"},
            config={"package": "com.example.patches", "mixins": ["CameraPatch"]},
            annotated={
                "com/example/patches/CameraPatch": ["net/minecraft/client/Camera"]
            },
        )
        meta = read_jar(jar)
        assert meta.configs and meta.configs[0].package == "com.example.patches"
        assert "net/minecraft/client/Camera" in meta.verified_targets

    def test_a_helper_in_a_mixin_package_is_not_a_mixin(self, tmp_path):
        """The other direction: a class the heuristic wrongly counted."""
        jar = fabric_jar(
            tmp_path / "a.jar",
            {"id": "a", "version": "1"},
            config={"package": "com.example.mixin", "mixins": ["RealMixin"]},
            annotated={"com/example/mixin/RealMixin": ["net/minecraft/client/Camera"]},
            mixin_classes={"Helper": ["Lnet/minecraft/world/level/Level;"]},
        )
        meta = read_jar(jar)
        # The helper is not a declared mixin, so its references are not targets.
        assert meta.verified_targets == frozenset({"net/minecraft/client/Camera"})

    def test_client_and_server_entries_are_resolved(self, tmp_path):
        jar = fabric_jar(
            tmp_path / "a.jar",
            {"id": "a", "version": "1"},
            config={
                "package": "com.example.mixin",
                "mixins": ["Shared"],
                "client": ["ClientOnly"],
                "server": ["ServerOnly"],
            },
        )
        meta = read_jar(jar)
        [config] = meta.configs
        assert config.classes == (
            "com/example/mixin/Shared",
            "com/example/mixin/ClientOnly",
            "com/example/mixin/ServerOnly",
        )

    def test_a_mixin_plugin_is_reported_as_making_the_list_a_lower_bound(self, tmp_path):
        jar = fabric_jar(
            tmp_path / "a.jar",
            {"id": "a", "version": "1"},
            config={
                "package": "com.example.mixin",
                "mixins": [],
                "plugin": "com.example.MixinPlugin",
            },
        )
        result = inspect_mods(read_jars([jar]))
        assert "mixin_plugin" in codes(result)

    def test_annotation_targets_read_string_form(self):
        data = annotated_class(["net/minecraft/server/MinecraftServer"])
        assert annotated_mixin_targets(data) == {
            "net/minecraft/server/MinecraftServer"
        }

    def test_a_class_with_no_mixin_annotation_returns_none(self):
        assert annotated_mixin_targets(class_naming(["Lnet/minecraft/Foo;"])) is None

    def test_verified_overlap_outranks_footprint_overlap(self, tmp_path):
        a = fabric_jar(
            tmp_path / "a.jar", {"id": "a", "version": "1"},
            config={"package": "a.mixin", "mixins": ["Hot"]},
            annotated={"a/mixin/Hot": ["net/minecraft/client/Camera"]},
        )
        b = fabric_jar(
            tmp_path / "b.jar", {"id": "b", "version": "1"},
            config={"package": "b.mixin", "mixins": ["Hot"]},
            annotated={"b/mixin/Hot": ["net/minecraft/client/Camera"]},
        )
        result = inspect_mods(read_jars([a, b]))
        assert result.verified_overlaps == {
            "net/minecraft/client/Camera": ("a", "b")
        }
        assert "mixin_overlap" in codes(result)


class TestNestedJars:
    def test_a_bundled_dependency_is_not_missing(self, tmp_path):
        """A Fabric library shipped inside its dependent.

        Reported as an absent hard dependency before nested jars were read,
        which is the false positive that teaches operators to ignore the tool.
        """
        inner = fabric_jar(tmp_path / "inner.jar", {"id": "lib", "version": "2.5"})
        outer = fabric_jar(
            tmp_path / "outer.jar",
            {"id": "app", "version": "1", "depends": {"lib": ">=2"}},
            nested={"lib.jar": inner},
        )
        meta = read_jar(outer)
        assert meta.nested_jars == ("META-INF/jars/lib.jar",)
        assert [n.mod_id for n in meta.nested] == ["lib"]

        result = inspect_mods([meta])
        assert result.ok
        assert "missing_dependency" not in codes(result)

    def test_a_bundled_jar_is_read_without_touching_the_filesystem(
        self, tmp_path, monkeypatch
    ):
        """The bytes are already in hand when a nested jar is found.

        Writing each one out and opening it back cost two file operations per
        nested jar and bounded nothing, since the shared budget already caps
        how many bytes can be read. Fabric API bundles nearly sixty, so
        inspecting five jars did a hundred and twenty opens and took 4.2s.
        """
        import tempfile

        def refuse(*args, **kwargs):
            raise AssertionError("a nested jar was written to disk")

        monkeypatch.setattr(tempfile, "TemporaryDirectory", refuse)
        monkeypatch.setattr(tempfile, "NamedTemporaryFile", refuse)

        inner = fabric_jar(tmp_path / "inner.jar", {"id": "lib", "version": "2.5"})
        outer = fabric_jar(
            tmp_path / "outer.jar",
            {"id": "app", "version": "1", "depends": {"lib": ">=2"}},
            nested={"lib.jar": inner},
        )
        meta = read_jar(outer)
        assert [n.mod_id for n in meta.nested] == ["lib"]
        # Named for where it lives, which is what a reader can act on.
        assert meta.nested[0].path.name == "lib.jar"
        assert "META-INF" in str(meta.nested[0].path)

    def test_a_bundled_dependency_at_the_wrong_version_still_fails(self, tmp_path):
        inner = fabric_jar(tmp_path / "inner.jar", {"id": "lib", "version": "1.0"})
        outer = fabric_jar(
            tmp_path / "outer.jar",
            {"id": "app", "version": "1", "depends": {"lib": ">=2"}},
            nested={"lib.jar": inner},
        )
        result = inspect_mods([read_jar(outer)])
        assert "version_conflict" in codes(result)

    def test_nesting_is_bounded(self, tmp_path):
        """A hostile archive must not drive unbounded recursion."""
        current = fabric_jar(tmp_path / "level0.jar", {"id": "l0", "version": "1"})
        for level in range(1, 6):
            current = fabric_jar(
                tmp_path / f"level{level}.jar",
                {"id": f"l{level}", "version": "1"},
                nested={"inner.jar": current},
            )
        meta = read_jar(current)

        depth = 0
        node = meta
        while node.nested:
            depth += 1
            node = node.nested[0]
        assert depth <= 3
        assert any("nesting deeper" in e for e in node.errors)
