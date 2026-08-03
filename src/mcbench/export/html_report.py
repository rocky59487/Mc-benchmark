"""Self-contained HTML report.

One file, no external requests, no CDN, no fonts to fetch. It opens from a USB
stick, survives being attached to an email, and renders the same in five years.
That matters for a benchmark whose output is meant to be cited and re-examined
later.

Layout order is deliberate: caveats first, then verdicts, then the evidence
behind them. A reader who stops after the first screen should come away with the
qualifications, not just the winning number.
"""

from __future__ import annotations

import html
from collections.abc import Sequence
from typing import Any

from ..metrics import METRICS
from ..report import SuiteResult
from ..stats import Verdict
from .charts import (
    PALETTE,
    VERDICT_COLOUR,
    Series,
    chart_styles,
    distribution_cdf,
    forest_plot,
    interaction_plot,
    interval_bar_chart,
    order_effect_scatter,
)
from .tables import Table, tables_for, to_html

__all__ = ["render_html_report"]

_PAGE_CSS = """
*,*::before,*::after{box-sizing:border-box}
body{margin:0;padding:0;background:var(--bg);color:var(--fg);
  font:15px/1.6 ui-sans-serif,system-ui,-apple-system,"Segoe UI",sans-serif;
  -webkit-text-size-adjust:100%}
:root{--bg:#fff;--fg:#1a1a1a;--muted:#666;--line:#e3e3e3;--card:#fafafa;
  --accent:#0072B2;--warn-bg:#fff4e5;--warn-fg:#8a4b00;--warn-line:#e69f00}
@media (prefers-color-scheme:dark){
  :root{--bg:#131313;--fg:#e8e8e8;--muted:#a0a0a0;--line:#2e2e2e;--card:#1c1c1c;
    --accent:#56B4E9;--warn-bg:#2a2113;--warn-fg:#e8c88a;--warn-line:#8a6d1f}}
:root[data-theme="light"]{--bg:#fff;--fg:#1a1a1a;--muted:#666;--line:#e3e3e3;
  --card:#fafafa;--accent:#0072B2;--warn-bg:#fff4e5;--warn-fg:#8a4b00;--warn-line:#e69f00}
:root[data-theme="dark"]{--bg:#131313;--fg:#e8e8e8;--muted:#a0a0a0;--line:#2e2e2e;
  --card:#1c1c1c;--accent:#56B4E9;--warn-bg:#2a2113;--warn-fg:#e8c88a;--warn-line:#8a6d1f}
.wrap{max-width:1080px;margin:0 auto;padding:32px 20px 80px}
h1{font-size:26px;margin:0 0 4px;letter-spacing:-.01em}
h2{font-size:19px;margin:44px 0 12px;padding-bottom:6px;border-bottom:1px solid var(--line)}
h3{font-size:15px;margin:28px 0 10px;color:var(--muted);font-weight:600;
  text-transform:uppercase;letter-spacing:.05em}
p{margin:0 0 12px;max-width:76ch}
a{color:var(--accent)}
.sub{color:var(--muted);font-size:13px;margin-bottom:24px}
.card{background:var(--card);border:1px solid var(--line);border-radius:8px;
  padding:16px 18px;margin:0 0 18px}
.callout{background:var(--warn-bg);color:var(--warn-fg);
  border:1px solid var(--warn-line);border-left-width:4px;border-radius:6px;
  padding:14px 16px;margin:0 0 18px}
.callout strong{display:block;margin-bottom:6px}
.callout ul{margin:6px 0 0;padding-left:20px}
.grid{display:grid;gap:14px;grid-template-columns:repeat(auto-fit,minmax(190px,1fr));
  margin-bottom:22px}
.stat{background:var(--card);border:1px solid var(--line);border-radius:8px;padding:12px 14px}
.stat .k{font-size:11px;text-transform:uppercase;letter-spacing:.05em;color:var(--muted)}
.stat .v{font-size:22px;font-weight:650;margin-top:2px;letter-spacing:-.01em}
.chart{margin:0 0 26px;overflow-x:auto}
.tablewrap{overflow-x:auto;margin:0 0 26px;border:1px solid var(--line);border-radius:8px}
table.mcb-table{border-collapse:collapse;width:100%;font-size:12.5px;
  font-variant-numeric:tabular-nums}
table.mcb-table caption{text-align:left;padding:10px 12px;color:var(--muted);
  font-size:12px;border-bottom:1px solid var(--line)}
table.mcb-table th,table.mcb-table td{padding:7px 11px;text-align:left;
  border-bottom:1px solid var(--line);white-space:nowrap}
table.mcb-table th{background:var(--card);font-weight:600;position:sticky;top:0;
  cursor:pointer;user-select:none}
table.mcb-table th::after{content:"";opacity:.4;margin-left:5px}
table.mcb-table th[aria-sort="ascending"]::after{content:"\\2191";opacity:1}
table.mcb-table th[aria-sort="descending"]::after{content:"\\2193";opacity:1}
table.mcb-table td.mcb-num{text-align:right;font-variant-numeric:tabular-nums}
table.mcb-table tbody tr:hover{background:var(--card)}
.badge{display:inline-block;padding:1px 8px;border-radius:11px;font-size:11px;
  font-weight:600;color:#fff}
.legend{font-size:12px;color:var(--muted);margin:-14px 0 22px}
footer{margin-top:56px;padding-top:18px;border-top:1px solid var(--line);
  color:var(--muted);font-size:12px}
@media(max-width:640px){.wrap{padding:20px 14px 60px}h1{font-size:21px}}
"""

