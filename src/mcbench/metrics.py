"""Metric definitions and per-run reduction.

Implements docs/METHODOLOGY.md sections 1 and 2: turning a raw sample stream
from one run into the summary metrics that the statistics layer then compares
across runs.

The central rule is that we aggregate in the time domain and convert to FPS only
for display. Averaging per-second FPS samples computes a harmonic mean of
frametimes, over-weighting fast frames and hiding exactly the stutter players
notice. Every FPS figure mcbench reports is derived from a frametime aggregate.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass, field
from enum import Enum

from .stats import (
    coefficient_of_variation,
    mean,
    mean_of_worst_fraction,
    percentile,
)

__all__ = [
    "COMMON_REFRESH_HZ",
    "vsync_suspected",
    "Direction",
    "MetricDef",
    "METRICS",
    "RunFlag",
    "ClientSamples",
    "ServerSamples",
    "RunMetrics",
    "reduce_client_run",
    "reduce_server_run",
    "find_steady_state",
]

NS_PER_MS = 1_000_000.0
TICK_BUDGET_MS = 50.0  # 20 TPS


class Direction(str, Enum):
    """Whether a larger value is better or worse for a metric."""

    LOWER_IS_BETTER = "lower_is_better"
    HIGHER_IS_BETTER = "higher_is_better"


@dataclass(frozen=True)
class MetricDef:
    """Declares a metric's meaning, units, and polarity.

    The polarity is data rather than something each call site decides, because
    getting it backwards silently inverts a verdict — a regression reported as
    an improvement is the worst failure mode this project has.
    """

    key: str
    label: str
    unit: str
    direction: Direction
    description: str

    @property
    def lower_is_better(self) -> bool:
        return self.direction is Direction.LOWER_IS_BETTER


def _m(key: str, label: str, unit: str, direction: Direction, description: str) -> MetricDef:
    return MetricDef(key, label, unit, direction, description)


LOWER = Direction.LOWER_IS_BETTER
HIGHER = Direction.HIGHER_IS_BETTER

#: The canonical metric registry. Report keys are stable identifiers; changing
#: the meaning of one is a breaking change to the published corpus.
METRICS: dict[str, MetricDef] = {
    d.key: d
    for d in [
        # --- client: speed ---
        _m("frametime_mean_ms", "Mean frametime", "ms", LOWER,
           "Arithmetic mean of retained frame durations. The primary client metric."),
        _m("fps_avg", "Average FPS", "fps", HIGHER,
           "1000 / frametime_mean_ms. Derived for display only."),
        _m("frametime_p50_ms", "Median frametime", "ms", LOWER,
           "50th percentile of the frametime distribution."),
        _m("frametime_p95_ms", "p95 frametime", "ms", LOWER,
           "95th percentile. Where occasional hitching shows up."),
        _m("frametime_p99_ms", "p99 frametime", "ms", LOWER,
           "99th percentile. Dominated by GC pauses and chunk work."),
        # --- client: smoothness ---
        _m("fps_1pct_low", "1% low FPS", "fps", HIGHER,
           "1000 / mean of the worst 1% of frametimes. Not the 99th percentile."),
        _m("fps_0p1pct_low", "0.1% low FPS", "fps", HIGHER,
           "1000 / mean of the worst 0.1% of frametimes."),
        _m("stutter_rate", "Stutter rate", "per 1k frames", LOWER,
           "Frames exceeding twice the running median, per 1000 frames."),
        _m("frametime_cv", "Frametime CV", "ratio", LOWER,
           "Coefficient of variation. Smoothness, measured independently of speed."),
        # --- server ---
        _m("mspt_mean", "Mean MSPT", "ms", LOWER,
           "Mean milliseconds per tick. The primary server metric."),
        _m("mspt_p95", "p95 MSPT", "ms", LOWER, "95th percentile tick cost."),
        _m("mspt_p99", "p99 MSPT", "ms", LOWER,
           "99th percentile tick cost. Where tick spikes live."),
        _m("tick_headroom", "Tick headroom", "fraction", HIGHER,
           "1 - mspt_mean/50. Fraction of the tick budget left unused."),
        _m("warp_throughput", "Warp throughput", "ticks/s", HIGHER,
           "Ticks per wall-clock second under tick warp, with the 20 TPS clamp removed."),
        _m("tps_effective", "Effective TPS", "tps", HIGHER,
           "Only meaningful for saturated scenarios; uninformative below budget."),
        # --- memory and GC ---
        _m("alloc_rate_mb_s", "Allocation rate", "MB/s", LOWER,
           "Heap allocation per second. Independent of measured pause time."),
        _m("gc_pause_total_ms", "Total GC pause", "ms", LOWER,
           "Sum of stop-the-world pauses during the measurement window."),
        _m("gc_pause_p99_ms", "p99 GC pause", "ms", LOWER, "Worst-case pause length."),
        _m("heap_steady_mb", "Steady-state heap", "MB", LOWER,
           "Post-collection live-set size."),
        _m("heap_peak_mb", "Peak heap", "MB", LOWER, "Maximum observed heap usage."),
        # --- world ---
        _m("chunkgen_rate", "Chunk generation rate", "chunks/s", HIGHER,
           "Fresh chunk generation throughput."),
        _m("chunkload_rate", "Chunk load rate", "chunks/s", HIGHER,
           "Throughput loading already-generated chunks from disk."),
    ]
}


class RunFlag(str, Enum):
    """Conditions that qualify a run's admissibility.

    Flags travel with the run into every result derived from it. A run is never
    silently dropped or silently trusted.
    """

    WARMUP_NOT_CONVERGED = "warmup_not_converged"
    VSYNC_SUSPECTED = "vsync_suspected"
    """Frametimes pinned to a refresh interval: the display was measured."""
    TOO_FEW_SAMPLES = "too_few_samples"
    ENVIRONMENT_NOISY = "environment_noisy"
    WORLD_FINGERPRINT_MISMATCH = "world_fingerprint_mismatch"
    CRASHED = "crashed"


@dataclass
class ClientSamples:
    """Raw client-side sample stream from one run.

    Frametimes are nanoseconds: at 60 fps a millisecond-resolution timer has
    roughly 6% quantisation error, which is larger than most effects we need to
    resolve.
    """

    frametimes_ns: list[int] = field(default_factory=list)
    gc_pauses_ms: list[float] = field(default_factory=list)
    alloc_bytes: int = 0
    duration_s: float = 0.0
    heap_samples_mb: list[float] = field(default_factory=list)
    heap_post_gc_mb: list[float] = field(default_factory=list)


@dataclass
class ServerSamples:
    """Raw server-side sample stream from one run."""

    tick_durations_ns: list[int] = field(default_factory=list)
    wall_clock_s: float = 0.0
    gc_pauses_ms: list[float] = field(default_factory=list)
    alloc_bytes: int = 0
    heap_samples_mb: list[float] = field(default_factory=list)
    heap_post_gc_mb: list[float] = field(default_factory=list)
    chunks_generated: int = 0
    chunks_loaded: int = 0
    saturated: bool = False
    """True when the scenario intends to push the server past the tick budget.
    Only then is `tps_effective` reported."""


@dataclass
class RunMetrics:
    """Reduced metrics for a single run, with the flags it carries."""

    values: dict[str, float]
    flags: list[RunFlag] = field(default_factory=list)
    sample_count: int = 0

    @property
    def admissible(self) -> bool:
        """Whether this run may contribute to a published comparison."""
        return not any(
            f in (RunFlag.CRASHED, RunFlag.TOO_FEW_SAMPLES,
                  RunFlag.WORLD_FINGERPRINT_MISMATCH)
            for f in self.flags
        )


def _stutter_rate(frametimes_ms: Sequence[float], *, window: int = 120) -> float:
    """Frames exceeding twice the running median, per 1000 frames.

    A *running* median rather than a global one, because a scenario legitimately
    changes intensity as it progresses. Against a global median, every frame in
    a genuinely heavy section would count as a stutter.
    """
    if len(frametimes_ms) < window:
        return 0.0

    stutters = 0
    # Recomputing an exact median per frame is O(n*w log w) and needlessly slow
    # for multi-hundred-thousand-frame runs; a periodic refresh tracks the
    # scenario's intensity closely enough for a threshold test.
    refresh = max(1, window // 4)
    running_median = 0.0
    for i in range(window, len(frametimes_ms)):
        if (i - window) % refresh == 0:
            running_median = percentile(frametimes_ms[i - window : i], 50.0)
        if running_median > 0 and frametimes_ms[i] > 2.0 * running_median:
            stutters += 1

    counted = len(frametimes_ms) - window
    return (stutters / counted) * 1000.0 if counted else 0.0


def _memory_metrics(
    gc_pauses_ms: Sequence[float],
    alloc_bytes: int,
    duration_s: float,
    heap_samples_mb: Sequence[float],
    heap_post_gc_mb: Sequence[float],
) -> dict[str, float]:
    out: dict[str, float] = {}
    if gc_pauses_ms:
        out["gc_pause_total_ms"] = float(sum(gc_pauses_ms))
        out["gc_pause_p99_ms"] = percentile(gc_pauses_ms, 99.0)
    if duration_s > 0 and alloc_bytes > 0:
        out["alloc_rate_mb_s"] = (alloc_bytes / (1024 * 1024)) / duration_s
    if heap_samples_mb:
        out["heap_peak_mb"] = max(heap_samples_mb)
    if heap_post_gc_mb:
        # Live set, not peak: post-collection occupancy is what actually
        # constrains an operator running a smaller heap.
        out["heap_steady_mb"] = mean(heap_post_gc_mb)
    return out


def reduce_client_run(
    samples: ClientSamples, *, min_frames: int = 1000
) -> RunMetrics:
    """Reduce one client run's sample stream to summary metrics."""
    flags: list[RunFlag] = []
    frames_ms = [ns / NS_PER_MS for ns in samples.frametimes_ns]

    if len(frames_ms) < min_frames:
        flags.append(RunFlag.TOO_FEW_SAMPLES)
    if not frames_ms:
        return RunMetrics(values={}, flags=flags, sample_count=0)

    frametime_mean = mean(frames_ms)
    values: dict[str, float] = {
        "frametime_mean_ms": frametime_mean,
        "fps_avg": 1000.0 / frametime_mean if frametime_mean > 0 else 0.0,
        "frametime_p50_ms": percentile(frames_ms, 50.0),
        "frametime_p95_ms": percentile(frames_ms, 95.0),
        "frametime_p99_ms": percentile(frames_ms, 99.0),
        "stutter_rate": _stutter_rate(frames_ms),
    }

    worst_1 = mean_of_worst_fraction(frames_ms, 0.01)
    values["fps_1pct_low"] = 1000.0 / worst_1 if worst_1 > 0 else 0.0
    worst_01 = mean_of_worst_fraction(frames_ms, 0.001)
    values["fps_0p1pct_low"] = 1000.0 / worst_01 if worst_01 > 0 else 0.0

    if len(frames_ms) >= 2:
        values["frametime_cv"] = coefficient_of_variation(frames_ms)

    if (hz := vsync_suspected(frames_ms)) is not None:
        # Not dropped, but flagged: every variant would score the refresh rate
        # and the comparison would confidently report equivalence.
        flags.append(RunFlag.VSYNC_SUSPECTED)
        values["suspected_refresh_hz"] = hz

    values.update(
        _memory_metrics(
            samples.gc_pauses_ms,
            samples.alloc_bytes,
            samples.duration_s,
            samples.heap_samples_mb,
            samples.heap_post_gc_mb,
        )
    )

    return RunMetrics(values=values, flags=flags, sample_count=len(frames_ms))


