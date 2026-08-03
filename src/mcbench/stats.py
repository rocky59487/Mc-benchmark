"""Statistical core of mcbench.

Implements docs/METHODOLOGY.md sections 4 and 5, in the standard library only.

Every estimator here is chosen for the data this project actually has. Frametime
and MSPT distributions are heavily right-skewed, run counts are small, and
several quantities of interest (percentiles, 1% lows) have no closed-form
standard error. That rules out normal-theory intervals and
standard-deviation-based outlier rules, and rules in bootstrap intervals and
robust estimators.
"""

from __future__ import annotations

import bisect
import math
import random
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from enum import Enum
from statistics import median

__all__ = [
    "MIN_ADMISSIBLE_RUNS",
    "Verdict",
    "Interval",
    "Estimate",
    "Comparison",
    "OutlierReport",
    "sufficient_runs",
    "mean",
    "percentile",
    "quantile_sketch",
    "mean_of_worst_fraction",
    "coefficient_of_variation",
    "mad",
    "reject_outlying_runs",
    "bootstrap_ci",
    "estimate",
    "cliffs_delta",
    "compare",
    "interaction_term",
    "benjamini_hochberg",
    "runs_needed_for_resolution",
]

# Defaults from docs/METHODOLOGY.md. Callers may override, but these are the
# values the published corpus is defined in terms of.
DEFAULT_BOOTSTRAP_RESAMPLES = 10_000
DEFAULT_CONFIDENCE = 0.95
DEFAULT_ROPE = 0.02  # +/-2% region of practical equivalence
DEFAULT_MAD_THRESHOLD = 8.0  # deliberately conservative; see reject_outlying_runs
MAD_TO_SIGMA = 1.4826  # scale factor making MAD consistent for normal data

#: The minimum-admissible-run policy, shared by reporting, interaction terms and
#: the bisect oracle. METHODOLOGY section 3 states five runs per cell as the
#: floor; one value against one value otherwise yields ``+100% [+100%, +100%]``,
#: the most decisive output the system can emit from the least evidence.
MIN_ADMISSIBLE_RUNS = 5


class Verdict(str, Enum):
    """Outcome of a baseline-vs-variant comparison (METHODOLOGY.md section 5).

    ``INCONCLUSIVE`` is a first-class result rather than a failure.

    ``INSUFFICIENT_DATA`` is distinct from it: inconclusive means the runs were
    made and disagreed, insufficient means they were never obtained. Conflating
    them lets a failed suite read as a measured null.
    """

    IMPROVEMENT = "improvement"
    REGRESSION = "regression"
    EQUIVALENT = "equivalent"
    INCONCLUSIVE = "inconclusive"
    INSUFFICIENT_DATA = "insufficient_data"

    @property
    def decisive(self) -> bool:
        """Whether this verdict asserts a direction of change."""
        return self in (Verdict.IMPROVEMENT, Verdict.REGRESSION)


def sufficient_runs(
    *samples: Sequence[float], min_runs: int = MIN_ADMISSIBLE_RUNS
) -> bool:
    """Whether every arm retained enough values to support a decisive verdict."""
    return all(len(s) >= min_runs for s in samples)


@dataclass(frozen=True)
class Interval:
    """A confidence interval."""

    low: float
    high: float
    confidence: float = DEFAULT_CONFIDENCE

    @property
    def width(self) -> float:
        return self.high - self.low

    def contains(self, value: float) -> bool:
        return self.low <= value <= self.high

    def within(self, low: float, high: float) -> bool:
        """True if this interval lies entirely inside ``[low, high]``."""
        return self.low >= low and self.high <= high

    def entirely_above(self, value: float) -> bool:
        return self.low > value

    def entirely_below(self, value: float) -> bool:
        return self.high < value


@dataclass(frozen=True)
class Estimate:
    """A point estimate with its bootstrap interval and the sample behind it."""

    value: float
    ci: Interval
    n: int

    def __str__(self) -> str:
        return f"{self.value:.4g} [{self.ci.low:.4g}, {self.ci.high:.4g}] (n={self.n})"


