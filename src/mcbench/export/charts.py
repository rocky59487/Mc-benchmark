"""Chart rendering — hand-built SVG, no plotting dependency.

Charts are emitted as standalone SVG so a report is a single file that opens
anywhere, survives being emailed, and renders identically in ten years. A
plotting library would add a heavyweight dependency to a tool whose whole
premise is that anyone can verify it with a stock interpreter.

Every chart here is built around one rule: **an interval is never optional**.
A bare bar chart of point estimates is the visual form of the error this
project exists to correct, so each chart type either shows uncertainty or shows
a distribution.

Colours come from a colourblind-safe qualitative palette (Okabe-Ito), and all
charts adapt to light and dark themes through CSS custom properties.
"""

from __future__ import annotations

import html
import math
from collections.abc import Sequence
from dataclasses import dataclass

__all__ = [
    "Series",
    "PALETTE",
    "interval_bar_chart",
    "forest_plot",
    "distribution_cdf",
    "order_effect_scatter",
    "interaction_plot",
    "chart_styles",
]

#: Okabe-Ito colourblind-safe qualitative palette.
PALETTE = [
    "#0072B2",  # blue
    "#E69F00",  # orange
    "#009E73",  # green
    "#CC79A7",  # reddish purple
    "#56B4E9",  # sky blue
    "#D55E00",  # vermillion
    "#F0E442",  # yellow
    "#999999",  # grey
]

VERDICT_COLOUR = {
    "improvement": "#009E73",
    "regression": "#D55E00",
    "equivalent": "#999999",
    "inconclusive": "#8C8C8C",
    # Deliberately the palest of the set. A cell that never obtained the runs to
    # answer must not draw the eye the way a finding does.
    "insufficient_data": "#BFBFBF",
}


@dataclass(frozen=True)
class Series:
    """One labelled datum with an optional interval."""

    label: str
    value: float
    low: float | None = None
    high: float | None = None
    colour: str | None = None
    annotation: str = ""

    @property
    def has_interval(self) -> bool:
        return self.low is not None and self.high is not None


def chart_styles() -> str:
    """CSS the charts rely on. Emitted once per document."""
    return """
.mcb-chart { --mcb-fg:#1a1a1a; --mcb-muted:#6b6b6b; --mcb-grid:#e0e0e0;
  --mcb-axis:#9a9a9a; --mcb-bg:transparent; --mcb-rope:#0072B233;
  font-family: ui-sans-serif, system-ui, -apple-system, "Segoe UI", sans-serif; }
@media (prefers-color-scheme: dark) {
  .mcb-chart { --mcb-fg:#e8e8e8; --mcb-muted:#a0a0a0; --mcb-grid:#333;
    --mcb-axis:#666; --mcb-rope:#56B4E933; }
}
:root[data-theme="dark"] .mcb-chart { --mcb-fg:#e8e8e8; --mcb-muted:#a0a0a0;
  --mcb-grid:#333; --mcb-axis:#666; --mcb-rope:#56B4E933; }
:root[data-theme="light"] .mcb-chart { --mcb-fg:#1a1a1a; --mcb-muted:#6b6b6b;
  --mcb-grid:#e0e0e0; --mcb-axis:#9a9a9a; --mcb-rope:#0072B233; }
.mcb-chart text { fill: var(--mcb-fg); }
.mcb-chart .mcb-muted { fill: var(--mcb-muted); }
.mcb-chart .mcb-grid { stroke: var(--mcb-grid); stroke-width: 1; }
.mcb-chart .mcb-axis { stroke: var(--mcb-axis); stroke-width: 1.5; }
.mcb-chart .mcb-rope { fill: var(--mcb-rope); }
.mcb-chart .mcb-whisker { stroke: var(--mcb-fg); stroke-width: 1.8; }
"""


# --------------------------------------------------------------------------
# SVG primitives
# --------------------------------------------------------------------------


def _esc(text: str) -> str:
    return html.escape(str(text), quote=True)