def reduce_server_run(
    samples: ServerSamples, *, min_ticks: int = 500
) -> RunMetrics:
    """Reduce one server run's sample stream to summary metrics."""
    flags: list[RunFlag] = []
    ticks_ms = [ns / NS_PER_MS for ns in samples.tick_durations_ns]

    if len(ticks_ms) < min_ticks:
        flags.append(RunFlag.TOO_FEW_SAMPLES)
    if not ticks_ms:
        return RunMetrics(values={}, flags=flags, sample_count=0)

    mspt_mean = mean(ticks_ms)
    values: dict[str, float] = {
        "mspt_mean": mspt_mean,
        "mspt_p95": percentile(ticks_ms, 95.0),
        "mspt_p99": percentile(ticks_ms, 99.0),
        # Headroom is the metric TPS should have been. Below budget every
        # configuration reports 20 TPS, so TPS cannot tell 5 ms/tick from
        # 30 ms/tick; headroom separates them cleanly. Clamped at zero so a
        # saturated server reads "no headroom" rather than a negative figure.
        "tick_headroom": max(0.0, 1.0 - mspt_mean / TICK_BUDGET_MS),
    }

    if samples.wall_clock_s > 0:
        values["warp_throughput"] = len(ticks_ms) / samples.wall_clock_s
        if samples.saturated:
            # Only meaningful when the scenario deliberately exceeds budget.
            # Reporting it otherwise publishes a constant 20 and invites the
            # "both servers are fine" misreading this project exists to end.
            values["tps_effective"] = min(
                20.0, len(ticks_ms) / samples.wall_clock_s
            )
        if samples.chunks_generated:
            values["chunkgen_rate"] = samples.chunks_generated / samples.wall_clock_s
        if samples.chunks_loaded:
            values["chunkload_rate"] = samples.chunks_loaded / samples.wall_clock_s

    values.update(
        _memory_metrics(
            samples.gc_pauses_ms,
            samples.alloc_bytes,
            samples.wall_clock_s,
            samples.heap_samples_mb,
            samples.heap_post_gc_mb,
        )
    )

    return RunMetrics(values=values, flags=flags, sample_count=len(ticks_ms))


