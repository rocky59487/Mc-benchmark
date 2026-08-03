"""Tests for preflight and the probe protocol."""

from __future__ import annotations

import json

import pytest

from mcbench.metrics import RunFlag
from mcbench.runner import (
    PROTOCOL_VERSION,
    ProbeError,
    ProbeStream,
    Severity,
    adopt_agent_frames,
    describe_host,
    parse_probe_stream,
    run_preflight,
)


def _stream(*events: dict) -> str:
    return "\n".join(json.dumps(e) for e in events) + "\n"


def _hello() -> dict:
    return {"type": "hello", "protocol": PROTOCOL_VERSION, "probe_version": "0.1.0"}


class TestPreflight:
    def test_produces_a_check_for_every_probe(self):
        result = run_preflight(require_account=False)
        names = {c.name for c in result.checks}
        for expected in ("gpu", "display", "memory", "disk", "cpu_count"):
            assert expected in names

    def test_server_only_runs_do_not_require_a_gpu(self):
        result = run_preflight(needs_gpu=False, require_account=False)
        gpu = next(c for c in result.checks if c.name == "gpu")
        assert gpu.severity is not Severity.BLOCK

    def test_blockers_make_a_run_inadmissible(self):
        result = run_preflight(require_account=False)
        assert result.admissible == (not result.blockers)

    def test_warnings_block_publication_but_not_measurement(self):
        result = run_preflight(needs_gpu=False, require_account=False)
        if result.warnings and not result.blockers:
            assert result.admissible
            assert not result.publishable

    def test_an_impossible_heap_is_a_blocker(self):
        result = run_preflight(
            needs_gpu=False, heap_mb=1024 * 1024, require_account=False
        )
        memory = next(c for c in result.checks if c.name == "memory")
        assert memory.severity is Severity.BLOCK

    def test_blockers_carry_an_actionable_remedy(self):
        result = run_preflight(needs_gpu=True, require_account=True)
        for check in result.blockers:
            assert check.remedy, f"{check.name} blocks without telling the user why"

    def test_host_description_records_provenance_fields(self):
        host = describe_host()
        for key in ("os", "arch", "cpu_count", "gpu"):
            assert key in host


