"""Chart and table export.

Everything here is dependency-free and self-contained: SVG charts built by hand,
tables rendered to CSV/TSV/Markdown/HTML, and a single-file HTML report that
opens anywhere without fetching a thing.
"""

from .charts import (
    PALETTE,
    Series,
    chart_styles,
    distribution_cdf,
    forest_plot,
    interaction_plot,
    interval_bar_chart,
    order_effect_scatter,
)
from .html_report import render_html_report
from .tables import (
    Table,
    cells_table,
    comparisons_table,
    interactions_table,
    runs_table,
    tables_for,
    to_csv,
    to_html,
    to_markdown,
    to_tsv,
    write_all,
)

__all__ = [
    "PALETTE", "Series", "chart_styles", "distribution_cdf", "forest_plot",
    "interaction_plot", "interval_bar_chart", "order_effect_scatter",
    "render_html_report",
    "Table", "cells_table", "comparisons_table", "interactions_table",
    "runs_table", "tables_for", "to_csv", "to_html", "to_markdown", "to_tsv",
    "write_all",
]