#: Refresh rates a display is plausibly locked to.
COMMON_REFRESH_HZ = (60.0, 75.0, 90.0, 120.0, 144.0, 165.0, 240.0)


def vsync_suspected(
    frametimes_ms: Sequence[float], *, tolerance: float = 0.06, share: float = 0.80
) -> float | None:
    """Detect frametimes pinned to a refresh interval.

    The check that actually matters for vsync, because environment inspection
    cannot see a compositor-level override. A vsync-locked client quantises
    frametimes to the refresh interval, so most frames land within a percent or
    two of 16.67 ms, 8.33 ms and so on.

    That is fatal to a rendering comparison and invisible in the summary: every
    variant reports the same average FPS, the intervals are tight, and the
    benchmark confidently concludes the mods are equivalent — when what it
    measured was the monitor.

    Returns the suspected refresh rate, or None. Deliberately conservative: a
    genuinely CPU-bound scene can sit near a round frametime by coincidence, and
    a false alarm that discards a good run is its own kind of damage.
    """
    if len(frametimes_ms) < 200:
        return None

    for hz in COMMON_REFRESH_HZ:
        interval = 1000.0 / hz
        near = sum(
            1 for value in frametimes_ms
            if abs(value - interval) <= interval * tolerance
        )
        if near / len(frametimes_ms) >= share:
            return hz
    return None


def find_steady_state(
    samples: Sequence[float],
    *,
    window: int,
    tolerance: float = 0.05,
    min_index: int = 0,
) -> int | None:
    """Index at which the rolling median settles, or None if it never does.

    Implements the frametime half of the warmup rule in METHODOLOGY.md section
    2. Steady state is declared at the first index where the rolling median over
    the preceding window is within ``tolerance`` of the median over the window
    before that — the series has stopped trending.

    Returning None is meaningful: the caller must flag the run
    ``warmup_not_converged`` rather than accept it silently, because an
    unconverged run charges the mod for the JIT's warmup cost.
    """
    if window < 2:
        raise ValueError(f"window must be at least 2, got {window}")
    if len(samples) < 2 * window:
        return None

    start = max(min_index, 2 * window)
    for i in range(start, len(samples) + 1):
        previous = percentile(samples[i - 2 * window : i - window], 50.0)
        current = percentile(samples[i - window : i], 50.0)
        if previous <= 0:
            continue
        if abs(current - previous) / previous <= tolerance:
            return i
    return None
