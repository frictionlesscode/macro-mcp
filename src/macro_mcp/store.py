"""SQLite access and schema.

The whole system of record for food, library, goals, and body composition is one file.
See README "Backup" — that file is the thing worth protecting.
"""

from __future__ import annotations

import os
import sqlite3
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator

from .models import now, iso

#: Bumped when the targets engine added goal.protein_g_per_lb/fat_g_per_lb_floor/
#: start_weight_lb/start_percent_fat. No real deployment exists yet to migrate, so this is a
#: straight schema edit (CREATE TABLE IF NOT EXISTS won't retroactively add columns to an
#: existing file) rather than a real migration -- worth revisiting once a real DB is at stake.
SCHEMA_VERSION = 2

DEFAULT_DB_PATH = "./data/macro.db"

# Seeded day types. Weights are relative, not absolute: the weekly energy budget is divided
# across the week in proportion to each day's energy_weight. carb_weight biases how a day's
# share is split once protein and the fat floor are satisfied. Both are tunable — the owner's
# programme, not ours, decides what "heavy" means.
DEFAULT_DAY_TYPES = [
    ("rest", 0.90, 0.80),
    ("moderate", 1.00, 1.00),
    ("heavy", 1.15, 1.30),
]

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
-- because garmin-mcp declared body composition a non-goal and goals can terminate on BF%.
CREATE TABLE IF NOT EXISTS body_comp (
    day               TEXT PRIMARY KEY,
    percent_fat       REAL NOT NULL CHECK (percent_fat > 0 AND percent_fat < 100),
    method            TEXT NOT NULL CHECK (method IN ('scale','calipers','dexa','estimate')),
    pushed_to_garmin  INTEGER NOT NULL DEFAULT 0 CHECK (pushed_to_garmin IN (0,1)),
    push_error        TEXT,
    created_at        TEXT NOT NULL,
    updated_at        TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS goal (
    id                  INTEGER PRIMARY KEY AUTOINCREMENT,
    mode                TEXT NOT NULL CHECK (mode IN ('cut','bulk','maintain')),
    rate_lb_per_week    REAL NOT NULL,
    protein_g_per_lb    REAL NOT NULL CHECK (protein_g_per_lb > 0),
    fat_g_per_lb_floor  REAL NOT NULL CHECK (fat_g_per_lb_floor > 0),
    stop_metric         TEXT NOT NULL CHECK (stop_metric IN ('weight','bodyfat','date','none')),
    stop_value          TEXT,
    start_weight_lb     REAL,
    start_percent_fat   REAL,
    successor_goal_id   INTEGER REFERENCES goal(id),
    status              TEXT NOT NULL CHECK (status IN ('active','met','superseded','abandoned')),
    started_on          TEXT NOT NULL,
    ended_on            TEXT,
    created_at          TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS ix_goal_status ON goal(status);

CREATE TABLE IF NOT EXISTS day_type (
    name          TEXT PRIMARY KEY,
    energy_weight REAL NOT NULL CHECK (energy_weight > 0),
    carb_weight   REAL NOT NULL CHECK (carb_weight > 0)
);

CREATE TABLE IF NOT EXISTS training_plan (
    weekday  INTEGER PRIMARY KEY CHECK (weekday BETWEEN 0 AND 6),
    day_type TEXT NOT NULL REFERENCES day_type(name)
);

CREATE TABLE IF NOT EXISTS day_plan (
    day             TEXT PRIMARY KEY,
    day_type        TEXT REFERENCES day_type(name),
    explicit_macros TEXT,
    source          TEXT NOT NULL CHECK (source IN ('plan','override','reconciled')),
    updated_at      TEXT NOT NULL,
    CHECK (day_type IS NOT NULL OR explicit_macros IS NOT NULL)
);

CREATE TABLE IF NOT EXISTS proposal (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    kind       TEXT NOT NULL CHECK (kind IN ('target','transition','reconciliation')),
    created_on TEXT NOT NULL,
    payload    TEXT NOT NULL,
    status     TEXT NOT NULL DEFAULT 'pending'
               CHECK (status IN ('pending','accepted','declined')),
    decided_on TEXT,
    decline_reason TEXT
);
CREATE INDEX IF NOT EXISTS ix_proposal_status ON proposal(status);
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
    _seed_day_types(conn)
    conn.execute(f"PRAGMA user_version = {SCHEMA_VERSION}")


def _seed_day_types(conn: sqlite3.Connection) -> None:
    for name, energy, carb in DEFAULT_DAY_TYPES:
        conn.execute(
            "INSERT OR IGNORE INTO day_type (name, energy_weight, carb_weight) "
            "VALUES (?, ?, ?)",
            (name, energy, carb),
        )


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
    fully logged, and assuming otherwise would feed a half-day's intake into the expenditure
    fit as if it were a real deficit.
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
