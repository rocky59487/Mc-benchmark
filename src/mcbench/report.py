"""Aggregation across runs, and report rendering.

This is where per-run metrics become the statements mcbench is willing to make:
a cell summary with confidence intervals, a comparison against the baseline with
a ROPE verdict, and an interaction term where the suite declares one.

The rendering deliberately shows uncertainty everywhere and never presents a
bare point estimate. A number without an interval invites exactly the false
confidence this project exists to correct.
"""

from __future__ import annotations

import json
import math
from collections.abc import Iterable, Sequence
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime
from statistics import mean
from typing import Any

from .metrics import METRICS, RunFlag, RunMetrics
from .planner import Cell
from .stats import (
    DEFAULT_ROPE,
    MIN_ADMISSIBLE_RUNS,
    Comparison,
    Estimate,
    OutlierReport,
    Verdict,
    benjamini_hochberg,
    compare,
    estimate,
    interaction_term,
    reject_outlying_runs,
    runs_needed_for_resolution,
    sufficient_runs,
)

__all__ = [
    "ResultsError",
    "parse_results_document",
    "CellResult",
    "MetricComparison",
    "SuiteResult",
    "aggregate_cell",
    "compare_to_baseline",
    "render_markdown",
    "render_json",
]

class ResultsError(ValueError):
    """A results document is malformed."""


#: Runs per cell we will ingest. A corpus submission is untrusted input, and an
#: unbounded list is a cheap way to exhaust memory in whatever ingests it.
MAX_RUNS_PER_CELL = 10_000
MAX_CELLS = 5_000

#: How an attempt ended. ``failed`` runs carry no values and are excluded from
#: every estimate, but they are ingested and counted.
RUN_STATUSES = frozenset({"completed", "inadmissible", "failed"})


