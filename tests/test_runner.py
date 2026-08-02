"""Tests for preflight and the probe protocol."""

from __future__ import annotations

import json

import pytest

from mcbench.metrics import RunFlag
from mcbench.runner import (
    PROTOCOL_VERSION,
    ProbeError,
    Severity,
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
        for key in ("os", "arch", "cpu_count", "gpu_nodes"):
            assert key in host


class TestProbeProtocol:
    def test_parses_a_complete_client_run(self):
        text = _stream(
            _hello(),
            {"type": "phase", "phase": "warmup"},
            {"type": "frame", "durations_ns": [20_000_000] * 10},
            {"type": "phase", "phase": "measurement"},
            {"type": "frame", "durations_ns": [16_000_000] * 100},
            {"type": "gc", "pauses_ms": [1.5, 2.0]},
            {"type": "memory", "heap_mb": 800.0, "post_gc": True,
             "allocated_bytes": 1024 * 1024 * 100},
            {"type": "bye", "measurement_duration_s": 60.0},
        )
        stream = parse_probe_stream("test", text=text)
        assert stream.completed
        assert len(stream.client.frametimes_ns) == 100
        assert stream.client.gc_pauses_ms == [1.5, 2.0]
        assert stream.client.duration_s == 60.0

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


class TestHarnessPreflight:
    """The harness adds checks that depend on how it was constructed."""

    def _harness(self, tmp_path, headlessmc=None):
        from mcbench.config import parse_suite
        from mcbench.runner import Harness
        from mcbench.scenario import load_scenarios
        from pathlib import Path

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


class TestPluginPlatforms:
    """Paper and friends are server plugin platforms, not mod loaders."""

    def _harness(self, tmp_path, loader, scenarios_wanted):
        from mcbench.config import parse_suite
        from mcbench.runner import Harness
        from mcbench.scenario import load_scenarios
        from pathlib import Path

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
