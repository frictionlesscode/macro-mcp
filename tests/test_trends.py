import pytest

from macro_mcp import trends
from macro_mcp.models import ValidationError


def _point(date, status, **macros):
    base = {"kcal": None, "protein_g": None, "carb_g": None, "fat_g": None, "fiber_g": None}
    base.update(macros)
    return {"date": date, "status": status, **base}


def _target(**macros):
    base = {"kcal": 0, "protein_g": 0, "carb_g": 0, "fat_g": 0, "fiber_g": 0}
    base.update(macros)
    return base


# --- validation ------------------------------------------------------------------------


def test_rejects_unknown_metric():
    with pytest.raises(ValidationError, match="unknown metric"):
        trends.compute([], {}, metrics=["calories"])


def test_rolling_average_rejects_non_positive_window():
    with pytest.raises(ValidationError):
        trends.rolling_average([], "kcal", window=0)


# --- suppression on sparse data --------------------------------------------------------


def test_suppresses_below_minimum_days():
    points = [_point(f"2026-09-0{i}", "complete", kcal=2000) for i in range(1, 4)]
    result = trends.compute(points, {}, minimum_days=7)
    assert result["averages"] is None
    assert result["adherence"] is None
    assert "3" in result["suppressed_reason"]
    assert "7" in result["suppressed_reason"]


def test_meets_minimum_produces_real_statistics():
    points = [_point(f"2026-09-{i:02d}", "complete", kcal=2000) for i in range(1, 8)]
    result = trends.compute(points, {}, minimum_days=7)
    assert result["suppressed_reason"] is None
    assert result["averages"]["kcal"] == 2000


# --- unlogged days are excluded, not zeroed --------------------------------------------


def test_unlogged_days_excluded_from_average():
    points = (
        [_point(f"2026-09-{i:02d}", "complete", kcal=2000) for i in range(1, 8)]
        + [_point(f"2026-09-{i:02d}", "unlogged") for i in range(8, 12)]
    )
    result = trends.compute(points, {}, minimum_days=7)
    # if unlogged days counted as zero, the average would be well below 2000
    assert result["averages"]["kcal"] == 2000
    assert result["coverage"]["days_unlogged"] == 4


def test_partial_days_excluded_from_average_too():
    points = (
        [_point(f"2026-09-{i:02d}", "complete", kcal=2000) for i in range(1, 8)]
        + [_point("2026-09-08", "partial", kcal=50)]  # a half-logged day, not a real total
    )
    result = trends.compute(points, {}, minimum_days=7)
    assert result["averages"]["kcal"] == 2000
    assert result["coverage"]["days_partial"] == 1


def test_series_carries_none_not_zero_for_unlogged_days():
    points = [_point("2026-09-01", "unlogged")]
    result = trends.compute(points, {}, minimum_days=1)
    assert result["series"]["kcal"][0]["intake"] is None


# --- adherence ---------------------------------------------------------------------


def test_adherence_days_over_under_on_target():
    points = [
        _point("2026-09-01", "complete", kcal=2200),  # over (target 2000, +10%)
        _point("2026-09-02", "complete", kcal=1800),  # under (-10%)
        _point("2026-09-03", "complete", kcal=2010),  # within band (+0.5%)
        _point("2026-09-04", "complete", kcal=2000),  # exact
        _point("2026-09-05", "complete", kcal=2200),
        _point("2026-09-06", "complete", kcal=1800),
        _point("2026-09-07", "complete", kcal=2010),
    ]
    by_day = {p["date"]: _target(kcal=2000) for p in points}
    result = trends.compute(points, by_day, metrics=["kcal"], minimum_days=7)
    adherence = result["adherence"]["kcal"]
    assert adherence["days_compared"] == 7
    assert adherence["days_over"] == 2
    assert adherence["days_under"] == 2
    assert adherence["days_on_target"] == 3


def test_adherence_bias_vs_scatter_are_reported_separately():
    """A day 100 over and a day 100 under average to zero bias but nonzero scatter -- the
    two numbers must not collapse into one, or a real problem (wild swings) would be
    invisible behind a falsely reassuring mean.
    """
    points = [
        _point("2026-09-01", "complete", kcal=2100),
        _point("2026-09-02", "complete", kcal=1900),
        _point("2026-09-03", "complete", kcal=2100),
        _point("2026-09-04", "complete", kcal=1900),
        _point("2026-09-05", "complete", kcal=2100),
        _point("2026-09-06", "complete", kcal=1900),
        _point("2026-09-07", "complete", kcal=2000),
    ]
    by_day = {p["date"]: _target(kcal=2000) for p in points}
    result = trends.compute(points, by_day, metrics=["kcal"], minimum_days=7)
    adherence = result["adherence"]["kcal"]
    assert adherence["mean_deviation"] == pytest.approx(0.0, abs=1e-6)
    assert adherence["mean_abs_deviation"] > 50


def test_adherence_null_reason_when_no_days_have_both_intake_and_target():
    points = [_point(f"2026-09-{i:02d}", "complete", kcal=2000) for i in range(1, 8)]
    result = trends.compute(points, {}, metrics=["kcal"], minimum_days=7)  # no targets at all
    assert result["adherence"]["kcal"]["days_compared"] == 0
    assert result["adherence"]["kcal"]["null_reason"]


def test_coverage_always_present_even_when_suppressed():
    points = [_point("2026-09-01", "complete", kcal=2000)]
    result = trends.compute(points, {}, minimum_days=7)
    assert result["coverage"]["days_requested"] == 1
    assert result["coverage"]["days_complete"] == 1


# --- rolling_average ---------------------------------------------------------------


def test_rolling_average_skips_unlogged_days():
    points = [
        _point("2026-09-01", "complete", kcal=2000),
        _point("2026-09-02", "unlogged"),
        _point("2026-09-03", "complete", kcal=2000),
    ]
    out = trends.rolling_average(points, "kcal", window=3)
    # the unlogged day must not drag the average toward zero
    assert out[-1]["value"] == 2000


def test_rolling_average_null_before_half_the_window_is_usable():
    points = [_point("2026-09-01", "unlogged")]
    out = trends.rolling_average(points, "kcal", window=7)
    assert out[0]["value"] is None
