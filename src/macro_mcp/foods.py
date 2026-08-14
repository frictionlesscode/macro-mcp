"""Food logging, the personal library, and recipes.

M1 scope. Targets and expenditure are not computed here — see SPEC.md milestones. Where a
target-dependent field would normally appear, this module returns ``None`` alongside a
``*_reason``, rather than inventing a placeholder.
"""

from __future__ import annotations

import json
import sqlite3
import uuid
from datetime import date as Date, datetime
from typing import Any, Iterable, Sequence

from .models import (
    CONFIDENCES,
    DAY_STATUSES,
    MACRO_FIELDS,
    MEALS,
    SOURCES,
    FoodItem,
    Macros,
    ValidationError,
    iso,
    now,
    require,
    today,
    tz,
)
from .store import day_status, touch_day, transaction

#: Relative gap between stated calories and the Atwater estimate that is worth flagging.
#: Reported, never corrected — see Macros.implied_kcal.
ATWATER_TOLERANCE = 0.20


# --- helpers -----------------------------------------------------------------


def _resolve_when(when: datetime | str | None) -> tuple[str, str]:
    """Return ``(logged_at_iso, day_iso)``. Day boundary is midnight local."""
    if when is None:
        moment = now()
    elif isinstance(when, str):
        moment = datetime.fromisoformat(when)
        if moment.tzinfo is None:
            moment = moment.replace(tzinfo=tz())
    else:
        moment = when if when.tzinfo else when.replace(tzinfo=tz())
    return iso(moment), moment.date().isoformat()


def _as_day(day: Date | str | None) -> str:
    if day is None:
        return today().isoformat()
    if isinstance(day, Date):
        return day.isoformat()
    Date.fromisoformat(day)  # validate shape, raise on garbage
    return day


def _atwater_warning(items: Sequence[FoodItem]) -> list[str]:
    warnings: list[str] = []
    for item in items:
        implied = item.macros.implied_kcal()
        stated = item.kcal
        if stated <= 0 and implied <= 0:
            continue
        baseline = max(implied, stated, 1.0)
        if abs(implied - stated) / baseline > ATWATER_TOLERANCE:
            warnings.append(
                f"{item.name}: stated {stated:.0f} kcal vs {implied:.0f} implied by "
                f"macros ({abs(implied - stated):.0f} apart). Stored as given."
            )
    return warnings


def _row_macros(row: sqlite3.Row) -> Macros:
    return Macros.from_row(row)


# --- logging -----------------------------------------------------------------


