"""Tabular export: CSV, TSV, Markdown, and HTML.

One table model rendered four ways, so the numbers in a spreadsheet always match
the numbers in the report. Every export carries its uncertainty columns: a CSV
of bare point estimates would let the interval get lost the moment someone
pastes it somewhere else, which is precisely how over-confident benchmark claims
propagate.
"""

from __future__ import annotations

import csv
import html
import io
from collections.abc import Iterable, Sequence
from dataclasses import dataclass, field
from typing import Any

from ..metrics import METRICS
from ..report import SuiteResult

__all__ = [
    "Table",
    "cells_table",
    "comparisons_table",
    "interactions_table",
    "runs_table",
    "tables_for",
    "to_csv",
    "to_tsv",
    "to_markdown",
    "to_html",
]


@dataclass
class Table:
    """A rendered-format-agnostic table."""

    name: str
    columns: list[str]
    rows: list[list[Any]] = field(default_factory=list)
    caption: str = ""

    @property
    def slug(self) -> str:
        return self.name.lower().replace(" ", "-").replace("/", "-")


def _num(value: Any, digits: int = 4) -> Any:
    """Round floats for display without turning them into strings.

    Keeping them numeric matters for CSV: a spreadsheet must be able to sort and
    chart these columns without the user cleaning them first.
    """
    if isinstance(value, float):
        return round(value, digits)
    return value


def cells_table(result: SuiteResult) -> Table:
    """Per-cell aggregated metrics with intervals."""
    table = Table(
        name="cells",
        columns=[
            "scenario", "variant", "metric", "unit", "value",
            "ci_low", "ci_high", "n_runs", "runs_excluded",
            "runs_attempted", "runs_completed", "runs_admissible", "runs_failed",
            "unstable", "trustworthy", "flags",
        ],
        caption="Aggregated metrics per (scenario, variant), with 95% bootstrap "
                "confidence intervals. The run counts are separate on purpose: "
                "a cell of five values out of five attempts and one of five out "
                "of twelve are not the same experiment.",
    )
    for cell, data in result.cells.items():
        for key, estimate in sorted(data.estimates.items()):
            definition = METRICS.get(key)
            excluded = len(data.outliers[key].excluded) if key in data.outliers else 0
            table.rows.append([
                cell.scenario, cell.variant, key,
                definition.unit if definition else "",
                _num(estimate.value), _num(estimate.ci.low), _num(estimate.ci.high),
                estimate.n, excluded,
                data.runs_attempted, data.runs_total, data.runs_admissible,
                data.runs_failed, data.unstable, data.trustworthy,
                ";".join(sorted(f.value for f in data.flags)),
            ])
    return table


def comparisons_table(result: SuiteResult) -> Table:
    """Variant-vs-baseline comparisons with verdicts."""
    table = Table(
        name="comparisons",
        columns=[
            "scenario", "variant", "baseline", "metric", "unit", "direction",
            "baseline_value", "variant_value", "relative_delta_pct",
            "delta_ci_low_pct", "delta_ci_high_pct", "cliffs_delta",
            "effect_magnitude", "verdict", "verdict_raw", "p_value", "q_value",
            "family", "n_baseline", "n_variant", "suppressed_reason",
            "rope_pct", "additional_runs_needed",
        ],
        caption="Each variant against the baseline. A verdict is issued only "
                "when the delta interval lies wholly inside or wholly outside "
                "the region of practical equivalence, and only when enough runs "
                "survived in both arms. 'verdict' is the one to act on; "
                "'verdict_raw' is before multiplicity correction.",
    )
    for entry in result.comparisons:
        definition = METRICS.get(entry.metric)
        c = entry.comparison
        table.rows.append([
            entry.scenario, entry.variant, result.baseline, entry.metric,
            definition.unit if definition else "",
            "lower_is_better" if c.lower_is_better else "higher_is_better",
            _num(c.baseline.value), _num(c.variant.value),
            _num(c.relative_delta.value * 100, 3),
            _num(c.relative_delta.ci.low * 100, 3),
            _num(c.relative_delta.ci.high * 100, 3),
            _num(c.cliffs_delta, 3), c.effect_magnitude,
            entry.verdict.value, c.verdict.value,
            _num(entry.p_value, 4),
            _num(entry.q_value, 4) if entry.q_value is not None else "",
            entry.family, c.baseline.n, c.variant.n, entry.suppressed_reason,
            _num(c.rope * 100, 2), entry.additional_runs_needed or "",
        ])
    return table