_SORT_JS = """
document.querySelectorAll('table.mcb-sortable').forEach(function(t){
  var head=t.tHead; if(!head) return;
  Array.from(head.rows[0].cells).forEach(function(th,i){
    th.addEventListener('click',function(){
      var asc=th.getAttribute('aria-sort')!=='ascending';
      Array.from(head.rows[0].cells).forEach(function(o){o.removeAttribute('aria-sort')});
      th.setAttribute('aria-sort',asc?'ascending':'descending');
      var body=t.tBodies[0];
      var rows=Array.from(body.rows);
      rows.sort(function(a,b){
        var x=a.cells[i].textContent.trim(), y=b.cells[i].textContent.trim();
        var nx=parseFloat(x), ny=parseFloat(y);
        var both=!isNaN(nx)&&!isNaN(ny)&&x!==''&&y!=='';
        var c=both?(nx-ny):x.localeCompare(y);
        return asc?c:-c;
      });
      rows.forEach(function(r){body.appendChild(r)});
    });
  });
});
"""


def _esc(text: Any) -> str:
    return html.escape(str(text), quote=True)


def _verdict_badge(verdict: str) -> str:
    colour = VERDICT_COLOUR.get(verdict, "#999")
    return f'<span class="badge" style="background:{colour}">{_esc(verdict)}</span>'


def _summary_stats(result: SuiteResult) -> str:
    counts = {v: 0 for v in ("improvement", "regression", "equivalent", "inconclusive")}
    for entry in result.comparisons:
        counts[entry.comparison.verdict.value] = (
            counts.get(entry.comparison.verdict.value, 0) + 1
        )

    tiles = [
        ("variants", len(result.variants)),
        ("scenarios", len(result.scenarios)),
        ("comparisons", len(result.comparisons)),
    ] + [(k, v) for k, v in counts.items()]

    cells = "".join(
        f'<div class="stat"><div class="k">{_esc(k)}</div>'
        f'<div class="v">{_esc(v)}</div></div>'
        for k, v in tiles
    )
    return f'<div class="grid">{cells}</div>'


