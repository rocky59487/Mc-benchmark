"""Instance provisioning, run bookkeeping, and the analysis path end to end.

Each group covers a place where the code recorded something and never used it,
so a check appeared to exist while nothing performed it: a probe configuration
with no in-game consumer, failed attempts that vanished before serialisation, a
launch configuration parsed and applied nowhere, and FDR control that lived only
in the library.
"""

from __future__ import annotations

import json
import zipfile
from pathlib import Path

import pytest

from mcbench.config import parse_suite
from mcbench.metrics import RunFlag, RunMetrics
from mcbench.planner import Cell, PlannedRun
from mcbench.report import REFERENCE_SCENARIO
from mcbench.runner import Harness, Severity
from mcbench.runner.harness import RunOutcome, outcomes_to_cells, run_counts
from mcbench.scenario import load_scenarios

REPO = Path(__file__).resolve().parents[1]


def scenarios():
    return {s.id: s for s in load_scenarios(REPO / "scenarios")}


def jar(path: Path) -> Path:
    with zipfile.ZipFile(path, "w") as archive:
        archive.writestr("fabric.mod.json", '{"schemaVersion":1,"id":"probe"}')
    return path


def harness(tmp_path, *, loader="fabric", scenario="entity-mobcap-saturation", **extra):
    suite = parse_suite({
        "name": "t", "minecraft_version": "1.21.1", "loader": loader,
        "scenarios": [scenario],
        "variants": [{"name": "base", "mods": []}],
        "baseline": "base",
        **extra.pop("suite", {}),
    })
    return Harness(suite, scenarios(), work_dir=tmp_path, **extra)


class TestProbeProvisioning:
    def test_a_missing_probe_blocks_the_suite(self, tmp_path):
        """Without a probe there is no in-game consumer for the configuration.

        The harness writes MCBENCH_PROBE_CONFIG and waits for a probe.jsonl that
        only exists if something inside the game reads it. Nothing was installing
        that something, so a vanilla baseline could not produce a stream at all —
        and the failure looked like a crash two hours into a suite.
        """
        h = harness(tmp_path, headlessmc=tmp_path / "hmc.jar", probe_jar=tmp_path / "nope.jar")
        result = h.preflight(require_account=False)
        check = next(c for c in result.checks if c.name == "probe")
        assert check.severity is Severity.BLOCK
        assert "gradlew" in check.remedy
        assert not result.admissible

    def test_the_probe_is_installed_into_every_instance(self, tmp_path):
        """Including the baseline, which is the arm that had no mods at all."""
        probe = jar(tmp_path / "mcbench-probe.jar")
        h = harness(tmp_path / "work", probe_jar=probe)
        h._resolved["base"] = type(
            "R", (), {"jars": [], "provenance": [], "variant": None}
        )()

        planned = PlannedRun(
            cell=Cell("entity-mobcap-saturation", "base"),
            replicate=0, position=0, round_index=0,
        )
        instance = h._prepare_instance(planned, scenarios()["entity-mobcap-saturation"])
        assert (instance / "mods" / "mcbench-probe.jar").exists()

    def test_paper_plugins_go_to_the_plugins_directory(self, tmp_path):
        """Paper does not read mods/ at all.

        Every plugin previously installed there was silently ignored, so the
        "variant" being measured was in fact vanilla Paper — and it started
        cleanly and measured nothing unusual.
        """
        probe = jar(tmp_path / "probe.jar")
        h = harness(
            tmp_path / "work", loader="paper",
            scenario="entity-mobcap-saturation", probe_jar=probe,
        )
        assert h.probe.install_dir == "plugins"

        h._resolved["base"] = type("R", (), {"jars": [], "provenance": []})()
        planned = PlannedRun(
            cell=Cell("entity-mobcap-saturation", "base"),
            replicate=0, position=0, round_index=0,
        )
        instance = h._prepare_instance(planned, scenarios()["entity-mobcap-saturation"])
        assert (instance / "plugins" / "probe.jar").exists()
        assert not (instance / "mods").exists()

    def test_fabric_needs_the_api_the_probe_depends_on(self, tmp_path):
        probe = jar(tmp_path / "probe.jar")
        h = harness(tmp_path / "work", probe_jar=probe)
        assert h.probe.needs_fabric_api
        gaps = h.probe.missing()
        assert any("Fabric API" in gap for gap in gaps)


