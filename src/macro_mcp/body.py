"""Body composition: the one piece of body data macro-mcp owns.

garmin-mcp owns weight (SPEC.md, this project's "Locked decisions"); it also declared body
composition and circumference tracking a non-goal. Since goals here can terminate on a body-fat
percentage, that data needs a home, and this is it -- ``body_comp`` is system of record for
percent-fat, independent of whether a push back to Garmin succeeds.

Pushing to Garmin (``push_to_garmin=True``) was investigated in M4 and deliberately left
unimplemented -- not because it's unbuilt, but because it was tested live against the real
account and doesn't work. ``garminconnect``'s ``add_body_composition()`` uploads a synthetic
FIT file through Garmin's generic device-data pipeline; two live tests (a date with an existing
same-day weigh-in, and a clean date with none) both reported upload success but produced zero
observable effect on the actual data. Enabling this would mean claiming ``pushed: true`` for a
write with no verified effect, which is worse than being honest that it doesn't happen. See
SPEC.md's M4 section for the full evidence. Revisit only if a future `garminconnect` version
or a different write path changes this.
"""

from __future__ import annotations

import sqlite3
from datetime import date as Date
from typing import Any

from .models import BODY_COMP_METHODS, ValidationError, iso, now, require, today
from .store import transaction

PUSH_NOT_WIRED_REASON = (
    "body-fat push tested live against the real account (SPEC.md M4) and had no observable "
    "effect on Garmin's data -- deliberately not enabled, not just unbuilt"
)


def log_body_comp(
    conn: sqlite3.Connection,
    percent_fat: float,
    method: str = "scale",
    day: Date | str | None = None,
    push_to_garmin: bool = False,
) -> dict[str, Any]:
    require(method, BODY_COMP_METHODS, "method")
    if not 0 < percent_fat < 100:
        raise ValidationError(f"percent_fat must be between 0 and 100; got {percent_fat}")

    target = day.isoformat() if isinstance(day, Date) else (day or today().isoformat())
    Date.fromisoformat(target)  # validate shape if caller passed a string

    pushed = False
    push_error = PUSH_NOT_WIRED_REASON if push_to_garmin else None

    stamp = iso(now())
    with transaction(conn):
        conn.execute(
            """INSERT INTO body_comp
               (day, percent_fat, method, pushed_to_garmin, push_error, created_at, updated_at)
               VALUES (?,?,?,?,?,?,?)
               ON CONFLICT(day) DO UPDATE SET
                   percent_fat = excluded.percent_fat,
                   method = excluded.method,
                   pushed_to_garmin = excluded.pushed_to_garmin,
                   push_error = excluded.push_error,
                   updated_at = excluded.updated_at""",
            (target, percent_fat, method, int(pushed), push_error, stamp, stamp),
        )

    return {
        "ok": True,
        "day": target,
        "percent_fat": percent_fat,
        "method": method,
        "pushed": pushed,
        "push_error": push_error,
    }


def get_body_comp(conn: sqlite3.Connection, days: int = 90) -> dict[str, Any]:
    if days <= 0:
        raise ValidationError("days must be positive")

    end = today()
    start = Date.fromordinal(end.toordinal() - days + 1)
    rows = conn.execute(
        "SELECT * FROM body_comp WHERE day BETWEEN ? AND ? ORDER BY day",
        (start.isoformat(), end.isoformat()),
    ).fetchall()

    points = [
        {"date": r["day"], "percent_fat": r["percent_fat"], "method": r["method"]}
        for r in rows
    ]
    return {
        "points": points,
        "latest": points[-1] if points else None,
        "pushed_to_garmin": bool(rows[-1]["pushed_to_garmin"]) if rows else None,
    }
