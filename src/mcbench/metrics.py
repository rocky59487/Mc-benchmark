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

import bisect
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
    "frame_cap_suspected",
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
    getting it backwards silently inverts a verdict, reporting a regression as
    an improvement.
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
        # --- server, when the platform can only report tick period ---
        #
        # A different measurement, separately named. The period includes what
        # the server loop waits out, so on an unsaturated server it is the 50 ms
        # budget however cheap the tick was.
        _m("tick_period_mean_ms", "Mean tick period", "ms", LOWER,
           "Mean interval between ticks. NOT MSPT: on an unsaturated server it "
           "measures the tick budget, not the work inside the tick."),
        _m("tick_period_p95_ms", "p95 tick period", "ms", LOWER,
           "95th percentile interval between ticks."),
        _m("tick_period_p99_ms", "p99 tick period", "ms", LOWER,
           "99th percentile interval between ticks. Only informative once the "
           "server is over budget, where the period does track tick cost."),
        _m("warp_throughput", "Warp throughput", "ticks/s", HIGHER,
           "Ticks per wall-clock second under tick warp, with the 20 TPS clamp removed."),
        _m("tps_effective", "Effective TPS", "tps", HIGHER,
           "Only meaningful for saturated scenarios; uninformative below budget."),
        # --- memory and GC ---
        _m("alloc_rate_mb_s", "Allocation rate", "MB/s", LOWER,
           "Bytes allocated per second, from the JVM's allocation counter. "
           "Reported only where that counter exists; heap growth is published "
           "under its own name instead."),
        _m("heap_growth_rate_mb_s", "Heap growth rate", "MB/s", LOWER,
           "Net heap growth per second. A floor on allocation, not a measure of "
           "it: objects allocated and collected between two samples never "
           "appear, which is most of what a busy tick allocates."),
        _m("gc_pause_total_ms", "Total GC pause", "ms", LOWER,
           "Sum of stop-the-world pauses during the measurement window."),
        _m("gc_pause_p99_ms", "p99 GC pause", "ms", LOWER,
           "99th percentile of individual pause durations. Reported only when "
           "the JVM supplied per-collection events; a percentile over "
           "per-interval totals would describe the sampling cadence."),
        _m("gc_pause_max_ms", "Longest GC pause", "ms", LOWER,
           "The single worst stop-the-world pause in the window."),
        _m("gc_count", "Collections", "count", LOWER,
           "Stop-the-world collections during the measurement window."),
        _m("heap_steady_mb", "Steady-state heap", "MB", LOWER,
           "Live set, measured immediately after a collection."),
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
    FRAME_CAP_SUSPECTED = "frame_cap_suspected"
    """Frametimes pinned to the configured cap: the limiter was measured."""
    TOO_FEW_SAMPLES = "too_few_samples"
    ENVIRONMENT_NOISY = "environment_noisy"
    WORLD_FINGERPRINT_MISMATCH = "world_fingerprint_mismatch"
    CRASHED = "crashed"
    SETUP_FAILED = "setup_failed"
    """A setup command was rejected, so the world measured is not the world the
    scenario describes. The numbers are real and describe a different run."""
    WORKLOAD_FAILED = "workload_failed"
    """A workload command was rejected, so the load was not sustained."""
    PROBE_ERROR = "probe_error"
    """The probe reported an error during the run."""
    CONFIGURATION_MISMATCH = "configuration_mismatch"
    """The game reported running something other than what the results document
    says it ran. The numbers are real; they describe a configuration nothing
    else in the document describes."""
    TICK_PERIOD_ONLY = "tick_period_only"
    """Tick figures are periods, not execution time. Admissible, with the
    metrics named accordingly, but this run carries no MSPT."""


@dataclass
class ClientSamples:
    """Raw client-side sample stream from one run.

    Frametimes are nanoseconds: at 60 fps a millisecond-resolution timer has
    roughly 6% quantisation error, which is larger than most effects we need to
    resolve.
    """

    frametimes_ns: list[int] = field(default_factory=list)
    gc_pauses_ms: list[float] = field(default_factory=list)
    """Individual stop-the-world pause durations. Empty when the JVM could only
    report totals; see ``gc_total_pause_ms``."""
    gc_total_pause_ms: float = 0.0
    """Aggregate collection time, when individual events were unavailable. Kept
    apart from the pause list so no percentile is ever computed from sums."""
    alloc_bytes: int = 0
    heap_growth_bytes: int = 0
    real_allocation: bool = False
    """Whether ``alloc_bytes`` came from an allocation counter. When False it is
    a copy of heap growth and must not be published as an allocation rate."""
    duration_s: float = 0.0
    heap_samples_mb: list[float] = field(default_factory=list)
    heap_post_gc_mb: list[float] = field(default_factory=list)


@dataclass
class ServerSamples:
    """Raw server-side sample stream from one run."""

    tick_durations_ns: list[int] = field(default_factory=list)
    measures_execution: bool = True
    """False when ``tick_durations_ns`` holds tick *periods* rather than tick
    execution times, in which case they are published under ``tick_period_*``
    and never as MSPT."""
    wall_clock_s: float = 0.0
    gc_pauses_ms: list[float] = field(default_factory=list)
    gc_total_pause_ms: float = 0.0
    alloc_bytes: int = 0
    heap_growth_bytes: int = 0
    real_allocation: bool = False
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

    #: Flags that make a run unusable: it produced real numbers describing
    #: something other than the experiment asked for, and they look ordinary.
    INADMISSIBLE_FLAGS = frozenset({
        RunFlag.CRASHED,
        RunFlag.TOO_FEW_SAMPLES,
        RunFlag.WORLD_FINGERPRINT_MISMATCH,
        RunFlag.SETUP_FAILED,
        RunFlag.WORKLOAD_FAILED,
        RunFlag.PROBE_ERROR,
        RunFlag.CONFIGURATION_MISMATCH,
    })

    @property
    def admissible(self) -> bool:
        """Whether this run may contribute to a published comparison."""
        return not any(f in self.INADMISSIBLE_FLAGS for f in self.flags)


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


