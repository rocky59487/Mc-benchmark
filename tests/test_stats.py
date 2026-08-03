"""Tests for the statistical core.

Where possible these are known-answer tests against values computed by hand or
by definition, rather than characterisation tests that merely lock in whatever
the code currently does. A benchmark's statistics layer is exactly the place
where a plausible-looking wrong answer does the most damage.
"""

from __future__ import annotations

import math
import random

import pytest

from mcbench.stats import (
    DEFAULT_MAD_THRESHOLD,
    Verdict,
    benjamini_hochberg,
    bootstrap_ci,
    cliffs_delta,
    coefficient_of_variation,
    compare,
    interaction_term,
    mad,
    mean_of_worst_fraction,
    percentile,
    reject_outlying_runs,
    runs_needed_for_resolution,
)


class TestPercentile:
    def test_matches_linear_interpolation_definition(self):
        # numpy.percentile([1,2,3,4], 25) == 1.75 under linear interpolation.
        assert percentile([1, 2, 3, 4], 25) == pytest.approx(1.75)
        assert percentile([1, 2, 3, 4], 50) == pytest.approx(2.5)
        assert percentile([1, 2, 3, 4], 75) == pytest.approx(3.25)

    def test_endpoints(self):
        assert percentile([5, 1, 3], 0) == 1
        assert percentile([5, 1, 3], 100) == 5

    def test_single_value(self):
        assert percentile([42.0], 37) == 42.0

    def test_rejects_out_of_range(self):
        with pytest.raises(ValueError, match="q must be in"):
            percentile([1, 2], 101)


class TestMeanOfWorstFraction:
    def test_is_mean_of_worst_not_a_percentile(self):
        # This distinction is the whole point: for 1..100 the 99th percentile is
        # 99, but the mean of the worst 1% is 100. Reporting one as the other is
        # a widespread error in published Minecraft benchmarks.
        values = list(range(1, 101))
        assert mean_of_worst_fraction(values, 0.01) == 100.0
        assert percentile(values, 99) == pytest.approx(99.01, abs=0.02)

    def test_averages_across_the_worst_slice(self):
        values = list(range(1, 101))
        # Worst 10% is 91..100, mean 95.5.
        assert mean_of_worst_fraction(values, 0.10) == pytest.approx(95.5)

    def test_always_retains_at_least_one_sample(self):
        # A tiny fraction of a short run must not round down to zero samples.
        assert mean_of_worst_fraction([1.0, 2.0, 3.0], 0.001) == 3.0

    def test_rejects_invalid_fraction(self):
        with pytest.raises(ValueError, match="fraction must be"):
            mean_of_worst_fraction([1.0], 0.0)


class TestCoefficientOfVariation:
    def test_zero_for_constant_input(self):
        assert coefficient_of_variation([7.0] * 10) == 0.0

    def test_scale_invariant(self):
        # CV is dimensionless: doubling every sample must not change it. This is
        # what lets it express smoothness independently of speed.
        base = [10.0, 12.0, 9.0, 11.0, 13.0]
        doubled = [v * 2 for v in base]
        assert coefficient_of_variation(base) == pytest.approx(
            coefficient_of_variation(doubled)
        )

    def test_requires_two_samples(self):
        with pytest.raises(ValueError, match="at least two"):
            coefficient_of_variation([1.0])


class TestMad:
    def test_known_value(self):
        # For [1,2,3,4,5]: median 3, absolute deviations [2,1,0,1,2], median 1.
        assert mad([1, 2, 3, 4, 5], scaled=False) == 1.0
        assert mad([1, 2, 3, 4, 5]) == pytest.approx(1.4826)

    def test_resists_a_contaminating_value(self):
        clean = [10, 11, 12, 11, 10]
        contaminated = clean + [10_000]
        # Standard deviation would explode; MAD barely moves. That robustness is
        # why outlier detection uses MAD rather than sigma.
        assert mad(contaminated) < 2 * mad(clean) + 1.0