def interactions_table(result: SuiteResult) -> Table:
    """Non-additivity between declared mod pairs."""
    table = Table(
        name="interactions",
        columns=["scenario", "pair", "metric", "lower_is_better",
                 "interaction_pct", "ci_low_pct", "ci_high_pct",
                 "n_per_arm", "verdict"],
        caption="The term is in raw metric units, so its sign reads against "
                "the metric's direction — the verdict column states which. "
                "Zero means the two mods' effects simply add.",
    )
    for item in result.interactions:
        table.rows.append([
            item["scenario"], " + ".join(item["pair"]), item["metric"],
            item.get("lower_is_better", True),
            _num(item["value"] * 100, 3),
            _num(item["ci"][0] * 100, 3), _num(item["ci"][1] * 100, 3),
            "/".join(str(n) for n in item.get("n_per_arm", [])),
            item["verdict"],
        ])
    return table


def runs_table(
    raw_runs: dict[str, Sequence[dict[str, Any]]] | None,
) -> Table:
    """Every individual run, unaggregated.

    Included so a reader can redo the analysis rather than take ours on trust.
    A benchmark that publishes only summaries cannot be independently checked,
    and this project's claim to authority rests on being checkable.
    """
    table = Table(
        name="runs",
        columns=["scenario", "variant", "run_index", "position", "metric", "value"],
        caption="Raw per-run values before aggregation, for independent re-analysis.",
    )
    if not raw_runs:
        return table

    for cell_key, runs in raw_runs.items():
        scenario, _, variant = cell_key.partition("/")
        for run_index, run in enumerate(runs):
            values = run.get("values", run) if isinstance(run, dict) else {}
            position = run.get("position", "") if isinstance(run, dict) else ""
            for metric, value in sorted(values.items()):
                table.rows.append([
                    scenario, variant, run_index, position, metric, _num(value),
                ])
    return table


def tables_for(
    result: SuiteResult,
    *,
    raw_runs: dict[str, Sequence[dict[str, Any]]] | None = None,
) -> list[Table]:
    """Every table for a suite result, skipping ones with no rows."""
    candidates = [
        comparisons_table(result),
        cells_table(result),
        interactions_table(result),
        runs_table(raw_runs),
    ]
    return [t for t in candidates if t.rows]


# --------------------------------------------------------------------------
# Renderers
# --------------------------------------------------------------------------


def _delimited(table: Table, delimiter: str) -> str:
    buffer = io.StringIO()
    writer = csv.writer(buffer, delimiter=delimiter, lineterminator="\n")
    writer.writerow(table.columns)
    writer.writerows(table.rows)
    return buffer.getvalue()


def to_csv(table: Table) -> str:
    return _delimited(table, ",")


def to_tsv(table: Table) -> str:
    return _delimited(table, "\t")


def to_markdown(table: Table) -> str:
    """GitHub-flavoured Markdown, for pasting into issues and pull requests."""
    lines = []
    if table.caption:
        lines.append(f"_{table.caption}_")
        lines.append("")
    lines.append("| " + " | ".join(table.columns) + " |")
    lines.append("|" + "|".join("---" for _ in table.columns) + "|")
    for row in table.rows:
        cells = ["" if v is None else str(v).replace("|", "\\|") for v in row]
        lines.append("| " + " | ".join(cells) + " |")
    return "\n".join(lines) + "\n"


def to_html(table: Table, *, sortable: bool = True) -> str:
    """An HTML table, optionally sortable by the report's inline script."""
    classes = "mcb-table" + (" mcb-sortable" if sortable else "")
    parts = [f'<table class="{classes}" id="tbl-{html.escape(table.slug)}">']
    if table.caption:
        parts.append(f"<caption>{html.escape(table.caption)}</caption>")
    parts.append("<thead><tr>")
    for column in table.columns:
        parts.append(f'<th scope="col">{html.escape(column)}</th>')
    parts.append("</tr></thead><tbody>")
    for row in table.rows:
        parts.append("<tr>")
        for value in row:
            text = "" if value is None else str(value)
            is_number = isinstance(value, (int, float)) and not isinstance(value, bool)
            numeric = ' class="mcb-num"' if is_number else ""
            parts.append(f"<td{numeric}>{html.escape(text)}</td>")
        parts.append("</tr>")
    parts.append("</tbody></table>")
    return "".join(parts)


def write_all(
    tables: Iterable[Table], directory: str, *, formats: Sequence[str] = ("csv",)
) -> list[str]:
    """Write each table to ``directory`` in each requested format."""
    from pathlib import Path

    renderers = {
        "csv": (to_csv, "csv"),
        "tsv": (to_tsv, "tsv"),
        "md": (to_markdown, "md"),
        "markdown": (to_markdown, "md"),
        "html": (to_html, "html"),
    }
    root = Path(directory)
    root.mkdir(parents=True, exist_ok=True)

    written: list[str] = []
    for table in tables:
        for fmt in formats:
            if fmt not in renderers:
                raise ValueError(
                    f"unknown table format {fmt!r}; "
                    f"expected one of {sorted(renderers)}"
                )
            render, suffix = renderers[fmt]
            path = root / f"{table.slug}.{suffix}"
            path.write_text(render(table), encoding="utf-8")
            written.append(str(path))
    return written
