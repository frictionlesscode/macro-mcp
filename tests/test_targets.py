import pytest

from macro_mcp import targets
from macro_mcp.models import ValidationError


# --- set_targets: validation ---------------------------------------------------------


def test_rejects_empty_batch(db):
    with pytest.raises(ValidationError, match="no targets"):
        targets.set_targets(db, [])


def test_rejects_missing_date(db):
    with pytest.raises(ValidationError, match="date"):
        targets.set_targets(db, [{"protein_g": 190, "carb_g": 60, "fat_g": 32}])


def test_rejects_missing_required_macro(db):
    with pytest.raises(ValidationError, match="carb_g"):
        targets.set_targets(db, [{"date": "2026-09-01", "protein_g": 190, "fat_g": 32}])


def test_rejects_unknown_field(db):
    with pytest.raises(ValidationError, match="unknown field"):
        targets.set_targets(db, [{"date": "2026-09-01", "protein_g": 190, "carb_g": 60,
                                  "fat_g": 32, "day_type": "heavy"}])


def test_rejects_negative_macro(db):
    with pytest.raises(ValidationError):
        targets.set_targets(db, [{"date": "2026-09-01", "protein_g": -1, "carb_g": 60, "fat_g": 32}])


def test_rejects_duplicate_date_in_one_batch(db):
    with pytest.raises(ValidationError, match="more than once"):
        targets.set_targets(db, [
            {"date": "2026-09-01", "protein_g": 190, "carb_g": 60, "fat_g": 32},
            {"date": "2026-09-01", "protein_g": 150, "carb_g": 40, "fat_g": 20},
        ])


def test_bad_entry_leaves_nothing_written(db):
    """The whole batch validates before anything is written -- a malformed entry at the end
    must not leave earlier entries partially applied.
    """
    try:
        targets.set_targets(db, [
            {"date": "2026-09-01", "protein_g": 190, "carb_g": 60, "fat_g": 32},
            {"date": "2026-09-02", "protein_g": -1, "carb_g": 60, "fat_g": 32},
        ])
    except ValidationError:
        pass
    assert targets.get_targets(db, "2026-09-01")["targets"] is None


# --- set_targets: Atwater derivation ---------------------------------------------------


def test_derives_kcal_when_omitted(db):
    result = targets.set_targets(db, [
        {"date": "2026-09-01", "protein_g": 190, "carb_g": 60, "fat_g": 32},
    ])
    assert result["ok"]
    assert "2026-09-01" in result["kcal_derived_for"]
    stored = targets.get_targets(db, "2026-09-01")
    assert stored["targets"]["kcal"] == pytest.approx(190 * 4 + 60 * 4 + 32 * 9)


def test_supplied_kcal_is_never_overwritten(db):
    # 2000 vs 1288 implied is ~36% apart -- well outside ATWATER_TOLERANCE (20%), so this
    # should warn but must still store exactly what was given.
    result = targets.set_targets(db, [
        {"date": "2026-09-01", "kcal": 2000, "protein_g": 190, "carb_g": 60, "fat_g": 32},
    ])
    assert "kcal_derived_for" not in result
    assert "warnings" in result
    assert targets.get_targets(db, "2026-09-01")["targets"]["kcal"] == 2000


def test_supplied_kcal_matching_atwater_has_no_warning(db):
    result = targets.set_targets(db, [
        {"date": "2026-09-01", "kcal": 1288, "protein_g": 190, "carb_g": 60, "fat_g": 32},
    ])
    assert "warnings" not in result


def test_zero_kcal_with_all_zero_macros_is_not_treated_as_derivable(db):
    """implied_kcal() is 0 too in this degenerate case -- must not divide by zero or loop."""
    result = targets.set_targets(db, [
        {"date": "2026-09-01", "protein_g": 0, "carb_g": 0, "fat_g": 0},
    ])
    assert result["ok"]
    assert targets.get_targets(db, "2026-09-01")["targets"]["kcal"] == 0


# --- get_targets -----------------------------------------------------------------------


def test_get_targets_null_reason_names_the_date(db):
    result = targets.get_targets(db, "2026-09-01")
    assert result["targets"] is None
    assert "2026-09-01" in result["targets_null_reason"]
    assert "set_targets" in result["targets_null_reason"]


def test_get_targets_defaults_to_today(db, monkeypatch):
    # targets.py does `from .models import today`, binding the function into its own
    # namespace -- patching macro_mcp.models.today has no effect on that already-bound
    # reference, so the patch target has to be targets.today specifically.
    import datetime
    monkeypatch.setattr(targets, "today", lambda: datetime.date(2026, 9, 1))
    targets.set_targets(db, [{"date": "2026-09-01", "protein_g": 190, "carb_g": 60, "fat_g": 32}])
    result = targets.get_targets(db)
    assert result["date"] == "2026-09-01"
    assert result["targets"] is not None


def test_get_targets_returns_the_note(db):
    targets.set_targets(db, [
        {"date": "2026-09-01", "protein_g": 190, "carb_g": 60, "fat_g": 32, "note": "training day"},
    ])
    assert targets.get_targets(db, "2026-09-01")["note"] == "training day"


def test_set_targets_upserts(db):
    targets.set_targets(db, [{"date": "2026-09-01", "protein_g": 190, "carb_g": 60, "fat_g": 32}])
    targets.set_targets(db, [{"date": "2026-09-01", "protein_g": 150, "carb_g": 40, "fat_g": 20}])
    result = targets.get_targets(db, "2026-09-01")
    assert result["targets"]["protein_g"] == 150


# --- get_targets_range --------------------------------------------------------------


def test_get_targets_range_only_includes_set_dates(db):
    targets.set_targets(db, [
        {"date": "2026-09-01", "protein_g": 190, "carb_g": 60, "fat_g": 32},
        {"date": "2026-09-03", "protein_g": 190, "carb_g": 45, "fat_g": 20},
    ])
    result = targets.get_targets_range(db, "2026-09-01", "2026-09-05")
    assert set(result.keys()) == {"2026-09-01", "2026-09-03"}
    assert "2026-09-02" not in result  # absent, not present-with-nulls


# --- delete_targets ------------------------------------------------------------------


def test_delete_targets_reports_whether_it_existed(db):
    targets.set_targets(db, [{"date": "2026-09-01", "protein_g": 190, "carb_g": 60, "fat_g": 32}])
    first = targets.delete_targets(db, "2026-09-01")
    assert first["ok"] and first["existed"] is True
    second = targets.delete_targets(db, "2026-09-01")
    assert second["ok"] and second["existed"] is False
    assert targets.get_targets(db, "2026-09-01")["targets"] is None


# --- compare -------------------------------------------------------------------------


def test_compare_returns_none_without_targets():
    assert targets.compare({"kcal": 500}, None) is None


def test_compare_computes_signed_remaining():
    totals = {"kcal": 1400, "protein_g": 200, "carb_g": 50, "fat_g": 40, "fiber_g": 0}
    target = {"kcal": 1288, "protein_g": 190, "carb_g": 60, "fat_g": 32, "fiber_g": 0}
    result = targets.compare(totals, target)
    # over on kcal/protein/fat (negative remaining), under on carb (positive remaining)
    assert result["remaining"]["kcal"] == pytest.approx(-112)
    assert result["remaining"]["carb_g"] == pytest.approx(10)
    assert set(result["over"]) == {"kcal", "protein_g", "fat_g"}
    assert "carb_g" in result["within"]