@dataclass(frozen=True)
class OutlierReport:
    """Which whole runs were excluded from a cell, and why.

    Exclusions are always reported; a cell that loses too many runs is flagged
    unstable rather than quietly averaged.
    """

    kept: list[float]
    excluded: list[tuple[int, float, float]] = field(default_factory=list)
    """``(original_index, value, distance_in_mad)`` for each excluded run."""
    unstable: bool = False
    """True when more than 20% of runs were excluded."""

    @property
    def exclusion_rate(self) -> float:
        total = len(self.kept) + len(self.excluded)
        return len(self.excluded) / total if total else 0.0

    @property
    def instability(self) -> str:
        """Why this cell is unstable, empty when it is not.

        Two different things set the flag and they need different sentences. The
        rule may have excluded more than the tolerated share, or it may have
        wanted to exclude a run and been unable to without taking the cell below
        the minimum — in which case every run was kept and nothing was excluded
        at all. Reporting the first when the second happened tells a reader
        their data was thrown away when none of it was.
        """
        if not self.unstable:
            return ""
        if self.excluded:
            return (
                f"{self.exclusion_rate * 100:.0f}% of runs were excluded as "
                f"outliers, which is more than a stable cell tolerates"
            )
        return (
            f"a run was far enough out to exclude, and excluding it would have "
            f"left too few to estimate from, so all {len(self.kept)} were kept"
        )


@dataclass(frozen=True)
class Comparison:
    """Result of comparing a variant against a baseline."""

    baseline: Estimate
    variant: Estimate
    relative_delta: Estimate
    """Fractional change, e.g. -0.07 for 7% lower. CI is on the delta itself."""
    cliffs_delta: float
    verdict: Verdict
    rope: float
    lower_is_better: bool
    min_runs: int = MIN_ADMISSIBLE_RUNS
    """The run floor this comparison was judged against."""
    p_value: float = 1.0
    """Two-sided bootstrap p-value for a zero relative change, from the same
    delta replicates as the interval. Feeds multiplicity correction; the verdict
    itself remains a ROPE decision, not a significance test."""

    @property
    def percent(self) -> float:
        return self.relative_delta.value * 100.0

    @property
    def underpowered(self) -> bool:
        """True when an arm has fewer retained values than the floor."""
        return min(self.baseline.n, self.variant.n) < self.min_runs

    @property
    def decisive(self) -> bool:
        return self.verdict.decisive

    @property
    def effect_magnitude(self) -> str:
        """Conventional descriptive bands for Cliff's delta."""
        d = abs(self.cliffs_delta)
        if d < 0.147:
            return "negligible"
        if d < 0.33:
            return "small"
        if d < 0.474:
            return "medium"
        return "large"


# --------------------------------------------------------------------------
# Descriptive statistics
# --------------------------------------------------------------------------


def mean(values: Sequence[float]) -> float:
    if not values:
        raise ValueError("mean() requires at least one value")
    return math.fsum(values) / len(values)


def _percentile_of_sorted(ordered: Sequence[float], q: float) -> float:
    if len(ordered) == 1:
        return ordered[0]
    pos = (len(ordered) - 1) * (q / 100.0)
    lower = math.floor(pos)
    upper = math.ceil(pos)
    if lower == upper:
        return ordered[int(pos)]
    return ordered[lower] * (upper - pos) + ordered[upper] * (pos - lower)


def percentile(values: Sequence[float], q: float) -> float:
    """Linear-interpolated percentile. ``q`` in [0, 100].

    Matches the ``numpy.percentile`` default so that results computed here and
    results computed by third parties verifying them agree exactly.
    """
    if not values:
        raise ValueError("percentile() requires at least one value")
    if not 0.0 <= q <= 100.0:
        raise ValueError(f"q must be in [0, 100], got {q}")
    return _percentile_of_sorted(sorted(values), q)


