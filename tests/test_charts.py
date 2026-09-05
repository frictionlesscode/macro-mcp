import pytest

from macro_mcp import charts
from macro_mcp.models import ValidationError


def _s(date, intake=None, target=None, status="complete"):
    return {"date": date, "status": status, "intake": intake, "target": target}


# --- line_chart -----------------------------------------------------------------------


def test_line_chart_rejects_empty_series():
    with pytest.raises(ValidationError, match="no points"):
        charts.line_chart([], "kcal")


def test_line_chart_null_when_no_day_has_a_logged_total():
    series = [_s("2026-09-01", intake=None), _s("2026-09-02", intake=None)]
    result = charts.line_chart(series, "kcal")
    assert result["svg"] is None
    assert result["svg_null_reason"]
    assert result["points_plotted"] == 0


def test_line_chart_produces_svg_when_data_present():
    series = [
        _s("2026-09-01", intake=2000, target=2100),
        _s("2026-09-02", intake=None, target=2100),  # gap, not a zero
        _s("2026-09-03", intake=2200, target=2100),
    ]
    result = charts.line_chart(series, "kcal")
    assert result["svg"].startswith("<svg")
    assert result["svg_null_reason"] is None
    assert result["points_plotted"] == 2


def test_line_chart_escapes_untrusted_text_in_title():
    series = [_s("2026-09-01", intake=2000)]
    result = charts.line_chart(series, "kcal", title="<script>alert(1)</script>")
    assert "<script>" not in result["svg"]
    assert "&lt;script&gt;" in result["svg"]


# --- deviation_bars ---------------------------------------------------------------------


def test_deviation_bars_null_when_no_paired_days():
    series = [_s("2026-09-01", intake=2000, target=None), _s("2026-09-02", intake=None, target=2000)]
    result = charts.deviation_bars(series, "kcal")
    assert result["svg"] is None
    assert result["svg_null_reason"]


def test_deviation_bars_produces_svg_for_paired_days():
    series = [
        _s("2026-09-01", intake=2200, target=2000),
        _s("2026-09-02", intake=1800, target=2000),
    ]
    result = charts.deviation_bars(series, "kcal")
    assert result["svg"].startswith("<svg")
    assert result["points_plotted"] == 2


# --- point_series_chart -------------------------------------------------------------------


def test_point_series_chart_null_when_no_data():
    result = charts.point_series_chart([], "Body fat %")
    assert result["svg"] is None
    assert "body fat" in result["svg_null_reason"].lower()


def test_point_series_chart_produces_svg_when_data_present():
    points = [{"date": "2026-09-01", "value": 18.5}, {"date": "2026-09-08", "value": 18.2}]
    result = charts.point_series_chart(points, "Body fat %")
    assert result["svg"].startswith("<svg")
    assert result["points_plotted"] == 2


def test_point_series_chart_marks_estimated_points_distinctly():
    """A photo-based or typed body-fat guess (method="estimate") must not render
    indistinguishably from a real scale/DEXA reading -- see dashboard.py's _weight_bodyfat_chart.
    """
    points = [
        {"date": "2026-09-01", "value": 18.5, "estimated": False},
        {"date": "2026-09-08", "value": 17.0, "estimated": True},
    ]
    result = charts.point_series_chart(points, "Body fat %")
    assert 'class="intake-dot"' in result["svg"]
    assert 'class="estimate-dot"' in result["svg"]
    assert "2026-09-08: 17.0 (estimate)" in result["svg"]
    assert "2026-09-01: 18.5 (estimate)" not in result["svg"]


# --- dual_axis_chart ----------------------------------------------------------------------


def test_dual_axis_chart_null_when_both_series_empty():
    result = charts.dual_axis_chart("2026-09-01", "2026-09-03", {}, "Weight (lb)", {}, "Body fat %")
    assert result["svg"] is None
    assert "weight" in result["svg_null_reason"].lower()
    assert "body fat" in result["svg_null_reason"].lower()


def test_dual_axis_chart_rejects_end_before_start():
    with pytest.raises(ValidationError, match="must not be before"):
        charts.dual_axis_chart("2026-09-05", "2026-09-01", {"2026-09-05": 180}, "Weight (lb)", {}, "Body fat %")


def test_dual_axis_chart_renders_with_only_one_side_populated():
    left = {"2026-09-01": 180.0, "2026-09-03": 179.0}
    result = charts.dual_axis_chart("2026-09-01", "2026-09-03", left, "Weight (lb)", {}, "Body fat %")
    assert result["svg"].startswith("<svg")
    assert result["points_plotted_left"] == 2
    assert result["points_plotted_right"] == 0
    assert 'class="bf-line"' not in result["svg"]
    assert 'class="intake-line"' in result["svg"]


def test_dual_axis_chart_connects_across_gaps_with_a_straight_line():
    """Missing days are interpolated visually (one continuous line, no invented point at
    the gap) rather than breaking the line -- no fabricated data, just a straight line
    between the two real readings on either side.
    """
    left = {"2026-09-01": 180.0, "2026-09-05": 178.0}  # gap on 09-02..09-04
    result = charts.dual_axis_chart("2026-09-01", "2026-09-05", left, "Weight (lb)", {}, "Body fat %")
    path = result["svg"].split('class="intake-line" d="')[1].split('"')[0]
    assert path.count("M") == 1
    assert path.count("L") == 1


def test_dual_axis_chart_marks_estimated_right_points():
    left = {"2026-09-01": 180.0}
    right = {"2026-09-01": 18.5, "2026-09-02": 17.0}
    result = charts.dual_axis_chart(
        "2026-09-01", "2026-09-02", left, "Weight (lb)", right, "Body fat %",
        right_estimated={"2026-09-02": True},
    )
    assert 'class="bf-dot"' in result["svg"]
    assert 'class="bf-estimate-dot"' in result["svg"]
    assert "17.0 (estimate)" in result["svg"]


def test_dual_axis_chart_embeds_hover_geometry_attributes():
    left = {"2026-09-01": 180.0, "2026-09-08": 178.0}
    result = charts.dual_axis_chart("2026-09-01", "2026-09-08", left, "Weight (lb)", {}, "Body fat %")
    svg = result["svg"]
    assert 'id="trend-chart"' in svg
    assert 'data-start="2026-09-01"' in svg
    assert 'data-end="2026-09-08"' in svg
    assert 'data-pad-left="52"' in svg
    expected_plot_w = charts.WIDTH - charts.PAD_LEFT - charts.PAD_RIGHT_DUAL
    assert f'data-plot-w="{expected_plot_w:.2f}"' in svg
    assert f'data-plot-top="{charts.PAD_TOP}"' in svg
    assert f'data-plot-bottom="{charts.HEIGHT - charts.PAD_BOTTOM}"' in svg


# --- _nice_bounds ------------------------------------------------------------------------


def test_nice_bounds_handles_degenerate_single_value():
    lo, hi = charts._nice_bounds([2000])
    assert lo < 2000 < hi