class TestProbeProtocol:
    def test_parses_a_complete_client_run(self):
        text = _stream(
            _hello(),
            {"type": "phase", "phase": "warmup"},
            {"type": "frame", "durations_ns": [20_000_000] * 10},
            {"type": "phase", "phase": "measurement"},
            {"type": "frame", "durations_ns": [16_000_000] * 100},
            {"type": "gc", "source": "events", "events": [
                {"collector": "G1 Young Generation", "action": "end of minor GC",
                 "duration_ms": 1.5, "heap_before_mb": 900.0,
                 "heap_after_mb": 400.0, "stop_the_world": True},
                {"collector": "G1 Young Generation", "action": "end of minor GC",
                 "duration_ms": 2.0, "heap_before_mb": 880.0,
                 "heap_after_mb": 410.0, "stop_the_world": True},
            ]},
            {"type": "memory", "heap_mb": 800.0, "post_gc": True,
             "allocated_bytes": 1024 * 1024 * 100, "real_allocation": True},
            {"type": "bye", "measurement_duration_s": 60.0},
        )
        stream = parse_probe_stream("test", text=text)
        assert stream.completed
        assert len(stream.client.frametimes_ns) == 100
        assert stream.client.gc_pauses_ms == [1.5, 2.0]
        assert stream.client.duration_s == 60.0
        # The live set comes from the collection itself, not from whatever the
        # next sampling interval happened to catch.
        assert 400.0 in stream.client.heap_post_gc_mb
        assert stream.real_allocation

    def test_several_collections_in_one_interval_stay_several_pauses(self):
        """The failure the aggregate form produced.

        Three short collections inside one sampling interval used to arrive as a
        single 15 ms number, so the p99 described a sum rather than a pause and
        a collector doing many quick collections read as one doing long ones.
        """
        text = _stream(
            _hello(),
            {"type": "phase", "phase": "measurement"},
            {"type": "frame", "durations_ns": [16_000_000] * 100},
            {"type": "gc", "source": "events", "events": [
                {"collector": "G1 Young Generation", "action": "end of minor GC",
                 "duration_ms": 5.0, "heap_before_mb": 900.0,
                 "heap_after_mb": 400.0, "stop_the_world": True},
                {"collector": "G1 Young Generation", "action": "end of minor GC",
                 "duration_ms": 5.0, "heap_before_mb": 890.0,
                 "heap_after_mb": 405.0, "stop_the_world": True},
                {"collector": "G1 Young Generation", "action": "end of minor GC",
                 "duration_ms": 5.0, "heap_before_mb": 880.0,
                 "heap_after_mb": 410.0, "stop_the_world": True},
            ]},
            {"type": "bye", "measurement_duration_s": 60.0},
        )
        stream = parse_probe_stream("test", text=text)
        assert stream.client.gc_pauses_ms == [5.0, 5.0, 5.0]

        from mcbench.metrics import reduce_client_run

        metrics = reduce_client_run(stream.client, min_frames=1)
        assert metrics.values["gc_count"] == 3
        assert metrics.values["gc_pause_total_ms"] == pytest.approx(15.0)
        # The worst pause was 5 ms, not the 15 ms the interval total would give.
        assert metrics.values["gc_pause_max_ms"] == pytest.approx(5.0)
        assert metrics.values["gc_pause_p99_ms"] == pytest.approx(5.0)

    def test_a_concurrent_cycle_is_not_counted_as_a_pause(self):
        """A concurrent collector's cycle spans work the application ran through.

        Counting it would make a low-pause collector read as the worst one on
        the machine and put hundreds of milliseconds into a percentile meant to
        describe hitching.
        """
        text = _stream(
            _hello(),
            {"type": "phase", "phase": "measurement"},
            {"type": "frame", "durations_ns": [16_000_000] * 100},
            {"type": "gc", "source": "events", "events": [
                {"collector": "G1 Concurrent GC", "action": "end of concurrent cycle",
                 "duration_ms": 400.0, "heap_before_mb": 900.0,
                 "heap_after_mb": 400.0, "stop_the_world": False},
                {"collector": "G1 Young Generation", "action": "end of minor GC",
                 "duration_ms": 3.0, "heap_before_mb": 880.0,
                 "heap_after_mb": 410.0, "stop_the_world": True},
            ]},
            {"type": "bye", "measurement_duration_s": 60.0},
        )
        stream = parse_probe_stream("test", text=text)
        assert stream.client.gc_pauses_ms == [3.0]

    def test_the_run_summary_travels_into_the_results(self):
        """A reader must be able to tell a converged run from a timed-out one."""
        text = _stream(
            _hello(),
            {"type": "phase", "phase": "measurement"},
            {"type": "frame", "durations_ns": [16_000_000] * 100},
            {"type": "bye", "measurement_duration_s": 60.0,
             "setup_duration": 12.0, "warmup_duration": 61.0,
             "warmup_gate": "ceiling reached: compilation was still active",
             "warmup_converged": False, "compilation_gate": True,
             "tick_source": "bracket", "failed_setup_commands": 0,
             "failed_workload_commands": 0, "real_allocation": True,
             "gc_events": True},
        )
        stream = parse_probe_stream("test", text=text)
        assert stream.summary["warmup_converged"] is False
        assert "compilation was still active" in stream.summary["warmup_gate"]
        assert stream.summary["setup_duration"] == 12.0

    def test_legacy_aggregate_pauses_are_not_treated_as_individual_ones(self):
        """The old `pauses_ms` shape held per-interval totals, not pauses.

        Three short collections inside one sampling interval arrived as a single
        number, so a percentile over those numbers described the flush cadence.
        Streams in that shape still parse; their totals are kept and no
        percentile is derived from them.
        """
        text = _stream(
            _hello(),
            {"type": "phase", "phase": "measurement"},
            {"type": "frame", "durations_ns": [16_000_000] * 100},
            {"type": "gc", "pauses_ms": [1.5, 2.0]},
            {"type": "bye", "measurement_duration_s": 60.0},
        )
        stream = parse_probe_stream("test", text=text)
        assert stream.gc_aggregate_only
        assert stream.client.gc_pauses_ms == []
        assert stream.client.gc_total_pause_ms == pytest.approx(3.5)

    def test_warmup_samples_are_kept_separate_from_measurement(self):
        """Warmup must never reach the measurement statistics."""
        text = _stream(
            _hello(),
            {"type": "phase", "phase": "warmup"},
            {"type": "frame", "durations_ns": [90_000_000] * 30},
            {"type": "phase", "phase": "measurement"},
            {"type": "frame", "durations_ns": [16_000_000] * 50},
            {"type": "bye", "measurement_duration_s": 10.0},
        )
        stream = parse_probe_stream("test", text=text)
        assert len(stream.client.frametimes_ns) == 50
        assert len(stream.warmup_frames_ns) == 30
        assert all(v == 16_000_000 for v in stream.client.frametimes_ns)

    def test_provision_samples_are_discarded_entirely(self):
        text = _stream(
            _hello(),
            {"type": "frame", "durations_ns": [500_000_000] * 5},
            {"type": "phase", "phase": "measurement"},
            {"type": "frame", "durations_ns": [16_000_000] * 20},
            {"type": "bye", "measurement_duration_s": 5.0},
        )
        stream = parse_probe_stream("test", text=text)
        assert len(stream.client.frametimes_ns) == 20

    def test_parses_a_server_run_with_chunk_counters(self):
        text = _stream(
            _hello(),
            {"type": "phase", "phase": "measurement"},
            {"type": "tick", "durations_ns": [25_000_000] * 500},
            {"type": "chunk", "generated": 800, "loaded": 200},
            {"type": "bye", "measurement_duration_s": 20.0, "saturated": True},
        )
        stream = parse_probe_stream("test", text=text)
        assert len(stream.server.tick_durations_ns) == 500
        assert stream.server.chunks_generated == 800
        assert stream.server.saturated

    def test_truncated_stream_is_readable_and_flagged_crashed(self):
        """A killed run leaves a valid prefix; that prefix is diagnostic."""
        text = _stream(
            _hello(),
            {"type": "phase", "phase": "measurement"},
            {"type": "frame", "durations_ns": [16_000_000] * 40},
        ) + '{"type": "frame", "durations_n'  # torn mid-write
        stream = parse_probe_stream("test", text=text)
        assert not stream.completed
        assert RunFlag.CRASHED in stream.flags
        assert len(stream.client.frametimes_ns) == 40

    def test_missing_hello_is_a_clear_error(self):
        text = _stream({"type": "frame", "durations_ns": [1]})
        with pytest.raises(ProbeError, match="never started"):
            parse_probe_stream("test", text=text)

    def test_refuses_an_incompatible_protocol_version(self):
        # Guessing at field meanings across a format change would silently
        # corrupt results, which is worse than failing.
        text = _stream({"type": "hello", "protocol": PROTOCOL_VERSION + 99})
        with pytest.raises(ProbeError, match="protocol"):
            parse_probe_stream("test", text=text)

    def test_rejects_an_unknown_phase(self):
        text = _stream(_hello(), {"type": "phase", "phase": "teatime"})
        with pytest.raises(ProbeError, match="unknown phase"):
            parse_probe_stream("test", text=text)

    def test_carries_probe_raised_flags(self):
        text = _stream(
            _hello(),
            {"type": "flag", "flag": "warmup_not_converged"},
            {"type": "phase", "phase": "measurement"},
            {"type": "frame", "durations_ns": [16_000_000] * 10},
            {"type": "bye", "measurement_duration_s": 1.0},
        )
        stream = parse_probe_stream("test", text=text)
        assert RunFlag.WARMUP_NOT_CONVERGED in stream.flags

    def test_unknown_flag_is_recorded_not_fatal(self):
        text = _stream(
            _hello(),
            {"type": "flag", "flag": "from_a_newer_probe"},
            {"type": "bye", "measurement_duration_s": 1.0},
        )
        stream = parse_probe_stream("test", text=text)
        assert any("from_a_newer_probe" in e for e in stream.errors)

    def test_records_the_world_fingerprint(self):
        text = _stream(
            _hello(),
            {"type": "fingerprint", "sha256": "abc123"},
            {"type": "bye", "measurement_duration_s": 1.0},
        )
        assert parse_probe_stream("test", text=text).world_fingerprint == "abc123"

    def test_blank_and_malformed_lines_are_skipped(self):
        text = _stream(_hello()) + "\n\nnot json at all\n" + _stream(
            {"type": "bye", "measurement_duration_s": 1.0}
        )
        stream = parse_probe_stream("test", text=text)
        assert stream.completed

    def test_missing_file_gives_a_clear_error(self):
        with pytest.raises(ProbeError, match="not found"):
            parse_probe_stream("/nonexistent/probe.jsonl")