def quantile_sketch(values: Sequence[float], points: int = 256) -> list[float]:
    """``points`` evenly spaced quantiles, from the minimum to the maximum.

    A run holds a few hundred thousand frametimes and a results document has to
    stay readable, so the distribution travels as a sketch rather than as every
    sample. At this resolution an empirical CDF drawn from the sketch is the
    same curve as one drawn from the full data at any chart size, and the tail
    survives: the last point is the maximum, which is where stutter lives and
    what a histogram of fixed bins would smear away.

    Summaries alone cannot be checked by a reader; this is the smallest thing
    that lets one redraw the distribution rather than trust the percentiles.
    """
    if not values:
        return []
    if points < 2:
        raise ValueError(f"points must be at least 2, got {points}")
    ordered = sorted(values)
    if len(ordered) <= points:
        return ordered
    step = 100.0 / (points - 1)
    return [_percentile_of_sorted(ordered, min(100.0, i * step)) for i in range(points)]


def _interval_of(replicates: list[float], confidence: float) -> Interval:
    """Both bounds of a bootstrap interval from one sort of the replicates."""
    ordered = sorted(replicates)
    alpha = 1.0 - confidence
    return Interval(
        low=_percentile_of_sorted(ordered, 100.0 * (alpha / 2.0)),
        high=_percentile_of_sorted(ordered, 100.0 * (1.0 - alpha / 2.0)),
        confidence=confidence,
    )


def mean_of_worst_fraction(values: Sequence[float], fraction: float) -> float:
    """Mean of the worst ``fraction`` of samples, treating larger as worse.

    This is the definition behind ``fps_1pct_low`` and ``fps_0p1pct_low``: the
    mean of the worst 1% (or 0.1%) of *frametimes*, not the 99th percentile.
    Both definitions circulate in the wild and they are not the same number; the
    mean-of-worst form responds to how bad the bad frames are, which is what the
    metric is for. Reports always state which definition was used.

    At least one sample is always retained, so this is defined for small runs.
    """
    if not values:
        raise ValueError("mean_of_worst_fraction() requires at least one value")
    if not 0.0 < fraction <= 1.0:
        raise ValueError(f"fraction must be in (0, 1], got {fraction}")
    ordered = sorted(values, reverse=True)
    count = max(1, int(round(len(ordered) * fraction)))
    return mean(ordered[:count])


def coefficient_of_variation(values: Sequence[float]) -> float:
    """Standard deviation over mean: smoothness, independent of speed.

    Separates "is it smooth" from "is it fast". A mod that raises average FPS
    while widening the frametime distribution has made the game feel worse, and
    an FPS-only benchmark scores that as a win.
    """
    if len(values) < 2:
        raise ValueError("coefficient_of_variation() requires at least two values")
    mu = mean(values)
    if mu == 0:
        raise ValueError("coefficient_of_variation() undefined for zero mean")
    variance = math.fsum((v - mu) ** 2 for v in values) / (len(values) - 1)
    return math.sqrt(variance) / mu


def mad(values: Sequence[float], *, scaled: bool = True) -> float:
    """Median absolute deviation.

    Used instead of standard deviation for outlier detection because the
    outliers we are looking for inflate the standard deviation, so an SD-based
    rule breaks down precisely when it is needed. ``scaled`` applies the 1.4826
    factor that makes MAD comparable to sigma for normally distributed data.
    """
    if not values:
        raise ValueError("mad() requires at least one value")
    centre = median(values)
    deviation = median([abs(v - centre) for v in values])
    return deviation * MAD_TO_SIGMA if scaled else deviation


# --------------------------------------------------------------------------
# Outlier handling (METHODOLOGY.md section 4)
# --------------------------------------------------------------------------