def parse_results_document(raw: Any) -> tuple[str, str, dict[str, list[dict[str, Any]]]]:
    """Validate a results document and return ``(suite, baseline, cells)``.

    Result documents are untrusted input: they are meant to be exchanged, and a
    public corpus would ingest them from strangers. Validating here rather than
    letting the values flow into the statistics matters for one reason above the
    rest: **a non-finite value silently poisons everything downstream**.

    JSON has no infinity, but ``1e400`` parses to one, and it then travels
    through the aggregation into a report whose JSON contains ``Infinity``: not
    valid JSON, unreadable by strict consumers, and presented as though it were a
    measurement. A string where a number belongs merely crashes, which is the
    kinder failure.

    Nothing is coerced or repaired. A malformed document is a malformed
    document, and quietly fixing one produces a result that looks valid.
    """
    if not isinstance(raw, dict):
        raise ResultsError("results must be a JSON object")

    baseline = raw.get("baseline")
    if not isinstance(baseline, str) or not baseline:
        raise ResultsError("results must name a 'baseline' variant")

    suite = raw.get("suite")
    if not isinstance(suite, str) or not suite:
        suite = "mcbench results"

    cells_raw = raw.get("cells", raw)
    if not isinstance(cells_raw, dict):
        raise ResultsError("'cells' must be an object keyed by 'scenario/variant'")
    if len(cells_raw) > MAX_CELLS:
        raise ResultsError(
            f"{len(cells_raw)} cells exceeds the {MAX_CELLS} limit"
        )

    cleaned: dict[str, list[dict[str, Any]]] = {}
    for key, runs in cells_raw.items():
        if not isinstance(key, str) or "/" not in key:
            raise ResultsError(
                f"malformed cell key {key!r}; expected 'scenario/variant'"
            )
        if not isinstance(runs, list):
            raise ResultsError(f"{key}: runs must be a list")
        if len(runs) > MAX_RUNS_PER_CELL:
            raise ResultsError(
                f"{key}: {len(runs)} runs exceeds the {MAX_RUNS_PER_CELL} limit"
            )

        checked_runs: list[dict[str, Any]] = []
        for index, run in enumerate(runs):
            where = f"{key}[{index}]"
            if not isinstance(run, dict):
                raise ResultsError(f"{where}: each run must be an object")
            values = run.get("values", {})
            if not isinstance(values, dict):
                raise ResultsError(f"{where}: 'values' must be an object")

            checked: dict[str, float] = {}
            for metric, value in values.items():
                if not isinstance(metric, str):
                    raise ResultsError(f"{where}: metric names must be strings")
                if isinstance(value, bool) or not isinstance(value, (int, float)):
                    raise ResultsError(
                        f"{where}.{metric}: expected a number, got {value!r}"
                    )
                if not math.isfinite(value):
                    raise ResultsError(
                        f"{where}.{metric}: {value} is not a finite measurement. "
                        f"JSON has no infinity, but 1e400 parses to one, and it "
                        f"would travel into a report whose JSON is then invalid."
                    )
                checked[metric] = float(value)

            flags = run.get("flags", [])
            if not isinstance(flags, list) or not all(isinstance(f, str) for f in flags):
                raise ResultsError(f"{where}: 'flags' must be a list of strings")

            status = run.get("status", "completed" if checked else "failed")
            if status not in RUN_STATUSES:
                raise ResultsError(
                    f"{where}: unknown status {status!r}; expected one of "
                    f"{sorted(RUN_STATUSES)}"
                )
            if status == "completed" and not checked:
                # Would count toward the run floor while contributing no
                # values, making a cell look better-evidenced than it is.
                raise ResultsError(
                    f"{where}: status is 'completed' but no metric values are "
                    f"present; a run that produced nothing is 'failed'"
                )

            entry: dict[str, Any] = {
                "values": checked, "flags": flags, "status": status
            }
            position = run.get("position")
            if position is not None:
                if isinstance(position, bool) or not isinstance(position, int):
                    raise ResultsError(f"{where}: 'position' must be an integer")
                entry["position"] = position
            for optional in (
                "error", "log", "exit_code", "world", "probe",
                # What the game said it was, and where that disagreed with what
                # the run recorded. The flag alone decides admissibility; these
                # are what tell a reader which field disagreed and by how much.
                "reported", "configuration_mismatches",
                # The distribution's shape, for the CDF chart and for a reader
                # who wants to redraw it rather than trust the percentiles.
                "frametime_sketch_ms",
                # Whether this run made its world or was handed one. A run
                # refused for a fingerprint nothing else shares reads as a mod
                # altering worldgen; if it is the run that generated the world,
                # it is usually generation finishing after it saved.
                "world_source",
            ):
                if optional in run:
                    entry[optional] = run[optional]
            checked_runs.append(entry)

        cleaned[key] = checked_runs

    return suite, baseline, cleaned


VERDICT_MARK = {
    Verdict.IMPROVEMENT: "✅ improvement",
    Verdict.REGRESSION: "🔴 regression",
    Verdict.EQUIVALENT: "⚪ equivalent",
    Verdict.INCONCLUSIVE: "❔ inconclusive",
    Verdict.INSUFFICIENT_DATA: "🚫 insufficient data",
}


@dataclass
class CellResult:
    """Aggregated results for one (scenario, variant) cell."""

    cell: Cell
    estimates: dict[str, Estimate] = field(default_factory=dict)
    samples: dict[str, list[float]] = field(default_factory=dict)
    """Retained per-run values per metric, kept so comparisons can resample."""
    outliers: dict[str, OutlierReport] = field(default_factory=dict)
    flags: set[RunFlag] = field(default_factory=set)
    runs_attempted: int = 0
    """Runs the plan scheduled, including ones that produced no metrics. A cell
    whose launches failed is not a smaller experiment."""
    runs_total: int = 0
    """Runs that produced a metrics document, admissible or not."""
    runs_admissible: int = 0
    runs_failed: int = 0
    """Attempts that produced no metrics: crashes, timeouts, launch failures."""

    @property
    def unstable(self) -> bool:
        return any(r.unstable for r in self.outliers.values())

    def retained(self, metric: str) -> int:
        """Values left for ``metric`` after inadmissible runs and outliers."""
        return len(self.samples.get(metric, ()))

    @property
    def trustworthy(self) -> bool:
        return self.runs_admissible >= MIN_ADMISSIBLE_RUNS and not self.unstable

    def untrustworthy_reason(self, metric: str) -> str:
        """Why this cell cannot support a decisive verdict for ``metric``.

        Returns an empty string when the cell is fine.

        Per metric, because outlier rejection is: a run spoiled by a GC storm
        loses its pause values and keeps its frametimes.
        """
        retained = self.retained(metric)
        if retained < MIN_ADMISSIBLE_RUNS:
            return (
                f"only {retained} of {self.runs_attempted or self.runs_total} "
                f"run(s) survived for this metric; {MIN_ADMISSIBLE_RUNS} are required"
            )
        report = self.outliers.get(metric)
        if report is not None and report.unstable:
            return report.instability
        return ""


