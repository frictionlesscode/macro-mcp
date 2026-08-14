from datetime import date, timedelta

import pytest

from macro_mcp import goals
from macro_mcp.models import ValidationError


# --- set_goal ----------------------------------------------------------------------


def test_set_goal_rejects_unknown_mode(db):
    with pytest.raises(ValidationError):
        goals.set_goal(db, "shred", -1.0, 1.0, 0.35, "none", None)


def test_set_goal_rejects_unknown_stop_metric(db):
    with pytest.raises(ValidationError):
        goals.set_goal(db, "cut", -1.0, 1.0, 0.35, "vibes", None)


def test_set_goal_requires_stop_value_unless_none(db):
    with pytest.raises(ValidationError, match="stop_value is required"):
        goals.set_goal(db, "cut", -1.0, 1.0, 0.35, "weight", None)
    # 'none' is the one metric that doesn't need a stop_value
    result = goals.set_goal(db, "maintain", 0.0, 1.0, 0.35, "none", None)
    assert result["ok"]


def test_set_goal_rejects_non_positive_protein_or_fat(db):
    with pytest.raises(ValidationError):
        goals.set_goal(db, "cut", -1.0, 0, 0.35, "none", None)
    with pytest.raises(ValidationError):
        goals.set_goal(db, "cut", -1.0, 1.0, -0.1, "none", None)


def test_set_goal_rejects_unknown_successor(db):
    with pytest.raises(ValidationError, match="successor"):
        goals.set_goal(db, "cut", -1.0, 1.0, 0.35, "none", None, successor_goal_id=999)


def test_set_goal_weekly_budget_is_null_without_tdee(db):
    result = goals.set_goal(db, "cut", -1.0, 1.0, 0.35, "none", None)
    assert result["weekly_budget"] is None
    assert "weekly_budget_null_reason" in result


def test_set_goal_computes_weekly_budget_when_tdee_given(db):
    result = goals.set_goal(db, "cut", -1.0, 1.0, 0.35, "none", None, tdee=2600.0)
    assert result["weekly_budget"] == pytest.approx(7 * 2600 - 3500)


def test_set_goal_implied_rate_needs_current_weight(db):
    result = goals.set_goal(db, "cut", -1.0, 1.0, 0.35, "none", None)
    assert result["implied_rate_pct_bodyweight"] is None
    result2 = goals.set_goal(db, "cut", -1.0, 1.0, 0.35, "none", None, current_weight_lb=200.0)
    assert result2["implied_rate_pct_bodyweight"] == pytest.approx(-0.5)


def test_set_goal_supersedes_the_previous_active_goal(db):
    first = goals.set_goal(db, "maintain", 0.0, 1.0, 0.35, "none", None)
    second = goals.set_goal(db, "cut", -1.0, 1.0, 0.35, "none", None)
    row = db.execute("SELECT status, ended_on FROM goal WHERE id = ?", (first["goal_id"],)).fetchone()
    assert row["status"] == "superseded"
    assert row["ended_on"] is not None
    active = db.execute("SELECT status FROM goal WHERE id = ?", (second["goal_id"],)).fetchone()
    assert active["status"] == "active"


def test_only_one_active_goal_at_a_time(db):
    goals.set_goal(db, "maintain", 0.0, 1.0, 0.35, "none", None)
    goals.set_goal(db, "cut", -1.0, 1.0, 0.35, "none", None)
    goals.set_goal(db, "bulk", 0.5, 1.0, 0.3, "none", None)
    active = db.execute("SELECT COUNT(*) c FROM goal WHERE status = 'active'").fetchone()
    assert active["c"] == 1


# --- get_goal ------------------------------------------------------------------------


def test_get_goal_with_no_active_goal(db):
    assert goals.get_goal(db) == {"active": False}