def _finite_sample_factor(n: int) -> float:
    """Croux-Rousseeuw small-sample correction for the MAD scale estimate.

    The 1.4826 constant makes MAD consistent with sigma only asymptotically. At
    the sample sizes a benchmark actually has, MAD is biased *low*, so a nominal
    "3.5 MAD" threshold bites considerably tighter than 3.5 sigma and starts
    discarding legitimate runs.

    That failure mode is worse than it looks. The runs it discards are the ones
    in the tails, so removing them makes the sample look more precise than it
    is, narrowing the confidence interval and inflating confidence in the
    result. Measured on clean Gaussian samples, the uncorrected rule dropped a
    run in roughly a fifth of seven-run cells.
    """
    if n < 2:
        return 1.0
    return n / (n - 0.8) if n % 2 else n / (n - 0.9)


def reject_outlying_runs(
    run_values: Sequence[float],
    *,
    threshold: float = DEFAULT_MAD_THRESHOLD,
    min_runs: int = 4,
    unstable_rate: float = 0.20,
) -> OutlierReport:
    """Exclude whole runs whose summary statistic is contaminated.

    This operates on *per-run summaries*, never on individual frames. Within a
    run nothing is ever removed: a long frame is the phenomenon under study, and
    a GC pause is a real cost that must reach the tail metrics. What this
    targets is a whole run spoiled by something outside the experiment: an OS
    update, a stray process, a thermal event.

    The default threshold of 8 MAD is far more permissive than the ~3 commonly
    used for outlier screening, and the choice is deliberate. At the sample
    sizes a benchmark has (5-10 runs), the centre and the scale are estimated
    from the same handful of points, which makes the studentised deviation
    heavy-tailed; calibration on clean Gaussian samples showed a 3.5 threshold
    falsely flagging around 13% of seven-run cells, against roughly 1.6% at 8.

    The two errors are not symmetric, which settles the choice. Falsely
    excluding a run removes a tail observation, narrows the confidence interval
    and makes mcbench more confident than the data warrants. Failing to exclude
    a mildly odd run widens the interval and pushes the verdict toward
    ``inconclusive``,
    which is honest. So the threshold is set where gross contamination is still
    caught essentially always, and marginal runs are left in the data to widen
    the interval as they should.

    With fewer than ``min_runs`` samples the MAD is not trustworthy, so nothing
    is excluded and every run is kept.
    """
    values = list(run_values)
    if len(values) < min_runs:
        return OutlierReport(kept=values)

    centre = median(values)
    scale = mad(values) * _finite_sample_factor(len(values))

    # A zero MAD means the majority of runs are identical. Any deviation at all
    # would then read as infinitely many MADs, which would throw away legitimate
    # runs, so we decline to exclude anything.
    if scale == 0:
        return OutlierReport(kept=values)

    kept: list[float] = []
    excluded: list[tuple[int, float, float]] = []
    for index, value in enumerate(values):
        distance = abs(value - centre) / scale
        if distance > threshold:
            excluded.append((index, value, distance))
        else:
            kept.append(value)

    # Refuse to strip a cell down to nothing; if the rule wants to remove almost
    # everything, the cell is pathological and the caller must see it as such.
    if len(kept) < min_runs:
        return OutlierReport(kept=values, unstable=True)

    rate = len(excluded) / len(values)
    return OutlierReport(kept=kept, excluded=excluded, unstable=rate > unstable_rate)


# --------------------------------------------------------------------------
# Bootstrap (METHODOLOGY.md section 5)
# --------------------------------------------------------------------------


def bootstrap_ci(
    values: Sequence[float],
    statistic: Callable[[Sequence[float]], float] = mean,
    *,
    resamples: int = DEFAULT_BOOTSTRAP_RESAMPLES,
    confidence: float = DEFAULT_CONFIDENCE,
    seed: int = 0,
) -> Interval:
    """Percentile bootstrap confidence interval for any statistic.

    Assumes nothing about the shape of the distribution, which is required here:
    frametimes are not normal, and percentile statistics have no closed-form
    standard error. ``seed`` is always explicit so intervals are reproducible.
    """
    if not values:
        raise ValueError("bootstrap_ci() requires at least one value")
    if not 0.0 < confidence < 1.0:
        raise ValueError(f"confidence must be in (0, 1), got {confidence}")

    n = len(values)
    if n == 1:
        only = float(values[0])
        return Interval(only, only, confidence)

    # choices() draws the whole resample in one call instead of n calls to
    # randrange(). It consumes the generator differently, so a given seed gives
    # a different draw than before; it is still fully determined by that seed.
    draw = random.Random(seed).choices
    pool = list(values)
    replicates = [statistic(draw(pool, k=n)) for _ in range(resamples)]
    return _interval_of(replicates, confidence)