class TestOutlierRejection:
    def test_keeps_everything_when_clean(self):
        report = reject_outlying_runs([10.0, 10.1, 9.9, 10.2, 9.8, 10.0])
        assert report.excluded == []
        assert len(report.kept) == 6
        assert not report.unstable

    def test_excludes_a_contaminated_run(self):
        report = reject_outlying_runs([10.0, 10.1, 9.9, 10.2, 9.8, 45.0])
        assert len(report.excluded) == 1
        assert report.excluded[0][1] == 45.0
        assert 45.0 not in report.kept

    def test_reports_distance_in_mad(self):
        report = reject_outlying_runs([10.0, 10.1, 9.9, 10.2, 9.8, 45.0])
        _, value, distance = report.excluded[0]
        assert value == 45.0
        assert distance > DEFAULT_MAD_THRESHOLD

    def test_rarely_fires_on_clean_data(self):
        """False exclusions must stay rare, because they bias toward confidence.

        Dropping a legitimate run removes a tail observation and narrows the
        interval, making mcbench surer than the data warrants. This pins the
        calibration behind the 8-MAD default: on clean Gaussian seven-run cells
        the false-flag rate should stay a few percent, not the ~13% a
        conventional 3.5 threshold produces at this sample size.
        """
        rng = random.Random(3)
        trials = 3000
        flagged = sum(
            bool(reject_outlying_runs([rng.gauss(100, 2) for _ in range(7)]).excluded)
            for _ in range(trials)
        )
        assert flagged / trials < 0.05

    def test_still_catches_gross_contamination(self):
        """The permissive threshold must not cost real detection power.

        A run spoiled by something outside the experiment sits tens of MAD out,
        so raising the threshold to 8 leaves it comfortably detected.
        """
        rng = random.Random(5)
        trials = 1000
        caught = 0
        for _ in range(trials):
            values = [rng.gauss(100, 2) for _ in range(6)] + [180.0]
            report = reject_outlying_runs(values)
            caught += any(v == 180.0 for _, v, _ in report.excluded)
        assert caught / trials > 0.99

    def test_declines_to_act_on_too_few_runs(self):
        # With three samples the MAD is not trustworthy; excluding on it would
        # discard legitimate data.
        values = [10.0, 10.1, 40.0]
        report = reject_outlying_runs(values)
        assert report.kept == values
        assert report.excluded == []

    def test_zero_mad_excludes_nothing(self):
        # Identical majority => MAD 0 => every deviation is "infinite" MADs.
        # Excluding on that would throw away real runs.
        report = reject_outlying_runs([5.0, 5.0, 5.0, 5.0, 6.0])
        assert report.excluded == []

    def test_flags_unstable_when_many_runs_excluded(self):
        values = [10.0] * 5 + [10.05, 10.02] + [900.0, 901.0, 902.0]
        report = reject_outlying_runs(values)
        assert report.unstable


class TestWhyACellIsUnstable:
    """The flag has two causes and they need different sentences.

    The report used to state one of them unconditionally: "over 20% of runs
    excluded as outliers". On four real cells from this repository's own suite
    that was false — nothing had been excluded, and the reader was told their
    runs had been thrown away.
    """

    def test_a_stable_cell_says_nothing(self):
        report = reject_outlying_runs([10.0, 10.1, 9.9, 10.2, 9.8, 10.0])
        assert not report.unstable
        assert report.instability == ""

    def test_too_many_excluded_says_the_share(self):
        values = [10.0] * 5 + [10.05, 10.02] + [900.0, 901.0, 902.0]
        report = reject_outlying_runs(values)
        assert report.excluded
        assert "%" in report.instability
        assert "excluded" in report.instability

    def test_an_outlier_that_could_not_be_dropped_says_so(self):
        # Four runs, one far out. Excluding it leaves three, below the minimum
        # the estimator needs, so every run is kept and the cell is flagged
        # instead. Nothing was excluded, and the sentence must not claim it was.
        report = reject_outlying_runs([10.0, 10.1, 9.9, 40.0])
        assert report.unstable
        assert report.excluded == []
        assert report.kept == [10.0, 10.1, 9.9, 40.0]
        assert "kept" in report.instability
        assert "%" not in report.instability
        assert "excluded as outliers" not in report.instability


