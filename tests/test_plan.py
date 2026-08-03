"""Tests for compiling scenarios into probe execution plans.

This is the join between the harness and the probe, and it was broken once
already — the harness wrote a scenario JSON the probe could not read, so the
probe would never have started. These tests exist so that seam stays connected.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from mcbench.config import Loader
from mcbench.runner.plan import (
    MAX_FILL_VOLUME,
    PlanError,
    _compile_action,
    _split_volume,
    compile_plan,
    write_plan,
)
from mcbench.scenario import Preset, Side, load_scenarios, parse_scenario
from mcbench.targets import Target

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

    def test_the_tag_is_a_separate_argument(self):
        """`summon` takes NBT as its own argument, so it needs a separator.

        Block and item NBT attaches with none — `minecraft:chest{Items:[...]}`
        — because there it belongs to one argument's grammar, and writing
        summon the same way is the natural mistake. Brigadier ends the position
        argument at `{` and then requires whitespace before the next, so every
        spawn_ring command was rejected and every run of the two scenarios that
        use one failed its setup.

        The assertion above passes either way, which is how it survived.
        """
        lines = compile_one({
            "op": "spawn_ring",
            "entities": [{"type": "minecraft:cow", "count": 2}],
            "radius": 8,
            "persistent": True,
        })
        for line in lines:
            head, sep, tag = line.partition("{")
            assert sep, line
            assert head.endswith(" "), line
            # And the coordinate before it is still a coordinate.
            assert head.split()[-1].replace("-", "").replace(".", "").isdigit(), line

    def test_no_flags_leaves_no_trailing_space(self):
        lines = compile_one({
            "op": "spawn_ring",
            "entities": [{"type": "minecraft:cow", "count": 1}],
            "radius": 8,
        })
        assert lines == [line.rstrip() for line in lines]
        assert "{" not in lines[0]

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


class TestCommandsNameSomethingThatExists:
    """Every emitted command has to resolve from the source that runs it.

    The probe dispatches through the platform's command dispatcher, whose
    source is the server rather than an entity. `@s` therefore matches nothing.
    A real run of visual-biome-flyby emitted 1200 rejected teleports, one per
    path step, and reported a full set of frametimes for a camera that never
    moved.
    """

    @pytest.mark.parametrize("action", [
        {"op": "camera_path", "duration_s": 1,
         "points": [{"x": 0, "y": 80, "z": 0}, {"x": 10, "y": 80, "z": 0}]},
        {"op": "teleport", "to": {"x": 1, "y": 2, "z": 3}},
        {"op": "look", "yaw": 90, "pitch": 0},
        {"op": "give", "item": "minecraft:stone"},
    ])
    def test_no_action_targets_the_command_source(self, action):
        for line in compile_one(action):
            assert "@s" not in line, line

    def test_looking_does_not_move_the_player(self):
        """`~ ~ ~` is relative to the source, which is the world origin.

        A bare `tp <player> ~ ~ ~ <yaw> <pitch>` from the server therefore
        turns the player's head by teleporting them to 0,0,0.
        """
        line = compile_one({"op": "look", "yaw": 90, "pitch": 10})[0]
        assert line.startswith("execute at ")
        assert "run tp " in line

    def test_a_scenario_may_still_name_its_own_target(self):
        line = compile_one({
            "op": "teleport", "target": "@e[type=cow,limit=1]",
            "to": {"x": 1, "y": 2, "z": 3},
        })[0]
        assert "@e[type=cow,limit=1]" in line

    def test_every_shipped_scenario_avoids_it(self):
        for scenario in load_scenarios(REPO / "scenarios"):
            plan = compile_plan(scenario)
            for line in [*plan.setup, *plan.workload]:
                assert "@s" not in line, f"{scenario.id}: {line}"


class TestTickWarp:
    """A scenario asking for tick warp has to actually be warped.

    Every other piece of this was already built: the schema field, the
    server-side-only validation, the Carpet requirement, the capability gate
    with its own refusal messages, the property handed to the probe, and the
    probe's parser for it. Nothing issued the command. Seven of the eight
    server scenarios declare `tick_warp`, and one of them says in its own
    description that without it "TPS reports 20 for every configuration and
    reveals nothing" — which is what they were all doing.
    """

    def warped(self, scenario_id: str):
        scenario = next(
            s for s in load_scenarios(REPO / "scenarios") if s.id == scenario_id
        )
        return scenario, compile_plan(scenario)

    def test_the_command_is_issued(self):
        _scenario, plan = self.warped("entity-mobcap-saturation")
        warps = [line for line in plan.setup if line.startswith("tick warp ")]
        assert len(warps) == 1, plan.setup

    def test_it_comes_after_the_world_is_built(self):
        # The setup above it contains waits counted in ticks. Compressed, they
        # would give the world less real time to settle than it asked for.
        _scenario, plan = self.warped("entity-mobcap-saturation")
        assert plan.setup[-1].startswith("tick warp ")

    def test_it_outlasts_warmup_and_measurement(self):
        # A warp that ran out partway would drop the server to 20 TPS in the
        # middle of the measurement, which is the failure the whole mechanism
        # exists to avoid, and it would leave no trace.
        scenario, plan = self.warped("entity-mobcap-saturation")
        warmup = scenario.measurement["warmup"]
        needed = (
            warmup["min"] * warmup.get("max_multiple", 3)
            + scenario.duration(Preset.FULL)
        )
        ticks = int(plan.setup[-1].removeprefix("tick warp "))
        assert ticks > needed

    def test_a_scenario_that_declined_it_is_not_warped(self):
        # tick-stability-saturated sets tick_warp false on purpose: it asks what
        # happens when real ticks overrun, and compressing them removes the
        # question.
        scenario, plan = self.warped("tick-stability-saturated")
        assert not scenario.uses_tick_warp
        assert not [line for line in plan.setup if line.startswith("tick warp")]

    def test_every_scenario_that_asks_for_it_gets_it(self):
        for scenario in load_scenarios(REPO / "scenarios"):
            plan = compile_plan(scenario)
            warps = [line for line in plan.setup if line.startswith("tick warp ")]
            assert len(warps) == (1 if scenario.uses_tick_warp else 0), scenario.id


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


class TestParticleOptions:
    """1.20.5 moved particle options into the particle.

    `particle minecraft:dust 1 0 0 2 ...` became
    `particle minecraft:dust{color:[1,0,0],scale:2} ...`. The old spelling is
    not degraded on a newer target, it is rejected, and a rejected workload
    command means a run reporting numbers for a load it never applied.

    Particle commands are raw `command` ops, so nothing compiled them and
    nothing checked them. visual-particle-storm shipped one and declared no
    version bound at all.
    """

    def _plan(self, command, version):
        scenario = parse_scenario({
            "id": "particle-test", "version": "1.0.0", "title": "P",
            "side": "client", "category": "visual",
            "world": {"seed": 1, "generator": "default"},
            "measurement": {"warmup": {"min": 60}, "duration": {"full": 60}},
            "workload": [{"op": "command", "value": command}],
        })
        target = Target(platform=Loader.FABRIC, minecraft_version=version)
        return compile_plan(scenario, target=target, strict=False)

    def test_the_old_spelling_is_refused_on_a_new_target(self):
        with pytest.raises(PlanError, match="belong to the particle"):
            self._plan("particle minecraft:dust 1 0 0 2 0 7 0 6 3 6 0 200", "1.21.1")

    def test_the_old_spelling_is_fine_on_an_old_target(self):
        plan = self._plan(
            "particle minecraft:dust 1 0 0 2 0 7 0 6 3 6 0 200", "1.20.4"
        )
        assert plan.workload

    def test_the_new_spelling_passes(self):
        plan = self._plan(
            "particle minecraft:dust{color:[1,0,0],scale:2} 0 7 0 6 3 6 0 200",
            "1.21.1",
        )
        assert plan.workload

    def test_a_particle_with_no_options_is_never_flagged(self):
        # Checked by name, not by counting arguments: the argument list has
        # grown a viewer since, and a count would refuse valid commands.
        for command in (
            "particle minecraft:flame 0 6 0 8 3 8 0.05 400 force",
            "particle minecraft:end_rod 0 8 0 8 3 8 0.02 300 force",
            "particle minecraft:crit 0 6 0 6 3 6 0.20 200 force",
        ):
            assert self._plan(command, "1.21.1").workload

    def test_every_shipped_scenario_compiles_for_the_versions_it_targets(self):
        for scenario in load_scenarios(REPO / "scenarios"):
            for version in ("1.20.4", "1.21.1"):
                target = Target(platform=Loader.FABRIC, minecraft_version=version)
                compile_plan(scenario, target=target, strict=False)