class TestClientWorldBootstrap:
    def test_a_client_run_is_launched_into_a_world_that_exists(self, tmp_path):
        """Quick-play only enters a world already on disk.

        Without one the client stops at the world-selection screen, the
        integrated server never starts, and the Fabric probe — which waits for
        SERVER_STARTED — never fires. The run then times out having measured a
        menu.
        """
        from mcbench.scenario import Side

        probe = jar(tmp_path / "probe.jar")
        found = scenarios()
        client = next(s for s in found.values() if s.side is Side.CLIENT)
        h = harness(
            tmp_path / "work", scenario=client.id, probe_jar=probe,
            headlessmc=tmp_path / "hmc.jar",
        )
        h._resolved["base"] = type("R", (), {"jars": [], "provenance": []})()

        planned = PlannedRun(
            cell=Cell(client.id, "base"), replicate=0, position=0, round_index=0,
        )
        instance = h._prepare_instance(planned, client)
        world = instance / "saves" / h._client_world_name(client)
        assert (world / "level.dat").exists()

        command = h._launch_command(instance, client, h.suite.variants[0])
        assert "--quickPlaySingleplayer" in command
        assert h._client_world_name(client) in command

    def test_the_world_is_authored_from_the_scenario_seed(self, tmp_path):
        """Two instances of the same scenario must get the same world."""
        import gzip

        from mcbench.nbt import parse_nbt
        from mcbench.world import level_dat

        first = level_dat(name="w", seed=1234)
        second = level_dat(name="w", seed=1234)
        assert first == second, "the same description must produce the same bytes"

        data = parse_nbt(gzip.decompress(first))["Data"]
        assert data["WorldGenSettings"]["seed"] == 1234
        # Pinned so the world does not drift under the measurement.
        assert data["GameRules"]["doDaylightCycle"] == "false"
        assert data["GameRules"]["randomTickSpeed"] == "0"

    def test_a_flat_scenario_gets_a_flat_world(self, tmp_path):
        import gzip

        from mcbench.nbt import parse_nbt
        from mcbench.world import level_dat

        data = parse_nbt(gzip.decompress(level_dat(name="w", seed=1, generator="flat")))
        overworld = data["Data"]["WorldGenSettings"]["dimensions"]["minecraft:overworld"]
        assert overworld["generator"]["type"] == "minecraft:flat"


class TestLaunchConfiguration:
    def _suite(self, **extra):
        return parse_suite({
            "name": "t", "minecraft_version": "1.21.1", "loader": "fabric",
            "loader_version": "0.16.5",
            "scenarios": ["entity-mobcap-saturation"],
            "jvm_args": ["-XX:+AlwaysPreTouch"],
            "game_settings": {"renderDistance": 8},
            "variants": [
                {"name": "base", "mods": []},
                {"name": "tuned", "mods": [],
                 "jvm_args": ["-XX:+UseZGC"],
                 "game_settings": {"renderDistance": 12}},
            ],
            "baseline": "base",
            **extra,
        })

    def test_each_variant_gets_its_own_jvm_arguments(self, tmp_path):
        """The baseline's flags were used for every variant.

        A suite that varied JVM flags on purpose therefore launched the same
        configuration twice and reported a difference between two mod sets.
        """
        h = Harness(self._suite(), scenarios(), work_dir=tmp_path)
        base, tuned = h.suite.variants

        assert "-XX:+AlwaysPreTouch" in h.effective_jvm_args(base)
        assert "-XX:+UseZGC" not in h.effective_jvm_args(base)
        assert "-XX:+UseZGC" in h.effective_jvm_args(tuned)
        # Suite-level arguments are inherited, not replaced.
        assert "-XX:+AlwaysPreTouch" in h.effective_jvm_args(tuned)

    def test_variant_game_settings_override_the_suite(self, tmp_path):
        h = Harness(self._suite(), scenarios(), work_dir=tmp_path)
        base, tuned = h.suite.variants
        assert h.effective_game_settings(base)["renderDistance"] == 8
        assert h.effective_game_settings(tuned)["renderDistance"] == 12

    def test_the_requested_loader_version_is_asked_for(self, tmp_path):
        h = Harness(
            self._suite(), scenarios(), work_dir=tmp_path,
            headlessmc=tmp_path / "hmc.jar",
        )
        command = h._launch_command(
            tmp_path, scenarios()["entity-mobcap-saturation"], h.suite.variants[0]
        )
        assert "--loader-version" in command
        assert "0.16.5" in command

    def test_provenance_reconstructs_the_effective_configuration(self, tmp_path):
        """A result bundle has to say what was actually launched.

        Two runs on different loader builds, different JVM flags and different
        artefacts previously serialised identically, so the reproducibility the
        schema implies was not something the data could support.
        """
        h = Harness(self._suite(), scenarios(), work_dir=tmp_path)
        h._resolved = {
            v.name: type("R", (), {"jars": [], "provenance": []})()
            for v in h.suite.variants
        }
        provenance = h.provenance()

        assert provenance["loader_version"] == "0.16.5"
        assert provenance["client_max_fps"] == 260
        assert "-XX:+UseZGC" in provenance["jvm_args"]["tuned"]
        assert "-XX:+UseZGC" not in provenance["jvm_args"]["base"]
        assert provenance["game_settings"]["tuned"]["renderDistance"] == 12
        assert provenance["scenarios"]["entity-mobcap-saturation"]["content_hash"]
        # Round trip: the bundle survives serialisation.
        assert json.loads(json.dumps(provenance)) == provenance

    def test_preflight_serialises_its_readings_not_just_a_verdict(self, tmp_path):
        h = Harness(self._suite(), scenarios(), work_dir=tmp_path)
        payload = h.preflight(require_account=False).to_dict()
        assert payload["checks"], "the individual readings must survive"
        assert {"admissible", "publishable", "forced", "host"} <= set(payload)
        assert all({"name", "severity", "detail"} <= set(c) for c in payload["checks"])