class TestBootstrap:
    def test_is_deterministic_for_a_given_seed(self):
        values = [10.0, 11.0, 9.5, 10.5, 12.0, 9.0, 10.2]
        first = bootstrap_ci(values, resamples=500, seed=42)
        second = bootstrap_ci(values, resamples=500, seed=42)
        assert (first.low, first.high) == (second.low, second.high)

    def test_interval_brackets_the_point_estimate(self):
        values = [10.0, 11.0, 9.5, 10.5, 12.0, 9.0, 10.2]
        interval = bootstrap_ci(values, resamples=2000, seed=1)
        assert interval.low <= sum(values) / len(values) <= interval.high

    def test_degenerate_for_a_single_sample(self):
        interval = bootstrap_ci([7.0], resamples=100, seed=0)
        assert interval.low == interval.high == 7.0

    def test_constant_data_gives_zero_width(self):
        interval = bootstrap_ci([5.0] * 20, resamples=500, seed=0)
        assert interval.width == pytest.approx(0.0)

    def test_more_data_narrows_the_interval(self):
        # Width should shrink roughly as 1/sqrt(n).
        small = bootstrap_ci([10, 11, 9, 12, 8] * 2, resamples=2000, seed=3)
        large = bootstrap_ci([10, 11, 9, 12, 8] * 20, resamples=2000, seed=3)
        assert large.width < small.width


class TestCliffsDelta:
    def test_plus_one_when_fully_separated_above(self):
        assert cliffs_delta([1, 2, 3], [4, 5, 6]) == 1.0

    def test_minus_one_when_fully_separated_below(self):
        assert cliffs_delta([4, 5, 6], [1, 2, 3]) == -1.0

    def test_zero_for_identical_samples(self):
        assert cliffs_delta([1, 2, 3], [1, 2, 3]) == 0.0

    def test_known_partial_overlap(self):
        # baseline [1,2], variant [2,3]: pairs (1,2)>,(1,3)>,(2,2)=,(2,3)>
        # => 3 greater, 0 less, over 4 pairs = 0.75
        assert cliffs_delta([1, 2], [2, 3]) == pytest.approx(0.75)

    def test_handles_ties_without_counting_them(self):
        assert cliffs_delta([5, 5, 5], [5, 5, 5]) == 0.0