def _nice_ticks(low: float, high: float, count: int = 5) -> list[float]:
    """Human-friendly axis ticks spanning [low, high]."""
    if not math.isfinite(low) or not math.isfinite(high):
        return [0.0]
    if math.isclose(low, high):
        # A flat series still needs an axis; centre it on the value.
        pad = abs(low) * 0.1 or 1.0
        low, high = low - pad, high + pad

    span = high - low
    raw = span / max(1, count)
    magnitude = 10 ** math.floor(math.log10(raw)) if raw > 0 else 1
    for multiple in (1, 2, 2.5, 5, 10):
        step = magnitude * multiple
        if span / step <= count:
            break
    start = math.floor(low / step) * step
    ticks: list[float] = []
    value = start
    while value <= high + step * 0.5:
        ticks.append(round(value, 10))
        value += step
    return ticks


def _fmt_tick(value: float) -> str:
    if value == 0:
        return "0"
    if abs(value) >= 1000:
        return f"{value:,.0f}"
    if abs(value) >= 10:
        return f"{value:.0f}"
    if abs(value) >= 1:
        return f"{value:.1f}"
    return f"{value:.3g}"


def _svg(width: int, height: int, body: str, title: str) -> str:
    return (
        f'<svg class="mcb-chart" xmlns="http://www.w3.org/2000/svg" '
        f'viewBox="0 0 {width} {height}" width="100%" height="auto" '
        f'role="img" aria-label="{_esc(title)}" '
        f'style="max-width:{width}px">'
        f"<title>{_esc(title)}</title>{body}</svg>"
    )


# --------------------------------------------------------------------------
# Charts
# --------------------------------------------------------------------------


def interval_bar_chart(
    series: Sequence[Series],
    *,
    title: str,
    unit: str = "",
    width: int = 720,
    bar_height: int = 34,
    zero_based: bool = True,
) -> str:
    """Horizontal bars with confidence-interval whiskers.

    The workhorse for "how do the variants compare on this metric". Bars are
    zero-based by default because a truncated axis exaggerates differences —
    the most common way a benchmark chart misleads without stating anything
    false.
    """
    if not series:
        return _svg(width, 60, '<text x="10" y="30">no data</text>', title)

    left, right, top, bottom = 190, 30, 52, 46
    plot_w = width - left - right
    height = top + bar_height * len(series) + bottom

    values = [s.value for s in series]
    lows = [s.low for s in series if s.low is not None]
    highs = [s.high for s in series if s.high is not None]
    data_min = min(values + lows)
    data_max = max(values + highs)
    axis_min = 0.0 if zero_based and data_min >= 0 else data_min - (data_max - data_min) * 0.1
    axis_max = data_max + (data_max - axis_min) * 0.08 or 1.0

    ticks = _nice_ticks(axis_min, axis_max)
    axis_min, axis_max = min(axis_min, ticks[0]), max(axis_max, ticks[-1])
    span = axis_max - axis_min or 1.0

    def x_of(value: float) -> float:
        return left + (value - axis_min) / span * plot_w

    parts = [f'<text x="12" y="26" font-size="14" font-weight="600">{_esc(title)}</text>']
    if unit:
        parts.append(
            f'<text x="{left + plot_w}" y="26" font-size="11" text-anchor="end" '
            f'class="mcb-muted">{_esc(unit)}</text>'
        )

    for tick in ticks:
        x = x_of(tick)
        parts.append(
            f'<line class="mcb-grid" x1="{x:.1f}" y1="{top}" '
            f'x2="{x:.1f}" y2="{top + bar_height * len(series)}"/>'
        )
        parts.append(
            f'<text x="{x:.1f}" y="{top + bar_height * len(series) + 18}" '
            f'font-size="10" text-anchor="middle" class="mcb-muted">'
            f"{_esc(_fmt_tick(tick))}</text>"
        )

    for index, item in enumerate(series):
        y = top + index * bar_height
        cy = y + bar_height / 2
        colour = item.colour or PALETTE[index % len(PALETTE)]
        bar_x0, bar_x1 = x_of(max(axis_min, 0.0)), x_of(item.value)
        parts.append(
            f'<rect x="{min(bar_x0, bar_x1):.1f}" y="{y + 7:.1f}" '
            f'width="{abs(bar_x1 - bar_x0):.1f}" height="{bar_height - 16}" '
            f'rx="2" fill="{colour}" fill-opacity="0.82"/>'
        )

        if item.has_interval:
            lo, hi = x_of(item.low), x_of(item.high)
            parts.append(
                f'<line class="mcb-whisker" x1="{lo:.1f}" y1="{cy:.1f}" '
                f'x2="{hi:.1f}" y2="{cy:.1f}"/>'
                f'<line class="mcb-whisker" x1="{lo:.1f}" y1="{cy - 5:.1f}" '
                f'x2="{lo:.1f}" y2="{cy + 5:.1f}"/>'
                f'<line class="mcb-whisker" x1="{hi:.1f}" y1="{cy - 5:.1f}" '
                f'x2="{hi:.1f}" y2="{cy + 5:.1f}"/>'
            )

        parts.append(
            f'<text x="{left - 10}" y="{cy + 4:.1f}" font-size="11.5" '
            f'text-anchor="end">{_esc(item.label)}</text>'
        )
        label = item.annotation or _fmt_tick(item.value)
        parts.append(
            f'<text x="{max(bar_x0, bar_x1) + 8:.1f}" y="{cy + 4:.1f}" '
            f'font-size="10.5" class="mcb-muted">{_esc(label)}</text>'
        )

    parts.append(
        f'<line class="mcb-axis" x1="{x_of(max(axis_min, 0.0)):.1f}" y1="{top}" '
        f'x2="{x_of(max(axis_min, 0.0)):.1f}" '
        f'y2="{top + bar_height * len(series)}"/>'
    )
    return _svg(width, height, "".join(parts), title)


