"""Aggregation across runs, and report rendering.

This is where per-run metrics become the statements mcbench is willing to make:
a cell summary with confidence intervals, a comparison against the baseline with
a ROPE verdict, and — where the suite declares one — an interaction term.

The rendering deliberately shows uncertainty everywhere and never presents a
bare point estimate. A number without an interval invites exactly the false
confidence this project exists to correct.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Iterable, Sequence

from .metrics import METRICS, RunFlag, RunMetrics
from .planner import Cell
from .stats import (
    Comparison,
    DEFAULT_ROPE,
    Estimate,
    OutlierReport,
    Verdict,
    compare,
    estimate,
    interaction_term,
    reject_outlying_runs,
    runs_needed_for_resolution,
)

__all__ = [
    "CellResult",
    "MetricComparison",
    "SuiteResult",
    "aggregate_cell",
    "compare_to_baseline",
    "render_markdown",
    "render_json",
]

VERDICT_MARK = {
    Verdict.IMPROVEMENT: "✅ improvement",
    Verdict.REGRESSION: "🔴 regression",
    Verdict.EQUIVALENT: "⚪ equivalent",
    Verdict.INCONCLUSIVE: "❔ inconclusive",
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
    runs_total: int = 0
    runs_admissible: int = 0

    @property
    def unstable(self) -> bool:
        return any(r.unstable for r in self.outliers.values())

    @property
    def trustworthy(self) -> bool:
        return self.runs_admissible >= 5 and not self.unstable


@dataclass
class MetricComparison:
    """One metric compared between baseline and a variant."""

    metric: str
    variant: str
    scenario: str
    comparison: Comparison
    additional_runs_needed: int | None = None


@dataclass
class SuiteResult:
    """Everything a suite produced, ready to render."""

    suite_name: str
    baseline: str
    cells: dict[Cell, CellResult] = field(default_factory=dict)
    comparisons: list[MetricComparison] = field(default_factory=list)
    interactions: list[dict[str, Any]] = field(default_factory=list)
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
) -> CellResult:
    """Aggregate a cell's runs into estimates with intervals.

    Inadmissible runs (crashed, too few samples, fingerprint mismatch) are
    dropped before anything else, and their flags are carried onto the result so
    a reader can see the cell was affected. Whole-run outlier rejection then
    operates per metric, because a run can be contaminated in one dimension —
    a stray GC storm inflating pause metrics — while remaining valid in others.
    """
    result = CellResult(cell=cell, runs_total=len(runs))

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
) -> list[MetricComparison]:
    """Compare every shared metric between a baseline cell and a variant cell."""
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
        )
        out.append(
            MetricComparison(
                metric=key,
                variant=variant.cell.variant,
                scenario=variant.cell.scenario,
                comparison=result,
                additional_runs_needed=runs_needed_for_resolution(
                    result, current_runs=len(var_values)
                ),
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
) -> dict[str, Any] | None:
    """Compute the non-additivity term for a declared mod pair.

    Returns None when any of the four cells lacks data for the metric, rather
    than reporting a term derived from a partial design.
    """
    try:
        samples = [cells[k].samples[metric] for k in (none_key, a_key, b_key, ab_key)]
    except KeyError:
        return None
    if not all(samples):
        return None

    term = interaction_term(*samples, rope=rope, seed=seed)
    if term.ci.within(-rope, rope):
        verdict = "additive"
    elif term.ci.low > rope:
        verdict = "costs more together"
    elif term.ci.high < -rope:
        verdict = "costs less together"
    else:
        verdict = "inconclusive"

    return {
        "scenario": scenario,
        "metric": metric,
        "pair": [a_key, b_key],
        "value": term.value,
        "ci": [term.ci.low, term.ci.high],
        "verdict": verdict,
    }


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
    decisive ones — a benchmark that only reports wins teaches people to trust
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
        "inside or wholly outside the region of practical equivalence."
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
        unstable = [name for name, cell in scenario_cells.items() if cell.unstable]
        if warnings or unstable:
            add("> **Caveats**")
            for warning in warnings:
                add(f"> - {warning}")
            for name in unstable:
                add(f"> - `{name}`: unstable — more than 20% of runs excluded as outliers")
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
            add("| Metric | Baseline | Variant | Change | Effect | Verdict |")
            add("|---|---|---|---|---|---|")
            for entry in entries:
                definition = METRICS[entry.metric]
                c = entry.comparison
                delta = (
                    f"{_fmt_pct(c.relative_delta.value)} "
                    f"[{_fmt_pct(c.relative_delta.ci.low)}, "
                    f"{_fmt_pct(c.relative_delta.ci.high)}]"
                )
                verdict = VERDICT_MARK[c.verdict]
                if c.verdict is Verdict.INCONCLUSIVE and entry.additional_runs_needed:
                    verdict += f" (needs ~{entry.additional_runs_needed} runs)"
                add(
                    f"| {definition.label} "
                    f"| {_fmt_estimate(c.baseline, definition.unit)} "
                    f"| {_fmt_estimate(c.variant, definition.unit)} "
                    f"| {delta} "
                    f"| {c.effect_magnitude} ({c.cliffs_delta:+.2f}) "
                    f"| {verdict} |"
                )
            add("")

    if result.interactions:
        add("## Interaction effects")
        add("")
        add(
            "Non-additivity between mod pairs. A positive term means the pair "
            "costs more together than their individual costs predict — usually "
            "contention over a shared lock, a cache one invalidates for the "
            "other, or a fast path one forces the other off."
        )
        add("")
        add("| Scenario | Pair | Metric | Interaction | 95% CI | Verdict |")
        add("|---|---|---|---|---|---|")
        for item in result.interactions:
            pair = " + ".join(item["pair"])
            ci = f"[{_fmt_pct(item['ci'][0])}, {_fmt_pct(item['ci'][1])}]"
            add(
                f"| {item['scenario']} | {pair} | {METRICS[item['metric']].label} "
                f"| {_fmt_pct(item['value'])} | {ci} | {item['verdict']} |"
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
    """Machine-readable report — the corpus ingestion format."""
    payload: dict[str, Any] = {
        "schema": "mcbench.result/1",
        "suite": result.suite_name,
        "baseline": result.baseline,
        "generated_at": result.generated_at
        or datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "provenance": result.provenance,
        "cells": [
            {
                "scenario": cell.scenario,
                "variant": cell.variant,
                "runs_total": data.runs_total,
                "runs_admissible": data.runs_admissible,
                "flags": sorted(f.value for f in data.flags),
                "unstable": data.unstable,
                "metrics": {
                    key: {
                        "value": est.value,
                        "ci": [est.ci.low, est.ci.high],
                        "n": est.n,
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
                "verdict": entry.comparison.verdict.value,
                "rope": entry.comparison.rope,
                "additional_runs_needed": entry.additional_runs_needed,
            }
            for entry in result.comparisons
        ],
        "interactions": result.interactions,
    }
    return json.dumps(payload, indent=2, sort_keys=False) + "\n"
