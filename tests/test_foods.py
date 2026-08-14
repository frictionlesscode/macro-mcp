import pytest

from macro_mcp import foods
from macro_mcp.models import ValidationError


# --- logging -------------------------------------------------------------


def test_log_food_totals_multiple_items(db):
    result = foods.log_food(
        db, "oats and whey", "breakfast",
        [
            {"name": "Oats", "kcal": 300, "protein_g": 10, "carb_g": 54, "fat_g": 5,
             "fiber_g": 8, "source": "label", "confidence": "high"},
            {"name": "Whey", "kcal": 120, "protein_g": 24, "carb_g": 3, "fat_g": 1,
             "source": "label", "confidence": "high"},
        ],
    )
    assert result["ok"] is True
    assert result["totals"]["kcal"] == 420
    assert result["totals"]["protein_g"] == 34
    assert result["item_count"] == 2


def test_log_food_marks_day_partial_not_complete(db):
    result = foods.log_food(
        db, "snack", "snack",
        [{"name": "Apple", "kcal": 95, "protein_g": 0.5, "carb_g": 25, "fat_g": 0.3}],
    )
    assert result["status"] == "partial"


def test_log_food_rejects_unknown_meal(db):
    with pytest.raises(ValidationError):
        foods.log_food(db, "x", "brunch", [{"name": "x", "kcal": 1, "protein_g": 0,
                                            "carb_g": 0, "fat_g": 0}])


def test_log_food_requires_at_least_one_item(db):
    with pytest.raises(ValidationError):
        foods.log_food(db, "x", "lunch", [])


def test_log_food_rejects_negative_macro(db):
    with pytest.raises(ValidationError):
        foods.log_food(db, "x", "lunch", [
            {"name": "x", "kcal": -1, "protein_g": 0, "carb_g": 0, "fat_g": 0}
        ])


def test_planned_meal_excluded_from_actual_totals(db):
    foods.log_food(
        db, "tomorrow's lunch", "lunch",
        [{"name": "Chicken", "kcal": 400, "protein_g": 40, "carb_g": 0, "fat_g": 20}],
        when="2026-01-02T12:00:00", planned=True,
    )
    day = foods.get_day(db, "2026-01-02")
    assert day["totals"]["kcal"] == 0
    assert day["planned_totals"]["kcal"] == 400
    # a purely-planned day must not be marked partial/complete by real intake
    assert day["status"] == "unlogged"


def test_atwater_mismatch_is_reported_not_corrected(db):
    result = foods.log_food(
        db, "x", "lunch",
        [{"name": "Suspicious", "kcal": 100, "protein_g": 50, "carb_g": 50, "fat_g": 50}],
    )
    # implied = 50*4+50*4+50*9 = 850, stated 100 -> should warn
    assert result["warnings"]
    # but the stated value is what's stored, unaltered
    assert result["totals"]["kcal"] == 100


def test_day_boundary_is_midnight_local(db):
    foods.log_food(
        db, "late snack", "snack",
        [{"name": "x", "kcal": 100, "protein_g": 0, "carb_g": 0, "fat_g": 0}],
        when="2026-01-01T23:59:00",
    )
    foods.log_food(
        db, "past midnight", "snack",
        [{"name": "y", "kcal": 50, "protein_g": 0, "carb_g": 0, "fat_g": 0}],
        when="2026-01-02T00:01:00",
    )
    assert foods.get_day(db, "2026-01-01")["totals"]["kcal"] == 100
    assert foods.get_day(db, "2026-01-02")["totals"]["kcal"] == 50


# --- library / quick-repeat ------------------------------------------------


def test_quick_log_by_grams_scales_correctly(db, oats):
    result = foods.log_from_library(db, meal="breakfast", food_id=oats, grams=80.0)
    # 80g is 2x the 40g serving
    assert result["totals"]["kcal"] == 300
    assert result["totals"]["protein_g"] == 10


def test_quick_log_by_servings(db, oats):
    result = foods.log_from_library(db, meal="breakfast", food_id=oats, servings=0.5)
    assert result["totals"]["kcal"] == 75


def test_quick_log_requires_exactly_one_quantity_kind(db, oats):
    with pytest.raises(ValidationError):
        foods.log_from_library(db, meal="breakfast", food_id=oats)
    with pytest.raises(ValidationError):
        foods.log_from_library(db, meal="breakfast", food_id=oats, servings=1, grams=40)


def test_quick_log_by_grams_fails_without_serving_mass(db):
    food_id = foods.save_food(
        db, name="Mystery bar", serving_desc="1 bar",
        kcal=200, protein_g=5, carb_g=30, fat_g=8,
    )["id"]
    with pytest.raises(ValidationError, match="serving mass"):
        foods.log_from_library(db, meal="snack", food_id=food_id, grams=50)


