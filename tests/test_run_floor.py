"""The minimum-admissible-run policy, across every path that issues a verdict.

The failure being guarded against is the one where a mostly-failed experiment
produces the strongest-looking conclusion the system can express. One value
against one value is a 100% difference with a zero-width interval, and every
consumer that only checks ``verdict == "regression"`` will read it as the most
certain finding in the report.

So the floor is checked here on the baseline path, the variant path, the
interaction path, and the bisect oracle, at every surviving-run count from zero
to one below the floor.
"""

from __future__ import annotations

import pytest

from mcbench.diagnose import Outcome
from mcbench.metrics import RunFlag, RunMetrics
from mcbench.planner import Cell
from mcbench.report import (
    SuiteResult,
    aggregate_cell,
    build_interaction,
    compare_to_baseline,
    render_json,
    render_markdown,
)
from mcbench.runner.oracle import BenchmarkOracle
from mcbench.stats import MIN_ADMISSIBLE_RUNS, Verdict, compare, sufficient_runs

METRIC = "frametime_mean_ms"


def runs(values, *, flags=()):
    return [RunMetrics(values={METRIC: v}, flags=list(flags)) for v in values]


def cell(name, values, **kwargs):
    return aggregate_cell(Cell("bench", name), runs(values), **kwargs)


class TestPolicyConstant:
    def test_the_floor_is_five(self):
        assert MIN_ADMISSIBLE_RUNS == 5

    def test_sufficient_runs_requires_every_arm(self):
        five = [1.0] * 5
        assert sufficient_runs(five, five)
        assert not sufficient_runs(five, [1.0] * 4)
        assert not sufficient_runs([], five)


class TestCompare:
    @pytest.mark.parametrize("n", [1, 2, 3, 4])
    def test_no_decisive_verdict_below_the_floor(self, n):
        baseline = [10.0 + i * 0.01 for i in range(n)]
        variant = [20.0 + i * 0.01 for i in range(n)]
        result = compare(baseline, variant)
        assert result.verdict is Verdict.INSUFFICIENT_DATA
        assert not result.decisive
        assert result.underpowered

    def test_the_reproduction_from_the_issue(self):
        """Baseline [10.0] against variant [20.0]."""
        result = compare([10.0], [20.0])
        assert result.verdict is Verdict.INSUFFICIENT_DATA
        # The degenerate interval is still computed — a reader may look at it —
        # but it is no longer allowed to carry a verdict.
        assert result.relative_delta.ci.width == 0.0

    def test_a_decisive_verdict_returns_at_the_floor(self):
        baseline = [10.0 + i * 0.01 for i in range(MIN_ADMISSIBLE_RUNS)]
        variant = [20.0 + i * 0.01 for i in range(MIN_ADMISSIBLE_RUNS)]
        assert compare(baseline, variant).verdict is Verdict.REGRESSION

    def test_an_explicit_opt_out_is_available_and_unused_by_default(self):
        assert compare([10.0], [20.0], min_runs=1).verdict is Verdict.REGRESSION


class TestReportPaths:
    @pytest.mark.parametrize("n", [1, 2, 3, 4])
    def test_a_short_variant_suppresses_the_verdict(self, n):
        baseline = cell("baseline", [10.0 + i * 0.01 for i in range(7)])
        variant = cell("mod", [20.0 + i * 0.01 for i in range(n)])
        [entry] = compare_to_baseline(baseline, variant, metrics=[METRIC])
        assert entry.verdict is Verdict.INSUFFICIENT_DATA
        assert entry.suppressed_reason
        assert entry.additional_runs_needed == MIN_ADMISSIBLE_RUNS

    @pytest.mark.parametrize("n", [1, 2, 3, 4])
    def test_a_short_baseline_suppresses_the_verdict(self, n):
        baseline = cell("baseline", [10.0 + i * 0.01 for i in range(n)])
        variant = cell("mod", [20.0 + i * 0.01 for i in range(7)])
        [entry] = compare_to_baseline(baseline, variant, metrics=[METRIC])
        assert entry.verdict is Verdict.INSUFFICIENT_DATA
        assert "baseline" in entry.suppressed_reason

    def test_inadmissible_runs_do_not_count_toward_the_floor(self):
        """A crashed run is present in the document and absent from the evidence."""
        good = runs([10.0 + i * 0.01 for i in range(3)])
        crashed = runs([10.0, 10.0, 10.0, 10.0], flags=[RunFlag.CRASHED])
        baseline = aggregate_cell(Cell("bench", "baseline"), good + crashed)
        assert baseline.runs_total == 7
        assert baseline.runs_admissible == 3
        assert baseline.retained(METRIC) == 3
        assert not baseline.trustworthy
        assert baseline.untrustworthy_reason(METRIC)

    def test_the_markdown_report_does_not_print_a_withheld_interval(self):
        baseline = cell("baseline", [10.0 + i * 0.01 for i in range(7)])
        variant = cell("mod", [20.0])
        result = SuiteResult(suite_name="s", baseline="baseline")
        result.cells = {baseline.cell: baseline, variant.cell: variant}
        result.comparisons = compare_to_baseline(baseline, variant, metrics=[METRIC])
        text = render_markdown(result)
        assert "insufficient data" in text
        assert "+100" not in text
        assert "Verdicts withheld" in text

    def test_the_json_report_names_the_policy_and_the_counts(self):
        baseline = cell("baseline", [10.0 + i * 0.01 for i in range(7)])
        variant = cell("mod", [20.0, 20.1])
        result = SuiteResult(suite_name="s", baseline="baseline")
        result.cells = {baseline.cell: baseline, variant.cell: variant}
        result.comparisons = compare_to_baseline(baseline, variant, metrics=[METRIC])

        import json

        payload = json.loads(render_json(result))
        assert payload["policy"]["min_admissible_runs"] == MIN_ADMISSIBLE_RUNS
        [comparison] = payload["comparisons"]
        assert comparison["verdict"] == "insufficient_data"
        assert comparison["n_variant"] == 2
        assert comparison["suppressed_reason"]


