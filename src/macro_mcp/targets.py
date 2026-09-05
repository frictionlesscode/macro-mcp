"""Stored macro targets — storage, not derivation.

This module deliberately contains no nutritional logic. It does not know what a training day
is, does not distribute a weekly budget, does not compute anything from bodyweight or
expenditure. Claude decides the numbers and the cadence; this keeps them and hands them back.

That is a reversal of an earlier design in which the server derived targets from a goal rate
and a TDEE estimate. See SPEC.md "Charter change (2026-08-14)" for why it was removed: the
derivation was a nutritional decision wearing arithmetic's clothes, and it made a
fat-cycling protocol inexpressible.

The one computation retained is Atwater energy derivation when ``kcal`` is omitted, because a
target is a *specification* — 190P/60C/32F has exactly one energy content. That is the
opposite of the rule for logged food, whose stated calories are never rewritten (see
``foods.log_food``): a logged entry is testimony, and testimony is preserved verbatim.
"""

from __future__ import annotations

import sqlite3
from datetime import date as Date, timedelta
from typing import Any, Iterable, Sequence

from .foods import ATWATER_TOLERANCE
from .models import MACRO_FIELDS, Macros, ValidationError, iso, now, today
from .store import transaction


def _as_day(day: Date | str) -> str:
    if isinstance(day, Date):
        return day.isoformat()
    Date.fromisoformat(day)  # validate shape, raise on garbage
    return day


def _coerce(entry: dict[str, Any]) -> tuple[str, Macros, str | None, bool, float]:
    """Validate one target entry -> (day, macros, note, kcal_was_derived, implied_kcal)."""
    if "date" not in entry:
        raise ValidationError("each target needs a 'date'")
    day = _as_day(entry["date"])

    unknown = set(entry) - {"date", "note", *MACRO_FIELDS}
    if unknown:
        raise ValidationError(
            f"unknown field(s) for {day}: {', '.join(sorted(unknown))}; "
            f"expected date, note, and any of {', '.join(MACRO_FIELDS)}"
        )

    for field in ("protein_g", "carb_g", "fat_g"):
        if field not in entry:
            raise ValidationError(f"{day} is missing {field}")

    try:
        macros = Macros(
            kcal=float(entry.get("kcal", 0.0) or 0.0),
            protein_g=float(entry["protein_g"]),
            carb_g=float(entry["carb_g"]),
            fat_g=float(entry["fat_g"]),
            fiber_g=float(entry.get("fiber_g", 0.0) or 0.0),
        )
    except (TypeError, ValueError) as exc:
        raise ValidationError(f"{day}: macro values must be numeric ({exc})") from exc
    macros.validate()

    implied = macros.implied_kcal()
    derived = False
    if macros.kcal == 0 and implied > 0:
        macros = Macros(kcal=implied, protein_g=macros.protein_g, carb_g=macros.carb_g,
                        fat_g=macros.fat_g, fiber_g=macros.fiber_g)
        derived = True

    note = entry.get("note")
    return day, macros, note, derived, implied