@dataclass
class MetricComparison:
    """One metric compared between baseline and a variant."""

    metric: str
    variant: str
    scenario: str
    comparison: Comparison
    additional_runs_needed: int | None = None
    suppressed_reason: str = ""
    """Why a decisive verdict was withheld. The comparison is kept intact; only
    the verdict is downgraded."""
    family: str = ""
    """Which multiplicity family this test belongs to (see :func:`assign_families`)."""
    q_value: float | None = None
    """Benjamini-Hochberg adjusted p-value, once the family has been corrected."""
    fdr_verdict: Verdict | None = None
    """The verdict after FDR correction. ``None`` until correction has run."""

    @property
    def verdict(self) -> Verdict:
        """The verdict a reader should act on: FDR-adjusted where available."""
        return self.fdr_verdict or self.comparison.verdict

    @property
    def p_value(self) -> float:
        return self.comparison.p_value


@dataclass
class SuiteResult:
    """Everything a suite produced, ready to render."""

    suite_name: str
    baseline: str
    cells: dict[Cell, CellResult] = field(default_factory=dict)
    comparisons: list[MetricComparison] = field(default_factory=list)
    interactions: list[dict[str, Any]] = field(default_factory=list)
    normalised: list[dict[str, Any]] = field(default_factory=list)
    """Reference-normalised ratios (METHODOLOGY section 8), empty when the suite
    did not run the reference scenario."""
    multiplicity: dict[str, Any] = field(default_factory=dict)
    """What :func:`apply_fdr` did, empty when correction has not been applied."""
    provenance: dict[str, Any] = field(default_factory=dict)
    generated_at: str = ""

    def comparisons_for(self, scenario: str) -> list[MetricComparison]:
        return [c for c in self.comparisons if c.scenario == scenario]

    @property
    def scenarios(self) -> list[str]:
        seen: list[str] = []
        for cell in self.cells:
            if cell.scenario not in seen:
                seen.append(cell.scenario)
        return seen

    @property
    def variants(self) -> list[str]:
        seen: list[str] = []
        for cell in self.cells:
            if cell.variant not in seen:
                seen.append(cell.variant)
        return seen


def aggregate_cell(
    cell: Cell,
    runs: Sequence[RunMetrics],
    *,
    metrics: Iterable[str] | None = None,
    seed: int = 0,
    attempted: int | None = None,
    failed: int = 0,
) -> CellResult:
    """Aggregate a cell's runs into estimates with intervals.

    Inadmissible runs (crashed, too few samples, fingerprint mismatch) are
    dropped before anything else, and their flags are carried onto the result so
    a reader can see the cell was affected. Whole-run outlier rejection then
    operates per metric, because a run can be contaminated in one dimension,
    such as a stray GC storm inflating pause metrics, while valid in others.

    ``attempted`` and ``failed`` cover runs that produced no metrics and so
    never reached here, so five values out of five and five out of twelve do not
    serialise identically.
    """
    result = CellResult(
        cell=cell,
        runs_total=len(runs),
        runs_attempted=len(runs) + failed if attempted is None else attempted,
        runs_failed=failed,
    )

    admissible = [r for r in runs if r.admissible]
    result.runs_admissible = len(admissible)
    for run in runs:
        result.flags.update(run.flags)

    if not admissible:
        return result

    keys = list(metrics) if metrics is not None else sorted(
        {k for run in admissible for k in run.values}
    )

    for index, key in enumerate(keys):
        values = [run.values[key] for run in admissible if key in run.values]
        if not values:
            continue

        report = reject_outlying_runs(values)
        result.outliers[key] = report
        result.samples[key] = report.kept
        if report.kept:
            # Vary the seed per metric so independent metrics do not share a
            # bootstrap resampling pattern, which would correlate their intervals.
            result.estimates[key] = estimate(report.kept, seed=seed + index * 977)

    return result