def estimate(
    values: Sequence[float],
    statistic: Callable[[Sequence[float]], float] = mean,
    *,
    resamples: int = DEFAULT_BOOTSTRAP_RESAMPLES,
    confidence: float = DEFAULT_CONFIDENCE,
    seed: int = 0,
) -> Estimate:
    """Point estimate plus its bootstrap interval."""
    return Estimate(
        value=statistic(values),
        ci=bootstrap_ci(
            values, statistic, resamples=resamples, confidence=confidence, seed=seed
        ),
        n=len(values),
    )


# --------------------------------------------------------------------------
# Effect size and verdicts
# --------------------------------------------------------------------------


def cliffs_delta(baseline: Sequence[float], variant: Sequence[float]) -> float:
    """Cliff's delta: a non-parametric ordinal effect size in [-1, 1].

    P(variant > baseline) - P(variant < baseline). Robust to skew, meaningful at
    the sample sizes benchmarks actually have, and it answers how often one
    configuration beats the other.

    Sign convention is in raw metric units: positive means the variant tends to
    produce larger values. Whether larger is better is the caller's business.
    """
    if not baseline or not variant:
        raise ValueError("cliffs_delta() requires non-empty samples")

    # O(n log n) via sorted counting rather than the O(n*m) double loop, so
    # per-frame samples remain tractable. bisect_left counts the baseline values
    # strictly below a variant value; bisect_right counts those at or below it.
    ordered = sorted(baseline)
    total = len(ordered)
    left = bisect.bisect_left
    right = bisect.bisect_right

    greater = 0
    less = 0
    for value in variant:
        greater += left(ordered, value)
        less += total - right(ordered, value)

    return (greater - less) / (total * len(variant))


def _relative_delta(baseline: Sequence[float], variant: Sequence[float]) -> float:
    base = mean(baseline)
    if base == 0:
        raise ValueError("relative delta undefined against a zero baseline")
    return (mean(variant) - base) / base


def compare(
    baseline: Sequence[float],
    variant: Sequence[float],
    *,
    lower_is_better: bool = True,
    rope: float = DEFAULT_ROPE,
    resamples: int = DEFAULT_BOOTSTRAP_RESAMPLES,
    confidence: float = DEFAULT_CONFIDENCE,
    seed: int = 0,
    min_runs: int = MIN_ADMISSIBLE_RUNS,
) -> Comparison:
    """Compare a variant against a baseline and issue a verdict.

    ``lower_is_better`` should be True for cost metrics (frametime, MSPT,
    allocation rate) and False for throughput metrics (FPS, ticks/sec).

    The verdict applies the ROPE rule from METHODOLOGY.md section 5: a
    difference must be both statistically resolvable *and* larger than the
    region of practical equivalence before it is called a win or a loss. With
    enough runs a 0.3% difference becomes detectable and is still irrelevant,
    and a standard that reports it as a victory teaches people to game it.

    The run floor applies first: below ``min_runs`` retained values in either
    arm the verdict is ``insufficient_data`` and no direction is asserted. The
    descriptive numbers are still returned, labelled rather than presented as a
    finding. ``min_runs=1`` opts out; nothing in mcbench does.
    """
    if not baseline or not variant:
        raise ValueError("compare() requires non-empty samples")

    baseline_est = estimate(
        baseline, resamples=resamples, confidence=confidence, seed=seed
    )
    variant_est = estimate(
        variant, resamples=resamples, confidence=confidence, seed=seed + 1
    )

    # Bootstrap the delta directly rather than deriving it from the two marginal
    # intervals. Comparing whether two CIs overlap is a distinctly weaker and
    # more conservative test than an interval on the difference itself.
    draw = random.Random(seed + 2).choices
    nb, nv = len(baseline), len(variant)
    base_pool, var_pool = list(baseline), list(variant)
    fsum = math.fsum
    replicates: list[float] = []
    append = replicates.append
    for _ in range(resamples):
        b_mean = fsum(draw(base_pool, k=nb)) / nb
        if b_mean == 0:
            continue
        append((fsum(draw(var_pool, k=nv)) / nv - b_mean) / b_mean)

    if not replicates:
        raise ValueError("delta bootstrap produced no valid replicates")

    delta_ci = _interval_of(replicates, confidence)
    delta_est = Estimate(
        value=_relative_delta(baseline, variant), ci=delta_ci, n=min(nb, nv)
    )

    if sufficient_runs(baseline, variant, min_runs=min_runs):
        verdict = _verdict(delta_ci, rope=rope, lower_is_better=lower_is_better)
    else:
        verdict = Verdict.INSUFFICIENT_DATA

    return Comparison(
        baseline=baseline_est,
        variant=variant_est,
        relative_delta=delta_est,
        cliffs_delta=cliffs_delta(baseline, variant),
        verdict=verdict,
        rope=rope,
        lower_is_better=lower_is_better,
        min_runs=min_runs,
        p_value=_bootstrap_p_value(replicates),
    )


