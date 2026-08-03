"""Tests for the statistical oracle behind culprit isolation.

The measurement function is injected, so the whole oracle — interleaving,
verdict mapping, failure handling — is verified without launching a game.
"""

from __future__ import annotations

import random

import pytest

from mcbench.diagnose import Outcome, isolate
from mcbench.runner.oracle import BenchmarkOracle


def frametime_model(culprits: set[str], *, penalty: float = 0.25, noise: float = 0.01):
    """A measurement stand-in: frametime rises when every culprit is present.

    ``noise`` is the run-to-run spread. Keeping it well below the penalty makes
    the effect resolvable, which is what lets these tests assert on verdicts
    rather than on luck.
    """
    rng = random.Random(1234)

    def measure(mods, replicate):
        base = 16.7
        if culprits and culprits <= set(mods):
            base *= 1 + penalty
        return base * (1 + rng.gauss(0, noise))

    return measure


class TestVerdictMapping:
    def test_a_large_regression_is_detected(self):
        oracle = BenchmarkOracle(frametime_model({"bad"}), runs_per_probe=5)
        assert oracle(["bad", "other"]) is Outcome.REGRESSION

    def test_a_clean_subset_is_clean(self):
        oracle = BenchmarkOracle(frametime_model({"bad"}), runs_per_probe=5)
        assert oracle(["other"]) is Outcome.CLEAN

    def test_a_difference_inside_the_rope_is_clean(self):
        """A real but irrelevant difference does not reproduce the regression.

        Treating it as one would make the search chase noise-sized effects and
        never converge.
        """
        oracle = BenchmarkOracle(
            frametime_model({"tiny"}, penalty=0.005, noise=0.001),
            runs_per_probe=6,
            rope=0.02,
        )
        assert oracle(["tiny"]) is Outcome.CLEAN

    def test_an_improvement_is_not_a_culprit(self):
        def faster(mods, replicate):
            return 16.7 * (0.8 if "fast" in mods else 1.0)

        oracle = BenchmarkOracle(faster, runs_per_probe=5)
        assert oracle(["fast"]) is Outcome.CLEAN

    def test_a_borderline_noisy_effect_is_inconclusive(self):
        oracle = BenchmarkOracle(
            frametime_model({"maybe"}, penalty=0.03, noise=0.08),
            runs_per_probe=5,
            rope=0.02,
        )
        assert oracle(["maybe"]) is Outcome.INCONCLUSIVE

    def test_polarity_comes_from_the_metric_registry(self):
        """A throughput metric must not be read as a cost metric.

        Same data, opposite meaning: lower FPS is a regression, lower frametime
        is an improvement.
        """
        def lower_with_mod(mods, replicate):
            return 60.0 * (0.7 if "bad" in mods else 1.0)

        as_fps = BenchmarkOracle(lower_with_mod, metric="fps_avg", runs_per_probe=5)
        as_frametime = BenchmarkOracle(
            lower_with_mod, metric="frametime_mean_ms", runs_per_probe=5
        )
        assert as_fps(["bad"]) is Outcome.REGRESSION
        assert as_frametime(["bad"]) is Outcome.CLEAN


class TestInterleaving:
    def test_baseline_and_subset_alternate_within_a_probe(self):
        """The control that stops drift being attributed to a mod.

        A baseline measured once at the start would be compared against probes
        taken hours later, which is the blocked ordering the methodology rejects.
        """
        seen: list[int] = []

        def measure(mods, replicate):
            seen.append(len(mods))
            return 16.7

        oracle = BenchmarkOracle(measure, runs_per_probe=4)
        oracle(["a", "b"])

        assert len(seen) == 8, "each replicate must measure both arms"
        assert seen.count(0) == 4, "baseline arm ran every round"
        assert seen.count(2) == 4, "subset arm ran every round"

    def test_arm_order_varies_between_rounds(self):
        # A fixed order would give one arm the cooler half of every probe.
        orders: set[tuple[int, ...]] = set()

        def measure(mods, replicate):
            measure.round_calls.append(len(mods))
            return 16.7

        measure.round_calls = []
        oracle = BenchmarkOracle(measure, runs_per_probe=12, seed=3)
        oracle(["a"])

        calls = measure.round_calls
        for index in range(0, len(calls), 2):
            orders.add(tuple(calls[index : index + 2]))
        assert len(orders) > 1

    def test_sequential_mode_measures_the_baseline_once(self):
        calls: list[int] = []

        def measure(mods, replicate):
            calls.append(len(mods))
            return 16.7

        oracle = BenchmarkOracle(measure, runs_per_probe=4, interleave_baseline=False)
        oracle(["a"])
        oracle(["b"])
        # 4 baseline runs total, then 4 per subset.
        assert calls.count(0) == 4

    def test_total_runs_reports_the_real_cost(self):
        oracle = BenchmarkOracle(frametime_model({"x"}), runs_per_probe=5)
        oracle(["x"])
        assert oracle.total_runs == 10


class TestFailureHandling:
    def test_failed_runs_are_dropped_not_substituted(self):
        def flaky(mods, replicate):
            return None if replicate == 0 else 16.7

        oracle = BenchmarkOracle(flaky, runs_per_probe=5)
        oracle(["a"])
        record = oracle.records[0]
        assert len(record.baseline_values) == 4
        assert len(record.subset_values) == 4

    def test_a_probe_with_no_usable_runs_is_inconclusive(self):
        """Never 'clean'.

        Reporting clean would exonerate a mod on the strength of a measurement
        that failed entirely.
        """
        oracle = BenchmarkOracle(lambda mods, replicate: None, runs_per_probe=5)
        assert oracle(["a"]) is Outcome.INCONCLUSIVE
        assert "insufficient successful runs" in oracle.records[0].note

    def test_rejects_an_unknown_metric(self):
        with pytest.raises(ValueError, match="unknown metric"):
            BenchmarkOracle(lambda m, r: 1.0, metric="vibes")

    def test_rejects_too_few_runs_to_estimate_variance(self):
        with pytest.raises(ValueError, match="at least 2"):
            BenchmarkOracle(lambda m, r: 1.0, runs_per_probe=1)


class TestEndToEndIsolation:
    def test_isolates_a_single_culprit_through_the_real_oracle(self):
        mods = [f"mod{i}" for i in range(8)]
        oracle = BenchmarkOracle(
            frametime_model({"mod5"}, penalty=0.30, noise=0.01), runs_per_probe=5
        )
        result = isolate(mods, oracle, max_probes=60)
        assert result.converged
        assert result.culprits == ("mod5",)

    def test_isolates_an_interacting_pair_through_the_real_oracle(self):
        """The full stack on the case that matters most.

        Neither mod is individually slow, so leave-one-out and bisection both
        miss it; only the pair together regresses.
        """
        mods = [f"mod{i}" for i in range(8)]
        oracle = BenchmarkOracle(
            frametime_model({"mod1", "mod6"}, penalty=0.35, noise=0.01),
            runs_per_probe=5,
        )
        result = isolate(mods, oracle, max_probes=80)
        assert result.converged
        assert set(result.culprits) == {"mod1", "mod6"}
        assert result.is_interaction

    def test_audit_records_every_probe_with_its_numbers(self):
        mods = [f"mod{i}" for i in range(4)]
        oracle = BenchmarkOracle(
            frametime_model({"mod2"}, penalty=0.30, noise=0.01), runs_per_probe=5
        )
        isolate(mods, oracle, max_probes=40)
        audit = oracle.audit()
        assert audit
        for entry in audit:
            assert "outcome" in entry and "ci" in entry
            assert entry["baseline_runs"] > 0