def forest_plot(
    series: Sequence[Series],
    *,
    title: str,
    rope: float = 0.02,
    width: int = 720,
    row_height: int = 32,
) -> str:
    """Relative changes with intervals against a shaded ROPE band.

    The single most important chart mcbench produces, because it renders the
    verdict rule directly: the shaded band is the region of practical
    equivalence, and an interval that touches it has not established anything.
    Readers can see at a glance which differences are real, which are real but
    too small to matter, and which are simply unresolved — a distinction that a
    table of percentages makes far too easy to skip past.
    """
    if not series:
        return _svg(width, 60, '<text x="10" y="30">no data</text>', title)

    left, right, top, bottom = 210, 90, 58, 52
    plot_w = width - left - right
    height = top + row_height * len(series) + bottom

    bounds = [abs(s.low or s.value) for s in series] + \
             [abs(s.high or s.value) for s in series] + [rope * 2.5]
    limit = max(bounds) * 1.15 or 0.05

    def x_of(value: float) -> float:
        return left + (value + limit) / (2 * limit) * plot_w

    parts = [f'<text x="12" y="24" font-size="14" font-weight="600">{_esc(title)}</text>']
    parts.append(
        f'<text x="12" y="42" font-size="10.5" class="mcb-muted">'
        f"shaded band = region of practical equivalence (±{rope * 100:g}%); "
        f"an interval touching it is not a result</text>"
    )

    plot_bottom = top + row_height * len(series)
    parts.append(
        f'<rect class="mcb-rope" x="{x_of(-rope):.1f}" y="{top}" '
        f'width="{x_of(rope) - x_of(-rope):.1f}" height="{plot_bottom - top}"/>'
    )

    for tick in _nice_ticks(-limit, limit, 6):
        if abs(tick) > limit:
            continue
        x = x_of(tick)
        parts.append(
            f'<line class="mcb-grid" x1="{x:.1f}" y1="{top}" x2="{x:.1f}" '
            f'y2="{plot_bottom}"/>'
            f'<text x="{x:.1f}" y="{plot_bottom + 18}" font-size="10" '
            f'text-anchor="middle" class="mcb-muted">{tick * 100:+.0f}%</text>'
        )

    parts.append(
        f'<line class="mcb-axis" x1="{x_of(0):.1f}" y1="{top}" '
        f'x2="{x_of(0):.1f}" y2="{plot_bottom}"/>'
    )

    for index, item in enumerate(series):
        cy = top + index * row_height + row_height / 2
        colour = item.colour or PALETTE[index % len(PALETTE)]
        if item.has_interval:
            lo, hi = x_of(item.low), x_of(item.high)
            parts.append(
                f'<line x1="{lo:.1f}" y1="{cy:.1f}" x2="{hi:.1f}" y2="{cy:.1f}" '
                f'stroke="{colour}" stroke-width="2.4"/>'
                f'<line x1="{lo:.1f}" y1="{cy - 5:.1f}" x2="{lo:.1f}" '
                f'y2="{cy + 5:.1f}" stroke="{colour}" stroke-width="2.4"/>'
                f'<line x1="{hi:.1f}" y1="{cy - 5:.1f}" x2="{hi:.1f}" '
                f'y2="{cy + 5:.1f}" stroke="{colour}" stroke-width="2.4"/>'
            )
        parts.append(
            f'<circle cx="{x_of(item.value):.1f}" cy="{cy:.1f}" r="4.5" fill="{colour}"/>'
        )
        parts.append(
            f'<text x="{left - 10}" y="{cy + 4:.1f}" font-size="11.5" '
            f'text-anchor="end">{_esc(item.label)}</text>'
        )
        if item.annotation:
            parts.append(
                f'<text x="{width - right + 8}" y="{cy + 4:.1f}" font-size="10.5" '
                f'class="mcb-muted">{_esc(item.annotation)}</text>'
            )

    return _svg(width, height, "".join(parts), title)


