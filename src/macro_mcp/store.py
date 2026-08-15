"""SQLite access and schema.

The whole system of record for food, the library, stored targets, and body composition is one
file. See README "Backup" — that file is the thing worth protecting.
"""

from __future__ import annotations

import os
import sqlite3
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator

from .models import now, iso

#: v3 dropped the server-side target-derivation tables (goal, day_type, training_plan,
#: day_plan, proposal) in favour of a single day_target table holding exactly what Claude set
#: -- see SPEC.md "Charter change (2026-08-14)". There was no live data to migrate, so this is
#: a straight replacement rather than a migration; CREATE TABLE IF NOT EXISTS will not drop
#: the retired tables from an older file, so an existing database must be recreated.
#: v4 added body_photo (progress photos + cached pose landmarks) -- purely additive, no
#: existing table changed.
SCHEMA_VERSION = 4

DEFAULT_DB_PATH = "./data/macro.db"

SCHEMA = """
CREATE TABLE IF NOT EXISTS day_log (
    day        TEXT PRIMARY KEY,
    status     TEXT NOT NULL CHECK (status IN ('complete','partial','unlogged')),
    notes      TEXT,
    updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS library_food (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    name         TEXT NOT NULL,
    brand        TEXT,
    barcode      TEXT,
    serving_desc TEXT NOT NULL,
    serving_g    REAL,
    kcal         REAL NOT NULL,
    protein_g    REAL NOT NULL,
    carb_g       REAL NOT NULL,
    fat_g        REAL NOT NULL,
    fiber_g      REAL NOT NULL DEFAULT 0,
    source       TEXT NOT NULL CHECK (source IN ('label','barcode','library','estimate')),
    times_used   INTEGER NOT NULL DEFAULT 0,
    last_used    TEXT,
    created_at   TEXT NOT NULL
);
CREATE UNIQUE INDEX IF NOT EXISTS ux_library_barcode
    ON library_food(barcode) WHERE barcode IS NOT NULL;
CREATE INDEX IF NOT EXISTS ix_library_name ON library_food(name);

CREATE TABLE IF NOT EXISTS recipe (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    name       TEXT NOT NULL,
    servings   REAL NOT NULL CHECK (servings > 0),
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS recipe_ingredient (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    recipe_id       INTEGER NOT NULL REFERENCES recipe(id) ON DELETE CASCADE,
    library_food_id INTEGER NOT NULL REFERENCES library_food(id),
    grams           REAL,
    servings        REAL,
    CHECK (grams IS NOT NULL OR servings IS NOT NULL)
);
CREATE INDEX IF NOT EXISTS ix_recipe_ing ON recipe_ingredient(recipe_id);

CREATE TABLE IF NOT EXISTS food_entry (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    group_id        TEXT NOT NULL,
    logged_at       TEXT NOT NULL,
    day             TEXT NOT NULL,
    meal            TEXT NOT NULL CHECK (meal IN ('breakfast','lunch','dinner','snack','other')),
    description     TEXT,
    name            TEXT NOT NULL,
    qty             REAL,
    unit            TEXT,
    kcal            REAL NOT NULL CHECK (kcal >= 0),
    protein_g       REAL NOT NULL CHECK (protein_g >= 0),
    carb_g          REAL NOT NULL CHECK (carb_g >= 0),
    fat_g           REAL NOT NULL CHECK (fat_g >= 0),
    fiber_g         REAL NOT NULL DEFAULT 0 CHECK (fiber_g >= 0),
    source          TEXT NOT NULL CHECK (source IN ('label','barcode','library','estimate')),
    confidence      TEXT NOT NULL CHECK (confidence IN ('high','medium','low')),
    library_food_id INTEGER REFERENCES library_food(id),
    planned         INTEGER NOT NULL DEFAULT 0 CHECK (planned IN (0,1)),
    created_at      TEXT NOT NULL,
    updated_at      TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS ix_entry_day ON food_entry(day);
CREATE INDEX IF NOT EXISTS ix_entry_group ON food_entry(group_id);

-- Body composition. Weight deliberately lives in garmin-mcp, not here; body fat lives here
-- because garmin-mcp declared body composition a non-goal, leaving this the only home for it.
-- Tracking only -- nothing derives from it (SPEC.md "Data model").
CREATE TABLE IF NOT EXISTS body_comp (
    day               TEXT PRIMARY KEY,
    percent_fat       REAL NOT NULL CHECK (percent_fat > 0 AND percent_fat < 100),
    method            TEXT NOT NULL CHECK (method IN ('scale','calipers','dexa','estimate')),
    pushed_to_garmin  INTEGER NOT NULL DEFAULT 0 CHECK (pushed_to_garmin IN (0,1)),
    push_error        TEXT,
    created_at        TEXT NOT NULL,
    updated_at        TEXT NOT NULL
);

-- Targets exactly as Claude set them for a date. No day types, no relative weights, no
-- recurrence, no derivation -- Claude owns the cadence and writes each date explicitly.
-- Replaces the goal / day_type / training_plan / day_plan / proposal tables, which
-- implemented server-side target derivation; see SPEC.md "Charter change (2026-08-14)".
CREATE TABLE IF NOT EXISTS day_target (
    day        TEXT PRIMARY KEY,
    kcal       REAL NOT NULL CHECK (kcal >= 0),
    protein_g  REAL NOT NULL CHECK (protein_g >= 0),
    carb_g     REAL NOT NULL CHECK (carb_g >= 0),
    fat_g      REAL NOT NULL CHECK (fat_g >= 0),
    fiber_g    REAL NOT NULL DEFAULT 0 CHECK (fiber_g >= 0),
    note       TEXT,
    set_at     TEXT NOT NULL
);

-- Progress photos. One per (day, angle) -- a later save for the same day+angle overwrites,
-- matching day_target's upsert-by-date pattern. file_path points at a JPEG under PHOTO_DIR;
-- the image itself is never stored in the DB. landmarks_json caches the pose landmarks
-- detected at save time (see body_photos.py) so alignment for the dashboard doesn't re-run
-- pose detection on every render; it is null when detection failed or wasn't available.
CREATE TABLE IF NOT EXISTS body_photo (
    day            TEXT NOT NULL,
    angle          TEXT NOT NULL DEFAULT 'front' CHECK (angle IN ('front','side','back')),
    file_path      TEXT NOT NULL,
    width          INTEGER NOT NULL,
    height         INTEGER NOT NULL,
    landmarks_json TEXT,
    align_status   TEXT NOT NULL DEFAULT 'pending' CHECK (align_status IN ('pending','ok','failed')),
    align_reason   TEXT,
    note           TEXT,
    created_at     TEXT NOT NULL,
    PRIMARY KEY (day, angle)
);
CREATE INDEX IF NOT EXISTS ix_body_photo_angle ON body_photo(angle);
"""


