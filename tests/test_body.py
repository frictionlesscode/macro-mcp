import pytest

from macro_mcp import body
from macro_mcp.models import ValidationError


def test_log_and_get_round_trips(db):
    body.log_body_comp(db, percent_fat=18.5, method="scale", day="2026-01-01")
    # days=400: get_body_comp's window trails from the real current date, not the fixed
    # 2026-01-01 test date, so it must be wide enough to actually contain that day (the
    # same pitfall foods.get_intake_trend's tests avoid the same way).
    result = body.get_body_comp(db, days=400)
    assert result["latest"] == {"date": "2026-01-01", "percent_fat": 18.5, "method": "scale"}


def test_upsert_replaces_same_day_reading(db):
    body.log_body_comp(db, percent_fat=18.5, day="2026-01-01")
    body.log_body_comp(db, percent_fat=17.9, day="2026-01-01")
    result = body.get_body_comp(db, days=400)
    assert len(result["points"]) == 1
    assert result["latest"]["percent_fat"] == 17.9


def test_rejects_out_of_range_percent_fat(db):
    with pytest.raises(ValidationError):
        body.log_body_comp(db, percent_fat=0.0, day="2026-01-01")
    with pytest.raises(ValidationError):
        body.log_body_comp(db, percent_fat=100.0, day="2026-01-01")


def test_rejects_unknown_method(db):
    with pytest.raises(ValidationError):
        body.log_body_comp(db, percent_fat=18.0, method="guess", day="2026-01-01")


def test_push_to_garmin_is_honestly_not_wired_up(db):
    result = body.log_body_comp(db, percent_fat=18.0, day="2026-01-01", push_to_garmin=True)
    assert result["pushed"] is False
    assert "M4" in result["push_error"]


def test_push_not_requested_leaves_push_error_null(db):
    result = body.log_body_comp(db, percent_fat=18.0, day="2026-01-01", push_to_garmin=False)
    assert result["pushed"] is False
    assert result["push_error"] is None


def test_empty_history_returns_null_latest(db):
    result = body.get_body_comp(db, days=30)
    assert result["latest"] is None
    assert result["points"] == []
    assert result["pushed_to_garmin"] is None


def test_days_window_excludes_older_readings(db):
    body.log_body_comp(db, percent_fat=20.0, day="2020-01-01")
    result = body.get_body_comp(db, days=30)
    assert result["points"] == []