def test_get_goal_weight_progress_partway_through_a_cut(db):
    goals.set_goal(db, "cut", -1.0, 1.0, 0.35, "weight", "185", current_weight_lb=200.0)
    # lost 5 of the 15 lb needed
    result = goals.get_goal(db, current_weight_lb=195.0, trend_lb_per_week=-1.0)
    # result["progress"] is rounded to 4dp by get_goal -- compare with matching tolerance
    assert result["progress"] == pytest.approx(5 / 15, abs=1e-4)
    assert result["stop_condition_met"] is False
    assert result["projected_completion"] is not None


def test_get_goal_weight_stop_condition_met(db):
    goals.set_goal(db, "cut", -1.0, 1.0, 0.35, "weight", "185", current_weight_lb=200.0)
    result = goals.get_goal(db, current_weight_lb=184.0, trend_lb_per_week=-1.0)
    assert result["stop_condition_met"] is True
    assert result["progress"] > 1.0  # past the target, not clamped for weight/bodyfat
    assert result["projected_completion_null_reason"] == "already met"


def test_get_goal_projection_null_when_trend_points_wrong_way(db):
    goals.set_goal(db, "cut", -1.0, 1.0, 0.35, "weight", "185", current_weight_lb=200.0)
    # trending upward while trying to cut down to 185
    result = goals.get_goal(db, current_weight_lb=195.0, trend_lb_per_week=+0.5)
    assert result["projected_completion"] is None
    assert "toward the goal" in result["projected_completion_null_reason"]


def test_get_goal_progress_null_without_current_weight(db):
    goals.set_goal(db, "cut", -1.0, 1.0, 0.35, "weight", "185", current_weight_lb=200.0)
    result = goals.get_goal(db)
    assert result["progress"] is None
    assert result["progress_null_reason"]


def test_get_goal_progress_null_without_a_start_snapshot(db):
    # current_weight_lb omitted at set_goal time -- no baseline recorded
    goals.set_goal(db, "cut", -1.0, 1.0, 0.35, "weight", "185")
    result = goals.get_goal(db, current_weight_lb=195.0)
    assert result["progress"] is None
    assert "no starting value" in result["progress_null_reason"]


def test_get_goal_bodyfat_progress(db):
    goals.set_goal(db, "cut", -1.0, 1.0, 0.35, "bodyfat", "15", current_percent_fat=22.0)
    result = goals.get_goal(db, current_percent_fat=18.5)
    assert result["progress"] == pytest.approx((22.0 - 18.5) / (22.0 - 15.0))
    assert result["stop_condition_met"] is False
    assert "isn't modeled" in result["projected_completion_null_reason"]


def test_get_goal_date_metric_progress(db):
    started = date.today()
    stop = (started + timedelta(days=30)).isoformat()
    goals.set_goal(db, "cut", -1.0, 1.0, 0.35, "date", stop)
    result = goals.get_goal(db)
    assert 0.0 <= result["progress"] <= 0.1  # just started
    assert result["stop_condition_met"] is False
    assert result["projected_completion"] == stop


def test_get_goal_none_metric_has_no_progress(db):
    goals.set_goal(db, "maintain", 0.0, 1.0, 0.35, "none", None)
    result = goals.get_goal(db)
    assert result["progress"] is None
    assert result["stop_condition_met"] is False


# --- training plan / day plan --------------------------------------------------------


def test_set_training_plan_rejects_bad_weekday(db):
    with pytest.raises(ValidationError):
        goals.set_training_plan(db, {7: "heavy"})


def test_set_training_plan_rejects_unknown_day_type(db):
    with pytest.raises(ValidationError):
        goals.set_training_plan(db, {0: "ultra-day"})


def test_set_training_plan_upserts(db):
    goals.set_training_plan(db, {0: "heavy", 1: "rest"})
    goals.set_training_plan(db, {0: "moderate"})  # overwrite Monday only
    rows = {r["weekday"]: r["day_type"] for r in db.execute("SELECT * FROM training_plan")}
    assert rows[0] == "moderate"
    assert rows[1] == "rest"