def compare_to_baseline(
    baseline: CellResult,
    variant: CellResult,
    *,
    metrics: Iterable[str] | None = None,
    rope: float = DEFAULT_ROPE,
    seed: int = 0,
    min_runs: int = MIN_ADMISSIBLE_RUNS,
) -> list[MetricComparison]:
    """Compare every shared metric between a baseline cell and a variant cell.

    A decisive verdict needs both cells to support one. Where they cannot, from
    too few surviving runs or too many excluded as outliers, it is downgraded to
    ``insufficient_data`` with the reason recorded. Suppression rather than
    annotation, because a caveat beside a red badge is read as a regression.
    """
    if baseline.cell.scenario != variant.cell.scenario:
        raise ValueError(
            f"cannot compare across scenarios: {baseline.cell.scenario} "
            f"vs {variant.cell.scenario}"
        )

    keys = (
        list(metrics)
        if metrics is not None
        else sorted(set(baseline.samples) & set(variant.samples))
    )

    out: list[MetricComparison] = []
    for index, key in enumerate(keys):
        base_values = baseline.samples.get(key)
        var_values = variant.samples.get(key)
        if not base_values or not var_values:
            continue

        definition = METRICS.get(key)
        if definition is None:
            # Unknown metrics are skipped rather than guessed at: assuming a
            # polarity would risk reporting a regression as an improvement.
            continue

        result = compare(
            base_values,
            var_values,
            lower_is_better=definition.lower_is_better,
            rope=rope,
            seed=seed + index * 613,
            min_runs=min_runs,
        )

        # Both cells must be able to carry a verdict, not just the variant: a
        # baseline reduced to two runs cannot establish what the variant is
        # being compared against.
        reason = baseline.untrustworthy_reason(key)
        if reason:
            reason = f"baseline `{baseline.cell.variant}`: {reason}"
        else:
            reason = variant.untrustworthy_reason(key)
            if reason:
                reason = f"`{variant.cell.variant}`: {reason}"

        if reason and result.verdict.decisive:
            result = replace(result, verdict=Verdict.INSUFFICIENT_DATA)

        out.append(
            MetricComparison(
                metric=key,
                variant=variant.cell.variant,
                scenario=variant.cell.scenario,
                comparison=result,
                additional_runs_needed=runs_needed_for_resolution(
                    result, current_runs=len(var_values)
                ),
                suppressed_reason=reason,
            )
        )
    return out


def build_interaction(
    scenario: str,
    metric: str,
    cells: dict[str, CellResult],
    *,
    none_key: str,
    a_key: str,
    b_key: str,
    ab_key: str,
    rope: float = DEFAULT_ROPE,
    seed: int = 0,
    min_runs: int = MIN_ADMISSIBLE_RUNS,
) -> dict[str, Any] | None:
    """Compute the non-additivity term for a declared mod pair.

    Returns None when any of the four cells lacks data for the metric, rather
    than reporting a term derived from a partial design.

    The run floor applies to all four arms. An interaction is a difference of
    differences and so the least robust quantity in the report.
    """
    try:
        samples = [cells[k].samples[metric] for k in (none_key, a_key, b_key, ab_key)]
    except KeyError:
        return None
    if not all(samples):
        return None

    term = interaction_term(*samples, rope=rope, seed=seed)

    # The sign is in raw metric units, so its meaning depends on polarity: a
    # positive term is the pair costing more on frametime and doing better than
    # additive on FPS.
    definition = METRICS.get(metric)
    worse_when_larger = definition.lower_is_better if definition else True
    larger = "costs more together" if worse_when_larger else "helps more together"
    smaller = "costs less together" if worse_when_larger else "helps less together"

    if not sufficient_runs(*samples, min_runs=min_runs):
        verdict = Verdict.INSUFFICIENT_DATA.value
    elif term.ci.within(-rope, rope):
        verdict = "additive"
    elif term.ci.low > rope:
        verdict = larger
    elif term.ci.high < -rope:
        verdict = smaller
    else:
        verdict = "inconclusive"

    none_values, a_values, b_values, ab_values = samples
    return {
        "scenario": scenario,
        "metric": metric,
        "pair": [a_key, b_key],
        "cells": [none_key, a_key, b_key, ab_key],
        "value": term.value,
        "ci": [term.ci.low, term.ci.high],
        "n_per_arm": [len(s) for s in samples],
        "min_runs": min_runs,
        "lower_is_better": worse_when_larger,
        "verdict": verdict,
        # The four arm means the interaction was computed from. The term says
        # how far the pair lands from the additive prediction; these are what
        # the prediction was made out of, and without them the chart that plots
        # one against the other has nothing to draw.
        "absolute": {
            "none": mean(none_values),
            "a": mean(a_values),
            "b": mean(b_values),
            "ab": mean(ab_values),
        },
    }


