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


# --- _nice_bounds ------------------------------------------------------------------------


def test_nice_bounds_handles_degenerate_single_value():
    lo, hi = charts._nice_bounds([2000])
    assert lo < 2000 < hi