class TestFailedAttempts:
    def _outcome(self, variant, position, *, metrics, error="", exit_code=None):
        return RunOutcome(
            planned=PlannedRun(
                cell=Cell("bench", variant), replicate=position,
                position=position, round_index=position,
            ),
            metrics=metrics,
            error=error,
            exit_code=exit_code,
        )

    def test_every_attempt_is_serialised(self, tmp_path):
        """A seven-run cell with two failures is not a clean five-run cell."""
        good = [
            self._outcome("base", i, metrics=RunMetrics(values={"mspt_mean": 10.0}))
            for i in range(5)
        ]
        failed = [
            self._outcome("base", 5 + i, metrics=None, error="timed out", exit_code=1)
            for i in range(2)
        ]
        cells = outcomes_to_cells([*good, *failed])
        runs = cells["bench/base"]
        assert len(runs) == 7
        assert sum(1 for r in runs if r["status"] == "failed") == 2
        assert all(r["values"] == {} for r in runs if r["status"] == "failed")
        assert any(r.get("error") == "timed out" for r in runs)

    def test_counts_separate_attempted_completed_and_admissible(self):
        outcomes = [
            self._outcome("base", 0, metrics=RunMetrics(values={"mspt_mean": 10.0})),
            self._outcome(
                "base", 1,
                metrics=RunMetrics(values={"mspt_mean": 10.0},
                                   flags=[RunFlag.SETUP_FAILED]),
            ),
            self._outcome("base", 2, metrics=None, error="crashed"),
        ]
        counts = run_counts(outcomes)["bench/base"]
        assert counts == {
            "attempted": 3, "completed": 2, "admissible": 1, "failed": 1
        }

    def test_a_failed_attempt_does_not_count_toward_the_run_floor(self, tmp_path):
        """It is in the document, out of the estimates, and visible in both."""
        from mcbench.cli import main

        payload = {
            "suite": "s", "baseline": "base",
            "cells": {
                "bench/base": [
                    {"values": {"mspt_mean": 10.0 + i * 0.01}, "flags": [],
                     "status": "completed"}
                    for i in range(7)
                ],
                "bench/mod": [
                    {"values": {"mspt_mean": 20.0}, "flags": [], "status": "completed"},
                    *[
                        {"values": {}, "flags": [], "status": "failed",
                         "error": "launch failed"}
                        for _ in range(6)
                    ],
                ],
            },
        }
        results = tmp_path / "results.json"
        results.write_text(json.dumps(payload), encoding="utf-8")
        out = tmp_path / "report.json"
        assert main(["analyse", str(results), "--format", "json", "-o", str(out)]) == 0

        report = json.loads(out.read_text())
        cell = next(c for c in report["cells"] if c["variant"] == "mod")
        assert cell["runs_attempted"] == 7
        assert cell["runs_failed"] == 6
        assert cell["runs_admissible"] == 1
        [comparison] = report["comparisons"]
        assert comparison["verdict"] == "insufficient_data"

    def test_a_run_that_reported_an_error_is_not_pooled(self):
        """The probe's own error events were parsed, retained, and ignored.

        A run that announced its own failure produced values describing a world
        the scenario never built, and those values look entirely ordinary.
        """
        from mcbench.runner.harness import Harness as H
        from mcbench.runner.protocol import ProbeStream

        stream = ProbeStream()
        stream.server.tick_durations_ns = [10_000_000] * 600
        stream.errors.append("setup command failed: /fill ...")
        metrics = H._reduce(stream, scenarios()["entity-mobcap-saturation"])
        assert RunFlag.PROBE_ERROR in metrics.flags
        assert not metrics.admissible

    def test_a_setup_failure_flag_makes_a_run_inadmissible(self):
        metrics = RunMetrics(
            values={"mspt_mean": 10.0}, flags=[RunFlag.SETUP_FAILED]
        )
        assert not metrics.admissible