# --------------------------------------------------------------------------
# Multiplicity control (METHODOLOGY.md section 5)
# --------------------------------------------------------------------------

#: FDR level the corpus is defined in terms of.
DEFAULT_FDR = 0.05


def family_of(comparison: MetricComparison) -> str:
    """Which hypothesis family a comparison belongs to.

    One family is one (scenario, metric): the tests sharing a baseline cell and
    a metric. Metrics are not pooled across the suite: ``frametime_mean_ms``
    and ``fps_avg`` are the same measurement twice, and correcting across
    strongly dependent tests is punitive rather than principled.
    """
    return f"{comparison.scenario}::{comparison.metric}"


def apply_fdr(
    comparisons: Sequence[MetricComparison], *, fdr: float = DEFAULT_FDR
) -> dict[str, Any]:
    """Correct each family for multiplicity, in place.

    Each comparison gains its family, its q-value, and an adjusted verdict. A
    decisive verdict that does not survive is demoted to ``inconclusive``.
    Nothing is promoted: correction only removes discoveries.

    :returns: a summary for the report's method section, since a correction nobody
        can see applied is indistinguishable from one that never ran.
    """
    families: dict[str, list[MetricComparison]] = {}
    for entry in comparisons:
        entry.family = family_of(entry)
        families.setdefault(entry.family, []).append(entry)

    summary: list[dict[str, Any]] = []
    demoted = 0
    for name, members in sorted(families.items()):
        p_values = [m.p_value for m in members]
        accepted = benjamini_hochberg(p_values, fdr=fdr)
        for member, keep in zip(members, accepted, strict=True):
            member.q_value = _adjusted_q(p_values, member.p_value)
            verdict = member.comparison.verdict
            if verdict.decisive and not keep:
                member.fdr_verdict = Verdict.INCONCLUSIVE
                demoted += 1
            else:
                member.fdr_verdict = verdict
        summary.append({
            "family": name,
            "tests": len(members),
            "discoveries": sum(1 for keep in accepted if keep),
        })

    return {"fdr": fdr, "families": summary, "demoted": demoted}


def _adjusted_q(p_values: Sequence[float], p: float) -> float:
    """Benjamini-Hochberg adjusted p-value for one member of a family.

    Step-up form: ``q_(i) = min over k >= i of (m/k) * p_(k)``, clamped to 1.
    Reported alongside the decision because a q of 0.051 and one of 0.9 are not
    the same finding.
    """
    m = len(p_values)
    if m == 0:
        return 1.0
    ordered = sorted(p_values)
    running = 1.0
    adjusted: list[float] = [1.0] * m
    for k in range(m, 0, -1):
        running = min(running, (m / k) * ordered[k - 1])
        adjusted[k - 1] = min(1.0, running)
    # Ties share a q-value; the first matching rank is the right one to report.
    for value, q in zip(ordered, adjusted, strict=True):
        if value == p:
            return q
    return 1.0