class TestAgentStreamAdoption:
    """The JVM agent supplies frames where no adapter can.

    It writes a second stream rather than sharing the adapter's, so the two
    have to be combined somewhere. Every rule here exists because the obvious
    combination — concatenate them — is wrong.
    """

    def _agent(self, *, frames: list[int], warmup: list[int] | None = None,
               scenario_hash: str = "h1", flags: list[str] = ()) -> ProbeStream:
        events = [
            {"type": "hello", "protocol": PROTOCOL_VERSION, "probe_version": "0.1.0",
             "metadata": {"platform": "jvm-agent", "scenario_hash": scenario_hash}},
        ]
        if warmup:
            events += [{"type": "phase", "phase": "warmup"},
                       {"type": "frame", "durations_ns": warmup}]
        events += [{"type": "phase", "phase": "measurement"},
                   {"type": "frame", "durations_ns": frames}]
        events += [{"type": "flag", "flag": f} for f in flags]
        events.append({"type": "bye", "measurement_duration_s": 2.0})
        return parse_probe_stream("agent", text=_stream(*events))

    def _adapter(self, *, frames: list[int] = (), scenario_hash: str = "h1"
                 ) -> ProbeStream:
        events = [
            {"type": "hello", "protocol": PROTOCOL_VERSION, "probe_version": "0.1.0",
             "metadata": {"platform": "fabric", "scenario_hash": scenario_hash}},
            {"type": "phase", "phase": "measurement"},
        ]
        if frames:
            events.append({"type": "frame", "durations_ns": list(frames)})
        events.append({"type": "bye", "measurement_duration_s": 3.0})
        return parse_probe_stream("adapter", text=_stream(*events))

    def test_frames_are_adopted_when_the_adapter_had_none(self):
        adapter = self._adapter()
        result = adopt_agent_frames(adapter, self._agent(frames=[1_000, 2_000]))
        assert adapter.client.frametimes_ns == [1_000, 2_000]
        assert adapter.metadata["frame_source"] == "jvm-agent"
        assert "adopted 2 frames" in result

    def test_an_adapter_with_frames_wins_and_nothing_is_concatenated(self):
        # Both time the *same* frames. Concatenating would double the sample
        # count and shrink every confidence interval accordingly — precision
        # that was never measured.
        adapter = self._adapter(frames=[5_000, 6_000])
        adopt_agent_frames(adapter, self._agent(frames=[1_000, 2_000]))
        assert adapter.client.frametimes_ns == [5_000, 6_000]
        assert "frame_source" not in adapter.metadata

    def test_a_stream_from_another_scenario_is_refused(self):
        # A stale file contributing frames from a different variant would
        # destroy a comparison while looking perfectly healthy.
        adapter = self._adapter(scenario_hash="h1")
        result = adopt_agent_frames(adapter, self._agent(frames=[1], scenario_hash="h2"))
        assert adapter.client.frametimes_ns == []
        assert "different scenario" in result

    def test_warmup_frames_travel_so_convergence_stays_checkable(self):
        adapter = self._adapter()
        adopt_agent_frames(adapter, self._agent(frames=[1_000], warmup=[9_000, 8_000]))
        assert adapter.warmup_frames_ns == [9_000, 8_000]

    def test_qualifying_flags_travel_with_the_frames_they_qualify(self):
        adapter = self._adapter()
        adopt_agent_frames(
            adapter, self._agent(frames=[1_000], flags=["vsync_suspected"])
        )
        assert RunFlag.VSYNC_SUSPECTED in adapter.flags

    def test_a_truncated_agent_stream_does_not_mark_the_run_crashed(self):
        # The agent dying says nothing about the run the adapter completed.
        agent = parse_probe_stream("agent", text=_stream(
            {"type": "hello", "protocol": PROTOCOL_VERSION, "probe_version": "0.1.0"},
            {"type": "phase", "phase": "measurement"},
            {"type": "frame", "durations_ns": [1_000]},
        ))
        assert RunFlag.CRASHED in agent.flags
        adapter = self._adapter()
        adopt_agent_frames(adapter, agent)
        assert adapter.client.frametimes_ns == [1_000]
        assert RunFlag.CRASHED not in adapter.flags

    def test_an_agent_that_recorded_nothing_changes_nothing(self):
        adapter = self._adapter()
        result = adopt_agent_frames(adapter, self._agent(frames=[]))
        assert "frame_source" not in adapter.metadata
        assert "no measurement frames" in result