class TestTickSourceReachesTheMetrics:
    def test_a_period_stream_publishes_period_metrics_not_mspt(self):
        from mcbench.runner.harness import Harness as H
        from mcbench.runner.protocol import ProbeStream, TickSource

        stream = ProbeStream()
        stream.tick_source = TickSource.PERIOD
        stream.server.tick_durations_ns = [50_000_000] * 600
        metrics = H._reduce(stream, scenarios()["entity-mobcap-saturation"])

        assert "mspt_mean" not in metrics.values
        assert "tick_headroom" not in metrics.values
        assert metrics.values["tick_period_mean_ms"] == pytest.approx(50.0)
        assert RunFlag.TICK_PERIOD_ONLY in metrics.flags
        # Not a reason to discard the run — only a reason not to call it MSPT.
        assert metrics.admissible

    def test_a_bracketed_stream_publishes_mspt(self):
        from mcbench.runner.harness import Harness as H
        from mcbench.runner.protocol import ProbeStream, TickSource

        stream = ProbeStream()
        stream.tick_source = TickSource.BRACKET
        stream.server.tick_durations_ns = [5_000_000] * 600
        metrics = H._reduce(stream, scenarios()["entity-mobcap-saturation"])

        assert metrics.values["mspt_mean"] == pytest.approx(5.0)
        assert metrics.values["tick_headroom"] == pytest.approx(0.9)


class TestSharedWorldCarriesTerrainOnly:
    """The cache is terrain, not the state a run left behind.

    Copying a whole world directory carried data/chunks.dat, which records
    which chunks are force-loaded. The next run's `/forceload add` then found
    nothing left to mark, which Minecraft reports as a failure, so every run
    after the first failed setup. It only showed up on the second run.
    """

    def _harness(self, tmp_path):
        from mcbench.runner import Harness

        suite = parse_suite({
            "name": "t", "minecraft_version": "1.21.1", "loader": "fabric",
            "scenarios": ["visual-biome-flyby"],
            "variants": [{"name": "base", "mods": []}],
            "baseline": "base",
        })
        return Harness(suite, scenarios(), work_dir=tmp_path)

    def _world(self, root, name="w"):
        world = root / name
        (world / "region").mkdir(parents=True)
        (world / "region" / "r.0.0.mca").write_bytes(b"terrain")
        (world / "DIM-1" / "region").mkdir(parents=True)
        (world / "DIM-1" / "region" / "r.0.0.mca").write_bytes(b"nether")
        (world / "data").mkdir()
        (world / "data" / "chunks.dat").write_bytes(b"forceloaded")
        (world / "playerdata").mkdir()
        (world / "playerdata" / "someone.dat").write_bytes(b"inventory")
        (world / "level.dat").write_bytes(b"authored")
        return world

    def test_terrain_is_carried_and_run_state_is_not(self, tmp_path):
        h = self._harness(tmp_path)
        scenario = scenarios()["visual-biome-flyby"]

        source = self._world(tmp_path / "first")
        h._cache_world(scenario, source)

        target = tmp_path / "second" / "w"
        (target / "region").mkdir(parents=True)
        (target / "level.dat").write_bytes(b"this run's own")
        assert h._restore_cached_world(scenario, target)

        assert (target / "region" / "r.0.0.mca").read_bytes() == b"terrain"
        assert (target / "DIM-1" / "region" / "r.0.0.mca").read_bytes() == b"nether"
        assert not (target / "data").exists(), "force-load state must not carry"
        assert not (target / "playerdata").exists()
        assert (target / "level.dat").read_bytes() == b"this run's own"

    def test_fresh_world_mode_caches_nothing(self, tmp_path):
        h = self._harness(tmp_path)
        h.fresh_world = True
        scenario = scenarios()["visual-biome-flyby"]

        h._cache_world(scenario, self._world(tmp_path / "first"))
        target = tmp_path / "second" / "w"
        (target / "region").mkdir(parents=True)
        assert not h._restore_cached_world(scenario, target)

    def test_a_scenario_that_measures_generation_refuses_the_cache(self, tmp_path):
        # worldgen-fresh-chunks declared force_fresh and nothing read it, so
        # every run after the first was handed finished terrain and timed chunk
        # loading — which is what chunkio-load-throughput measures, and the
        # opposite of what this scenario is for.
        h = self._harness(tmp_path)
        scenario = scenarios()["worldgen-fresh-chunks"]
        assert scenario.force_fresh
        assert not h.shares_world(scenario)

        h._cache_world(scenario, self._world(tmp_path / "first"))
        target = tmp_path / "second" / "w"
        (target / "region").mkdir(parents=True)
        assert not h._restore_cached_world(scenario, target)

    def test_every_other_scenario_still_shares_one_world(self, tmp_path):
        # Generating per run would make every pair of runs a fingerprint
        # mismatch, so the refusal has to stay confined to the one scenario
        # that asks for it.
        h = self._harness(tmp_path)
        refusing = [s.id for s in scenarios().values() if not h.shares_world(s)]
        assert refusing == ["worldgen-fresh-chunks"]


