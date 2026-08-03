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