def log_food(
    conn: sqlite3.Connection,
    description: str,
    meal: str,
    items: Iterable[FoodItem | dict[str, Any]],
    when: datetime | str | None = None,
    planned: bool = False,
) -> dict[str, Any]:
    """Log a meal as one or more items.

    Items are stored individually so a single component can later be corrected without
    re-estimating the whole plate. They share a ``group_id``, which is the ``entry_id``
    callers use to edit or delete the meal.
    """
    require(meal, MEALS, "meal")
    parsed = [
        item.validate() if isinstance(item, FoodItem) else FoodItem.from_dict(item)
        for item in items
    ]
    if not parsed:
        raise ValidationError("at least one item is required")

    logged_at, day = _resolve_when(when)
    group_id = uuid.uuid4().hex
    stamp = iso(now())

    with transaction(conn):
        for item in parsed:
            conn.execute(
                """INSERT INTO food_entry
                   (group_id, logged_at, day, meal, description, name, qty, unit,
                    kcal, protein_g, carb_g, fat_g, fiber_g,
                    source, confidence, library_food_id, planned, created_at, updated_at)
                   VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (
                    group_id, logged_at, day, meal, description, item.name,
                    item.qty, item.unit,
                    item.kcal, item.protein_g, item.carb_g, item.fat_g, item.fiber_g,
                    item.source, item.confidence, item.library_food_id,
                    1 if planned else 0, stamp, stamp,
                ),
            )
            if item.library_food_id is not None and not planned:
                _bump_library_use(conn, item.library_food_id, stamp)
        if not planned:
            touch_day(conn, day)

    result = {
        "ok": True,
        "entry_id": group_id,
        "item_count": len(parsed),
        **get_day(conn, day),
    }
    warnings = _atwater_warning(parsed)
    if warnings:
        result["warnings"] = warnings
    return result


def log_from_library(
    conn: sqlite3.Connection,
    meal: str,
    food_id: int | None = None,
    recipe_id: int | None = None,
    servings: float | None = None,
    grams: float | None = None,
    when: datetime | str | None = None,
    planned: bool = False,
) -> dict[str, Any]:
    """Log a previously-saved food or recipe.

    Exactly one of ``food_id``/``recipe_id``, and exactly one of ``servings``/``grams``.
    The SPEC's single ``qty`` argument was ambiguous about units; splitting it removes a
    class of silent 100x errors, which matter more here than signature brevity.
    """
    if (food_id is None) == (recipe_id is None):
        raise ValidationError("pass exactly one of food_id or recipe_id")
    if (servings is None) == (grams is None):
        raise ValidationError("pass exactly one of servings or grams")

    if food_id is not None:
        items = [_library_item(conn, food_id, servings, grams)]
        description = items[0].name
    else:
        items, description = _recipe_items(conn, recipe_id, servings, grams)

    return log_food(conn, description, meal, items, when=when, planned=planned)


def _library_item(
    conn: sqlite3.Connection,
    food_id: int,
    servings: float | None,
    grams: float | None,
) -> FoodItem:
    row = conn.execute("SELECT * FROM library_food WHERE id = ?", (food_id,)).fetchone()
    if row is None:
        raise ValidationError(f"no library food with id {food_id}")

    if grams is not None:
        if not row["serving_g"]:
            raise ValidationError(
                f"'{row['name']}' has no serving mass recorded, so it cannot be logged "
                f"by grams. Log it by servings, or save it again with serving_g set."
            )
        factor = grams / float(row["serving_g"])
        qty, unit = grams, "g"
    else:
        factor = float(servings)
        qty, unit = servings, "serving"

    if factor <= 0:
        raise ValidationError("quantity must be positive")

    scaled = _row_macros(row).scaled(factor)
    label = row["name"] if not row["brand"] else f"{row['brand']} {row['name']}"
    return FoodItem(
        name=label,
        kcal=scaled.kcal,
        protein_g=scaled.protein_g,
        carb_g=scaled.carb_g,
        fat_g=scaled.fat_g,
        fiber_g=scaled.fiber_g,
        qty=qty,
        unit=unit,
        source="library",
        confidence="high" if row["source"] in ("label", "barcode") else "medium",
        library_food_id=food_id,
    ).validate()


def _recipe_items(
    conn: sqlite3.Connection,
    recipe_id: int,
    servings: float | None,
    grams: float | None,
) -> tuple[list[FoodItem], str]:
    if grams is not None:
        raise ValidationError("recipes are logged by servings, not grams")
    recipe = conn.execute("SELECT * FROM recipe WHERE id = ?", (recipe_id,)).fetchone()
    if recipe is None:
        raise ValidationError(f"no recipe with id {recipe_id}")

    ingredients = conn.execute(
        "SELECT * FROM recipe_ingredient WHERE recipe_id = ?", (recipe_id,)
    ).fetchall()
    if not ingredients:
        raise ValidationError(f"recipe '{recipe['name']}' has no ingredients")

    total = Macros()
    for ing in ingredients:
        item = _library_item(
            conn, ing["library_food_id"], ing["servings"], ing["grams"]
        )
        total = total + item.macros

    per_serving = total.scaled(1.0 / float(recipe["servings"]))
    portion = per_serving.scaled(float(servings))
    item = FoodItem(
        name=recipe["name"],
        kcal=portion.kcal,
        protein_g=portion.protein_g,
        carb_g=portion.carb_g,
        fat_g=portion.fat_g,
        fiber_g=portion.fiber_g,
        qty=servings,
        unit="serving",
        source="library",
        confidence="medium",
    ).validate()
    return [item], recipe["name"]


# --- editing -----------------------------------------------------------------


def edit_entry(
    conn: sqlite3.Connection,
    entry_id: str,
    meal: str | None = None,
    description: str | None = None,
    when: datetime | str | None = None,
) -> dict[str, Any]:
    """Edit meal-level fields of a logged meal (all its items)."""
    rows = conn.execute(
        "SELECT DISTINCT day, planned FROM food_entry WHERE group_id = ?", (entry_id,)
    ).fetchall()
    if not rows:
        raise ValidationError(f"no entry with id {entry_id}")
    original_day = rows[0]["day"]
    is_planned = bool(rows[0]["planned"])

    sets: list[str] = []
    params: list[Any] = []
    if meal is not None:
        require(meal, MEALS, "meal")
        sets.append("meal = ?")
        params.append(meal)
    if description is not None:
        sets.append("description = ?")
        params.append(description)
    new_day = original_day
    if when is not None:
        logged_at, new_day = _resolve_when(when)
        sets += ["logged_at = ?", "day = ?"]
        params += [logged_at, new_day]
    if not sets:
        raise ValidationError("nothing to change")

    sets.append("updated_at = ?")
    params.append(iso(now()))
    params.append(entry_id)

    with transaction(conn):
        conn.execute(
            f"UPDATE food_entry SET {', '.join(sets)} WHERE group_id = ?", params
        )
        if new_day != original_day and not is_planned:
            touch_day(conn, new_day)

    affected = {original_day, new_day}
    return {
        "ok": True,
        "entry_id": entry_id,
        "days_affected": sorted(affected),
        **get_day(conn, new_day),
    }


def edit_item(conn: sqlite3.Connection, item_id: int, **fields: Any) -> dict[str, Any]:
    """Correct a single component of a meal.

    Retroactive edits are expected and cheap — SPEC.md commits to recomputing trends from
    scratch nightly rather than trying to patch derived state in place.
    """
    row = conn.execute("SELECT * FROM food_entry WHERE id = ?", (item_id,)).fetchone()
    if row is None:
        raise ValidationError(f"no item with id {item_id}")

    editable = set(MACRO_FIELDS) | {"name", "qty", "unit", "source", "confidence"}
    unknown = set(fields) - editable
    if unknown:
        raise ValidationError(f"cannot edit: {', '.join(sorted(unknown))}")
    if not fields:
        raise ValidationError("nothing to change")

    if "source" in fields:
        require(fields["source"], SOURCES, "source")
    if "confidence" in fields:
        require(fields["confidence"], CONFIDENCES, "confidence")
    for f in MACRO_FIELDS:
        if f in fields and fields[f] < 0:
            raise ValidationError(f"{f} cannot be negative")

    sets = ", ".join(f"{k} = ?" for k in fields) + ", updated_at = ?"
    params = list(fields.values()) + [iso(now()), item_id]
    with transaction(conn):
        conn.execute(f"UPDATE food_entry SET {sets} WHERE id = ?", params)

    return {"ok": True, "item_id": item_id, **get_day(conn, row["day"])}


def delete_entry(conn: sqlite3.Connection, entry_id: str) -> dict[str, Any]:
    """Delete a whole logged meal."""
    row = conn.execute(
        "SELECT day FROM food_entry WHERE group_id = ? LIMIT 1", (entry_id,)
    ).fetchone()
    if row is None:
        raise ValidationError(f"no entry with id {entry_id}")
    day = row["day"]
    with transaction(conn):
        conn.execute("DELETE FROM food_entry WHERE group_id = ?", (entry_id,))
    return {"ok": True, "deleted": entry_id, **get_day(conn, day)}


def delete_item(conn: sqlite3.Connection, item_id: int) -> dict[str, Any]:
    row = conn.execute("SELECT day FROM food_entry WHERE id = ?", (item_id,)).fetchone()
    if row is None:
        raise ValidationError(f"no item with id {item_id}")
    day = row["day"]
    with transaction(conn):
        conn.execute("DELETE FROM food_entry WHERE id = ?", (item_id,))
    return {"ok": True, "deleted_item": item_id, **get_day(conn, day)}


def set_day_status(
    conn: sqlite3.Connection,
    day: Date | str | None = None,
    status: str = "complete",
    notes: str | None = None,
) -> dict[str, Any]:
    """Mark how completely a day was logged.

    Only ``complete`` days feed the expenditure fit. Marking a day complete is an assertion
    that nothing is missing from it, which is why it is never inferred automatically.
    """
    require(status, DAY_STATUSES, "status")
    target = _as_day(day)
    with transaction(conn):
        conn.execute(
            """INSERT INTO day_log (day, status, notes, updated_at) VALUES (?,?,?,?)
               ON CONFLICT(day) DO UPDATE SET
                   status = excluded.status,
                   notes = COALESCE(excluded.notes, day_log.notes),
                   updated_at = excluded.updated_at""",
            (target, status, notes, iso(now())),
        )
    return {"ok": True, **get_day(conn, target)}


# --- library -----------------------------------------------------------------


def save_food(
    conn: sqlite3.Connection,
    name: str,
    serving_desc: str,
    kcal: float,
    protein_g: float,
    carb_g: float,
    fat_g: float,
    fiber_g: float = 0.0,
    brand: str | None = None,
    barcode: str | None = None,
    serving_g: float | None = None,
    source: str = "estimate",
) -> dict[str, Any]:
    """Save a food to the personal library so it never needs re-estimating."""
    if not name.strip():
        raise ValidationError("name is required")
    require(source, SOURCES, "source")
    Macros(kcal, protein_g, carb_g, fat_g, fiber_g).validate()
    if serving_g is not None and serving_g <= 0:
        raise ValidationError("serving_g must be positive")

    if barcode:
        existing = conn.execute(
            "SELECT id, name FROM library_food WHERE barcode = ?", (barcode,)
        ).fetchone()
        if existing:
            raise ValidationError(
                f"barcode {barcode} already saved as '{existing['name']}' "
                f"(id {existing['id']})"
            )

    with transaction(conn):
        cur = conn.execute(
            """INSERT INTO library_food
               (name, brand, barcode, serving_desc, serving_g,
                kcal, protein_g, carb_g, fat_g, fiber_g, source, created_at)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?)""",
            (name, brand, barcode, serving_desc, serving_g,
             kcal, protein_g, carb_g, fat_g, fiber_g, source, iso(now())),
        )
    return {"ok": True, "id": cur.lastrowid, "name": name}


def save_recipe(
    conn: sqlite3.Connection,
    name: str,
    servings: float,
    ingredients: Sequence[dict[str, Any]],
) -> dict[str, Any]:
    """Save a recipe as a composition of library foods.

    Each ingredient is ``{library_food_id, grams}`` or ``{library_food_id, servings}``.
    """
    if servings <= 0:
        raise ValidationError("servings must be positive")
    if not ingredients:
        raise ValidationError("a recipe needs at least one ingredient")

    for ing in ingredients:
        if "library_food_id" not in ing:
            raise ValidationError("each ingredient needs a library_food_id")
        if ("grams" in ing) == ("servings" in ing):
            raise ValidationError(
                "each ingredient needs exactly one of grams or servings"
            )

    with transaction(conn):
        cur = conn.execute(
            "INSERT INTO recipe (name, servings, created_at) VALUES (?,?,?)",
            (name, servings, iso(now())),
        )
        recipe_id = cur.lastrowid
        for ing in ingredients:
            conn.execute(
                "INSERT INTO recipe_ingredient (recipe_id, library_food_id, grams, servings) "
                "VALUES (?,?,?,?)",
                (recipe_id, ing["library_food_id"], ing.get("grams"), ing.get("servings")),
            )
    return {"ok": True, "id": recipe_id, "name": name, "servings": servings}


def search_library(
    conn: sqlite3.Connection, query: str = "", limit: int = 10
) -> list[dict[str, Any]]:
    """Search saved foods, most-used first — the repeat meal should surface immediately."""
    like = f"%{query.strip()}%"
    rows = conn.execute(
        """SELECT * FROM library_food
           WHERE name LIKE ? OR COALESCE(brand,'') LIKE ?
           ORDER BY times_used DESC, last_used DESC, name ASC
           LIMIT ?""",
        (like, like, limit),
    ).fetchall()
    return [
        {
            "id": r["id"],
            "name": r["name"],
            "brand": r["brand"],
            "serving_desc": r["serving_desc"],
            "serving_g": r["serving_g"],
            "macros": _row_macros(r).as_dict(),
            "source": r["source"],
            "times_used": r["times_used"],
        }
        for r in rows
    ]


def _bump_library_use(conn: sqlite3.Connection, food_id: int, stamp: str) -> None:
    conn.execute(
        "UPDATE library_food SET times_used = times_used + 1, last_used = ? WHERE id = ?",
        (stamp, food_id),
    )


# --- reads -------------------------------------------------------------------


def get_day(conn: sqlite3.Connection, day: Date | str | None = None) -> dict[str, Any]:
    """Everything logged on a day, with totals.

    ``targets`` and ``remaining`` are ``None`` in M1 — the target engine does not exist yet.
    They carry a reason rather than a zero so a caller can never mistake "not built" for
    "nothing left to eat".
    """
    target = _as_day(day)
    rows = conn.execute(
        "SELECT * FROM food_entry WHERE day = ? ORDER BY logged_at, id", (target,)
    ).fetchall()

    actual = Macros()
    planned = Macros()
    entries: dict[str, dict[str, Any]] = {}
    for r in rows:
        macros = _row_macros(r)
        if r["planned"]:
            planned = planned + macros
        else:
            actual = actual + macros
        group = entries.setdefault(
            r["group_id"],
            {
                "entry_id": r["group_id"],
                "meal": r["meal"],
                "description": r["description"],
                "logged_at": r["logged_at"],
                "planned": bool(r["planned"]),
                "items": [],
            },
        )
        group["items"].append(
            {
                "item_id": r["id"],
                "name": r["name"],
                "qty": r["qty"],
                "unit": r["unit"],
                "macros": macros.as_dict(),
                "source": r["source"],
                "confidence": r["confidence"],
            }
        )

    status = day_status(conn, target)
    return {
        "date": target,
        "status": status,
        "totals": actual.as_dict(),
        "planned_totals": planned.as_dict() if planned.kcal else None,
        "entries": list(entries.values()),
        "entry_count": len(entries),
        # Targets are deliberately not resolved here. This module is the raw food-log layer
        # and has no access to the active goal or to garmin-mcp's weight data; server.py's
        # get_day tool overlays real targets (or a specific reason they're unavailable) on
        # top of this. Callers that use this function directly -- notably scripts/log_cli.py
        # -- get this placeholder instead, so it must describe *that* situation accurately
        # rather than the state of the codebase. It previously read "target engine not
        # implemented until M3", which was true when written but became misleading once the
        # engine shipped: read out of context it implied a missing feature rather than an
        # unset goal.
        "targets": None,
        "targets_null_reason": (
            "not resolved by this layer -- use the get_day MCP tool or get_targets, which "
            "apply the active goal and current expenditure"
        ),
        "remaining": None,
        "confidence_mix": _confidence_mix(rows),
    }


def _confidence_mix(rows: Sequence[sqlite3.Row]) -> dict[str, int]:
    """How much of the day rests on guesses. Surfaced so a low-confidence day is visible."""
    mix = {c: 0 for c in CONFIDENCES}
    for r in rows:
        if not r["planned"]:
            mix[r["confidence"]] += 1
    return mix


def get_intake_trend(conn: sqlite3.Connection, days: int = 28) -> dict[str, Any]:
    """Daily intake over a window, with each day's logging status attached.

    Days with no entries appear with ``status: unlogged`` and ``null`` macros — never zeros.
    The expenditure engine (M2) depends on that distinction being preserved here.
    """
    if days <= 0:
        raise ValidationError("days must be positive")
    end = today()
    start = Date.fromordinal(end.toordinal() - days + 1)

    rows = conn.execute(
        """SELECT day,
                  SUM(kcal) AS kcal, SUM(protein_g) AS protein_g,
                  SUM(carb_g) AS carb_g, SUM(fat_g) AS fat_g, SUM(fiber_g) AS fiber_g
           FROM food_entry
           WHERE day BETWEEN ? AND ? AND planned = 0
           GROUP BY day""",
        (start.isoformat(), end.isoformat()),
    ).fetchall()
    by_day = {r["day"]: _row_macros(r) for r in rows}

    statuses = {
        r["day"]: r["status"]
        for r in conn.execute(
            "SELECT day, status FROM day_log WHERE day BETWEEN ? AND ?",
            (start.isoformat(), end.isoformat()),
        ).fetchall()
    }

    points = []
    counts = {"complete": 0, "partial": 0, "unlogged": 0}
    complete_kcal: list[float] = []
    for offset in range(days):
        d = Date.fromordinal(start.toordinal() + offset).isoformat()
        status = statuses.get(d, "unlogged")
        counts[status] += 1
        macros = by_day.get(d)
        if status == "complete" and macros is not None:
            complete_kcal.append(macros.kcal)
        points.append(
            {
                "date": d,
                "status": status,
                **(macros.as_dict() if macros else {f: None for f in MACRO_FIELDS}),
            }
        )

    return {
        "points": points,
        "days_requested": days,
        "days_complete": counts["complete"],
        "days_partial": counts["partial"],
        "days_unlogged": counts["unlogged"],
        "avg_kcal_complete_days": (
            round(sum(complete_kcal) / len(complete_kcal), 1) if complete_kcal else None
        ),
        "avg_kcal_null_reason": (
            None if complete_kcal else "no days marked complete in this window"
        ),
    }