def distribution_cdf(
    distributions: dict[str, Sequence[float]],
    *,
    title: str,
    unit: str = "ms",
    width: int = 720,
    height: int = 340,
    clip_percentile: float = 99.5,
) -> str:
    """Empirical CDFs, one curve per variant.

    For frametimes this is more honest than a histogram: bin widths change a
    histogram's story, while a CDF shows the whole distribution including the
    tail where stutter lives. Two variants with identical means and very
    different tails are indistinguishable in a bar chart and obviously
    different here.

    The x-axis is clipped at a high percentile so the curves stay readable when
    a handful of extreme frames would otherwise compress everything.
    """
    populated = {k: sorted(v) for k, v in distributions.items() if v}
    if not populated:
        return _svg(width, 60, '<text x="10" y="30">no data</text>', title)

    left, right, top, bottom = 62, 130, 46, 46
    plot_w, plot_h = width - left - right, height - top - bottom

    def pct(values: list[float], q: float) -> float:
        if len(values) == 1:
            return values[0]
        pos = (len(values) - 1) * q / 100.0
        lo, hi = math.floor(pos), math.ceil(pos)
        return values[lo] * (hi - pos) + values[hi] * (pos - lo) if lo != hi else values[int(pos)]

    axis_min = min(v[0] for v in populated.values())
    axis_max = max(pct(v, clip_percentile) for v in populated.values())
    if math.isclose(axis_min, axis_max):
        axis_max = axis_min + 1.0
    span = axis_max - axis_min

    def x_of(value: float) -> float:
        return left + min(1.0, max(0.0, (value - axis_min) / span)) * plot_w

    def y_of(fraction: float) -> float:
        return top + (1.0 - fraction) * plot_h

    parts = [f'<text x="12" y="26" font-size="14" font-weight="600">{_esc(title)}</text>']

    for fraction in (0.0, 0.25, 0.5, 0.75, 1.0):
        y = y_of(fraction)
        parts.append(
            f'<line class="mcb-grid" x1="{left}" y1="{y:.1f}" '
            f'x2="{left + plot_w}" y2="{y:.1f}"/>'
            f'<text x="{left - 8}" y="{y + 4:.1f}" font-size="10" '
            f'text-anchor="end" class="mcb-muted">{fraction * 100:.0f}%</text>'
        )
    for tick in _nice_ticks(axis_min, axis_max, 5):
        if not axis_min <= tick <= axis_max:
            continue
        x = x_of(tick)
        parts.append(
            f'<line class="mcb-grid" x1="{x:.1f}" y1="{top}" x2="{x:.1f}" '
            f'y2="{top + plot_h}"/>'
            f'<text x="{x:.1f}" y="{top + plot_h + 18}" font-size="10" '
            f'text-anchor="middle" class="mcb-muted">{_esc(_fmt_tick(tick))}</text>'
        )
    parts.append(
        f'<text x="{left + plot_w / 2:.0f}" y="{height - 8}" font-size="10.5" '
        f'text-anchor="middle" class="mcb-muted">{_esc(unit)}</text>'
    )

    for index, (label, values) in enumerate(populated.items()):
        colour = PALETTE[index % len(PALETTE)]
        # Downsample to at most ~400 vertices: an SVG path with a point per
        # frame would be megabytes for a three-minute run and no more legible.
        step = max(1, len(values) // 400)
        points = [
            f"{x_of(values[i]):.1f},{y_of((i + 1) / len(values)):.1f}"
            for i in range(0, len(values), step)
        ]
        points.append(f"{x_of(values[-1]):.1f},{y_of(1.0):.1f}")
        parts.append(
            f'<polyline points="{" ".join(points)}" fill="none" '
            f'stroke="{colour}" stroke-width="2" stroke-linejoin="round"/>'
        )
        ly = top + 6 + index * 18
        parts.append(
            f'<line x1="{left + plot_w + 12}" y1="{ly:.1f}" '
            f'x2="{left + plot_w + 30}" y2="{ly:.1f}" stroke="{colour}" '
            f'stroke-width="2.5"/>'
            f'<text x="{left + plot_w + 35}" y="{ly + 4:.1f}" font-size="10.5">'
            f"{_esc(label)}</text>"
        )

    parts.append(
        f'<line class="mcb-axis" x1="{left}" y1="{top}" x2="{left}" '
        f'y2="{top + plot_h}"/>'
        f'<line class="mcb-axis" x1="{left}" y1="{top + plot_h}" '
        f'x2="{left + plot_w}" y2="{top + plot_h}"/>'
    )
    return _svg(width, height, "".join(parts), title)


def order_effect_scatter(
    points_by_variant: dict[str, Sequence[tuple[int, float]]],
    *,
    title: str,
    unit: str = "",
    width: int = 720,
    height: int = 300,
) -> str:
    """Metric against execution position — the audit chart for drift.

    Interleaving is what prevents thermal drift from being attributed to a mod,
    but nothing guarantees it worked on a given machine. This plots each run
    against when it ran. A visible slope shared by all variants means the
    environment drifted during the suite and the whole run deserves suspicion;
    the interleaving means it will not have favoured any one variant, but it
    will have widened every interval.
    """
    populated = {k: list(v) for k, v in points_by_variant.items() if v}
    if not populated:
        return _svg(width, 60, '<text x="10" y="30">no data</text>', title)

    left, right, top, bottom = 62, 130, 46, 46
    plot_w, plot_h = width - left - right, height - top - bottom

    all_points = [p for pts in populated.values() for p in pts]
    max_pos = max(p[0] for p in all_points) or 1
    y_values = [p[1] for p in all_points]
    y_min, y_max = min(y_values), max(y_values)
    if math.isclose(y_min, y_max):
        y_min, y_max = y_min * 0.95, y_max * 1.05 or 1.0
    pad = (y_max - y_min) * 0.1
    y_min, y_max = y_min - pad, y_max + pad

    def x_of(pos: int) -> float:
        return left + (pos / max_pos) * plot_w

    def y_of(value: float) -> float:
        return top + (1 - (value - y_min) / (y_max - y_min)) * plot_h

    parts = [f'<text x="12" y="26" font-size="14" font-weight="600">{_esc(title)}</text>']
    for tick in _nice_ticks(y_min, y_max, 4):
        if not y_min <= tick <= y_max:
            continue
        y = y_of(tick)
        parts.append(
            f'<line class="mcb-grid" x1="{left}" y1="{y:.1f}" '
            f'x2="{left + plot_w}" y2="{y:.1f}"/>'
            f'<text x="{left - 8}" y="{y + 4:.1f}" font-size="10" '
            f'text-anchor="end" class="mcb-muted">{_esc(_fmt_tick(tick))}</text>'
        )

    for index, (label, points) in enumerate(populated.items()):
        colour = PALETTE[index % len(PALETTE)]
        for pos, value in points:
            parts.append(
                f'<circle cx="{x_of(pos):.1f}" cy="{y_of(value):.1f}" r="3.6" '
                f'fill="{colour}" fill-opacity="0.85"/>'
            )
        ly = top + 6 + index * 18
        parts.append(
            f'<circle cx="{left + plot_w + 20}" cy="{ly:.1f}" r="4" fill="{colour}"/>'
            f'<text x="{left + plot_w + 32}" y="{ly + 4:.1f}" font-size="10.5">'
            f"{_esc(label)}</text>"
        )

    parts.append(
        f'<text x="{left + plot_w / 2:.0f}" y="{height - 8}" font-size="10.5" '
        f'text-anchor="middle" class="mcb-muted">execution position'
        f'{" · " + _esc(unit) if unit else ""}</text>'
        f'<line class="mcb-axis" x1="{left}" y1="{top}" x2="{left}" '
        f'y2="{top + plot_h}"/>'
        f'<line class="mcb-axis" x1="{left}" y1="{top + plot_h}" '
        f'x2="{left + plot_w}" y2="{top + plot_h}"/>'
    )
    return _svg(width, height, "".join(parts), title)


def interaction_plot(
    *,
    none: float,
    a: float,
    b: float,
    ab: float,
    label_a: str,
    label_b: str,
    title: str,
    unit: str = "",
    width: int = 560,
    height: int = 320,
) -> str:
    """Observed A+B cost against the additive prediction.

    Two bars and a marker: what the pair actually costs, and what it would cost
    if the two mods' costs simply added. The gap between them is the
    interaction, and seeing it drawn is far more persuasive than a signed
    percentage in a table.
    """
    predicted = a + b - none
    left, right, top, bottom = 62, 30, 62, 58
    plot_w, plot_h = width - left - right, height - top - bottom

    values = [none, a, b, ab, predicted]
    axis_max = max(values) * 1.18 or 1.0

    def y_of(value: float) -> float:
        return top + (1 - value / axis_max) * plot_h

    bars = [
        ("baseline", none, PALETTE[7]),
        (label_a, a, PALETTE[0]),
        (label_b, b, PALETTE[1]),
        ("both", ab, PALETTE[3]),
    ]
    slot = plot_w / len(bars)
    bar_w = slot * 0.52

    parts = [f'<text x="12" y="26" font-size="14" font-weight="600">{_esc(title)}</text>']
    excess = ab - predicted
    verdict = (
        "costs more together than the parts predict" if excess > 0
        else "costs less together than the parts predict" if excess < 0
        else "additive"
    )
    parts.append(
        f'<text x="12" y="44" font-size="10.5" class="mcb-muted">'
        f"dashed line = additive prediction · {_esc(verdict)}</text>"
    )

    for tick in _nice_ticks(0, axis_max, 4):
        if tick > axis_max:
            continue
        y = y_of(tick)
        parts.append(
            f'<line class="mcb-grid" x1="{left}" y1="{y:.1f}" '
            f'x2="{left + plot_w}" y2="{y:.1f}"/>'
            f'<text x="{left - 8}" y="{y + 4:.1f}" font-size="10" '
            f'text-anchor="end" class="mcb-muted">{_esc(_fmt_tick(tick))}</text>'
        )

    for index, (label, value, colour) in enumerate(bars):
        cx = left + slot * (index + 0.5)
        y = y_of(value)
        parts.append(
            f'<rect x="{cx - bar_w / 2:.1f}" y="{y:.1f}" width="{bar_w:.1f}" '
            f'height="{top + plot_h - y:.1f}" rx="2" fill="{colour}" '
            f'fill-opacity="0.82"/>'
            f'<text x="{cx:.1f}" y="{top + plot_h + 18}" font-size="10.5" '
            f'text-anchor="middle">{_esc(label)}</text>'
            f'<text x="{cx:.1f}" y="{y - 6:.1f}" font-size="10" '
            f'text-anchor="middle" class="mcb-muted">{_esc(_fmt_tick(value))}</text>'
        )

    both_cx = left + slot * 3.5
    py = y_of(predicted)
    parts.append(
        f'<line x1="{both_cx - bar_w * 0.85:.1f}" y1="{py:.1f}" '
        f'x2="{both_cx + bar_w * 0.85:.1f}" y2="{py:.1f}" '
        f'stroke="{PALETTE[5]}" stroke-width="2.4" stroke-dasharray="6 4"/>'
        f'<text x="{both_cx + bar_w * 0.9:.1f}" y="{py - 5:.1f}" font-size="9.5" '
        f'text-anchor="end" fill="{PALETTE[5]}">predicted '
        f"{_esc(_fmt_tick(predicted))}</text>"
    )

    if unit:
        parts.append(
            f'<text x="{left + plot_w / 2:.0f}" y="{height - 8}" font-size="10.5" '
            f'text-anchor="middle" class="mcb-muted">{_esc(unit)}</text>'
        )
    parts.append(
        f'<line class="mcb-axis" x1="{left}" y1="{top}" x2="{left}" '
        f'y2="{top + plot_h}"/>'
        f'<line class="mcb-axis" x1="{left}" y1="{top + plot_h}" '
        f'x2="{left + plot_w}" y2="{top + plot_h}"/>'
    )
    return _svg(width, height, "".join(parts), title)