def test_repeat_log_is_source_library_and_identical(db, oats):
    a = foods.log_from_library(db, meal="breakfast", food_id=oats, grams=80.0,
                               when="2026-01-01T08:00:00")
    b = foods.log_from_library(db, meal="breakfast", food_id=oats, grams=80.0,
                               when="2026-01-02T08:00:00")
    item_a = a["entries"][0]["items"][0]
    item_b = b["entries"][-1]["items"][-1]
    assert item_a["source"] == item_b["source"] == "library"
    assert item_a["macros"] == item_b["macros"]


def test_library_use_count_increments(db, oats):
    foods.log_from_library(db, meal="breakfast", food_id=oats, grams=40.0)
    foods.log_from_library(db, meal="breakfast", food_id=oats, grams=40.0)
    row = foods.search_library(db, "Oats")[0]
    assert row["times_used"] == 2


def test_duplicate_barcode_rejected(db):
    foods.save_food(db, name="A", serving_desc="1", kcal=1, protein_g=0, carb_g=0,
                    fat_g=0, barcode="123")
    with pytest.raises(ValidationError, match="already saved"):
        foods.save_food(db, name="B", serving_desc="1", kcal=1, protein_g=0, carb_g=0,
                        fat_g=0, barcode="123")


# --- recipes ---------------------------------------------------------------


def test_recipe_scales_by_servings(db, oats):
    whey_id = foods.save_food(
        db, name="Whey", serving_desc="1 scoop", serving_g=30,
        kcal=120, protein_g=24, carb_g=3, fat_g=1, source="label",
    )["id"]
    recipe_id = foods.save_recipe(
        db, name="Shake", servings=2,
        ingredients=[
            {"library_food_id": oats, "grams": 80},   # 2 servings of oats -> 300 kcal
            {"library_food_id": whey_id, "servings": 1},  # 120 kcal
        ],
    )["id"]
    # total 420 kcal over 2 servings -> 210 kcal/serving
    result = foods.log_from_library(db, meal="breakfast", recipe_id=recipe_id, servings=1)
    assert result["totals"]["kcal"] == 210

    result2 = foods.log_from_library(db, meal="breakfast", recipe_id=recipe_id, servings=2,
                                     when="2026-01-02T08:00:00")
    assert result2["totals"]["kcal"] == 420


def test_recipe_rejects_grams(db, oats):
    recipe_id = foods.save_recipe(
        db, name="Just oats", servings=1, ingredients=[{"library_food_id": oats, "grams": 40}]
    )["id"]
    with pytest.raises(ValidationError, match="servings, not grams"):
        foods.log_from_library(db, meal="breakfast", recipe_id=recipe_id, grams=40)


# --- editing -----------------------------------------------------------------


def test_edit_item_updates_totals(db):
    result = foods.log_food(db, "x", "lunch",
                            [{"name": "Rice", "kcal": 200, "protein_g": 4, "carb_g": 44,
                              "fat_g": 1}])
    item_id = result["entries"][0]["items"][0]["item_id"]
    updated = foods.edit_item(db, item_id, kcal=250)
    assert updated["totals"]["kcal"] == 250


def test_edit_item_rejects_unknown_field(db):
    result = foods.log_food(db, "x", "lunch",
                            [{"name": "Rice", "kcal": 200, "protein_g": 4, "carb_g": 44,
                              "fat_g": 1}])
    item_id = result["entries"][0]["items"][0]["item_id"]
    with pytest.raises(ValidationError):
        foods.edit_item(db, item_id, meal="dinner")  # meal lives on the entry, not the item


def test_delete_entry_removes_all_its_items(db):
    result = foods.log_food(
        db, "x", "lunch",
        [{"name": "A", "kcal": 100, "protein_g": 0, "carb_g": 0, "fat_g": 0},
         {"name": "B", "kcal": 50, "protein_g": 0, "carb_g": 0, "fat_g": 0}],
    )
    entry_id = result["entry_id"]
    after = foods.delete_entry(db, entry_id)
    assert after["totals"]["kcal"] == 0
    assert after["entries"] == []


def test_moving_a_planned_entry_does_not_mark_target_day_partial(db):
    result = foods.log_food(
        db, "planned dinner", "dinner",
        [{"name": "Chicken", "kcal": 400, "protein_g": 40, "carb_g": 0, "fat_g": 20}],
        when="2026-02-01T18:00:00", planned=True,
    )
    foods.edit_entry(db, result["entry_id"], when="2026-02-02T18:00:00")
    assert foods.get_day(db, "2026-02-02")["status"] == "unlogged"


