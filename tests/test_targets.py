"""Tests for targets, versions, capabilities, and dialects.

This layer is what makes "write the scenario once, run it everywhere" true. Its
job is to notice when a target cannot express a scenario and say so, because the
alternative is a plan that half-executes and still produces numbers that look
entirely valid.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from mcbench.config import Loader
from mcbench.runner.plan import check_target, compile_plan, scenario_ops
from mcbench.scenario import load_scenarios, parse_scenario
from mcbench.targets import (
    Capability,
    Dialect,
    Target,
    UnsupportedTarget,
    Version,
    required_capabilities,
)

REPO = Path(__file__).resolve().parents[1]


class TestVersion:
    def test_orders_classic_releases(self):
        assert Version.parse("1.20.4") < Version.parse("1.21")
        assert Version.parse("1.21") < Version.parse("1.21.1")
        assert Version.parse("1.8.9") < Version.parse("1.16.5")

    def test_year_based_versions_sort_after_classic_ones(self):
        """Both numbering schemes have to order in one sequence.

        Releases from 2026 use a year-based major (26.1.2). Because 26 > 1 a
        plain tuple comparison already puts them after every 1.x, so the new
        scheme needs no special case.
        """
        assert Version.parse("1.21.11") < Version.parse("26.1")
        assert Version.parse("26.1.2") < Version.parse("26.2")

    def test_missing_components_are_padded(self):
        assert Version.parse("1.21") == Version.parse("1.21.0")
        assert Version.parse("1.21") <= Version.parse("1.21.0")

    def test_at_least_gates(self):
        assert Version.parse("1.21.1").at_least("1.20.3")
        assert not Version.parse("1.20.1").at_least("1.20.3")
        assert Version.parse("26.1.2").at_least("1.20.3")

    def test_prerelease_suffixes_are_stripped(self):
        assert Version.parse("1.21.1-rc1").at_least("1.21")

    def test_snapshots_are_unknown_rather_than_guessed(self):
        # A snapshot cannot be ordered against releases meaningfully.
        snapshot = Version.parse("24w14a")
        assert not snapshot.known
        assert not snapshot.at_least("1.20")

    def test_unknown_versions_fail_every_gate(self):
        """The conservative direction.

        An unparseable version makes capabilities look unavailable, producing a
        clear refusal, rather than assuming support and emitting a command the
        target rejects.
        """
        assert not Version.parse("nonsense").at_least("1.0")


class TestTarget:
    def test_parses_a_spec(self):
        target = Target.parse("fabric:1.21.1")
        assert target.platform is Loader.FABRIC
        assert target.minecraft_version == "1.21.1"

    def test_carries_mods(self):
        target = Target.parse("fabric:1.19.2", mods=["Carpet"])
        assert target.has_carpet

    def test_rejects_a_malformed_spec(self):
        with pytest.raises(UnsupportedTarget, match="platform:version"):
            Target.parse("fabric")

    def test_rejects_an_unknown_platform(self):
        with pytest.raises(UnsupportedTarget, match="unknown platform"):
            Target.parse("modloader64:1.21.1")


class TestCapabilities:
    def test_plugin_platforms_have_no_client(self):
        assert not Dialect(Target.parse("paper:1.21.1")).supports(Capability.CLIENT)
        assert Dialect(Target.parse("fabric:1.21.1")).supports(Capability.CLIENT)

    def test_tick_warp_is_vanilla_from_1_20_3(self):
        assert Dialect(Target.parse("paper:1.20.4")).supports(Capability.TICK_WARP)
        assert not Dialect(Target.parse("paper:1.20.1")).supports(Capability.TICK_WARP)

    def test_carpet_provides_tick_warp_on_older_mod_loaders(self):
        without = Dialect(Target.parse("fabric:1.19.2"))
        with_carpet = Dialect(Target.parse("fabric:1.19.2", mods=["carpet"]))
        assert not without.supports(Capability.TICK_WARP)
        assert with_carpet.supports(Capability.TICK_WARP)

    def test_carpet_does_not_help_a_plugin_platform(self):
        # Carpet has no Paper build, so claiming it would be simply wrong.
        dialect = Dialect(Target.parse("paper:1.19.2", mods=["carpet"]))
        assert not dialect.supports(Capability.TICK_WARP)

    def test_forceload_and_simulation_distance_are_version_gated(self):
        assert not Dialect(Target.parse("fabric:1.13")).supports(Capability.FORCELOAD)
        assert Dialect(Target.parse("fabric:1.13.1")).supports(Capability.FORCELOAD)
        assert not Dialect(Target.parse("fabric:1.17.1")).supports(
            Capability.SIMULATION_DISTANCE
        )
        assert Dialect(Target.parse("fabric:1.18")).supports(
            Capability.SIMULATION_DISTANCE
        )

    def test_pre_flattening_targets_lack_modern_identifiers(self):
        assert not Dialect(Target.parse("forge:1.12.2")).supports(
            Capability.MODERN_IDENTIFIERS
        )
        assert Dialect(Target.parse("forge:1.13")).supports(
            Capability.MODERN_IDENTIFIERS
        )

    def test_every_missing_capability_has_an_explanation(self):
        target = Target.parse("paper:1.12.2")
        dialect = Dialect(target)
        missing = dialect.missing(list(Capability))
        assert missing
        for capability in missing:
            reason = dialect.explain(capability)
            assert reason and not reason.endswith("None")


class TestDialectSpelling:
    def test_item_replace_changed_verb_in_1_17(self):
        """A hard-coded modern spelling silently failed on older targets."""
        modern = Dialect(Target.parse("fabric:1.21.1"))
        old = Dialect(Target.parse("fabric:1.16.5"))
        assert modern.item_replace_block(0, 0, 0, "container.0", "x", 1).startswith(
            "item replace block"
        )
        assert old.item_replace_block(0, 0, 0, "container.0", "x", 1).startswith(
            "replaceitem block"
        )


class TestRequiredCapabilities:
    def test_derived_from_what_a_scenario_actually_does(self):
        needed = required_capabilities(
            side="client", uses_tick_warp=False, ops={"camera_path"}
        )
        assert Capability.CLIENT in needed
        assert Capability.TICK_WARP not in needed

    def test_chunk_ops_require_forceload(self):
        needed = required_capabilities(
            side="server", uses_tick_warp=False, ops={"generate_chunks"}
        )
        assert Capability.FORCELOAD in needed


class TestScenarioOps:
    def test_collects_nested_loop_bodies(self):
        """Ops hidden inside a loop still determine what a target must support."""
        scenario = parse_scenario({
            "id": "t", "version": "1.0.0", "title": "T", "side": "server",
            "category": "entity",
            "world": {"seed": 1, "generator": "flat"},
            "measurement": {"warmup": {"min": 1}, "duration": {"full": 1}},
            "workload": [
                {"op": "loop", "body": [{"op": "generate_chunks", "radius_chunks": 4}]}
            ],
        })
        assert "generate_chunks" in scenario_ops(scenario)


class TestCompileForTarget:
    def _client_scenario(self):
        return parse_scenario({
            "id": "c", "version": "1.0.0", "title": "C", "side": "client",
            "category": "visual",
            "world": {"seed": 1, "generator": "flat"},
            "measurement": {"warmup": {"min": 1}, "duration": {"full": 1}},
        })

    def test_refuses_a_client_scenario_on_a_server_platform(self):
        with pytest.raises(UnsupportedTarget, match="no client"):
            compile_plan(self._client_scenario(), target=Target.parse("paper:1.21.1"))

    def test_the_refusal_names_the_scenario_and_the_target(self):
        with pytest.raises(UnsupportedTarget, match="'c' cannot run on paper:1.21.1"):
            compile_plan(self._client_scenario(), target=Target.parse("paper:1.21.1"))

    def test_compiles_the_same_scenario_for_a_mod_loader(self):
        plan = compile_plan(
            self._client_scenario(), target=Target.parse("fabric:1.21.1")
        )
        assert plan.properties["target.platform"] == "fabric"
        assert plan.properties["target.minecraft_version"] == "1.21.1"

    def test_strict_false_reports_without_refusing(self):
        # For tooling that wants a compatibility matrix, never for a real run.
        plan = compile_plan(
            self._client_scenario(),
            target=Target.parse("paper:1.21.1"),
            strict=False,
        )
        assert plan.properties["scenario.id"] == "c"

    def test_one_scenario_compiles_to_different_commands_per_target(self):
        """The whole point: one definition, correct output for each target."""
        scenario = parse_scenario({
            "id": "s", "version": "1.0.0", "title": "S", "side": "server",
            "category": "block-entity",
            "world": {"seed": 1, "generator": "flat"},
            "measurement": {"warmup": {"min": 1}, "duration": {"full": 1}},
            "setup": [{
                "op": "place_structure", "template": "hopper_chain", "count": 1,
                "origin": {"x": 0, "y": 5, "z": 0},
                "parameters": {"length": 3, "preload_items": 8},
            }],
        })
        modern = compile_plan(scenario, target=Target.parse("fabric:1.21.1"))
        old = compile_plan(scenario, target=Target.parse("fabric:1.16.5"))

        assert any(line.startswith("item replace block") for line in modern.setup)
        assert any(line.startswith("replaceitem block") for line in old.setup)


class TestShippedScenarioMatrix:
    @pytest.fixture(scope="class")
    @classmethod
    def scenarios(cls):
        return load_scenarios(REPO / "scenarios")

    def test_all_scenarios_run_on_modern_fabric_with_carpet(self, scenarios):
        target = Target.parse("fabric:1.21.1", mods=["carpet"])
        for scenario in scenarios:
            assert check_target(scenario, target) == [], scenario.id

    def test_paper_runs_every_server_scenario(self, scenarios):
        """Server scenarios must be portable to a platform with no client."""
        target = Target.parse("paper:1.21.1")
        for scenario in scenarios:
            if scenario.side.value == "server":
                assert check_target(scenario, target) == [], scenario.id

    def test_paper_refuses_every_client_scenario(self, scenarios):
        target = Target.parse("paper:1.21.1")
        for scenario in scenarios:
            if scenario.side.value == "client":
                missing = check_target(scenario, target)
                assert Capability.CLIENT in missing, scenario.id

    def test_pre_flattening_targets_are_refused_wholesale(self, scenarios):
        """Better to refuse than to place the wrong blocks.

        The 1.13 flattening renamed essentially every block and item, so a
        scenario using modern identifiers would build a different world rather
        than fail — and that world would measure as though it were correct.
        """
        target = Target.parse("forge:1.12.2", mods=["carpet"])
        for scenario in scenarios:
            assert Capability.MODERN_IDENTIFIERS in check_target(scenario, target)
