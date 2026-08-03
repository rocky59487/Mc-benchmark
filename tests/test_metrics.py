"""Tests for per-run metric reduction."""

from __future__ import annotations

import pytest

from mcbench.metrics import (
    METRICS,
    ClientSamples,
    Direction,
    RunFlag,
    ServerSamples,
    find_steady_state,
    reduce_client_run,
    reduce_server_run,
)

NS = 1_000_000  # nanoseconds per millisecond


def frametimes(ms_values):
    return [int(v * NS) for v in ms_values]


class TestMetricRegistry:
    def test_every_metric_declares_a_direction(self):
        for key, definition in METRICS.items():
            assert isinstance(definition.direction, Direction), key
            assert definition.unit, key
            assert definition.description, key

    def test_keys_are_self_consistent(self):
        for key, definition in METRICS.items():
            assert definition.key == key

    def test_cost_and_throughput_metrics_have_opposite_polarity(self):
        # A polarity error silently inverts a verdict, so pin the important ones.
        assert METRICS["frametime_mean_ms"].lower_is_better
        assert METRICS["mspt_mean"].lower_is_better
        assert METRICS["alloc_rate_mb_s"].lower_is_better
        assert not METRICS["fps_avg"].lower_is_better
        assert not METRICS["fps_1pct_low"].lower_is_better
        assert not METRICS["tick_headroom"].lower_is_better
        assert not METRICS["chunkgen_rate"].lower_is_better


class TestClientReduction:
    def test_derives_fps_from_the_frametime_mean(self):
        samples = ClientSamples(frametimes_ns=frametimes([10.0] * 2000))
        result = reduce_client_run(samples)
        assert result.values["frametime_mean_ms"] == pytest.approx(10.0)
        assert result.values["fps_avg"] == pytest.approx(100.0)

    def test_fps_avg_is_not_an_average_of_per_frame_fps(self):
        # Half at 10 ms, half at 50 ms. Correct answer 1000/30 = 33.3; averaging
        # per-frame FPS would give 60.
        samples = ClientSamples(frametimes_ns=frametimes([10.0] * 1000 + [50.0] * 1000))
        result = reduce_client_run(samples)
        assert result.values["fps_avg"] == pytest.approx(33.333, abs=0.01)

    def test_one_percent_low_reflects_the_worst_frames(self):
        # 1980 good frames plus 20 terrible ones: the worst 1% is exactly the
        # 20 bad frames at 100 ms, so the 1% low is 10 fps.
        samples = ClientSamples(
            frametimes_ns=frametimes([10.0] * 1980 + [100.0] * 20)
        )
        result = reduce_client_run(samples)
        assert result.values["fps_1pct_low"] == pytest.approx(10.0)
        assert result.values["fps_avg"] > result.values["fps_1pct_low"]

    def test_flags_a_run_with_too_few_frames(self):
        result = reduce_client_run(ClientSamples(frametimes_ns=frametimes([10.0] * 50)))
        assert RunFlag.TOO_FEW_SAMPLES in result.flags
        assert not result.admissible

    def test_empty_run_yields_no_metrics(self):
        result = reduce_client_run(ClientSamples())
        assert result.values == {}
        assert result.sample_count == 0

    def test_smooth_and_erratic_runs_differ_in_cv_at_equal_mean(self):
        """The property that makes CV worth reporting.

        Both runs average 20 ms, so mean frametime and average FPS are
        identical. Only the variance metric distinguishes them — and the erratic
        one is the one that feels bad to play.
        """
        smooth = reduce_client_run(
            ClientSamples(frametimes_ns=frametimes([20.0] * 2000))
        )
        erratic = reduce_client_run(
            ClientSamples(frametimes_ns=frametimes([10.0, 30.0] * 1000))
        )
        assert smooth.values["frametime_mean_ms"] == pytest.approx(
            erratic.values["frametime_mean_ms"]
        )
        assert smooth.values["frametime_cv"] < erratic.values["frametime_cv"]

    def test_reports_memory_metrics_when_supplied(self):
        samples = ClientSamples(
            frametimes_ns=frametimes([10.0] * 2000),
            gc_pauses_ms=[1.0, 2.0, 30.0],
            alloc_bytes=1024 * 1024 * 500,
            real_allocation=True,
            duration_s=10.0,
            heap_samples_mb=[512.0, 900.0, 700.0],
            heap_post_gc_mb=[400.0, 420.0],
        )
        result = reduce_client_run(samples)
        assert result.values["gc_pause_total_ms"] == pytest.approx(33.0)
        assert result.values["gc_pause_max_ms"] == pytest.approx(30.0)
        assert result.values["gc_count"] == 3
        assert result.values["alloc_rate_mb_s"] == pytest.approx(50.0)
        assert result.values["heap_peak_mb"] == 900.0
        assert result.values["heap_steady_mb"] == pytest.approx(410.0)

    def test_heap_growth_is_not_published_as_allocation(self):
        """The two are different numbers and only one is an allocation rate.

        Without an allocation counter, everything allocated and collected
        between two samples is invisible — which on a busy tick is most of it.
        Publishing that figure as `alloc_rate_mb_s` made a mod that allocates
        heavily and collects promptly measure as allocating nothing.
        """
        samples = ClientSamples(
            frametimes_ns=frametimes([10.0] * 2000),
            heap_growth_bytes=1024 * 1024 * 500,
            real_allocation=False,
            duration_s=10.0,
        )
        result = reduce_client_run(samples)
        assert "alloc_rate_mb_s" not in result.values
        assert result.values["heap_growth_rate_mb_s"] == pytest.approx(50.0)

    def test_an_aggregate_pause_total_yields_no_percentile(self):
        """A percentile over per-interval sums describes the sampling cadence."""
        samples = ClientSamples(
            frametimes_ns=frametimes([10.0] * 2000),
            gc_total_pause_ms=33.0,
            duration_s=10.0,
        )
        result = reduce_client_run(samples)
        assert result.values["gc_pause_total_ms"] == pytest.approx(33.0)
        assert "gc_pause_p99_ms" not in result.values

    def test_stutter_rate_is_zero_for_a_perfectly_even_run(self):
        result = reduce_client_run(
            ClientSamples(frametimes_ns=frametimes([16.6] * 3000))
        )
        assert result.values["stutter_rate"] == 0.0

    def test_stutter_rate_detects_periodic_spikes(self):
        pattern = ([16.6] * 99 + [100.0]) * 30
        result = reduce_client_run(ClientSamples(frametimes_ns=frametimes(pattern)))
        assert result.values["stutter_rate"] > 0.0