def test_edit_entry_moving_day_touches_both_days(db):
    result = foods.log_food(
        db, "x", "lunch", [{"name": "A", "kcal": 100, "protein_g": 0, "carb_g": 0, "fat_g": 0}],
        when="2026-01-01T12:00:00",
    )
    entry_id = result["entry_id"]
    moved = foods.edit_entry(db, entry_id, when="2026-01-02T12:00:00")
    assert set(moved["days_affected"]) == {"2026-01-01", "2026-01-02"}
    assert foods.get_day(db, "2026-01-01")["totals"]["kcal"] == 0
    assert foods.get_day(db, "2026-01-02")["totals"]["kcal"] == 100


# --- day status --------------------------------------------------------------


def test_set_day_status_requires_valid_value(db):
    with pytest.raises(ValidationError):
        foods.set_day_status(db, "2026-01-01", "done")


def test_set_day_status_preserves_notes_when_not_given(db):
    foods.set_day_status(db, "2026-01-01", "partial", notes="forgot to log dinner")
    result = foods.set_day_status(db, "2026-01-01", "complete")
    row = db.execute("SELECT notes FROM day_log WHERE day='2026-01-01'").fetchone()
    assert row["notes"] == "forgot to log dinner"
    assert result["status"] == "complete"


# --- trend / unlogged-vs-zero invariant --------------------------------------


def test_trend_distinguishes_unlogged_from_zero_intake(db):
    """The invariant the whole expenditure engine depends on (SPEC.md)."""
    foods.log_food(db, "x", "lunch",
                   [{"name": "A", "kcal": 500, "protein_g": 0, "carb_g": 0, "fat_g": 0}],
                   when="2026-01-01T12:00:00")
    foods.set_day_status(db, "2026-01-01", "complete")
    # 2026-01-02 never logged at all

    trend = foods.get_intake_trend(db, days=1)  # window will just be "today" in this fixture's clock
    # Use explicit dates instead of relying on "today" for this assertion:
    by_day = {p["date"]: p for p in
              foods.get_intake_trend(db, days=400)["points"]}
    assert by_day["2026-01-01"]["status"] == "complete"
    assert by_day["2026-01-01"]["kcal"] == 500

    unlogged_days = [p for p in foods.get_intake_trend(db, days=400)["points"]
                     if p["date"] != "2026-01-01"]
    assert all(p["status"] == "unlogged" for p in unlogged_days)
    assert all(p["kcal"] is None for p in unlogged_days)  # never 0


def test_trend_avg_only_uses_complete_days(db):
    foods.log_food(db, "x", "lunch",
                   [{"name": "A", "kcal": 500, "protein_g": 0, "carb_g": 0, "fat_g": 0}],
                   when="2026-01-01T12:00:00")
    foods.set_day_status(db, "2026-01-01", "complete")

    foods.log_food(db, "y", "lunch",
                   [{"name": "B", "kcal": 50, "protein_g": 0, "carb_g": 0, "fat_g": 0}],
                   when="2026-01-02T12:00:00")
    # left as 'partial' (the default) -- e.g. only breakfast was logged

    trend = foods.get_intake_trend(db, days=400)
    assert trend["avg_kcal_complete_days"] == 500.0
    assert trend["days_complete"] == 1
    assert trend["days_partial"] == 1


def test_trend_avg_is_null_with_reason_when_nothing_complete(db):
    foods.log_food(db, "y", "lunch",
                   [{"name": "B", "kcal": 50, "protein_g": 0, "carb_g": 0, "fat_g": 0}],
                   when="2026-01-02T12:00:00")
    trend = foods.get_intake_trend(db, days=400)
    assert trend["avg_kcal_complete_days"] is None
    assert trend["avg_kcal_null_reason"]


def test_get_day_does_not_resolve_targets_at_this_layer(db):
    """foods.get_day is the raw log layer -- no access to the active goal or to garmin-mcp's
    weight data -- so it always returns targets as null, with a reason pointing at the layer
    that does resolve them (server.py's get_day tool / goals.get_targets).

    Asserts the *contract* rather than exact wording. The original version pinned the literal
    string "M3", which encoded a temporary build state as a permanent assertion: it kept
    passing after the target engine shipped, so the suite ended up defending a stale message
    that read as "this feature doesn't exist" long after it did.
    """
    day = foods.get_day(db, "2026-01-01")
    assert day["targets"] is None
    assert day["remaining"] is None
    reason = day["targets_null_reason"]
    assert reason and "get_targets" in reason
    # must not imply the feature is unbuilt -- that's what made the old message misleading
    assert "not implemented" not in reason.lower()