class TestCompare:
    def test_detects_a_clear_improvement_for_cost_metrics(self):
        baseline = [20.0, 20.5, 19.8, 20.2, 20.1, 19.9, 20.3]
        variant = [15.0, 15.2, 14.8, 15.1, 15.3, 14.9, 15.0]
        result = compare(baseline, variant, lower_is_better=True, seed=7)
        assert result.verdict is Verdict.IMPROVEMENT
        assert result.relative_delta.value < 0
        assert result.effect_magnitude == "large"

    def test_same_data_is_a_regression_for_throughput_metrics(self):
        # Polarity must flip with the metric's direction. Getting this backwards
        # would report a regression as a win, the worst failure mode we have.
        baseline = [20.0, 20.5, 19.8, 20.2, 20.1, 19.9, 20.3]
        variant = [15.0, 15.2, 14.8, 15.1, 15.3, 14.9, 15.0]
        result = compare(baseline, variant, lower_is_better=False, seed=7)
        assert result.verdict is Verdict.REGRESSION

    def test_detects_a_clear_regression(self):
        baseline = [15.0, 15.2, 14.8, 15.1, 15.3, 14.9, 15.0]
        variant = [20.0, 20.5, 19.8, 20.2, 20.1, 19.9, 20.3]
        result = compare(baseline, variant, lower_is_better=True, seed=7)
        assert result.verdict is Verdict.REGRESSION
        assert result.relative_delta.value > 0

    def test_tiny_difference_is_equivalent_not_a_win(self):
        # ~0.5% apart with very low noise: statistically resolvable, practically
        # irrelevant. The ROPE rule exists precisely to refuse this as a win.
        baseline = [100.0, 100.1, 99.9, 100.0, 100.1, 99.9, 100.0]
        variant = [100.5, 100.6, 100.4, 100.5, 100.6, 100.4, 100.5]
        result = compare(baseline, variant, lower_is_better=True, rope=0.02, seed=11)
        assert result.verdict is Verdict.EQUIVALENT

    def test_noisy_borderline_difference_is_inconclusive(self):
        baseline = [100.0, 90.0, 110.0, 95.0, 105.0, 98.0, 102.0]
        variant = [103.0, 88.0, 118.0, 92.0, 111.0, 99.0, 108.0]
        result = compare(baseline, variant, lower_is_better=True, rope=0.02, seed=13)
        assert result.verdict is Verdict.INCONCLUSIVE

    def test_identical_low_noise_samples_are_equivalent(self):
        values = [10.0, 10.05, 9.95, 10.0, 10.02, 9.98, 10.01]
        result = compare(values, list(values), seed=5)
        assert result.verdict is Verdict.EQUIVALENT
        assert result.relative_delta.value == pytest.approx(0.0)

    def test_identical_noisy_samples_do_not_resolve_to_equivalent(self):
        """Identical data is not automatically "equivalent".

        Equivalence is a claim about precision, not about the point estimate.
        These samples are the same data, so the delta is exactly zero — but with
        ~7% run-to-run noise at n=7 the interval spans roughly ±6%, which cannot
        establish that the true difference is inside a ±2% ROPE. Reporting
        "equivalent" here would be a stronger claim than the data supports, so
        the honest verdict is inconclusive.
        """
        values = [10.0, 11.0, 9.0, 10.5, 9.5, 10.2, 9.8]
        result = compare(values, list(values), rope=0.02, seed=5)
        assert result.relative_delta.value == pytest.approx(0.0)
        assert result.relative_delta.ci.contains(0.0)
        assert result.cliffs_delta == 0.0
        assert result.verdict is Verdict.INCONCLUSIVE

    def test_percent_helper_matches_the_delta(self):
        baseline = [10.0] * 7
        variant = [11.0] * 7
        result = compare(baseline, variant, seed=1)
        assert result.percent == pytest.approx(10.0)

    def test_rejects_empty_samples(self):
        with pytest.raises(ValueError, match="non-empty"):
            compare([], [1.0])


class TestRunsNeeded:
    def test_none_when_already_resolved(self):
        baseline = [20.0, 20.5, 19.8, 20.2, 20.1, 19.9, 20.3]
        variant = [15.0, 15.2, 14.8, 15.1, 15.3, 14.9, 15.0]
        result = compare(baseline, variant, seed=7)
        assert runs_needed_for_resolution(result, current_runs=7) is None

    def test_suggests_more_runs_when_the_gap_is_resolvable(self):
        # A 4% effect against a ±2% ROPE, with an interval of roughly
        # [1.0%, 7.2%] that straddles the boundary. Moderately more data would
        # settle it, so the operator is told how much.
        baseline = [100.0, 95.1, 104.9, 97.9, 102.1, 100.0, 100.0]
        variant = [104.0, 99.1, 108.9, 101.9, 106.1, 104.0, 104.0]
        result = compare(baseline, variant, rope=0.02, seed=13)
        assert result.verdict is Verdict.INCONCLUSIVE
        needed = runs_needed_for_resolution(result, current_runs=7, max_runs=500)
        assert needed is not None
        assert 7 < needed <= 50

    def test_declines_to_suggest_runs_when_the_effect_hugs_the_rope_boundary(self):
        """Refusing to promise resolution is itself the useful answer.

        Here the effect is 2.7% against a 2% ROPE — only 0.7pp outside it —
        while run-to-run noise is around ±8.6%. Because interval width shrinks
        only as 1/sqrt(n), closing that gap projects to roughly a thousand runs.
        Telling an operator to collect ten more would be worse than useless, so
        None is returned instead.
        """
        baseline = [100.0, 90.0, 110.0, 95.0, 105.0, 98.0, 102.0]
        variant = [103.0, 88.0, 118.0, 92.0, 111.0, 99.0, 108.0]
        result = compare(baseline, variant, rope=0.02, seed=13)
        assert result.verdict is Verdict.INCONCLUSIVE
        assert runs_needed_for_resolution(result, current_runs=7, max_runs=500) is None