# --------------------------------------------------------------------------
# Hardware normalisation (METHODOLOGY.md section 8)
# --------------------------------------------------------------------------

#: The mod-free scenario every suite runs so results can be normalised.
REFERENCE_SCENARIO = "reference-hardware-baseline"


def reference_ratios(
    cells: dict[Cell, CellResult],
    *,
    baseline: str,
    reference_scenario: str = REFERENCE_SCENARIO,
) -> list[dict[str, Any]]:
    """Express every cell's metrics as a ratio to the same-session reference.

    METHODOLOGY section 8: absolute figures do not compose across machines, so
    the corpus is defined in ratios to a fixed mod-free scenario measured in the
    same session.

    Empty when the suite did not run the reference scenario, because there is nothing
    to normalise against, and a made-up denominator would look comparable.
    """
    reference = cells.get(Cell(reference_scenario, baseline))
    if reference is None or not reference.estimates:
        return []

    out: list[dict[str, Any]] = []
    for cell, data in cells.items():
        if cell.scenario == reference_scenario:
            continue
        for key, est in sorted(data.estimates.items()):
            denominator = reference.estimates.get(key)
            if denominator is None or denominator.value == 0:
                continue
            out.append({
                "scenario": cell.scenario,
                "variant": cell.variant,
                "metric": key,
                "ratio": est.value / denominator.value,
                "ci": [
                    est.ci.low / denominator.value,
                    est.ci.high / denominator.value,
                ],
                "reference_value": denominator.value,
                "reference_cell": str(Cell(reference_scenario, baseline)),
            })
    return out


# --------------------------------------------------------------------------
# Rendering
# --------------------------------------------------------------------------


def _fmt_pct(value: float) -> str:
    return f"{value * 100:+.2f}%"


def _fmt_estimate(est: Estimate, unit: str) -> str:
    return f"{est.value:.3g} {unit} [{est.ci.low:.3g}, {est.ci.high:.3g}]"


