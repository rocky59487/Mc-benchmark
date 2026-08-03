"""Tests for chart and table export.

Charts are validated as well-formed XML and checked for self-containment: a
report that silently depends on a CDN stops rendering the moment it is opened
offline or in five years, which defeats the point of a citable artefact.
"""

from __future__ import annotations

import xml.etree.ElementTree as ET

import pytest

from mcbench.export import (
    Series,
    distribution_cdf,
    forest_plot,
    interaction_plot,
    interval_bar_chart,
    order_effect_scatter,
    render_html_report,
    tables_for,
    to_csv,
    to_html,
    to_markdown,
    to_tsv,
)
from mcbench.export.tables import Table, write_all
from mcbench.metrics import RunMetrics
from mcbench.planner import Cell
from mcbench.report import SuiteResult, aggregate_cell, compare_to_baseline


def _parses(svg: str) -> ET.Element:
    return ET.fromstring(svg)


@pytest.fixture
def sample_result():
    def runs(mean, n=7):
        return [
            RunMetrics(values={
                "frametime_mean_ms": mean + (i - n // 2) * 0.1,
                "fps_avg": 1000.0 / (mean + (i - n // 2) * 0.1),
            })
            for i in range(n)
        ]

    baseline_cell = Cell("visual-biome-flyby", "vanilla")
    variant_cell = Cell("visual-biome-flyby", "sodium")
    baseline = aggregate_cell(baseline_cell, runs(20.0))
    variant = aggregate_cell(variant_cell, runs(13.0))

    result = SuiteResult(
        suite_name="Test suite",
        baseline="vanilla",
        cells={baseline_cell: baseline, variant_cell: variant},
        generated_at="2026-08-02T00:00:00+00:00",
    )
    result.comparisons.extend(compare_to_baseline(baseline, variant))
    return result


class TestCharts:
    def test_interval_bar_chart_is_well_formed(self):
        svg = interval_bar_chart(
            [Series("vanilla", 20.0, 19.5, 20.5), Series("sodium", 13.0, 12.7, 13.3)],
            title="Mean frametime", unit="ms",
        )
        root = _parses(svg)
        assert root.tag.endswith("svg")
        assert "Mean frametime" in svg

    def test_bars_are_zero_based_by_default(self):
        """A truncated axis exaggerates differences without stating anything false."""
        svg = interval_bar_chart(
            [Series("a", 100.0, 99.0, 101.0), Series("b", 102.0, 101.0, 103.0)],
            title="t",
        )
        assert "0" in svg
        _parses(svg)

    def test_interval_whiskers_are_drawn(self):
        with_ci = interval_bar_chart([Series("a", 10.0, 9.0, 11.0)], title="t")
        without = interval_bar_chart([Series("a", 10.0)], title="t")
        assert with_ci.count("mcb-whisker") > 0
        assert without.count("mcb-whisker") == 0

    def test_forest_plot_draws_the_rope_band(self):
        svg = forest_plot(
            [Series("sodium", -0.35, -0.38, -0.32, annotation="improvement")],
            title="Change vs baseline", rope=0.02,
        )
        _parses(svg)
        assert "mcb-rope" in svg
        assert "practical equivalence" in svg

    def test_distribution_cdf_is_well_formed(self):
        svg = distribution_cdf(
            {"vanilla": [16.6] * 100 + [30.0] * 5, "sodium": [11.0] * 100 + [20.0] * 5},
            title="Frametime CDF",
        )
        _parses(svg)
        assert "polyline" in svg

    def test_cdf_downsamples_large_inputs(self):
        """A vertex per frame would be megabytes and no more legible."""
        svg = distribution_cdf({"big": list(range(100_000))}, title="t")
        _parses(svg)
        vertices = svg.count(",")
        assert vertices < 2000

    def test_order_effect_scatter_is_well_formed(self):
        svg = order_effect_scatter(
            {"vanilla": [(0, 20.0), (2, 20.4)], "sodium": [(1, 13.0), (3, 13.2)]},
            title="Order effects",
        )
        _parses(svg)
        assert "circle" in svg

    def test_interaction_plot_shows_the_additive_prediction(self):
        svg = interaction_plot(
            none=10.0, a=12.0, b=14.0, ab=30.0,
            label_a="sodium", label_b="lithium",
            title="Interaction",
        )
        _parses(svg)
        assert "predicted" in svg
        assert "stroke-dasharray" in svg

    def test_charts_handle_empty_input_without_raising(self):
        for svg in (
            interval_bar_chart([], title="t"),
            forest_plot([], title="t"),
            distribution_cdf({}, title="t"),
            order_effect_scatter({}, title="t"),
        ):
            _parses(svg)
            assert "no data" in svg

    def test_flat_series_does_not_divide_by_zero(self):
        svg = interval_bar_chart([Series("a", 5.0, 5.0, 5.0)], title="t")
        _parses(svg)
        svg = distribution_cdf({"a": [7.0] * 50}, title="t")
        _parses(svg)

    def test_labels_are_escaped(self):
        svg = interval_bar_chart(
            [Series("<script>alert(1)</script>", 1.0)], title="t & t"
        )
        _parses(svg)
        assert "<script>" not in svg


class TestTables:
    def test_builds_tables_from_a_result(self, sample_result):
        tables = tables_for(sample_result)
        names = {t.name for t in tables}
        assert "comparisons" in names
        assert "cells" in names

    def test_csv_round_trips(self, sample_result):
        import csv
        import io

        table = tables_for(sample_result)[0]
        rows = list(csv.reader(io.StringIO(to_csv(table))))
        assert rows[0] == table.columns
        assert len(rows) == len(table.rows) + 1

    def test_tsv_uses_tabs(self, sample_result):
        table = tables_for(sample_result)[0]
        assert "\t" in to_tsv(table)

    def test_comparisons_carry_interval_columns(self, sample_result):
        table = next(t for t in tables_for(sample_result) if t.name == "comparisons")
        # A bare point estimate would let the interval get lost downstream.
        assert "delta_ci_low_pct" in table.columns
        assert "delta_ci_high_pct" in table.columns
        assert "verdict" in table.columns

    def test_markdown_escapes_pipes(self):
        table = Table("t", ["c"], [["a|b"]])
        assert r"a\|b" in to_markdown(table)

    def test_html_escapes_and_marks_numeric_cells(self):
        table = Table("t", ["c", "n"], [["<b>x</b>", 42.0]])
        rendered = to_html(table)
        assert "<b>x</b>" not in rendered
        assert "&lt;b&gt;" in rendered
        assert "mcb-num" in rendered

    def test_runs_table_preserves_individual_runs(self, sample_result):
        raw = {
            "visual-biome-flyby/vanilla": [
                {"values": {"frametime_mean_ms": 20.0}, "position": 0},
                {"values": {"frametime_mean_ms": 20.2}, "position": 3},
            ]
        }
        tables = tables_for(sample_result, raw_runs=raw)
        runs = next(t for t in tables if t.name == "runs")
        assert len(runs.rows) == 2
        assert "position" in runs.columns

    def test_write_all_emits_requested_formats(self, sample_result, tmp_path):
        written = write_all(
            tables_for(sample_result), str(tmp_path), formats=("csv", "md")
        )
        assert any(p.endswith(".csv") for p in written)
        assert any(p.endswith(".md") for p in written)
        for path in written:
            assert (tmp_path / path.rsplit("/", 1)[-1]).stat().st_size > 0

    def test_write_all_rejects_an_unknown_format(self, sample_result, tmp_path):
        with pytest.raises(ValueError, match="unknown table format"):
            write_all(tables_for(sample_result), str(tmp_path), formats=("pdf",))


class TestHtmlReport:
    def test_renders_valid_self_contained_html(self, sample_result):
        page = render_html_report(sample_result)
        assert page.startswith("<!doctype html>")
        assert "</html>" in page
        # Self-containment is the whole point: no CDN, no fonts, no images.
        assert "http://" not in page.replace("http://www.w3.org/2000/svg", "")
        assert "https://" not in page

    def test_every_embedded_svg_parses(self, sample_result):
        import re

        page = render_html_report(sample_result)
        svgs = re.findall(r"<svg\b.*?</svg>", page, re.S)
        assert svgs
        for svg in svgs:
            _parses(svg)

    def test_is_theme_aware(self, sample_result):
        page = render_html_report(sample_result)
        assert "prefers-color-scheme" in page
        assert 'data-theme="dark"' in page

    def test_caveats_appear_before_the_numbers(self, sample_result):
        """A reader who stops after one screen must get the qualifications."""
        cell = next(iter(sample_result.cells.values()))
        cell.runs_admissible = 2
        page = render_html_report(sample_result)
        assert "Caveats" in page
        assert page.index("Caveats") < page.index("Data tables")

    def test_surfaces_preflight_blockers(self, sample_result):
        page = render_html_report(
            sample_result,
            preflight={"blockers": [{"name": "gpu", "detail": "no graphics device"}],
                       "warnings": []},
        )
        assert "no graphics device" in page

    def test_includes_order_effect_chart_when_positions_are_known(self, sample_result):
        page = render_html_report(
            sample_result,
            order_points={
                "visual-biome-flyby": {
                    "vanilla": [(0, 20.0), (2, 20.3)],
                    "sodium": [(1, 13.0), (3, 13.1)],
                }
            },
        )
        assert "Order effects" in page

    def test_escapes_a_hostile_suite_name(self):
        result = SuiteResult(suite_name="<script>alert(1)</script>", baseline="b")
        page = render_html_report(result)
        assert "<script>alert(1)</script>" not in page