class TestHarnessPreflight:
    """The harness adds checks that depend on how it was constructed."""

    def _harness(self, tmp_path, headlessmc=None):
        from pathlib import Path

        from mcbench.config import parse_suite
        from mcbench.runner import Harness
        from mcbench.scenario import load_scenarios

        repo = Path(__file__).resolve().parents[1]
        scenarios = {s.id: s for s in load_scenarios(repo / "scenarios")}
        suite = parse_suite({
            "name": "t", "minecraft_version": "1.21.1", "loader": "fabric",
            "scenarios": ["entity-mobcap-saturation"],
            "variants": [{"name": "base", "mods": []}],
            "baseline": "base",
        })
        return Harness(suite, scenarios, work_dir=tmp_path,
                       headlessmc=headlessmc)

    def test_missing_headlessmc_blocks_before_any_run(self, tmp_path):
        """Without this the suite launches and fails identically N times.

        Dozens of duplicate errors bury the single real cause, so the check has
        to happen up front rather than per run.
        """
        harness = self._harness(tmp_path)
        harness.headlessmc = None
        result = harness.preflight(require_account=False)
        check = next(c for c in result.checks if c.name == "headlessmc")
        assert check.severity is Severity.BLOCK
        assert check.remedy
        assert not result.admissible

    def test_present_headlessmc_passes(self, tmp_path):
        stub = tmp_path / "headlessmc-launcher.jar"
        stub.write_bytes(b"")
        harness = self._harness(tmp_path, headlessmc=stub)
        result = harness.preflight(require_account=False)
        check = next(c for c in result.checks if c.name == "headlessmc")
        assert check.severity is Severity.OK

    def test_server_only_suite_does_not_demand_a_gpu(self, tmp_path):
        harness = self._harness(tmp_path)
        assert not harness.needs_gpu