class TestInteraction:
    def test_zero_when_costs_are_additive(self):
        # none=10, A=+2, B=+4, A+B=+6 exactly. Interaction should be ~0.
        none = [10.0, 10.1, 9.9, 10.0, 10.0, 10.1, 9.9]
        a = [12.0, 12.1, 11.9, 12.0, 12.0, 12.1, 11.9]
        b = [14.0, 14.1, 13.9, 14.0, 14.0, 14.1, 13.9]
        ab = [16.0, 16.1, 15.9, 16.0, 16.0, 16.1, 15.9]
        term = interaction_term(none, a, b, ab, resamples=500, seed=3)
        assert term.value == pytest.approx(0.0, abs=0.01)

    def test_positive_when_mods_fight_each_other(self):
        # A+B costs far more than A and B individually predict.
        none = [10.0] * 7
        a = [12.0] * 7
        b = [14.0] * 7
        ab = [30.0] * 7
        term = interaction_term(none, a, b, ab, resamples=500, seed=3)
        # Predicted 16, actual 30 => excess 14 over a baseline of 10 => +1.4
        assert term.value == pytest.approx(1.4, abs=0.01)

    def test_negative_when_mods_share_work(self):
        none = [10.0] * 7
        a = [20.0] * 7
        b = [20.0] * 7
        ab = [22.0] * 7
        term = interaction_term(none, a, b, ab, resamples=500, seed=3)
        assert term.value < 0

    def test_rejects_a_missing_cell(self):
        with pytest.raises(ValueError, match="'ab' sample"):
            interaction_term([1.0], [1.0], [1.0], [], resamples=10)


class TestBenjaminiHochberg:
    def test_empty_input(self):
        assert benjamini_hochberg([]) == []

    def test_all_tiny_p_values_are_discoveries(self):
        assert all(benjamini_hochberg([0.0001] * 5, fdr=0.05))

    def test_all_large_p_values_are_rejected(self):
        assert not any(benjamini_hochberg([0.9, 0.8, 0.7, 0.95], fdr=0.05))

    def test_known_step_up_behaviour(self):
        # Classic worked example: with m=4 and fdr=0.05 the thresholds are
        # 0.0125, 0.025, 0.0375, 0.05. p=0.03 clears rank 3, so the step-up rule
        # accepts ranks 1..3 — including p=0.02, which fails its own rank-2
        # threshold of 0.025.
        accepted = benjamini_hochberg([0.005, 0.02, 0.03, 0.6], fdr=0.05)
        assert accepted == [True, True, True, False]

    def test_preserves_input_order(self):
        accepted = benjamini_hochberg([0.6, 0.005], fdr=0.05)
        assert accepted == [False, True]

    def test_rejects_invalid_fdr(self):
        with pytest.raises(ValueError, match="fdr must be"):
            benjamini_hochberg([0.01], fdr=1.5)


def test_bootstrap_works_on_skewed_data():
    """Frametime distributions are right-skewed; the interval must still hold.

    A normal-theory interval would be symmetric and would misplace the bound on
    data shaped like this, which is the reason for bootstrapping at all.
    """
    samples = [16.6] * 90 + [33.2] * 8 + [120.0, 250.0]
    interval = bootstrap_ci(samples, resamples=3000, seed=17)
    observed_mean = sum(samples) / len(samples)
    assert interval.low <= observed_mean <= interval.high
    # Right skew should push the upper bound further from the mean than the lower.
    assert (interval.high - observed_mean) > (observed_mean - interval.low)


def test_fps_conversion_is_not_a_harmonic_mean_trap():
    """Averaging FPS per second differs from converting a frametime mean.

    Half the frames at 10 ms and half at 50 ms: the correct average FPS is
    1000/30 = 33.3, while averaging the two FPS figures (100 and 20) gives 60 —
    nearly double, and wrong in the flattering direction. mcbench always takes
    the former route.
    """
    frametimes = [10.0] * 500 + [50.0] * 500
    correct = 1000.0 / (sum(frametimes) / len(frametimes))
    naive = (100.0 + 20.0) / 2
    assert correct == pytest.approx(33.333, abs=0.01)
    assert not math.isclose(correct, naive, rel_tol=0.1)