def db_path() -> Path:
    return Path(os.environ.get("SQLITE_PATH") or DEFAULT_DB_PATH)


def connect(path: str | Path | None = None) -> sqlite3.Connection:
    """Open a connection with the invariants this project relies on already set."""
    target = Path(path) if path is not None else db_path()
    if str(target) != ":memory:":
        target.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(target), isolation_level=None)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA journal_mode = WAL")
    conn.execute("PRAGMA busy_timeout = 5000")
    return conn


def init_schema(conn: sqlite3.Connection) -> None:
    conn.executescript(SCHEMA)
    conn.execute(f"PRAGMA user_version = {SCHEMA_VERSION}")


def schema_version(conn: sqlite3.Connection) -> int:
    return int(conn.execute("PRAGMA user_version").fetchone()[0])


def open_db(path: str | Path | None = None) -> sqlite3.Connection:
    """Connect and ensure the schema exists. The normal entry point."""
    conn = connect(path)
    init_schema(conn)
    return conn


@contextmanager
def transaction(conn: sqlite3.Connection) -> Iterator[sqlite3.Connection]:
    """Explicit transaction. ``isolation_level=None`` means we drive BEGIN/COMMIT ourselves."""
    conn.execute("BEGIN")
    try:
        yield conn
    except Exception:
        conn.execute("ROLLBACK")
        raise
    conn.execute("COMMIT")


def touch_day(conn: sqlite3.Connection, day: str) -> None:
    """Ensure a day_log row exists for a day that now has entries.

    Defaults to ``partial``, never ``complete``. Logging breakfast does not mean the day is
    fully logged, and assuming otherwise would feed a half-day's intake into trend statistics
    as if it were the whole day's real total.
    """
    conn.execute(
        "INSERT INTO day_log (day, status, updated_at) VALUES (?, 'partial', ?) "
        "ON CONFLICT(day) DO NOTHING",
        (day, iso(now())),
    )


def day_status(conn: sqlite3.Connection, day: str) -> str:
    """Status of a day. A day with no row at all is ``unlogged`` — absent, not zero."""
    row = conn.execute("SELECT status FROM day_log WHERE day = ?", (day,)).fetchone()
    return row["status"] if row else "unlogged"