def _caveats(result: SuiteResult, preflight: dict[str, Any] | None) -> str:
    """Caveats first, before any number. A reader who stops early gets these."""
    items: list[str] = []

    if preflight:
        for check in preflight.get("blockers", []):
            items.append(
                f"<strong>blocked</strong> — {_esc(check.get('name'))}: "
                f"{_esc(check.get('detail'))}"
            )
        for check in preflight.get("warnings", []):
            items.append(
                f"{_esc(check.get('name'))}: {_esc(check.get('detail'))}"
            )

    for cell, data in result.cells.items():
        if data.flags:
            flags = ", ".join(sorted(f.value for f in data.flags))
            items.append(f"<code>{_esc(cell)}</code> — flags: {_esc(flags)}")
        if data.unstable:
            items.append(
                f"<code>{_esc(cell)}</code> — unstable: more than 20% of runs "
                f"excluded as outliers"
            )
        if data.runs_admissible < 5:
            items.append(
                f"<code>{_esc(cell)}</code> — only {data.runs_admissible} "
                f"admissible run(s); at least 5 are needed to estimate variance"
            )

    inconclusive = sum(
        1 for e in result.comparisons if e.comparison.verdict is Verdict.INCONCLUSIVE
    )
    if inconclusive:
        items.append(
            f"{inconclusive} comparison(s) are <em>inconclusive</em> — the "
            f"interval straddles the practical-equivalence boundary and the data "
            f"does not support a verdict either way"
        )

    if not items:
        return ""

    entries = "".join(f"<li>{item}</li>" for item in items)
    return (
        f'<div class="callout"><strong>Caveats — read before the numbers</strong>'
        f"<ul>{entries}</ul></div>"
    )


def _scenario_section(
    result: SuiteResult,
    scenario: str,
    *,
    distributions: dict[str, dict[str, Sequence[float]]] | None,
    order_points: dict[str, dict[str, Sequence[tuple[int, float]]]] | None,
) -> str:
    parts = [f"<h2>{_esc(scenario)}</h2>"]

    entries = result.comparisons_for(scenario)
    if not entries:
        return "".join(parts) + "<p>No comparisons available.</p>"

    # Forest plot per metric: the chart that renders the verdict rule directly.
    by_metric: dict[str, list] = {}
    for entry in entries:
        by_metric.setdefault(entry.metric, []).append(entry)

    primary_order = sorted(
        by_metric, key=lambda m: (not METRICS[m].lower_is_better, m)
    )
    for metric in primary_order:
        definition = METRICS[metric]
        rows = by_metric[metric]
        series = [
            Series(
                label=e.variant,
                value=e.comparison.relative_delta.value,
                low=e.comparison.relative_delta.ci.low,
                high=e.comparison.relative_delta.ci.high,
                colour=VERDICT_COLOUR.get(e.comparison.verdict.value),
                annotation=e.comparison.verdict.value,
            )
            for e in rows
        ]
        rope = rows[0].comparison.rope
        parts.append(
            '<div class="chart">'
            + forest_plot(
                series,
                title=f"{definition.label} — change vs baseline",
                rope=rope,
            )
            + "</div>"
        )
        direction = (
            "left is better" if definition.lower_is_better else "right is better"
        )
        parts.append(f'<p class="legend">{_esc(definition.description)} · {direction}.</p>')

    # Absolute values with intervals, so ratios are never read without scale.
    scenario_cells = {
        c.variant: d for c, d in result.cells.items() if c.scenario == scenario
    }
    for metric in primary_order[:2]:
        definition = METRICS[metric]
        series = [
            Series(
                label=variant,
                value=data.estimates[metric].value,
                low=data.estimates[metric].ci.low,
                high=data.estimates[metric].ci.high,
                colour=PALETTE[i % len(PALETTE)],
            )
            for i, (variant, data) in enumerate(scenario_cells.items())
            if metric in data.estimates
        ]
        if series:
            parts.append(
                '<div class="chart">'
                + interval_bar_chart(
                    series,
                    title=f"{definition.label} — absolute",
                    unit=definition.unit,
                )
                + "</div>"
            )

    if distributions and scenario in distributions:
        parts.append(
            '<div class="chart">'
            + distribution_cdf(
                distributions[scenario],
                title="Frametime distribution (empirical CDF)",
                unit="ms",
            )
            + "</div>"
        )
        parts.append(
            '<p class="legend">Two variants can share a mean and differ '
            "sharply in the tail, where stutter lives. A bar chart hides that; "
            "this does not.</p>"
        )

    if order_points and scenario in order_points:
        parts.append(
            '<div class="chart">'
            + order_effect_scatter(
                order_points[scenario],
                title="Order effects — metric vs execution position",
            )
            + "</div>"
        )
        parts.append(
            '<p class="legend">A slope shared by all variants means the machine '
            "drifted during the suite. Interleaving stops that favouring any one "
            "variant, but it still widens every interval.</p>"
        )

    return "".join(parts)