def _bootstrap_p_value(replicates: Sequence[float]) -> float:
    """Two-sided bootstrap p-value for a delta of zero.

    Percentile-bootstrap inversion: the smaller tail mass either side of zero,
    doubled. The +1 correction keeps it above zero, because a level below the
    resampling's resolution is not evidence of an infinitely small one.
    """
    n = len(replicates)
    if n == 0:
        return 1.0
    below = sum(1 for value in replicates if value <= 0.0)
    above = sum(1 for value in replicates if value >= 0.0)
    tail = min(below, above)
    return min(1.0, 2.0 * (tail + 1) / (n + 1))


def _verdict(delta_ci: Interval, *, rope: float, lower_is_better: bool) -> Verdict:
    if delta_ci.within(-rope, rope):
        return Verdict.EQUIVALENT
    if delta_ci.entirely_above(rope):
        # Metric went up: a regression for cost metrics, a win for throughput.
        return Verdict.REGRESSION if lower_is_better else Verdict.IMPROVEMENT
    if delta_ci.entirely_below(-rope):
        return Verdict.IMPROVEMENT if lower_is_better else Verdict.REGRESSION
    return Verdict.INCONCLUSIVE


def runs_needed_for_resolution(
    comparison: Comparison, *, current_runs: int, max_runs: int = 50
) -> int | None:
    """Estimate the run count that would resolve an ``inconclusive`` verdict.

    Reporting "we don't know" is only useful if it comes with a way forward.
    CI width shrinks roughly as 1/sqrt(n), so this projects the sample size at
    which the interval would clear the nearer ROPE boundary.

    Returns None when the verdict is already resolved, or when the projection
    exceeds ``max_runs``, where more runs will not settle it because the effect
    is too close to the boundary to distinguish.

    ``insufficient_data`` returns the floor without projecting: extrapolating
    an interval width from one or two values is arithmetic on noise.
    """
    if comparison.verdict is Verdict.INSUFFICIENT_DATA:
        return comparison.min_runs
    if comparison.verdict is not Verdict.INCONCLUSIVE:
        return None

    centre = comparison.relative_delta.value
    half_width = comparison.relative_delta.ci.width / 2.0
    if half_width <= 0:
        return None

    rope = comparison.rope
    # Distance the interval edge must travel to fall wholly inside or wholly
    # outside the ROPE, whichever this delta is heading toward.
    # Inside the ROPE the interval must shrink to sit wholly within it; outside,
    # it must shrink until it clears the boundary.
    target_half_width = (
        rope - abs(centre) if abs(centre) <= rope else abs(centre) - rope
    )

    if target_half_width <= 0:
        return None

    needed = math.ceil(current_runs * (half_width / target_half_width) ** 2)
    if needed <= current_runs:
        needed = current_runs + 1
    return needed if needed <= max_runs else None


