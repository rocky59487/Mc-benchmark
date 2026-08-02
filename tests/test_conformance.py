"""Cross-implementation conformance for the probe wire protocol.

The protocol has two independent implementations — the Java writer in
``probe/probe-core`` and the Python reader in ``src/mcbench/runner/protocol.py``
— and a wire format with two implementations drifts unless something checks.

The fixtures here were produced by the real Java ``ProbeSession`` via
``dev.mcbench.probe.core.SelfTest``, not hand-written. Regenerate them with::

    cd probe && gradle jar
    java -cp probe-core/build/libs/probe-core-0.1.0.jar \\
        dev.mcbench.probe.core.SelfTest <dir> client 42

If a change to the Java side breaks these, the two implementations have
diverged, which is precisely the failure this file exists to catch.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from mcbench.metrics import find_steady_state, reduce_client_run, reduce_server_run
from mcbench.runner import PROTOCOL_VERSION, parse_probe_stream

FIXTURES = Path(__file__).parent / "fixtures"
CLIENT = FIXTURES / "probe-client-reference.jsonl"
SERVER = FIXTURES / "probe-server-reference.jsonl"


@pytest.fixture(scope="module")
def client_stream():
    return parse_probe_stream(CLIENT)


@pytest.fixture(scope="module")
def server_stream():
    return parse_probe_stream(SERVER)


class TestWireFormat:
    def test_fixtures_are_real_java_output(self):
        """Guards against someone replacing these with hand-written JSON.

        A hand-written fixture would still pass every other test here while
        testing nothing at all about the Java implementation.
        """
        first = json.loads(CLIENT.read_text().splitlines()[0])
        assert first["type"] == "hello"
        assert first["metadata"]["source"] == "probe-core SelfTest"
        assert first["metadata"]["synthetic"] == "true"

    def test_protocol_versions_agree(self, client_stream):
        assert client_stream.protocol_version == PROTOCOL_VERSION

    def test_every_line_is_a_json_object(self):
        for path in (CLIENT, SERVER):
            for number, line in enumerate(path.read_text().splitlines(), start=1):
                if line.strip():
                    assert isinstance(json.loads(line), dict), f"{path}:{number}"

    def test_streams_are_complete(self, client_stream, server_stream):
        assert client_stream.completed
        assert server_stream.completed
        assert not client_stream.flags
        assert not server_stream.flags


class TestClientStream:
    def test_measurement_samples_are_parsed(self, client_stream):
        assert len(client_stream.client.frametimes_ns) > 400

    def test_warmup_is_separated_from_measurement(self, client_stream):
        assert client_stream.warmup_frames_ns
        assert not set(client_stream.warmup_frames_ns) & set(
            client_stream.client.frametimes_ns[:50]
        ), "warmup samples must not leak into the measurement window"

    def test_warmup_series_is_auditable(self, client_stream):
        """The point of emitting warmup: the harness can check the probe's claim.

        Java's PhaseController decided warmup had converged. Python re-derives
        that independently from the raw series. Two implementations agreeing is
        far stronger evidence than either asserting it alone.
        """
        warmup_ms = [v / 1e6 for v in client_stream.warmup_frames_ns]
        settled_at = find_steady_state(warmup_ms, window=60)
        assert settled_at is not None, "Java reported convergence; Python disagrees"
        assert settled_at <= len(warmup_ms), (
            "Python must settle no later than the point Java ended warmup, since "
            "Java additionally requires the minimum duration to elapse"
        )

    def test_reduces_to_sane_metrics(self, client_stream):
        metrics = reduce_client_run(client_stream.client, min_frames=100)
        # SelfTest simulates ~16.7 ms frames, i.e. about 60 fps.
        assert 15.0 < metrics.values["frametime_mean_ms"] < 19.0
        assert 52.0 < metrics.values["fps_avg"] < 67.0
        assert metrics.admissible

    def test_tail_metrics_reflect_the_injected_spikes(self, client_stream):
        metrics = reduce_client_run(client_stream.client, min_frames=100)
        assert metrics.values["fps_1pct_low"] < metrics.values["fps_avg"]
        assert metrics.values["frametime_p99_ms"] >= metrics.values["frametime_p50_ms"]

    def test_heap_is_reported(self, client_stream):
        """Regression guard for the MemoryMXBean-reports-zero bug.

        A JVM that had not yet collected reported 0 heap used, which silently
        zeroed every memory metric for the whole run.
        """
        metrics = reduce_client_run(client_stream.client, min_frames=100)
        assert metrics.values.get("heap_peak_mb", 0) > 0


class TestServerStream:
    def test_measurement_ticks_are_parsed(self, server_stream):
        assert len(server_stream.server.tick_durations_ns) > 1000

    def test_warmup_ticks_are_separated(self, server_stream):
        assert server_stream.warmup_ticks_ns

    def test_counters_and_fingerprint_survive(self, server_stream):
        assert server_stream.server.chunks_generated == 1280
        assert server_stream.world_fingerprint == "selftest-synthetic-world"

    def test_reduces_to_sane_metrics(self, server_stream):
        metrics = reduce_server_run(server_stream.server, min_ticks=100)
        # SelfTest simulates ~12 ms ticks against the 50 ms budget.
        assert 11.0 < metrics.values["mspt_mean"] < 13.5
        assert 0.70 < metrics.values["tick_headroom"] < 0.80

    def test_tps_withheld_for_an_unsaturated_scenario(self, server_stream):
        metrics = reduce_server_run(server_stream.server, min_ticks=100)
        assert "tps_effective" not in metrics.values


def test_end_to_end_from_java_output_to_a_verdict():
    """The whole chain: Java stream to a statistical verdict.

    Compares the client fixture against itself with an injected uniform shift,
    which must be detected as a regression. This exercises parse, reduce,
    aggregate, bootstrap, and ROPE in one path — the same code that will run on
    real measurements.
    """
    from mcbench.planner import Cell
    from mcbench.report import aggregate_cell, compare_to_baseline
    from mcbench.metrics import RunMetrics
    from mcbench.stats import Verdict

    stream = parse_probe_stream(CLIENT)
    base = reduce_client_run(stream.client, min_frames=100)

    baseline_runs = [
        RunMetrics(values={k: v * (1 + 0.002 * i) for k, v in base.values.items()})
        for i in range(7)
    ]
    # A uniform 20% slower variant: unambiguously outside any sane ROPE.
    slower_runs = [
        RunMetrics(values={k: v * 1.20 * (1 + 0.002 * i) for k, v in base.values.items()})
        for i in range(7)
    ]

    baseline = aggregate_cell(Cell("selftest", "baseline"), baseline_runs)
    variant = aggregate_cell(Cell("selftest", "slower"), slower_runs)
    comparisons = compare_to_baseline(baseline, variant, metrics=["frametime_mean_ms"])

    assert len(comparisons) == 1
    result = comparisons[0].comparison
    assert result.verdict is Verdict.REGRESSION
    assert result.relative_delta.value == pytest.approx(0.20, abs=0.01)
