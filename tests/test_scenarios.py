"""Validation of the shipped scenario definitions and the suite manifest.

These run against the real files in scenarios/ and suites/, so a malformed
definition fails CI rather than a benchmark run three hours in.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from mcbench.config import ConfigError, Loader, Platform, load_suite, parse_suite
from mcbench.metrics import METRICS
from mcbench.planner import OrderStrategy
from mcbench.scenario import (
    Category,
    Preset,
    ScenarioError,
    Side,
    load_scenarios,
    parse_scenario,
    select,
)

REPO = Path(__file__).resolve().parents[1]
SCENARIO_ROOT = REPO / "scenarios"


@pytest.fixture(scope="module")
def scenarios():
    return load_scenarios(SCENARIO_ROOT)


class TestShippedScenarios:
    def test_all_load_and_validate(self, scenarios):
        assert len(scenarios) >= 10

    def test_ids_are_unique(self, scenarios):
        ids = [s.id for s in scenarios]
        assert len(ids) == len(set(ids))

    def test_every_seed_is_fixed(self, scenarios):
        # An unfixed seed makes a comparison meaningless: variants would be
        # measured against different worlds.
        for scenario in scenarios:
            assert isinstance(scenario.seed, int)

    def test_primary_metrics_exist_in_the_registry(self, scenarios):
        for scenario in scenarios:
            if scenario.primary_metric:
                assert scenario.primary_metric in METRICS, scenario.id

    def test_every_scenario_declares_a_warmup(self, scenarios):
        for scenario in scenarios:
            assert scenario.measurement["warmup"]["min"] > 0, scenario.id

    def test_presets_fall_back_to_full(self, scenarios):
        for scenario in scenarios:
            assert scenario.duration(Preset.QUICK) > 0
            assert scenario.duration(Preset.FULL) > 0
            assert scenario.duration(Preset.LONG) > 0

    def test_tick_warp_scenarios_require_carpet(self, scenarios):
        for scenario in scenarios:
            if scenario.uses_tick_warp:
                assert "carpet" in scenario.requires, scenario.id
                assert scenario.side is not Side.CLIENT, scenario.id

    def test_the_user_requested_axes_are_all_covered(self, scenarios):
        """Every dimension this benchmark was commissioned to cover."""
        categories = {s.category for s in scenarios}
        for required in (
            Category.VISUAL,          # 視覺 (FPS)
            Category.TICK_STABILITY,  # 穩定性 (avg tick)
            Category.ENTITY,          # 生物優化
            Category.WORLDGEN,        # 生成優化
            Category.REFERENCE,       # hardware normalisation
        ):
            assert required in categories, f"missing category {required.value}"

    def test_a_reference_scenario_exists_for_normalisation(self, scenarios):
        references = select(scenarios, category=Category.REFERENCE)
        assert len(references) == 1
        assert references[0].id == "reference-hardware-baseline"

    def test_tps_is_only_reported_for_saturated_scenarios(self, scenarios):
        # Below budget every configuration reports 20 TPS, so publishing it
        # elsewhere would invite the "both are fine" misreading.
        for scenario in scenarios:
            metrics = scenario.measurement.get("metrics", [])
            if "tps_effective" in metrics:
                assert scenario.saturated, scenario.id

    def test_pool_keys_are_distinct(self, scenarios):
        keys = [s.pool_key for s in scenarios]
        assert len(keys) == len(set(keys))

    def test_both_sides_are_represented(self, scenarios):
        sides = {s.side for s in scenarios}
        assert Side.CLIENT in sides
        assert Side.SERVER in sides


class TestScenarioValidation:
    def _minimal(self, **overrides):
        base = {
            "id": "test-scenario",
            "version": "1.0.0",
            "title": "Test",
            "side": "server",
            "category": "entity",
            "world": {"seed": 1, "generator": "flat"},
            "measurement": {"warmup": {"min": 100}, "duration": {"full": 1000}},
        }
        base.update(overrides)
        return base

    def test_minimal_scenario_is_valid(self):
        assert parse_scenario(self._minimal()).id == "test-scenario"

    def test_rejects_a_missing_seed(self):
        with pytest.raises(ScenarioError, match="world.seed is required"):
            parse_scenario(self._minimal(world={"generator": "flat"}))

    def test_rejects_a_non_semantic_version(self):
        with pytest.raises(ScenarioError, match="version must be semantic"):
            parse_scenario(self._minimal(version="1.0"))

    def test_rejects_a_non_kebab_case_id(self):
        with pytest.raises(ScenarioError, match="kebab-case"):
            parse_scenario(self._minimal(id="Test_Scenario"))

    def test_rejects_an_unknown_metric(self):
        with pytest.raises(ScenarioError, match="not in the metric registry"):
            parse_scenario(self._minimal(measurement={
                "warmup": {"min": 100},
                "duration": {"full": 1000},
                "primary_metric": "made_up_metric",
            }))

    def test_rejects_a_zero_warmup(self):
        with pytest.raises(ScenarioError, match="warmup.min must be a positive"):
            parse_scenario(self._minimal(measurement={
                "warmup": {"min": 0}, "duration": {"full": 1000},
            }))

    def test_rejects_an_unknown_action_op(self):
        with pytest.raises(ScenarioError, match="unknown op"):
            parse_scenario(self._minimal(workload=[{"op": "explode_everything"}]))

    def test_rejects_tick_warp_on_a_client_scenario(self):
        with pytest.raises(ScenarioError, match="tick_warp is server-side"):
            parse_scenario(self._minimal(
                side="client",
                requires=["carpet"],
                measurement={
                    "warmup": {"min": 10}, "duration": {"full": 60}, "tick_warp": True,
                },
            ))

    def test_rejects_tick_warp_without_declaring_carpet(self):
        with pytest.raises(ScenarioError, match="measured at 20 TPS"):
            parse_scenario(self._minimal(measurement={
                "warmup": {"min": 100}, "duration": {"full": 1000}, "tick_warp": True,
            }))

    def test_content_hash_ignores_cosmetic_edits(self):
        # Retitling must not invalidate a corpus.
        original = parse_scenario(self._minimal())
        retitled = parse_scenario(self._minimal(title="A Different Title"))
        assert original.content_hash == retitled.content_hash

    def test_content_hash_changes_when_the_seed_changes(self):
        original = parse_scenario(self._minimal())
        reseeded = parse_scenario(
            self._minimal(world={"seed": 999, "generator": "flat"})
        )
        assert original.content_hash != reseeded.content_hash


class TestSuiteManifest:
    def test_the_example_suite_is_valid(self):
        suite = load_suite(REPO / "suites" / "example-performance-mods.toml")
        assert suite.loader is Loader.FABRIC
        assert suite.baseline == "vanilla"
        assert suite.order is OrderStrategy.INTERLEAVED
        assert len(suite.variants) == 4

    def test_the_example_suite_is_publishable(self):
        suite = load_suite(REPO / "suites" / "example-performance-mods.toml")
        assert suite.publishable, suite.unpublishable_reasons()

    def test_the_example_suite_references_real_scenarios(self, scenarios):
        suite = load_suite(REPO / "suites" / "example-performance-mods.toml")
        known = {s.id for s in scenarios}
        assert set(suite.scenarios) <= known

    def test_the_example_suite_has_a_complete_factorial_for_its_interaction(self):
        suite = load_suite(REPO / "suites" / "example-performance-mods.toml")
        names = {v.name for v in suite.variants}
        # The interaction term needs all four cells; without A+B you can only
        # assume additivity.
        assert {"vanilla", "sodium", "entityculling", "sodium+entityculling"} <= names

    def test_mods_parse_from_the_shorthand_form(self):
        suite = parse_suite({
            "name": "t", "minecraft_version": "1.21.1", "loader": "fabric",
            "scenarios": ["s"],
            "variants": [
                {"name": "base", "mods": []},
                {"name": "x", "mods": ["modrinth:sodium@0.6.0"]},
            ],
            "baseline": "base",
        })
        mod = suite.variants[1].mods[0]
        assert mod.platform is Platform.MODRINTH
        assert mod.project == "sodium"
        assert mod.version == "0.6.0"
        assert mod.pinned

    def test_unpinned_mods_make_a_suite_unpublishable(self):
        suite = parse_suite({
            "name": "t", "minecraft_version": "1.21.1", "loader": "fabric",
            "scenarios": ["s"],
            "variants": [
                {"name": "base", "mods": []},
                {"name": "x", "mods": ["modrinth:sodium"]},
            ],
            "baseline": "base",
        })
        assert not suite.publishable
        assert any("unpinned" in r for r in suite.unpublishable_reasons())

    def test_local_jars_make_a_suite_unpublishable(self):
        """A local jar is legitimate for development but not verifiable by others.

        Benchmarking an unpublished build is a primary use case, so this must
        still run. It must not claim to be comparable to anyone else's numbers,
        because nobody else can obtain the file it measured.
        """
        suite = parse_suite({
            "name": "t", "minecraft_version": "1.21.1", "loader": "fabric",
            "scenarios": ["s"],
            "variants": [
                {"name": "base", "mods": []},
                {"name": "dev", "mods": [
                    {"platform": "local", "project": "build/libs/mymod.jar",
                     "version": "1.0.0"}
                ]},
            ],
            "baseline": "base",
        })
        assert not suite.publishable
        assert any("local mod files" in r for r in suite.unpublishable_reasons())

    def test_a_pinned_local_jar_is_still_unpublishable(self):
        # Pinning a version string does not make the file obtainable.
        suite = parse_suite({
            "name": "t", "minecraft_version": "1.21.1", "loader": "fabric",
            "scenarios": ["s"],
            "variants": [
                {"name": "base", "mods": []},
                {"name": "dev", "mods": [
                    {"platform": "local", "project": "a.jar", "version": "9.9.9"}
                ]},
            ],
            "baseline": "base",
        })
        mod = suite.variants[1].mods[0]
        assert mod.pinned
        assert not mod.third_party_obtainable
        assert not suite.publishable

    def test_blocked_ordering_makes_a_suite_unpublishable(self):
        suite = parse_suite({
            "name": "t", "minecraft_version": "1.21.1", "loader": "fabric",
            "scenarios": ["s"], "order": "blocked",
            "variants": [{"name": "base", "mods": []}],
            "baseline": "base",
        })
        assert not suite.publishable
        assert any("interleaved" in r for r in suite.unpublishable_reasons())

    def test_too_few_runs_makes_a_suite_unpublishable(self):
        suite = parse_suite({
            "name": "t", "minecraft_version": "1.21.1", "loader": "fabric",
            "scenarios": ["s"], "runs_per_cell": 2,
            "variants": [{"name": "base", "mods": []}],
            "baseline": "base",
        })
        assert not suite.publishable
        assert any("runs_per_cell" in r for r in suite.unpublishable_reasons())

    def test_rejects_an_ambiguous_implicit_baseline(self):
        with pytest.raises(ConfigError, match="baseline"):
            parse_suite({
                "name": "t", "minecraft_version": "1.21.1", "loader": "fabric",
                "scenarios": ["s"],
                "variants": [{"name": "a", "mods": []}, {"name": "b", "mods": []}],
            })

    def test_infers_an_unambiguous_baseline(self):
        suite = parse_suite({
            "name": "t", "minecraft_version": "1.21.1", "loader": "fabric",
            "scenarios": ["s"],
            "variants": [
                {"name": "a", "mods": []},
                {"name": "b", "mods": ["modrinth:x@1"]},
            ],
        })
        assert suite.baseline == "a"

    def test_rejects_an_unknown_loader(self):
        with pytest.raises(ConfigError, match="unknown loader"):
            parse_suite({
                "name": "t", "minecraft_version": "1.21.1", "loader": "modloader64",
                "scenarios": ["s"], "variants": [{"name": "a", "mods": []}],
            })

    def test_accepts_plugin_platforms_alongside_mod_loaders(self):
        # Paper and its derivatives are a large part of the server ecosystem that
        # mod loaders never touch, so the platform set has to include them.
        for platform in ("fabric", "neoforge", "forge", "quilt", "paper", "spigot"):
            suite = parse_suite({
                "name": "t", "minecraft_version": "1.21.1", "loader": platform,
                "scenarios": ["s"], "variants": [{"name": "a", "mods": []}],
                "baseline": "a",
            })
            assert suite.loader.value == platform

    def test_rejects_an_interaction_group_naming_an_undeclared_variant(self):
        with pytest.raises(ConfigError, match="undeclared variants"):
            parse_suite({
                "name": "t", "minecraft_version": "1.21.1", "loader": "fabric",
                "scenarios": ["s"],
                "variants": [{"name": "a", "mods": []}],
                "baseline": "a",
                "interactions": [["a", "ghost"]],
            })


class TestWorldgenReach:
    """Pre-generating terrain only helps if it covers where the camera goes.

    The baseline pre-generates 841 chunks and says the measurement never pays
    worldgen cost. The world it produced on this machine held 3684, reaching 34
    chunks from spawn: three quarters of its terrain was made while the scenario
    was running. A shared world hides that after the first run; --fresh-world
    does not.
    """

    def _client(self, **overrides):
        base = {
            "id": "reach-test",
            "version": "1.0.0",
            "title": "Reach",
            "side": "client",
            "category": "visual",
            "world": {"seed": 1, "generator": "default",
                      "spawn": {"x": 0.5, "y": 80.0, "z": 0.5}},
            "measurement": {"warmup": {"min": 60}, "duration": {"full": 60}},
            "setup": [
                {"op": "set_render_distance", "chunks": 8},
                {"op": "generate_chunks", "radius_chunks": 12},
            ],
            "workload": [{
                "op": "camera_path", "duration_s": 30, "loop": True,
                "points": [{"x": 0, "y": 80, "z": 0}, {"x": 32, "y": 80, "z": 32}],
            }],
        }
        base.update(overrides)
        return parse_scenario(base)

    def test_a_path_inside_the_generated_area_says_nothing(self):
        # Furthest camera chunk 2, plus render distance 8, is 10 against 12.
        assert self._client().worldgen_reach_gap() == ""

    def test_render_distance_counts_towards_the_reach(self):
        # Same path, render distance 16: the client loads what it can see, not
        # only the chunk it stands in.
        scenario = self._client(setup=[
            {"op": "set_render_distance", "chunks": 16},
            {"op": "generate_chunks", "radius_chunks": 12},
        ])
        gap = scenario.worldgen_reach_gap()
        assert "reaches 18 chunks" in gap
        assert "pre-generates 12" in gap

    def test_a_distant_camera_point_counts(self):
        scenario = self._client(workload=[{
            "op": "camera_path", "duration_s": 30, "loop": True,
            "points": [{"x": 0, "y": 80, "z": 0}, {"x": 400, "y": 80, "z": 0}],
        }])
        assert "reaches 33 chunks" in scenario.worldgen_reach_gap()

    def test_a_server_scenario_has_no_camera_to_answer_for(self):
        scenario = parse_scenario({
            "id": "server-reach", "version": "1.0.0", "title": "S",
            "side": "server", "category": "entity",
            "world": {"seed": 1, "generator": "flat"},
            "measurement": {"warmup": {"min": 100}, "duration": {"full": 1000}},
            "setup": [{"op": "generate_chunks", "radius_chunks": 4}],
        })
        assert scenario.worldgen_reach_gap() == ""

    def test_both_shipped_client_flybys_currently_overrun(self):
        # Not an assertion that this is acceptable: it records what the shipped
        # definitions do, so raising a radius has to be a deliberate edit with
        # the version bump that implies rather than a silent one.
        found = {
            s.id: s.worldgen_reach_gap()
            for s in load_scenarios(Path(__file__).resolve().parents[1] / "scenarios")
            if s.side is Side.CLIENT
        }
        assert "reaches 22 chunks" in found["reference-hardware-baseline"]
        assert "reaches 41 chunks" in found["visual-biome-flyby"]
        assert found["visual-particle-storm"] == ""


class TestFingerprintMargin:
    """A fingerprint has to sit in terrain that has stopped changing.

    Worldgen is not chunk-local, so a chunk near the edge of what has been
    generated keeps changing while its neighbours are made. Fingerprint there
    and the check stops being "did these runs measure the same world" and
    becomes "did they stop generating at the same moment".

    Both numbers here are measurements. visual-biome-flyby fingerprints 16
    inside a pre-generated 20 and the run that made the world disagreed with
    the runs that restored it on 5 of 1089 chunks, all in rings 14 to 16.
    reference-hardware-baseline fingerprints 8 inside 14 and thirty runs agree.
    """

    def _client(self, fingerprint, generated):
        return parse_scenario({
            "id": "margin-test", "version": "1.0.0", "title": "M",
            "side": "client", "category": "visual",
            "world": {
                "seed": 1, "generator": "default",
                "spawn": {"x": 0.5, "y": 80.0, "z": 0.5},
                "fingerprint_region": {"radius_chunks": fingerprint},
            },
            "measurement": {"warmup": {"min": 60}, "duration": {"full": 60}},
            "setup": [{"op": "generate_chunks", "radius_chunks": generated}],
        })

    def test_the_threshold_is_where_the_measurements_put_it(self):
        # 6 held over thirty runs; 4 did not hold over three.
        assert self._client(8, 14).fingerprint_margin_gap() == ""
        assert self._client(16, 20).fingerprint_margin_gap() != ""
        assert self._client(8, 13).fingerprint_margin_gap() != ""

    def test_the_message_carries_both_radii(self):
        gap = self._client(16, 20).fingerprint_margin_gap()
        assert "radius of 16" in gap
        assert "pre-generated 20" in gap
        assert "leaving 4" in gap

    def test_a_scenario_that_pre_generates_nothing_is_not_judged(self):
        # Nothing to be inside of. The fingerprint then covers whatever the run
        # produced, which is a different problem with a different answer.
        scenario = parse_scenario({
            "id": "no-pregen", "version": "1.0.0", "title": "N",
            "side": "client", "category": "visual",
            "world": {"seed": 1, "generator": "default",
                      "fingerprint_region": {"radius_chunks": 8}},
            "measurement": {"warmup": {"min": 60}, "duration": {"full": 60}},
        })
        assert scenario.fingerprint_margin_gap() == ""

    def test_a_scenario_with_no_declared_region_is_not_judged(self):
        scenario = parse_scenario({
            "id": "no-region", "version": "1.0.0", "title": "N",
            "side": "client", "category": "visual",
            "world": {"seed": 1, "generator": "default"},
            "measurement": {"warmup": {"min": 60}, "duration": {"full": 60}},
            "setup": [{"op": "generate_chunks", "radius_chunks": 10}],
        })
        assert scenario.fingerprint_margin_gap() == ""

    def test_the_shipped_scenarios_split_the_way_they_were_measured(self):
        found = {
            s.id: bool(s.fingerprint_margin_gap())
            for s in load_scenarios(SCENARIO_ROOT)
        }
        # Thirty runs of this one agreed on a digest.
        assert not found["reference-hardware-baseline"]
        # This one did not, on the third run of the suite.
        assert found["visual-biome-flyby"]


class TestUnknownScenarioKeys:
    """The schema says additionalProperties: false and nothing enforced it.

    Validation is deliberately self-contained rather than delegating to a JSON
    Schema library — the module docstring says so — but the self-contained half
    never checked the key set. So any key at all was accepted and then read by
    nobody, which is exactly how world.generator_settings came to sit in six
    scenario files, doing nothing, for however long.
    """

    def _minimal(self, **world):
        return {
            "id": "keys", "version": "1.0.0", "title": "K",
            "side": "client", "category": "visual",
            "world": {"seed": 1, "generator": "flat", **world},
            "measurement": {"warmup": {"min": 10}, "duration": {"full": 10}},
        }

    def test_an_invented_world_key_is_refused(self):
        with pytest.raises(ScenarioError, match="nonsense"):
            parse_scenario(self._minimal(nonsense=1))

    def test_a_near_miss_is_named(self):
        with pytest.raises(ScenarioError, match="did you mean 'generator_settings'"):
            parse_scenario(self._minimal(generator_setting={"layers": "x"}))

    def test_an_invented_top_level_key_is_refused(self):
        with pytest.raises(ScenarioError, match="warmup_seconds"):
            parse_scenario({**self._minimal(), "warmup_seconds": 30})

    def test_an_invented_measurement_key_is_refused(self):
        base = self._minimal()
        base["measurement"]["primary metric"] = "fps_avg"
        with pytest.raises(ScenarioError, match="primary metric"):
            parse_scenario(base)

    def test_an_invented_warmup_key_is_refused(self):
        # No "did you mean" here: 'tolerance' is not close enough to
        # 'steady_state_tolerance' to guess at, and guessing anyway would point
        # people at the wrong key. The message lists the real ones instead.
        base = self._minimal()
        base["measurement"]["warmup"]["tolerance"] = 0.05
        with pytest.raises(ScenarioError) as raised:
            parse_scenario(base)
        assert "'tolerance'" in str(raised.value)
        assert "steady_state_tolerance" in str(raised.value)
        assert "did you mean" not in str(raised.value)

    def test_every_shipped_scenario_still_loads(self):
        # The list is only right if it covers what the repository already
        # writes; an omission here refuses a file that was always valid.
        assert len(load_scenarios(SCENARIO_ROOT)) >= 11