class TestInteractionPath:
    @pytest.mark.parametrize("short", ["none", "a", "b", "ab"])
    def test_any_short_arm_withholds_the_interaction_verdict(self, short):
        def values(base):
            return [base + i * 0.01 for i in range(7)]

        cells = {
            "none": cell("none", values(10.0)),
            "a": cell("a", values(11.0)),
            "b": cell("b", values(11.0)),
            "ab": cell("ab", values(20.0)),
        }
        cells[short] = cell(short, [cells[short].samples[METRIC][0]] * 4)

        item = build_interaction(
            "bench", METRIC, cells,
            none_key="none", a_key="a", b_key="b", ab_key="ab",
        )
        assert item is not None
        assert item["verdict"] == "insufficient_data"

    def test_a_full_design_still_reports_non_additivity(self):
        def values(base):
            return [base + i * 0.01 for i in range(7)]

        cells = {
            "none": cell("none", values(10.0)),
            "a": cell("a", values(11.0)),
            "b": cell("b", values(11.0)),
            "ab": cell("ab", values(20.0)),
        }
        item = build_interaction(
            "bench", METRIC, cells,
            none_key="none", a_key="a", b_key="b", ab_key="ab",
        )
        assert item["verdict"] == "costs more together"

    def test_the_verdict_is_read_against_the_metric_direction(self):
        """The same positive term means opposite things on the two metrics.

        On frametime a positive non-additivity is the pair costing more; on FPS
        the identical term is the pair doing better than additive. Wording one as
        the other inverts the headline claim of any report that has an
        interaction in it.
        """
        def rising(base):
            return [base + i * 0.01 for i in range(7)]

        cells = {
            name: cell(name, rising(base))
            for name, base in (("none", 10.0), ("a", 11.0), ("b", 11.0),
                               ("ab", 20.0))
        }
        cost = build_interaction(
            "bench", "frametime_mean_ms", cells,
            none_key="none", a_key="a", b_key="b", ab_key="ab",
        )

        throughput_cells = {
            name: aggregate_cell(
                Cell("bench", name),
                [RunMetrics(values={"fps_avg": v}) for v in rising(base)],
            )
            for name, base in (("none", 10.0), ("a", 11.0), ("b", 11.0),
                               ("ab", 20.0))
        }
        gain = build_interaction(
            "bench", "fps_avg", throughput_cells,
            none_key="none", a_key="a", b_key="b", ab_key="ab",
        )

        # Same sign, opposite meaning.
        assert cost["value"] > 0 and gain["value"] > 0
        assert cost["verdict"] == "costs more together"
        assert gain["verdict"] == "helps more together"


class TestOraclePath:
    @pytest.mark.parametrize("survivors", [0, 1, 2, 3, 4])
    def test_the_oracle_declines_below_the_floor(self, survivors):
        def flaky(mods, replicate):
            if replicate >= survivors:
                return None
            return 20.0 if mods else 10.0

        oracle = BenchmarkOracle(flaky, runs_per_probe=7)
        outcome = oracle(["suspect"])
        assert outcome is not Outcome.REGRESSION
        assert outcome is not Outcome.CLEAN
        record = oracle.records[0]
        assert len(record.subset_values) == survivors
        assert not record.usable

    def test_the_oracle_answers_at_the_floor(self):
        def measure(mods, replicate):
            return (20.0 if mods else 10.0) + replicate * 0.01

        oracle = BenchmarkOracle(measure, runs_per_probe=MIN_ADMISSIBLE_RUNS)
        assert oracle(["suspect"]) is Outcome.REGRESSION