class TestWindowResolution:
    """METHODOLOGY section 7 promises the resolution is recorded. It was not.

    Nothing set it either, so a client run rendered at Minecraft's own 854x480
    default, where a renderer mod is CPU-bound and the GPU is idle. The same
    mod on the same machine at 1080p can show a completely different result,
    and two machines whose windows differed were pooled as though they had not.
    """

    def _harness(self, tmp_path, **suite_overrides):
        from mcbench.runner import Harness

        suite = parse_suite({
            "name": "t", "minecraft_version": "1.21.1", "loader": "fabric",
            "scenarios": ["visual-biome-flyby"],
            "variants": [{"name": "base", "mods": []}],
            "baseline": "base",
            **suite_overrides,
        })
        return Harness(suite, scenarios(), work_dir=tmp_path)

    def test_a_default_is_stated_rather_than_left_to_the_game(self, tmp_path):
        h = self._harness(tmp_path)
        assert h.effective_resolution(h.suite.variants[0]) == (1920, 1080)

    def test_a_suite_may_declare_one(self, tmp_path):
        h = self._harness(tmp_path, game_settings={"resolution": "1280x720"})
        assert h.effective_resolution(h.suite.variants[0]) == (1280, 720)

    def test_an_unparseable_resolution_is_refused(self, tmp_path):
        from mcbench.runner.harness import HarnessError

        h = self._harness(tmp_path, game_settings={"resolution": "1080p"})
        with pytest.raises(HarnessError, match="WIDTHxHEIGHT"):
            h.effective_resolution(h.suite.variants[0])

    def test_it_reaches_the_game_as_arguments(self, tmp_path):
        h = self._harness(tmp_path, game_settings={"resolution": "1280x720"})
        args = h._game_arguments(
            scenarios()["visual-biome-flyby"], h.suite.variants[0]
        )
        assert "--width" in args and args[args.index("--width") + 1] == "1280"
        assert "--height" in args and args[args.index("--height") + 1] == "720"

    def test_a_server_run_has_no_window(self, tmp_path):
        h = self._harness(tmp_path)
        assert h._game_arguments(scenarios()["entity-mobcap-saturation"]) == []