def render_markdown(result: SuiteResult) -> str:
    """Human-readable report.

    Every figure carries its interval, every verdict names the ROPE it was
    judged against, and inconclusive results are shown as prominently as
    decisive ones, because a benchmark that only reports wins teaches people to trust
    it when it should not be trusted.
    """
    lines: list[str] = []
    add = lines.append

    add(f"# {result.suite_name}")
    add("")
    add(f"Generated {result.generated_at or 'unknown'} · baseline `{result.baseline}`")
    add("")

    prov = result.provenance
    if prov:
        add("## Provenance")
        add("")
        add("| Field | Value |")
        add("|---|---|")
        for key in sorted(prov):
            add(f"| {key} | `{prov[key]}` |")
        add("")

    add("## Method")
    add("")
    add(
        "Metrics are aggregated in the time domain; FPS figures are derived as "
        "`1000 / mean(frametime_ms)`, never by averaging per-second FPS. "
        "\"1% low\" is the mean of the worst 1% of frametimes, not the 99th "
        "percentile. Intervals are 95% percentile bootstrap. A verdict is "
        "issued only when the interval on the relative change lies wholly "
        "inside or wholly outside the region of practical equivalence, and "
        f"only when at least {MIN_ADMISSIBLE_RUNS} runs survived in both arms."
    )
    add("")

    if result.multiplicity:
        families = result.multiplicity.get("families", [])
        tests = sum(f["tests"] for f in families)
        add(
            f"Multiple comparisons: Benjamini-Hochberg control at FDR "
            f"{result.multiplicity.get('fdr', DEFAULT_FDR):g} across "
            f"{len(families)} famil{'y' if len(families) == 1 else 'ies'} "
            f"({tests} test{'' if tests == 1 else 's'}); a family is one "
            f"scenario and metric across the variants sharing a baseline. "
            f"{result.multiplicity.get('demoted', 0)} decisive verdict(s) did "
            f"not survive correction and are reported as inconclusive."
        )
        add("")

    for scenario in result.scenarios:
        add(f"## {scenario}")
        add("")

        scenario_cells = {
            c.variant: r for c, r in result.cells.items() if c.scenario == scenario
        }

        warnings = [
            f"`{name}`: {', '.join(sorted(f.value for f in cell.flags))}"
            for name, cell in scenario_cells.items()
            if cell.flags
        ]
        # The reason is read off the report that set the flag rather than
        # assumed. Two different conditions raise it, and stating the wrong one
        # tells a reader their runs were discarded when none of them were.
        unstable = [
            (name, reason)
            for name, cell in scenario_cells.items()
            if cell.unstable
            for reason in (
                next(
                    (r.instability for r in cell.outliers.values() if r.unstable), ""
                ),
            )
        ]
        if warnings or unstable:
            add("> **Caveats**")
            for warning in warnings:
                add(f"> - {warning}")
            for name, reason in unstable:
                add(f"> - `{name}`: {reason}")
            add("")

        comparisons = result.comparisons_for(scenario)
        if not comparisons:
            add("_No comparisons available._")
            add("")
            continue

        by_variant: dict[str, list[MetricComparison]] = {}
        for comparison in comparisons:
            by_variant.setdefault(comparison.variant, []).append(comparison)

        for variant, entries in by_variant.items():
            add(f"### {variant} vs {result.baseline}")
            add("")
            add("| Metric | Baseline | Variant | Change | Effect | n | Verdict |")
            add("|---|---|---|---|---|---|---|")
            suppressed: list[str] = []
            for entry in entries:
                definition = METRICS[entry.metric]
                c = entry.comparison
                shown = entry.verdict
                if shown is Verdict.INSUFFICIENT_DATA:
                    # Deliberately not rendered: an interval computed from one
                    # or two runs looks tighter the less data produced it, and
                    # printing it beside a caveat is how it gets quoted anyway.
                    delta = "—"
                else:
                    delta = (
                        f"{_fmt_pct(c.relative_delta.value)} "
                        f"[{_fmt_pct(c.relative_delta.ci.low)}, "
                        f"{_fmt_pct(c.relative_delta.ci.high)}]"
                    )
                verdict = VERDICT_MARK[shown]
                if shown is Verdict.INCONCLUSIVE and entry.additional_runs_needed:
                    verdict += f" (needs ~{entry.additional_runs_needed} runs)"
                if shown is Verdict.INSUFFICIENT_DATA:
                    verdict += f" (needs {MIN_ADMISSIBLE_RUNS} runs per arm)"
                if entry.q_value is not None and c.verdict.decisive:
                    verdict += f" · q={entry.q_value:.3f}"
                if entry.suppressed_reason:
                    suppressed.append(
                        f"{definition.label}: {entry.suppressed_reason}"
                    )
                add(
                    f"| {definition.label} "
                    f"| {_fmt_estimate(c.baseline, definition.unit)} "
                    f"| {_fmt_estimate(c.variant, definition.unit)} "
                    f"| {delta} "
                    f"| {c.effect_magnitude} ({c.cliffs_delta:+.2f}) "
                    f"| {c.baseline.n} vs {c.variant.n} "
                    f"| {verdict} |"
                )
            add("")
            if suppressed:
                add("> **Verdicts withheld**")
                for line in suppressed:
                    add(f"> - {line}")
                add("")

    if result.interactions:
        add("## Interaction effects")
        add("")
        add(
            "Non-additivity between mod pairs. The term is in raw metric units, "
            "so its sign is read against the metric's direction: on a cost "
            "metric a positive term means the pair costs more together than "
            "their individual costs predict, usually contention over a shared "
            "lock, a cache one invalidates for the other, or a fast path one "
            "forces the other off. The verdict column states the reading."
        )
        add("")
        add("| Scenario | Pair | Metric | Interaction | 95% CI | n per arm | Verdict |")
        add("|---|---|---|---|---|---|---|")
        for item in result.interactions:
            pair = " + ".join(item["pair"])
            insufficient = item["verdict"] == Verdict.INSUFFICIENT_DATA.value
            ci = (
                "—"
                if insufficient
                else f"[{_fmt_pct(item['ci'][0])}, {_fmt_pct(item['ci'][1])}]"
            )
            value = "—" if insufficient else _fmt_pct(item["value"])
            counts = "/".join(str(n) for n in item.get("n_per_arm", []))
            add(
                f"| {item['scenario']} | {pair} | {METRICS[item['metric']].label} "
                f"| {value} | {ci} | {counts} | {item['verdict']} |"
            )
        add("")

    if result.normalised:
        add("## Reference-normalised ratios")
        add("")
        add(
            f"Each figure divided by the same metric from "
            f"`{REFERENCE_SCENARIO}/{result.baseline}`, measured on this "
            f"machine in this session. Ratios are what compose across "
            f"contributors; absolute figures do not (METHODOLOGY section 8)."
        )
        add("")
        add("| Scenario | Variant | Metric | Ratio to reference | 95% CI |")
        add("|---|---|---|---|---|")
        for item in result.normalised:
            ci = f"[{item['ci'][0]:.3f}, {item['ci'][1]:.3f}]"
            add(
                f"| {item['scenario']} | {item['variant']} "
                f"| {METRICS[item['metric']].label} "
                f"| {item['ratio']:.3f}× | {ci} |"
            )
        add("")

    add("---")
    add("")
    add(
        "Absolute figures are comparable only within this session on this "
        "machine. For cross-machine comparison use the ratios to "
        "`reference-hardware-baseline`. See docs/METHODOLOGY.md section 8."
    )

    return "\n".join(lines) + "\n"