def render_html_report(
    result: SuiteResult,
    *,
    raw_runs: dict[str, Sequence[dict[str, Any]]] | None = None,
    distributions: dict[str, dict[str, Sequence[float]]] | None = None,
    order_points: dict[str, dict[str, Sequence[tuple[int, float]]]] | None = None,
    preflight: dict[str, Any] | None = None,
    tables: Sequence[Table] | None = None,
) -> str:
    """Render the complete self-contained HTML report."""
    body: list[str] = []

    body.append(f"<h1>{_esc(result.suite_name)}</h1>")
    body.append(
        f'<div class="sub">Generated {_esc(result.generated_at or "unknown")} · '
        f"baseline <code>{_esc(result.baseline)}</code></div>"
    )

    body.append(_caveats(result, preflight))
    body.append(_summary_stats(result))

    body.append(
        '<div class="card"><strong>How to read this</strong>'
        "<p style=\"margin:8px 0 0\">Metrics are aggregated in the time domain; "
        "FPS is derived as <code>1000 / mean(frametime)</code>, never by "
        "averaging per-second FPS. &ldquo;1% low&rdquo; is the mean of the worst "
        "1% of frametimes, not the 99th percentile. Intervals are 95% percentile "
        "bootstrap. A verdict is issued only when the interval on the relative "
        "change lies wholly inside or wholly outside the region of practical "
        "equivalence &mdash; otherwise the result is "
        f"{_verdict_badge('inconclusive')}, which is a real answer, not a "
        "failure.</p></div>"
    )

    if result.provenance:
        rows = "".join(
            f"<tr><td>{_esc(k)}</td><td><code>{_esc(v)}</code></td></tr>"
            for k, v in sorted(result.provenance.items())
        )
        body.append("<h2>Provenance</h2>")
        body.append(
            '<div class="tablewrap"><table class="mcb-table">'
            "<thead><tr><th>Field</th><th>Value</th></tr></thead>"
            f"<tbody>{rows}</tbody></table></div>"
        )

    for scenario in result.scenarios:
        body.append(
            _scenario_section(
                result, scenario,
                distributions=distributions, order_points=order_points,
            )
        )

    if result.interactions:
        body.append("<h2>Interaction effects</h2>")
        body.append(
            "<p>Where two mods are non-additive. Positive means the pair costs "
            "more together than their individual costs predict &mdash; usually "
            "contention over a shared lock, a cache one invalidates for the "
            "other, or a fast path one forces the other off. This is the usual "
            "explanation for a modpack running far worse than its parts "
            "suggest.</p>"
        )
        for item in result.interactions:
            absolute = item.get("absolute")
            if absolute:
                body.append(
                    '<div class="chart">'
                    + interaction_plot(
                        none=absolute["none"], a=absolute["a"],
                        b=absolute["b"], ab=absolute["ab"],
                        label_a=item["pair"][0], label_b=item["pair"][1],
                        title=f"{item['scenario']} — {METRICS[item['metric']].label}",
                        unit=METRICS[item["metric"]].unit,
                    )
                    + "</div>"
                )

    rendered_tables = list(tables) if tables is not None else tables_for(
        result, raw_runs=raw_runs
    )
    if rendered_tables:
        body.append("<h2>Data tables</h2>")
        body.append(
            "<p>Click a column header to sort. Raw per-run values are included "
            "so the analysis can be redone independently rather than taken on "
            "trust.</p>"
        )
        for table in rendered_tables:
            body.append(f"<h3>{_esc(table.name)}</h3>")
            body.append(f'<div class="tablewrap">{to_html(table)}</div>')

    body.append(
        "<footer>Absolute figures are comparable only within this session on "
        "this machine. For cross-machine comparison use ratios to "
        "<code>reference-hardware-baseline</code>. Produced by mcbench &mdash; "
        "methodology in <code>docs/METHODOLOGY.md</code>.</footer>"
    )

    return (
        "<!doctype html><html lang=\"en\"><head><meta charset=\"utf-8\">"
        '<meta name="viewport" content="width=device-width,initial-scale=1">'
        f"<title>{_esc(result.suite_name)}</title>"
        f"<style>{_PAGE_CSS}{chart_styles()}</style></head>"
        f'<body><div class="wrap">{"".join(body)}</div>'
        f"<script>{_SORT_JS}</script></body></html>"
    )
