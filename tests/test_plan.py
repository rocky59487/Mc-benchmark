"""Tests for compiling scenarios into probe execution plans.

This is the join between the harness and the probe, and it was broken once
already — the harness wrote a scenario JSON the probe could not read, so the
probe would never have started. These tests exist so that seam stays connected.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from mcbench.runner.plan import (
    MAX_FILL_VOLUME,
    PlanError,
    _compile_action,
    _split_volume,
    compile_plan,
    write_plan,
)
from mcbench.scenario import Preset, Side, load_scenarios, parse_scenario

REPO = Path(__file__).resolve().parents[1]


def compile_one(action: dict) -> list[str]:
    lines, _ = _compile_action(action, "test")
    return lines


class TestSimpleActions:
    def test_command_passes_through_without_a_leading_slash(self):
        assert compile_one({"op": "command", "value": "/time set 6000"}) == [
            "time set 6000"
        ]

    def test_wait_becomes_a_pacing_directive(self):
        assert compile_one({"op": "wait", "ticks": 40}) == ["@wait 40"]

    def test_zero_wait_emits_nothing(self):
        assert compile_one({"op": "wait", "ticks": 0}) == []

    def test_setblock(self):
        assert compile_one(
            {"op": "setblock", "x": 1, "y": 2, "z": 3, "block": "minecraft:stone"}
        ) == ["setblock 1 2 3 minecraft:stone"]

    def test_integral_floats_are_not_written_with_a_decimal_point(self):
        # "/setblock 1.0 2.0 3.0" is a syntax error in Minecraft.
        assert compile_one(
            {"op": "setblock", "x": 1.0, "y": 2.0, "z": 3.0, "block": "minecraft:stone"}
        ) == ["setblock 1 2 3 minecraft:stone"]

    def test_gamerule_renders_booleans_lowercase(self):
        assert compile_one(
            {"op": "gamerule", "name": "doMobSpawning", "value": False}
        ) == ["gamerule doMobSpawning false"]

    def test_summon(self):
        assert compile_one(
            {"op": "summon", "type": "minecraft:cow", "x": 0, "y": 5, "z": 0}
        ) == ["summon minecraft:cow 0 5 0"]

    def test_unload_chunks(self):
        assert compile_one({"op": "unload_chunks", "all": True}) == [
            "forceload remove all"
        ]

    def test_unknown_op_is_fatal(self):
        """Silently skipping would build a world that is not the scenario.

        The run would still produce numbers, and they would look entirely valid.
        """
        with pytest.raises(PlanError, match="no compiler for action op"):
            compile_one({"op": "summon_a_dragon"})

    def test_missing_required_field_is_fatal(self):
        with pytest.raises(PlanError, match="needs 'block'"):
            compile_one({"op": "setblock", "x": 0, "y": 0, "z": 0})


class TestFillSplitting:
    def test_small_fill_is_one_command(self):
        lines = compile_one({
            "op": "fill",
            "from": {"x": 0, "y": 0, "z": 0},
            "to": {"x": 9, "y": 0, "z": 9},
            "block": "minecraft:stone",
        })
        assert lines == ["fill 0 0 0 9 0 9 minecraft:stone"]

    def test_large_fill_is_split_under_the_vanilla_limit(self):
        """Vanilla /fill rejects volumes over 32768.

        Exceeding it is a runtime command failure that leaves the world
        half-built — and a half-built world still measures as though it were
        fine, which is the dangerous part.
        """
        start, end = (-48, 40, -48), (48, 60, 48)
        pieces = _split_volume(start, end)
        assert len(pieces) > 1
        for (x0, y0, z0), (x1, y1, z1) in pieces:
            volume = (x1 - x0 + 1) * (y1 - y0 + 1) * (z1 - z0 + 1)
            assert volume <= MAX_FILL_VOLUME, f"piece of {volume} exceeds the limit"

    def test_split_covers_the_whole_region_exactly_once(self):
        start, end = (0, 0, 0), (40, 40, 40)
        covered = set()
        for (x0, y0, z0), (x1, y1, z1) in _split_volume(start, end):
            for x in range(x0, x1 + 1):
                for y in range(y0, y1 + 1):
                    for z in range(z0, z1 + 1):
                        assert (x, y, z) not in covered, "pieces overlap"
                        covered.add((x, y, z))
        assert len(covered) == 41 ** 3

    def test_a_single_layer_too_large_is_split_further(self):
        # 400x1x400 = 160000, far over the limit even as one Y layer.
        pieces = _split_volume((0, 0, 0), (399, 0, 399))
        assert len(pieces) > 1
        for (x0, y0, z0), (x1, y1, z1) in pieces:
            volume = (x1 - x0 + 1) * (y1 - y0 + 1) * (z1 - z0 + 1)
            assert volume <= MAX_FILL_VOLUME

    def test_vanilla_mode_is_passed_through(self):
        lines = compile_one({
            "op": "fill",
            "from": {"x": 0, "y": 0, "z": 0},
            "to": {"x": 4, "y": 0, "z": 4},
            "block": "minecraft:air",
            "mode": "replace",
        })
        assert lines == ["fill 0 0 0 4 0 4 minecraft:air replace"]

    def test_unknown_mode_is_fatal(self):
        with pytest.raises(PlanError, match="unknown fill mode"):
            compile_one({
                "op": "fill",
                "from": {"x": 0, "y": 0, "z": 0},
                "to": {"x": 1, "y": 1, "z": 1},
                "block": "minecraft:stone",
                "mode": "obliterate",
            })

    def test_hollow_grid_expands_to_spaced_setblocks(self):
        lines = compile_one({
            "op": "fill",
            "from": {"x": 0, "y": 5, "z": 0},
            "to": {"x": 8, "y": 5, "z": 8},
            "block": "minecraft:composter",
            "mode": "hollow_grid",
            "spacing": 4,
        })
        assert all(line.startswith("setblock ") for line in lines)
        assert len(lines) == 9  # 3 x-positions * 3 z-positions

    def test_hollow_grid_requires_spacing(self):
        with pytest.raises(PlanError, match="positive 'spacing'"):
            compile_one({
                "op": "fill",
                "from": {"x": 0, "y": 0, "z": 0},
                "to": {"x": 4, "y": 0, "z": 4},
                "block": "minecraft:stone",
                "mode": "hollow_grid",
            })


class TestSpawnRing:
    def test_places_the_requested_count(self):
        lines = compile_one({
            "op": "spawn_ring",
            "entities": [{"type": "minecraft:zombie", "count": 50}],
            "radius": 32,
            "y": 5,
        })
        assert len(lines) == 50
        assert all(line.startswith("summon minecraft:zombie ") for line in lines)

    def test_placement_is_deterministic(self):
        """Random placement would vary the population between runs.

        That is why the scenarios using this also disable doMobSpawning: the
        population has to be identical for every variant or the comparison is
        between different workloads.
        """
        action = {
            "op": "spawn_ring",
            "entities": [{"type": "minecraft:cow", "count": 30}],
            "radius": 20,
            "y": 5,
        }
        assert compile_one(action) == compile_one(action)

    def test_applies_nbt_flags(self):
        lines = compile_one({
            "op": "spawn_ring",
            "entities": [{"type": "minecraft:cow", "count": 2}],
            "radius": 8,
            "persistent": True,
            "no_ai": True,
        })
        assert all("PersistenceRequired:1b" in line for line in lines)
        assert all("NoAI:1b" in line for line in lines)

    def test_handles_several_entity_types(self):
        lines = compile_one({
            "op": "spawn_ring",
            "entities": [
                {"type": "minecraft:zombie", "count": 10},
                {"type": "minecraft:cow", "count": 5},
            ],
            "radius": 16,
        })
        assert sum("zombie" in line for line in lines) == 10
        assert sum("cow" in line for line in lines) == 5

    def test_rejects_an_empty_entity_list(self):
        with pytest.raises(PlanError, match="needs 'entities'"):
            compile_one({"op": "spawn_ring", "entities": []})


class TestCameraPath:
    def test_paces_on_ticks_not_frames(self):
        """The property that keeps the workload identical across variants.

        One step per rendered frame would make a fast machine fly the path
        faster, so the fast and slow configurations would traverse different
        amounts of world and the comparison would be meaningless.
        """
        lines = compile_one({
            "op": "camera_path",
            "duration_s": 10,
            "points": [
                {"x": 0, "y": 80, "z": 0, "yaw": 0, "pitch": 0},
                {"x": 100, "y": 80, "z": 0, "yaw": 90, "pitch": 0},
            ],
        })
        teleports = [line for line in lines if line.startswith("tp ")]
        waits = [line for line in lines if line.startswith("@wait")]
        assert len(teleports) == 200  # 10 s at 20 steps/s
        assert len(waits) == len(teleports)

    def test_interpolates_between_points(self):
        lines = compile_one({
            "op": "camera_path",
            "duration_s": 1,
            "points": [
                {"x": 0, "y": 0, "z": 0},
                {"x": 100, "y": 0, "z": 0},
            ],
        })
        teleports = [line for line in lines if line.startswith("tp ")]
        assert teleports[0].split()[2] == "0.00"
        assert teleports[-1].split()[2] == "100.00"

    def test_requires_at_least_two_points(self):
        with pytest.raises(PlanError, match="at least two points"):
            compile_one({"op": "camera_path", "points": [{"x": 0, "y": 0, "z": 0}]})


class TestStructures:
    def test_expands_a_known_template(self):
        lines = compile_one({
            "op": "place_structure",
            "template": "hopper_chain",
            "count": 3,
            "spacing": 4,
            "origin": {"x": 0, "y": 5, "z": 0},
            "parameters": {"length": 8},
        })
        assert lines
        assert sum("minecraft:chest" in line for line in lines) == 3

    def test_unknown_template_is_fatal(self):
        with pytest.raises(PlanError, match="unknown structure template"):
            compile_one({
                "op": "place_structure",
                "template": "death_star",
                "origin": {"x": 0, "y": 0, "z": 0},
            })


class TestInstanceSettings:
    def test_distances_are_settings_not_commands(self):
        """Vanilla has no command for these, so they must not be silently lost."""
        lines, settings = _compile_action({"op": "set_render_distance", "chunks": 16}, "t")
        assert lines == []
        assert settings == {"render_distance": 16}

        lines, settings = _compile_action(
            {"op": "set_simulation_distance", "chunks": 8}, "t"
        )
        assert lines == []
        assert settings == {"simulation_distance": 8}


class TestPlanAssembly:
    def _scenario(self, **overrides):
        base = {
            "id": "test-plan",
            "version": "1.0.0",
            "title": "Test",
            "side": "server",
            "category": "entity",
            "world": {
                "seed": 5,
                "generator": "flat",
                "gamerules": {"doMobSpawning": False, "randomTickSpeed": 0},
                "time": 6000,
                "weather": "clear",
                "difficulty": "hard",
            },
            "measurement": {"warmup": {"min": 100}, "duration": {"full": 1000}},
        }
        base.update(overrides)
        return parse_scenario(base)

    def test_gamerules_run_before_setup(self):
        """Setup must not race the rules meant to hold it steady.

        Spawning entities before doMobSpawning is disabled would let natural
        spawns contaminate a deterministic population.
        """
        scenario = self._scenario(
            setup=[{"op": "summon", "type": "minecraft:cow", "x": 0, "y": 5, "z": 0}]
        )
        plan = compile_plan(scenario)
        gamerule_index = next(
            i for i, line in enumerate(plan.setup) if line.startswith("gamerule doMobSpawning")
        )
        summon_index = next(
            i for i, line in enumerate(plan.setup) if line.startswith("summon")
        )
        assert gamerule_index < summon_index

    def test_world_state_is_pinned(self):
        plan = compile_plan(self._scenario())
        joined = "\n".join(plan.setup)
        assert "time set 6000" in joined
        assert "weather clear" in joined
        assert "difficulty hard" in joined

    def test_properties_carry_the_methodology_settings(self):
        plan = compile_plan(self._scenario())
        assert plan.properties["scenario.id"] == "test-plan"
        assert plan.properties["scenario.side"] == "server"
        assert plan.properties["warmup.min"] == "100"
        # Java parses this with Double.parseDouble, which accepts either form.
        assert float(plan.properties["measurement.duration"]) == 1000.0
        assert plan.properties["scenario.content_hash"]

    def test_preset_selects_the_duration(self):
        scenario = self._scenario(
            measurement={"warmup": {"min": 10}, "duration": {"quick": 5, "full": 50}}
        )
        assert compile_plan(scenario, preset=Preset.QUICK).properties[
            "measurement.duration"
        ] == "5.0"

    def test_steady_state_window_matches_the_side(self):
        # Server scenarios advance in ticks and client scenarios in seconds, so a
        # window sized for one is badly wrong for the other.
        server = compile_plan(self._scenario())
        client = compile_plan(
            self._scenario(side="client", category="visual",
                           measurement={"warmup": {"min": 10}, "duration": {"full": 60}})
        )
        assert server.properties["warmup.steady_state_window"] == "200"
        assert client.properties["warmup.steady_state_window"] == "120"

    def test_writes_the_three_files_the_probe_reads(self, tmp_path):
        path, plan = write_plan(self._scenario(), tmp_path)
        assert path.name == "probe.properties"
        assert (tmp_path / "setup.txt").exists()
        assert (tmp_path / "workload.txt").exists()
        # Java's ProbeConfig parses this with java.util.Properties.
        text = path.read_text()
        assert "scenario.id=test-plan" in text


class TestShippedScenariosCompile:
    """Every shipped scenario must compile, or it cannot actually be run."""

    @pytest.fixture(scope="class")
    @classmethod
    def scenarios(cls):
        return load_scenarios(REPO / "scenarios")

    def test_all_compile(self, scenarios):
        for scenario in scenarios:
            plan = compile_plan(scenario)
            assert plan.properties["scenario.id"] == scenario.id

    def test_all_produce_setup_work(self, scenarios):
        for scenario in scenarios:
            plan = compile_plan(scenario)
            assert plan.setup, f"{scenario.id} compiles to no setup at all"

    def test_client_scenarios_declare_a_render_distance(self, scenarios):
        # Render distance dominates client cost; leaving it to whatever the
        # instance defaults to would let it vary between runs.
        for scenario in scenarios:
            if scenario.side is Side.CLIENT:
                plan = compile_plan(scenario)
                assert "render_distance" in plan.instance_settings, scenario.id

    def test_no_fill_command_exceeds_the_vanilla_limit(self, scenarios):
        for scenario in scenarios:
            plan = compile_plan(scenario)
            for line in plan.setup + plan.workload:
                if not line.startswith("fill "):
                    continue
                parts = line.split()
                x0, y0, z0, x1, y1, z1 = (int(p) for p in parts[1:7])
                volume = (
                    (abs(x1 - x0) + 1) * (abs(y1 - y0) + 1) * (abs(z1 - z0) + 1)
                )
                assert volume <= MAX_FILL_VOLUME, f"{scenario.id}: {line}"