def render_json(result: SuiteResult) -> str:
    """Machine-readable report: the corpus ingestion format."""
    payload: dict[str, Any] = {
        "schema": "mcbench.result/1",
        "suite": result.suite_name,
        "baseline": result.baseline,
        "generated_at": result.generated_at
        or datetime.now(UTC).isoformat(timespec="seconds"),
        "provenance": result.provenance,
        "policy": {
            "min_admissible_runs": MIN_ADMISSIBLE_RUNS,
            "fdr": result.multiplicity.get("fdr") if result.multiplicity else None,
        },
        "cells": [
            {
                "scenario": cell.scenario,
                "variant": cell.variant,
                "runs_attempted": data.runs_attempted,
                "runs_total": data.runs_total,
                "runs_admissible": data.runs_admissible,
                "runs_failed": data.runs_failed,
                "flags": sorted(f.value for f in data.flags),
                "unstable": data.unstable,
                "trustworthy": data.trustworthy,
                "metrics": {
                    key: {
                        "value": est.value,
                        "ci": [est.ci.low, est.ci.high],
                        "n": est.n,
                        "retained": data.retained(key),
                        "excluded_runs": len(data.outliers[key].excluded)
                        if key in data.outliers
                        else 0,
                    }
                    for key, est in data.estimates.items()
                },
            }
            for cell, data in result.cells.items()
        ],
        "comparisons": [
            {
                "scenario": entry.scenario,
                "variant": entry.variant,
                "metric": entry.metric,
                "relative_delta": entry.comparison.relative_delta.value,
                "ci": [
                    entry.comparison.relative_delta.ci.low,
                    entry.comparison.relative_delta.ci.high,
                ],
                "cliffs_delta": entry.comparison.cliffs_delta,
                "effect_magnitude": entry.comparison.effect_magnitude,
                # Both are serialised: the raw ROPE verdict and the one a reader
                # should act on. A consumer that only ever sees the corrected
                # value cannot tell whether correction ran at all.
                "verdict_raw": entry.comparison.verdict.value,
                "verdict": entry.verdict.value,
                "p_value": entry.p_value,
                "q_value": entry.q_value,
                "family": entry.family,
                "n_baseline": entry.comparison.baseline.n,
                "n_variant": entry.comparison.variant.n,
                "min_runs": entry.comparison.min_runs,
                "suppressed_reason": entry.suppressed_reason,
                "rope": entry.comparison.rope,
                "additional_runs_needed": entry.additional_runs_needed,
            }
            for entry in result.comparisons
        ],
        "interactions": result.interactions,
        "normalised": result.normalised,
        "multiplicity": result.multiplicity,
    }
    return json.dumps(payload, indent=2, sort_keys=False) + "\n"