def test_set_day_plan_requires_exactly_one_of_day_type_or_macros(db):
    with pytest.raises(ValidationError):
        goals.set_day_plan(db, "2026-01-01")
    with pytest.raises(ValidationError):
        goals.set_day_plan(db, "2026-01-01", day_type="heavy",
                           macros={"kcal": 1, "protein_g": 1, "carb_g": 1, "fat_g": 1})


def test_set_day_plan_rejects_unknown_day_type(db):
    with pytest.raises(ValidationError):
        goals.set_day_plan(db, "2026-01-01", day_type="ultra-day")


def test_set_day_plan_rejects_malformed_macros(db):
    with pytest.raises(ValidationError):
        goals.set_day_plan(db, "2026-01-01", macros={"not_a_real_field": 1})


def test_set_day_plan_with_day_type(db):
    result = goals.set_day_plan(db, "2026-01-01", day_type="heavy")
    assert result["ok"]
    assert result["day_type"] == "heavy"
    assert result["explicit_macros"] is None


def test_set_day_plan_with_explicit_macros_normalizes_the_response(db):
    result = goals.set_day_plan(db, "2026-01-01",
                                macros={"kcal": 2000, "protein_g": 150, "carb_g": 200, "fat_g": 60})
    assert result["explicit_macros"]["kcal"] == 2000
    assert result["explicit_macros"]["fiber_g"] == 0.0  # normalized in, defaulted


def test_set_day_plan_upserts(db):
    goals.set_day_plan(db, "2026-01-01", day_type="heavy")
    goals.set_day_plan(db, "2026-01-01", day_type="rest")
    row = db.execute("SELECT day_type FROM day_plan WHERE day = '2026-01-01'").fetchone()
    assert row["day_type"] == "rest"


# --- weekly resolution composition --------------------------------------------------


def test_get_targets_null_without_active_goal(db):
    result = goals.get_targets(db, tdee=2600.0, trend_weight_lb=190.0)
    assert result["kcal"] is None
    assert "no active goal" in result["targets_null_reason"]


def test_get_targets_null_without_tdee(db):
    goals.set_goal(db, "cut", -1.0, 1.0, 0.35, "none", None)
    result = goals.get_targets(db, trend_weight_lb=190.0)
    assert result["kcal"] is None
    assert "TDEE" in result["targets_null_reason"]


def test_get_targets_resolves_with_an_active_goal(db):
    goals.set_goal(db, "cut", -1.0, 1.0, 0.35, "none", None)
    result = goals.get_targets(db, day="2026-08-12", tdee=2600.0, trend_weight_lb=190.0)
    assert result["targets_null_reason"] is None
    assert result["kcal"] > 0
    assert result["protein_g"] == pytest.approx(190.0)
    assert result["day_type"] == "moderate"  # default, no training_plan/day_plan set
    assert result["source"] == "resolved"


def test_get_targets_respects_training_plan(db):
    goals.set_goal(db, "cut", -1.0, 1.0, 0.35, "none", None)
    goals.set_training_plan(db, {2: "heavy"})  # Wednesday = 2 (Monday=0)
    result = goals.get_targets(db, day="2026-08-12", tdee=2600.0, trend_weight_lb=190.0)  # a Wednesday
    assert result["day_type"] == "heavy"


def test_get_targets_day_plan_override_beats_training_plan(db):
    goals.set_goal(db, "cut", -1.0, 1.0, 0.35, "none", None)
    goals.set_training_plan(db, {2: "heavy"})
    goals.set_day_plan(db, "2026-08-12", day_type="rest")
    result = goals.get_targets(db, day="2026-08-12", tdee=2600.0, trend_weight_lb=190.0)
    assert result["day_type"] == "rest"