def set_targets(
    conn: sqlite3.Connection, targets: Sequence[dict[str, Any]]
) -> dict[str, Any]:
    """Store targets for one or more dates, replacing any already set for those dates.

    Bulk by design. Claude owns the cadence, so it writes every date explicitly rather than
    the server inferring a recurrence — but a month-long protocol should still be one call,
    not thirty.

    The whole batch is validated before anything is written, so a malformed entry at position
    20 doesn't leave the first 19 applied.
    """
    if not targets:
        raise ValidationError("no targets supplied")

    prepared = [_coerce(t) for t in targets]

    days = [p[0] for p in prepared]
    duplicates = {d for d in days if days.count(d) > 1}
    if duplicates:
        raise ValidationError(
            f"the same date appears more than once: {', '.join(sorted(duplicates))}"
        )

    stamp = iso(now())
    with transaction(conn):
        for day, macros, note, _, _ in prepared:
            conn.execute(
                """INSERT INTO day_target
                   (day, kcal, protein_g, carb_g, fat_g, fiber_g, note, set_at)
                   VALUES (?,?,?,?,?,?,?,?)
                   ON CONFLICT(day) DO UPDATE SET
                       kcal = excluded.kcal, protein_g = excluded.protein_g,
                       carb_g = excluded.carb_g, fat_g = excluded.fat_g,
                       fiber_g = excluded.fiber_g, note = excluded.note,
                       set_at = excluded.set_at""",
                (day, macros.kcal, macros.protein_g, macros.carb_g, macros.fat_g,
                 macros.fiber_g, note, stamp),
            )

    derived = [d for d, _, _, was_derived, _ in prepared if was_derived]
    mismatched = [
        f"{d}: stated {m.kcal:.0f} kcal vs {implied:.0f} implied by its macros"
        for d, m, _, was_derived, implied in prepared
        if not was_derived and implied > 0
        and abs(implied - m.kcal) / max(implied, m.kcal) > ATWATER_TOLERANCE
    ]

    result: dict[str, Any] = {"ok": True, "count": len(prepared), "dates": sorted(days)}
    if derived:
        result["kcal_derived_for"] = sorted(derived)
        result["note"] = (
            f"kcal wasn't supplied for {len(derived)} date(s), so it was derived from the "
            f"macros via Atwater (4/4/9). Pass kcal explicitly to override."
        )
    if mismatched:
        # Stated values are kept as given -- flagged, never rewritten.
        result["warnings"] = mismatched
    return result


def get_targets(conn: sqlite3.Connection, day: Date | str | None = None) -> dict[str, Any]:
    """Targets for a date, or a null with a reason when none have been set for it."""
    target_day = today().isoformat() if day is None else _as_day(day)
    row = conn.execute("SELECT * FROM day_target WHERE day = ?", (target_day,)).fetchone()

    if row is None:
        return {
            "date": target_day,
            "targets": None,
            "targets_null_reason": f"no targets set for {target_day} (see set_targets)",
            "note": None,
            "set_at": None,
        }

    return {
        "date": target_day,
        "targets": {f: round(row[f], 1) for f in MACRO_FIELDS},
        "targets_null_reason": None,
        "note": row["note"],
        "set_at": row["set_at"],
    }


def get_targets_range(
    conn: sqlite3.Connection, start: Date | str, end: Date | str
) -> dict[str, dict[str, float]]:
    """All targets set within an inclusive date range, keyed by date.

    Dates without targets are simply absent rather than present-with-nulls -- callers
    (trends, charts) need to distinguish "no target set" from "target of zero".
    """
    first, last = _as_day(start), _as_day(end)
    rows = conn.execute(
        "SELECT * FROM day_target WHERE day BETWEEN ? AND ? ORDER BY day", (first, last)
    ).fetchall()
    return {r["day"]: {f: round(r[f], 1) for f in MACRO_FIELDS} for r in rows}


def delete_targets(conn: sqlite3.Connection, day: Date | str) -> dict[str, Any]:
    target_day = _as_day(day)
    with transaction(conn):
        cur = conn.execute("DELETE FROM day_target WHERE day = ?", (target_day,))
    return {"ok": True, "date": target_day, "existed": cur.rowcount > 0}


def compare(totals: dict[str, float], targets: dict[str, float] | None) -> dict[str, Any] | None:
    """Intake vs target for a single day: what's left, and what's over.

    ``remaining`` is signed -- negative means over target. ``over`` lists only the macros
    actually exceeded, so a caller doesn't have to re-derive that from the signs.
    """
    if targets is None:
        return None
    remaining = {f: round(targets[f] - totals.get(f, 0.0), 1) for f in MACRO_FIELDS}
    return {
        "remaining": remaining,
        "over": sorted(f for f, v in remaining.items() if v < 0),
        "within": sorted(f for f, v in remaining.items() if v >= 0),
    }