def _memory_metrics(samples: ClientSamples | ServerSamples) -> dict[str, float]:
    """Reduce the memory signals, naming each for what it actually measures.

    Three rules, each keeping a number under a name it has:

    * Pause percentiles come only from individual pauses; where the JVM reported
      per-interval totals, the total is given and the percentile omitted.
    * Allocation is ``alloc_rate_mb_s`` only from an allocation counter, and
      ``heap_growth_rate_mb_s`` otherwise. The two differ by everything
      allocated and collected between samples.
    * The live set comes from heap readings taken at the collection.
    """
    duration_s = (
        samples.duration_s
        if isinstance(samples, ClientSamples)
        else samples.wall_clock_s
    )
    out: dict[str, float] = {}

    if samples.gc_pauses_ms:
        out["gc_pause_total_ms"] = float(sum(samples.gc_pauses_ms))
        out["gc_pause_p99_ms"] = percentile(samples.gc_pauses_ms, 99.0)
        out["gc_pause_max_ms"] = max(samples.gc_pauses_ms)
        out["gc_count"] = float(len(samples.gc_pauses_ms))
    elif samples.gc_total_pause_ms > 0:
        out["gc_pause_total_ms"] = samples.gc_total_pause_ms

    if duration_s > 0:
        if samples.real_allocation and samples.alloc_bytes > 0:
            out["alloc_rate_mb_s"] = (
                samples.alloc_bytes / (1024 * 1024)
            ) / duration_s
        growth = samples.heap_growth_bytes or (
            samples.alloc_bytes if not samples.real_allocation else 0
        )
        if growth > 0:
            out["heap_growth_rate_mb_s"] = (growth / (1024 * 1024)) / duration_s

    if samples.heap_samples_mb:
        out["heap_peak_mb"] = max(samples.heap_samples_mb)
    if samples.heap_post_gc_mb:
        # Live set, not peak: post-collection occupancy is what actually
        # constrains an operator running a smaller heap.
        out["heap_steady_mb"] = mean(samples.heap_post_gc_mb)
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

    values.update(_memory_metrics(samples))

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

    tick_mean = mean(ticks_ms)
    if samples.measures_execution:
        values: dict[str, float] = {
            "mspt_mean": tick_mean,
            "mspt_p95": percentile(ticks_ms, 95.0),
            "mspt_p99": percentile(ticks_ms, 99.0),
            # Headroom is the metric TPS should have been: below budget every
            # configuration reports 20 TPS. Clamped at zero so a saturated
            # server reads "no headroom" rather than a negative figure.
            #
            # Only from bracketed or platform durations: from the period it
            # would read as near-zero headroom on an idle server.
            "tick_headroom": max(0.0, 1.0 - tick_mean / TICK_BUDGET_MS),
        }
    else:
        # No tick execution time available. Published under its own names: an
        # honest measurement of something else beats a mislabelled MSPT.
        values = {
            "tick_period_mean_ms": tick_mean,
            "tick_period_p95_ms": percentile(ticks_ms, 95.0),
            "tick_period_p99_ms": percentile(ticks_ms, 99.0),
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

    values.update(_memory_metrics(samples))

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
    benchmark concludes the mods are equivalent when what it measured was the
    monitor.

    Returns the suspected refresh rate, or None. Deliberately conservative: a
    genuinely CPU-bound scene can sit near a round frametime by coincidence, and
    a false alarm that discards a good run is its own kind of damage.
    """
    total = len(frametimes_ms)
    if total < 200:
        return None

    # One sort, then two bisections per candidate rate. Scanning every frame
    # once per rate costs len(COMMON_REFRESH_HZ) passes over a list that grows
    # with run length, and the count wanted is exactly a range of a sorted list.
    ordered = sorted(frametimes_ms)
    for hz in COMMON_REFRESH_HZ:
        interval = 1000.0 / hz
        margin = interval * tolerance
        near = bisect.bisect_right(ordered, interval + margin) - bisect.bisect_left(
            ordered, interval - margin
        )
        if near / total >= share:
            return hz
    return None


def frame_cap_suspected(
    frametimes_ms: Sequence[float], cap_fps: float, *, share: float = 0.50
) -> bool:
    """Whether the client spent most of the run against its frame limiter.

    Same failure mode as vsync, different cause: on a fast machine and a light
    scenario every variant scores the cap and the benchmark reports equivalence
    between mods it never compared.

    A bare majority rather than vsync's 80%, because a limiter is a floor rather
    than a lock: slower frames pass through untouched, so a run can be badly
    cap-bound with a large minority above it.

    A flag, never a block.
    """
    if cap_fps <= 0 or len(frametimes_ms) < 200:
        return False
    interval = 1000.0 / cap_fps
    # Anything at or below the cap interval is a frame the limiter could have
    # been holding back; a small tolerance covers timer granularity.
    at_cap = sum(1 for value in frametimes_ms if value <= interval * 1.02)
    return at_cap / len(frametimes_ms) >= share


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
    before that, meaning the series has stopped trending.

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