def test_get_targets_explicit_macros_override(db):
    goals.set_goal(db, "cut", -1.0, 1.0, 0.35, "none", None)
    goals.set_day_plan(db, "2026-08-12",
                       macros={"kcal": 1999, "protein_g": 150, "carb_g": 200, "fat_g": 60})
    result = goals.get_targets(db, day="2026-08-12", tdee=2600.0, trend_weight_lb=190.0)
    assert result["source"] == "explicit"
    assert result["kcal"] == 1999


# --- explicit-macro energy derivation (regression) -----------------------------------


def test_explicit_macros_derive_kcal_when_not_supplied(db):
    """Regression: Macros.kcal defaults to 0.0, so passing only protein/carb/fat stored a
    0-calorie target. Not cosmetic -- resolve_week subtracts explicit_kcal_total from the
    weekly budget, so a 0 made the week treat that day as free and over-allocate its real
    energy as carbs across the remaining days.
    """
    r = goals.set_day_plan(db, "2026-08-14",
                           macros={"protein_g": 190, "carb_g": 60, "fat_g": 32})
    assert r["explicit_macros"]["kcal"] == pytest.approx(190 * 4 + 60 * 4 + 32 * 9)
    assert r["kcal_derived_from_macros"] is True


def test_derived_kcal_is_persisted_not_just_returned(db):
    goals.set_day_plan(db, "2026-08-14",
                       macros={"protein_g": 190, "carb_g": 60, "fat_g": 32})
    import json
    row = db.execute("SELECT explicit_macros FROM day_plan WHERE day = '2026-08-14'").fetchone()
    assert json.loads(row["explicit_macros"])["kcal"] == pytest.approx(1288)


def test_supplied_kcal_is_never_overwritten(db):
    """A caller-stated figure is kept verbatim even if it disagrees with Atwater -- same
    principle as log_food, which reports a mismatch rather than rewriting the number.
    """
    # 2000 vs 1288 implied is ~36% apart, comfortably outside ATWATER_TOLERANCE (20%).
    # (1500 would only be 14% apart and correctly produces no warning.)
    r = goals.set_day_plan(db, "2026-08-14",
                           macros={"kcal": 2000, "protein_g": 190, "carb_g": 60, "fat_g": 32})
    assert r["explicit_macros"]["kcal"] == 2000
    assert "kcal_derived_from_macros" not in r
    assert "warning" in r


def test_supplied_kcal_matching_atwater_produces_no_warning(db):
    r = goals.set_day_plan(db, "2026-08-14",
                           macros={"kcal": 1288, "protein_g": 190, "carb_g": 60, "fat_g": 32})
    assert r["explicit_macros"]["kcal"] == 1288
    assert "warning" not in r


def test_explicit_day_energy_counts_against_the_weekly_budget(db):
    """The end-to-end consequence of the fix: the explicit day carries its real energy into
    the week's accounting, so the seven resolved days still sum to the weekly budget.

    Deliberately asserts that invariant rather than "the other days get fewer carbs". The
    direction of that effect depends on whether the explicit day is above or below what a
    resolved day would have been -- this 1288 kcal day is *below* average, so its neighbours
    actually get MORE carbs, not fewer. (Same trap as
    test_explicit_day_lighter_than_average_leaves_more_carbs_for_the_rest in test_targets.py;
    the summing invariant is direction-independent and is what the bug actually broke.)
    """
    from macro_mcp.targets import resolve_week

    goals.set_goal(db, "cut", -1.0, 1.0, 0.35, "none", None)
    goals.set_day_plan(db, "2026-08-14",
                       macros={"protein_g": 190, "carb_g": 60, "fat_g": 32})

    resolution, reason = goals.resolve_current_week(
        db, tdee=2600.0, trend_weight_lb=190.0, week_of=date(2026, 8, 13),
    )
    assert reason is None

    explicit_day = resolution.target_for("2026-08-14")
    assert explicit_day.explicit is True
    assert explicit_day.macros.kcal == pytest.approx(1288)  # not 0, which was the bug

    total = sum(d.macros.kcal for d in resolution.days)
    assert total == pytest.approx(resolution.weekly_budget_kcal, abs=0.01)