class TestAgentAttachment:
    """--agent-jar puts the JVM agent on the launch command."""

    def _harness(self, tmp_path, agent_jar=None):
        from pathlib import Path

        from mcbench.config import parse_suite
        from mcbench.runner import Harness
        from mcbench.scenario import load_scenarios

        repo = Path(__file__).resolve().parents[1]
        scenarios = {s.id: s for s in load_scenarios(repo / "scenarios")}
        suite = parse_suite({
            "name": "t", "minecraft_version": "1.21.1", "loader": "neoforge",
            "scenarios": ["visual-biome-flyby"],
            "variants": [{"name": "base", "mods": []}],
            "baseline": "base",
        })
        stub = tmp_path / "headlessmc-launcher.jar"
        stub.write_bytes(b"")
        return Harness(suite, scenarios, work_dir=tmp_path,
                       headlessmc=stub, agent_jar=agent_jar), scenarios

    def test_attached_for_rendering_scenarios(self, tmp_path):
        jar = tmp_path / "agent.jar"
        jar.write_bytes(b"")
        harness, scenarios = self._harness(tmp_path, agent_jar=jar)
        command = harness._launch_command(tmp_path, scenarios["visual-biome-flyby"])
        jvm = command[command.index("--jvm") + 1]
        assert f"-javaagent:{jar.resolve()}" in jvm

    def test_not_attached_for_server_scenarios(self, tmp_path):
        # A server run has no frames to time, so the transformer would pay a
        # pass over every class load for nothing.
        jar = tmp_path / "agent.jar"
        jar.write_bytes(b"")
        harness, scenarios = self._harness(tmp_path, agent_jar=jar)
        command = harness._launch_command(
            tmp_path, scenarios["entity-mobcap-saturation"]
        )
        assert "-javaagent" not in " ".join(command)

    def test_absent_by_default(self, tmp_path):
        harness, scenarios = self._harness(tmp_path)
        command = harness._launch_command(tmp_path, scenarios["visual-biome-flyby"])
        assert "-javaagent" not in " ".join(command)

    def test_a_missing_jar_fails_loudly_rather_than_silently_unmeasured(self, tmp_path):
        # Silently dropping it would produce a run with no frames and no
        # explanation — the exact failure mode the agent exists to prevent.
        from mcbench.runner import HarnessError

        harness, scenarios = self._harness(tmp_path, agent_jar=tmp_path / "nope.jar")
        with pytest.raises(HarnessError, match="agent jar not found"):
            harness._launch_command(tmp_path, scenarios["visual-biome-flyby"])