# --------------------------------------------------------------------------
# Interaction effects (METHODOLOGY.md section 6)
# --------------------------------------------------------------------------


def interaction_term(
    none: Sequence[float],
    a: Sequence[float],
    b: Sequence[float],
    ab: Sequence[float],
    *,
    resamples: int = DEFAULT_BOOTSTRAP_RESAMPLES,
    confidence: float = DEFAULT_CONFIDENCE,
    rope: float = DEFAULT_ROPE,
    seed: int = 0,
) -> Estimate:
    """Non-additivity of two mods, as a fraction of the baseline cost.

        interaction = [cost(A+B) - cost(none)]
                      - ([cost(A) - cost(none)] + [cost(B) - cost(none)])

    Zero means the two mods' costs simply add. Positive means the pair is more
    expensive together than their individual costs predict, typically because
    they contend over a shared lock, a cache one invalidates for the other, or a
    fast path one forces the other off.

    This is the axis every existing Minecraft benchmark ignores, and the usual
    explanation for a modpack that runs far worse than its parts suggest.
    """
    for name, sample in (("none", none), ("a", a), ("b", b), ("ab", ab)):
        if not sample:
            raise ValueError(f"interaction_term() requires a non-empty '{name}' sample")

    base_mean = mean(none)
    if base_mean == 0:
        raise ValueError("interaction term undefined against a zero baseline")

    def term(n: Sequence[float], x: Sequence[float], y: Sequence[float],
             xy: Sequence[float]) -> float:
        n_mean = math.fsum(n) / len(n)
        return (
            (math.fsum(xy) / len(xy) - n_mean)
            - ((math.fsum(x) / len(x) - n_mean) + (math.fsum(y) / len(y) - n_mean))
        ) / n_mean

    draw = random.Random(seed).choices
    pools = [(list(pool), len(pool)) for pool in (none, a, b, ab)]
    replicates: list[float] = []
    append = replicates.append
    for _ in range(resamples):
        resampled = [draw(pool, k=size) for pool, size in pools]
        if math.fsum(resampled[0]) == 0:
            continue
        append(term(*resampled))

    if not replicates:
        raise ValueError("interaction bootstrap produced no valid replicates")

    return Estimate(
        value=term(none, a, b, ab),
        ci=_interval_of(replicates, confidence),
        n=min(len(none), len(a), len(b), len(ab)),
    )


# --------------------------------------------------------------------------
# Multiple comparisons (METHODOLOGY.md section 5)
# --------------------------------------------------------------------------


def benjamini_hochberg(p_values: Sequence[float], *, fdr: float = 0.05) -> list[bool]:
    """Benjamini-Hochberg false-discovery-rate control.

    A suite comparing many mods across many scenarios runs many tests, and some
    will look significant by chance alone. Returns, per input p-value, whether
    it is still a discovery after controlling FDR at ``fdr``. Order matches the
    input.
    """
    if not p_values:
        return []
    if not 0.0 < fdr < 1.0:
        raise ValueError(f"fdr must be in (0, 1), got {fdr}")

    m = len(p_values)
    ordered = sorted(enumerate(p_values), key=lambda pair: pair[1])

    # Largest rank k whose p-value clears (k/m)*fdr; everything at or below that
    # rank is a discovery, including any earlier p-value that failed its own
    # threshold (the step-up property).
    cutoff_rank = 0
    for rank, (_, p) in enumerate(ordered, start=1):
        if p <= (rank / m) * fdr:
            cutoff_rank = rank

    accepted = [False] * m
    for rank, (original_index, _) in enumerate(ordered, start=1):
        if rank <= cutoff_rank:
            accepted[original_index] = True
    return accepted