class TestFrameCapFlag:
    """The flag means "the limiter was measured", not "the machine is fast".

    options.txt is written with maxFps at the top of vanilla's slider, which
    disables the limiter. Checking every run against that number flagged a
    5070 Ti for rendering a light scene faster than 260 fps, which is the
    opposite of the condition the flag exists to catch.
    """

    def _client_run(self, frametime_ms: float, **kwargs):
        from mcbench.runner.harness import Harness as H
        from mcbench.runner.protocol import ProbeStream

        stream = ProbeStream()
        stream.client.frametimes_ns = [int(frametime_ms * 1_000_000)] * 2000
        stream.client.duration_s = 60.0
        return H._reduce(stream, scenarios()["visual-biome-flyby"], **kwargs)

    def test_a_fast_machine_is_not_flagged_when_no_limiter_is_set(self):
        # 500 fps, well past the sentinel, with the limiter disabled.
        metrics = self._client_run(2.0)
        assert RunFlag.FRAME_CAP_SUSPECTED not in metrics.flags

    def test_a_run_against_a_configured_limiter_is_flagged(self):
        # The suite asked for 60 fps and the frametimes sit on 16.7 ms.
        metrics = self._client_run(1000.0 / 60.0, max_fps=60)
        assert RunFlag.FRAME_CAP_SUSPECTED in metrics.flags

    def test_a_run_clear_of_its_limiter_is_not_flagged(self):
        metrics = self._client_run(50.0, max_fps=60)
        assert RunFlag.FRAME_CAP_SUSPECTED not in metrics.flags
        assert RunFlag.TICK_PERIOD_ONLY not in metrics.flags


class TestAnalysisAppliesWhatItDocuments:
    def _payload(self, *, interactions=None, reference=False):
        def runs(base, n=7):
            return [
                {"values": {"frametime_mean_ms": base + i * 0.01},
                 "flags": [], "status": "completed"}
                for i in range(n)
            ]

        cells = {
            "bench/vanilla": runs(10.0),
            "bench/a": runs(11.0),
            "bench/b": runs(11.0),
            "bench/a+b": runs(20.0),
        }
        if reference:
            cells[f"{REFERENCE_SCENARIO}/vanilla"] = runs(5.0)
        payload = {"suite": "s", "baseline": "vanilla", "cells": cells}
        if interactions:
            payload["interactions"] = interactions
        return payload

    def _analyse(self, tmp_path, payload, *args):
        from mcbench.cli import main

        results = tmp_path / "results.json"
        results.write_text(json.dumps(payload), encoding="utf-8")
        out = tmp_path / "report.json"
        assert main(
            ["analyse", str(results), "--format", "json", "-o", str(out), *args]
        ) == 0
        return json.loads(out.read_text())

    def test_fdr_correction_runs_on_the_ordinary_path(self, tmp_path):
        """It existed as a library function no normal run ever called.

        A reader who knows the methodology assumed the protection was applied,
        which is worse than not having it at all.
        """
        report = self._analyse(tmp_path, self._payload())
        assert report["multiplicity"]["families"]
        for comparison in report["comparisons"]:
            assert comparison["q_value"] is not None
            assert comparison["family"]
            assert "verdict_raw" in comparison

    def test_a_family_is_one_scenario_and_metric(self, tmp_path):
        report = self._analyse(tmp_path, self._payload())
        families = {c["family"] for c in report["comparisons"]}
        assert families == {"bench::frametime_mean_ms"}

    def test_declared_interactions_are_computed(self, tmp_path):
        """`build_interaction` existed and suite analysis never called it."""
        report = self._analyse(
            tmp_path, self._payload(interactions=[["a", "b", "a+b"]])
        )
        assert report["interactions"], "a declared interaction must be computed"
        [item] = report["interactions"]
        assert item["pair"] == ["a", "b"]
        assert item["verdict"] == "costs more together"

    def test_no_interactions_are_invented_without_a_declaration(self, tmp_path):
        report = self._analyse(tmp_path, self._payload())
        assert report["interactions"] == []

    def test_reference_normalised_ratios_are_produced(self, tmp_path):
        report = self._analyse(tmp_path, self._payload(reference=True))
        assert report["normalised"], "documented ratios must actually be computed"
        ratio = next(
            r for r in report["normalised"]
            if r["variant"] == "vanilla" and r["scenario"] == "bench"
        )
        # 10.03 / 5.03: the mean of each arm, not the nominal base value.
        assert ratio["ratio"] == pytest.approx(10.03 / 5.03, rel=1e-6)

    def test_normalisation_is_omitted_rather_than_faked(self, tmp_path):
        """Without the reference scenario there is nothing to normalise against."""
        report = self._analyse(tmp_path, self._payload(reference=False))
        assert report["normalised"] == []