class TestPluginPlatforms:
    """Paper and friends are server plugin platforms, not mod loaders."""

    def _harness(self, tmp_path, loader, scenarios_wanted):
        from pathlib import Path

        from mcbench.config import parse_suite
        from mcbench.runner import Harness
        from mcbench.scenario import load_scenarios

        repo = Path(__file__).resolve().parents[1]
        scenarios = {s.id: s for s in load_scenarios(repo / "scenarios")}
        suite = parse_suite({
            "name": "t", "minecraft_version": "1.21.1", "loader": loader,
            "scenarios": scenarios_wanted,
            "variants": [{"name": "base", "mods": []}],
            "baseline": "base",
        })
        return Harness(suite, scenarios, work_dir=tmp_path)

    def test_paper_never_requires_a_gpu(self, tmp_path):
        # Paper has no client, so demanding a GPU would block a valid server run.
        harness = self._harness(tmp_path, "paper", ["visual-biome-flyby"])
        assert not harness.needs_gpu

    def test_paper_reports_client_scenarios_as_unsupported(self, tmp_path):
        # Surfaced up front; a client scenario on a headless server would record
        # no frames, and an empty result is more confusing than a refusal.
        harness = self._harness(tmp_path, "paper", ["visual-biome-flyby"])
        assert harness.unsupported_scenarios() == ["visual-biome-flyby"]

    def test_paper_accepts_server_scenarios(self, tmp_path):
        harness = self._harness(tmp_path, "paper", ["entity-mobcap-saturation"])
        assert harness.unsupported_scenarios() == []

    def test_mod_loaders_have_no_platform_restriction(self, tmp_path):
        harness = self._harness(tmp_path, "fabric", ["visual-biome-flyby"])
        assert harness.unsupported_scenarios() == []
        assert harness.needs_gpu

    def test_plugin_platform_flag(self):
        from mcbench.config import Loader

        assert Loader.PAPER.is_plugin_platform
        assert Loader.SPIGOT.is_plugin_platform
        assert not Loader.FABRIC.is_plugin_platform
        assert not Loader.NEOFORGE.is_plugin_platform