class TestServerReduction:
    def test_computes_headroom_from_the_tick_budget(self):
        # 25 ms per tick is half of the 50 ms budget.
        samples = ServerSamples(
            tick_durations_ns=[int(25.0 * NS)] * 1000, wall_clock_s=25.0
        )
        result = reduce_server_run(samples)
        assert result.values["mspt_mean"] == pytest.approx(25.0)
        assert result.values["tick_headroom"] == pytest.approx(0.5)

    def test_headroom_distinguishes_servers_that_tps_cannot(self):
        """Why headroom replaces TPS as the primary server metric.

        Both of these servers sit under the 50 ms budget and would report a flat
        20 TPS, indistinguishable. Headroom separates them cleanly.
        """
        light = reduce_server_run(
            ServerSamples(tick_durations_ns=[int(5.0 * NS)] * 1000, wall_clock_s=5.0)
        )
        heavy = reduce_server_run(
            ServerSamples(tick_durations_ns=[int(30.0 * NS)] * 1000, wall_clock_s=30.0)
        )
        assert light.values["tick_headroom"] == pytest.approx(0.90)
        assert heavy.values["tick_headroom"] == pytest.approx(0.40)

    def test_headroom_floors_at_zero_when_saturated(self):
        samples = ServerSamples(
            tick_durations_ns=[int(120.0 * NS)] * 1000, wall_clock_s=120.0
        )
        result = reduce_server_run(samples)
        assert result.values["tick_headroom"] == 0.0

    def test_tps_is_withheld_unless_the_scenario_is_saturated(self):
        unsaturated = reduce_server_run(
            ServerSamples(tick_durations_ns=[int(5.0 * NS)] * 1000, wall_clock_s=5.0)
        )
        assert "tps_effective" not in unsaturated.values

        saturated = reduce_server_run(
            ServerSamples(
                tick_durations_ns=[int(80.0 * NS)] * 1000,
                wall_clock_s=80.0,
                saturated=True,
            )
        )
        assert saturated.values["tps_effective"] == pytest.approx(12.5)

    def test_warp_throughput_reflects_ticks_per_wall_second(self):
        samples = ServerSamples(
            tick_durations_ns=[int(2.0 * NS)] * 5000, wall_clock_s=10.0
        )
        result = reduce_server_run(samples)
        assert result.values["warp_throughput"] == pytest.approx(500.0)

    def test_reports_chunk_rates_when_supplied(self):
        samples = ServerSamples(
            tick_durations_ns=[int(10.0 * NS)] * 1000,
            wall_clock_s=20.0,
            chunks_generated=800,
            chunks_loaded=400,
        )
        result = reduce_server_run(samples)
        assert result.values["chunkgen_rate"] == pytest.approx(40.0)
        assert result.values["chunkload_rate"] == pytest.approx(20.0)

    def test_flags_a_run_with_too_few_ticks(self):
        result = reduce_server_run(
            ServerSamples(tick_durations_ns=[int(10.0 * NS)] * 10, wall_clock_s=1.0)
        )
        assert RunFlag.TOO_FEW_SAMPLES in result.flags


class TestSteadyState:
    def test_detects_settling_after_a_warmup_ramp(self):
        # Fast decay from 100 ms to 10 ms, then flat.
        ramp = [100.0 - i * 3.0 for i in range(30)]
        flat = [10.0] * 200
        index = find_steady_state(ramp + flat, window=20)
        assert index is not None
        assert index >= 40

    def test_returns_none_for_a_series_that_never_settles(self):
        # Continuously rising: the caller must flag warmup_not_converged rather
        # than charging the mod for the JIT's warmup cost.
        rising = [10.0 * (1.5 ** (i / 10)) for i in range(200)]
        assert find_steady_state(rising, window=20, tolerance=0.01) is None

    def test_returns_none_when_there_is_not_enough_data(self):
        assert find_steady_state([10.0] * 10, window=20) is None

    def test_flat_series_settles_immediately_after_two_windows(self):
        assert find_steady_state([10.0] * 100, window=10) == 20

    def test_rejects_a_degenerate_window(self):
        with pytest.raises(ValueError, match="window must be at least 2"):
            find_steady_state([1.0] * 10, window=1)
