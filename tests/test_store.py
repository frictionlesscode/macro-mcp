import pytest

from macro_mcp.store import SCHEMA_VERSION, day_status, schema_version, touch_day


def test_schema_initialises_with_version(db):
    assert schema_version(db) == SCHEMA_VERSION


def test_schema_init_is_idempotent(db):
    """No seeded reference data exists to check counts on since the charter change removed
    day_type (SPEC.md "Charter change (2026-08-14)") -- what's left to verify is just that
    re-running init_schema against an already-initialised database doesn't raise.
    """
    from macro_mcp.store import init_schema

    init_schema(db)  # must not raise on a database that already has these tables
    assert schema_version(db) == SCHEMA_VERSION


def test_day_target_table_exists_and_is_empty_on_init(db):
    rows = db.execute("SELECT * FROM day_target").fetchall()
    assert rows == []


def test_absent_day_is_unlogged_not_zero(db):
    assert day_status(db, "2026-01-01") == "unlogged"


def test_touch_day_defaults_to_partial(db):
    """Logging one meal must never assert that the whole day is accounted for."""
    touch_day(db, "2026-01-01")
    assert day_status(db, "2026-01-01") == "partial"


def test_touch_day_does_not_downgrade_a_complete_day(db):
    from macro_mcp import foods

    foods.set_day_status(db, "2026-01-01", "complete")
    touch_day(db, "2026-01-01")
    assert day_status(db, "2026-01-01") == "complete"


def test_foreign_keys_are_enforced(db):
    with pytest.raises(Exception):
        db.execute(
            "INSERT INTO recipe_ingredient (recipe_id, library_food_id, grams) "
            "VALUES (999, 999, 10)"
        )


def test_negative_macros_rejected_at_the_db_layer(db):
    """The dataclass validates too; this guards the path that bypasses it."""
    with pytest.raises(Exception):
        db.execute(
            """INSERT INTO food_entry
               (group_id, logged_at, day, meal, name, kcal, protein_g, carb_g, fat_g,
                fiber_g, source, confidence, created_at, updated_at)
               VALUES ('g','t','2026-01-01','lunch','x',-5,0,0,0,0,'estimate','high','t','t')"""
        )
