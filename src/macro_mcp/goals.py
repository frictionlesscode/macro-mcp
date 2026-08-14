"""Goal lifecycle, training/day planning, and the weekly-resolution composition layer.

Split of responsibility, matching the rest of this codebase: this module owns persistence and
validation; targets.py owns the pure arithmetic; expenditure/body-comp data is fetched by the
caller (server.py) and passed in here rather than fetched by this module, so every function
here stays testable against known inputs without a live garmin-mcp connection.

Known limitation, stated rather than silently glossed over: `successor_goal_id` (SPEC.md's
"phases with proposed transitions" design) can reference an existing goal row today, but there
is no "planned, not yet active" status for a successor created ahead of time -- building that
lifecycle properly is M7 scope (the proposal-driven transition flow), and this pass doesn't
try to guess that design before the feature that actually needs it exists.
"""

from __future__ import annotations

import json
import sqlite3
from datetime import date as Date, timedelta
from typing import Any

from .foods import ATWATER_TOLERANCE
from .models import Macros, ValidationError, iso, now, require, today
from .store import transaction
from .targets import WeekResolution, resolve_week, week_start

GOAL_MODES = ("cut", "bulk", "maintain")
STOP_METRICS = ("weight", "bodyfat", "date", "none")


# --- goal lifecycle ------------------------------------------------------------------


def set_goal(
    conn: sqlite3.Connection,
    mode: str,
    rate_lb_per_week: float,
    protein_g_per_lb: float,
    fat_g_per_lb_floor: float,
    stop_metric: str,
    stop_value: str | float | None,
    current_weight_lb: float | None = None,
    current_percent_fat: float | None = None,
    tdee: float | None = None,
    kcal_per_lb: float = 3500.0,
    successor_goal_id: int | None = None,
) -> dict[str, Any]:
    require(mode, GOAL_MODES, "mode")
    require(stop_metric, STOP_METRICS, "stop_metric")
    if protein_g_per_lb <= 0:
        raise ValidationError(f"protein_g_per_lb must be positive; got {protein_g_per_lb}")
    if fat_g_per_lb_floor <= 0:
        raise ValidationError(f"fat_g_per_lb_floor must be positive; got {fat_g_per_lb_floor}")
    if stop_metric != "none" and stop_value is None:
        raise ValidationError(f"stop_value is required when stop_metric={stop_metric!r}")
    if successor_goal_id is not None:
        exists = conn.execute("SELECT 1 FROM goal WHERE id = ?", (successor_goal_id,)).fetchone()
        if not exists:
            raise ValidationError(f"no goal with id {successor_goal_id} to use as successor")

    stamp = iso(now())
    target = today().isoformat()
    with transaction(conn):
        conn.execute(
            "UPDATE goal SET status = 'superseded', ended_on = ? WHERE status = 'active'",
            (target,),
        )
        cur = conn.execute(
            """INSERT INTO goal
               (mode, rate_lb_per_week, protein_g_per_lb, fat_g_per_lb_floor,
                stop_metric, stop_value, start_weight_lb, start_percent_fat,
                successor_goal_id, status, started_on, created_at)
               VALUES (?,?,?,?,?,?,?,?,?,'active',?,?)""",
            (mode, rate_lb_per_week, protein_g_per_lb, fat_g_per_lb_floor,
             stop_metric, None if stop_value is None else str(stop_value),
             current_weight_lb, current_percent_fat,
             successor_goal_id, target, stamp),
        )
        goal_id = cur.lastrowid

    result: dict[str, Any] = {"ok": True, "goal_id": goal_id, "mode": mode}
    if tdee is not None:
        from .targets import weekly_budget
        result["weekly_budget"] = weekly_budget(tdee, rate_lb_per_week, kcal_per_lb)
    else:
        result["weekly_budget"] = None
        result["weekly_budget_null_reason"] = "no TDEE available yet (see get_expenditure)"

    if current_weight_lb:
        result["implied_rate_pct_bodyweight"] = round(rate_lb_per_week / current_weight_lb * 100, 3)
    else:
        result["implied_rate_pct_bodyweight"] = None
        result["implied_rate_pct_bodyweight_null_reason"] = "current weight unavailable"

    return result


def _active_goal_row(conn: sqlite3.Connection) -> sqlite3.Row | None:
    return conn.execute("SELECT * FROM goal WHERE status = 'active' LIMIT 1").fetchone()


def has_active_goal(conn: sqlite3.Connection) -> bool:
    """Cheap, synchronous check -- lets a caller skip an expensive garmin-mcp round trip
    (get_expenditure) entirely when there's no goal for it to feed into.
    """
    return _active_goal_row(conn) is not None


def _progress_for_weight_or_bodyfat(
    start: float | None, target: float, current: float | None,
    trend_per_week: float | None, unit_days_per_week: int = 7,
) -> dict[str, Any]:
    if current is None:
        return {"progress": None, "progress_null_reason": "current value unavailable",
                "stop_condition_met": False, "projected_completion": None}
    if start is None:
        return {"progress": None, "progress_null_reason": "no starting value was recorded for this goal",
                "stop_condition_met": current == target, "projected_completion": None}

    total_needed = target - start
    done = current - start
    progress = 1.0 if total_needed == 0 else done / total_needed

    if total_needed < 0:
        met = current <= target
    elif total_needed > 0:
        met = current >= target
    else:
        met = True

    projected_completion = None
    projection_null_reason = None
    remaining = target - current
    if met:
        projection_null_reason = "already met"
    elif not trend_per_week:
        projection_null_reason = "no current trend rate available"
    elif (remaining < 0) != (trend_per_week < 0):
        projection_null_reason = "current trend isn't moving toward the goal"
    else:
        days = remaining / (trend_per_week / unit_days_per_week)
        projected_completion = (today() + timedelta(days=round(days))).isoformat()

    out = {"progress": round(progress, 4), "stop_condition_met": met,
           "projected_completion": projected_completion}
    if projection_null_reason:
        out["projected_completion_null_reason"] = projection_null_reason
    return out


def get_goal(
    conn: sqlite3.Connection,
    current_weight_lb: float | None = None,
    current_percent_fat: float | None = None,
    trend_lb_per_week: float | None = None,
) -> dict[str, Any]:
    row = _active_goal_row(conn)
    if row is None:
        return {"active": False}

    out: dict[str, Any] = {
        "active": True,
        "goal_id": row["id"],
        "mode": row["mode"],
        "rate_lb_per_week": row["rate_lb_per_week"],
        "protein_g_per_lb": row["protein_g_per_lb"],
        "fat_g_per_lb_floor": row["fat_g_per_lb_floor"],
        "stop_metric": row["stop_metric"],
        "stop_value": row["stop_value"],
        "started_on": row["started_on"],
        "successor_goal_id": row["successor_goal_id"],
    }

    if current_weight_lb:
        out["implied_rate_pct_bodyweight"] = round(row["rate_lb_per_week"] / current_weight_lb * 100, 3)
    else:
        out["implied_rate_pct_bodyweight"] = None

    if row["stop_metric"] == "weight":
        out.update(_progress_for_weight_or_bodyfat(
            row["start_weight_lb"], float(row["stop_value"]), current_weight_lb, trend_lb_per_week,
        ))
    elif row["stop_metric"] == "bodyfat":
        out.update(_progress_for_weight_or_bodyfat(
            row["start_percent_fat"], float(row["stop_value"]), current_percent_fat, None,
        ))
        out["projected_completion"] = None
        out["projected_completion_null_reason"] = "body-fat trend rate isn't modeled yet"
    elif row["stop_metric"] == "date":
        stop_date = Date.fromisoformat(row["stop_value"])
        start_date = Date.fromisoformat(row["started_on"])
        total_days = max((stop_date - start_date).days, 1)
        elapsed_days = (today() - start_date).days
        out["progress"] = round(min(max(elapsed_days / total_days, 0.0), 1.0), 4)
        out["stop_condition_met"] = today() >= stop_date
        out["projected_completion"] = row["stop_value"]
    else:  # 'none'
        out["progress"] = None
        out["stop_condition_met"] = False
        out["projected_completion"] = None

    return out


# --- training plan / day plan --------------------------------------------------------


def set_training_plan(conn: sqlite3.Connection, weekday_map: dict[int, str]) -> dict[str, Any]:
    known_types = {r["name"] for r in conn.execute("SELECT name FROM day_type").fetchall()}
    for weekday, type_name in weekday_map.items():
        if not 0 <= int(weekday) <= 6:
            raise ValidationError(f"weekday must be 0-6; got {weekday}")
        if type_name not in known_types:
            raise ValidationError(f"unknown day_type {type_name!r}; known: {sorted(known_types)}")

    with transaction(conn):
        for weekday, type_name in weekday_map.items():
            conn.execute(
                "INSERT INTO training_plan (weekday, day_type) VALUES (?, ?) "
                "ON CONFLICT(weekday) DO UPDATE SET day_type = excluded.day_type",
                (int(weekday), type_name),
            )
    return {"ok": True, "updated": {int(k): v for k, v in weekday_map.items()}}


def set_day_plan(
    conn: sqlite3.Connection,
    day: Date | str,
    day_type: str | None = None,
    macros: dict[str, float] | Macros | None = None,
) -> dict[str, Any]:
    if (day_type is None) == (macros is None):
        raise ValidationError("pass exactly one of day_type or macros")
    target = day.isoformat() if isinstance(day, Date) else day
    Date.fromisoformat(target)

    resolved_macros: dict[str, float] | None = None
    if day_type is not None:
        known_types = {r["name"] for r in conn.execute("SELECT name FROM day_type").fetchall()}
        if day_type not in known_types:
            raise ValidationError(f"unknown day_type {day_type!r}; known: {sorted(known_types)}")
        explicit_json = None
    else:
        try:
            m = macros if isinstance(macros, Macros) else Macros(**macros)
        except TypeError as exc:
            raise ValidationError(f"invalid macros field(s): {exc}") from exc
        m.validate()

        # Derive energy from the macros when the caller didn't supply it. Macros.kcal
        # defaults to 0.0, so passing only protein/carb/fat silently stored a 0-calorie
        # target -- not merely a cosmetic display bug: resolve_week subtracts
        # explicit_kcal_total from the weekly budget, so a 0 there makes the week believe
        # an explicit day is free and over-allocates that day's real energy as carbs across
        # the remaining days.
        #
        # Deriving is correct here in a way it deliberately is NOT for logged food. A logged
        # entry has a user-stated calorie figure worth preserving verbatim (log_food only
        # *reports* an Atwater mismatch, never rewrites it). A target is a specification --
        # 190P/60C/32F has one well-defined energy content and there is no competing stated
        # value to overwrite.
        implied = m.implied_kcal()
        derived_kcal = False
        if m.kcal == 0 and implied > 0:
            m = Macros(kcal=implied, protein_g=m.protein_g, carb_g=m.carb_g,
                       fat_g=m.fat_g, fiber_g=m.fiber_g)
            derived_kcal = True

        resolved_macros = m.as_dict()
        explicit_json = json.dumps(resolved_macros)

    stamp = iso(now())
    with transaction(conn):
        conn.execute(
            """INSERT INTO day_plan (day, day_type, explicit_macros, source, updated_at)
               VALUES (?, ?, ?, 'override', ?)
               ON CONFLICT(day) DO UPDATE SET
                   day_type = excluded.day_type,
                   explicit_macros = excluded.explicit_macros,
                   source = excluded.source,
                   updated_at = excluded.updated_at""",
            (target, day_type, explicit_json, stamp),
        )
    result = {"ok": True, "day": target, "day_type": day_type, "explicit_macros": resolved_macros}
    if day_type is None:
        if derived_kcal:
            result["kcal_derived_from_macros"] = True
            result["note"] = (
                f"kcal wasn't supplied, so it was derived from the macros via Atwater "
                f"(4/4/9) as {resolved_macros['kcal']:.0f}. Pass kcal explicitly to override."
            )
        elif implied > 0 and abs(implied - m.kcal) / max(implied, m.kcal) > ATWATER_TOLERANCE:
            # Supplied kcal is kept as-is -- only flagged, matching log_food's behaviour of
            # never silently rewriting a number the caller stated.
            result["warning"] = (
                f"stated {m.kcal:.0f} kcal vs {implied:.0f} implied by the macros "
                f"({abs(implied - m.kcal):.0f} apart). Stored as given."
            )
    return result


# --- weekly resolution composition --------------------------------------------------


def _day_type_table(conn: sqlite3.Connection) -> dict[str, tuple[float, float]]:
    return {
        r["name"]: (r["energy_weight"], r["carb_weight"])
        for r in conn.execute("SELECT name, energy_weight, carb_weight FROM day_type").fetchall()
    }


def resolve_current_week(
    conn: sqlite3.Connection,
    tdee: float | None,
    trend_weight_lb: float | None,
    week_of: Date | None = None,
    kcal_per_lb: float = 3500.0,
) -> tuple[WeekResolution | None, str | None]:
    """Returns (resolution, null_reason) -- exactly one is not-None, matching this codebase's
    fail-closed convention elsewhere (expenditure.compute_expenditure, etc.).
    """
    goal = _active_goal_row(conn)
    if goal is None:
        return None, "no active goal set (see set_goal)"
    if tdee is None:
        return None, "no TDEE available yet (see get_expenditure)"
    if not trend_weight_lb:
        return None, "no current trend weight available yet (see get_expenditure)"

    week_of = week_of or today()
    start = week_start(week_of)
    dates = [(start + timedelta(days=i)).isoformat() for i in range(7)]

    training = {
        r["weekday"]: r["day_type"]
        for r in conn.execute("SELECT weekday, day_type FROM training_plan").fetchall()
    }
    day_type_assignment: dict[str, str] = {}
    explicit_macros: dict[str, Macros] = {}
    for d in dates:
        override = conn.execute(
            "SELECT day_type, explicit_macros FROM day_plan WHERE day = ?", (d,)
        ).fetchone()
        if override and override["explicit_macros"]:
            explicit_macros[d] = Macros(**json.loads(override["explicit_macros"]))
        elif override and override["day_type"]:
            day_type_assignment[d] = override["day_type"]
        else:
            weekday = Date.fromisoformat(d).weekday()
            if weekday in training:
                day_type_assignment[d] = training[weekday]

    resolution = resolve_week(
        week_of=week_of,
        tdee=tdee,
        trend_weight_lb=trend_weight_lb,
        rate_lb_per_week=goal["rate_lb_per_week"],
        protein_g_per_lb=goal["protein_g_per_lb"],
        fat_g_per_lb_floor=goal["fat_g_per_lb_floor"],
        kcal_per_lb=kcal_per_lb,
        day_type_assignment=day_type_assignment,
        day_types=_day_type_table(conn),
        explicit_macros=explicit_macros,
    )
    return resolution, None


def get_targets(
    conn: sqlite3.Connection,
    day: Date | str | None = None,
    tdee: float | None = None,
    trend_weight_lb: float | None = None,
    kcal_per_lb: float = 3500.0,
) -> dict[str, Any]:
    target_date = today() if day is None else (day if isinstance(day, Date) else Date.fromisoformat(day))
    resolution, null_reason = resolve_current_week(conn, tdee, trend_weight_lb, week_of=target_date, kcal_per_lb=kcal_per_lb)

    if resolution is None:
        return {
            "date": target_date.isoformat(), "day_type": None,
            "kcal": None, "protein_g": None, "carb_g": None, "fat_g": None, "fiber_g": None,
            "source": None, "targets_null_reason": null_reason, "week_budget_delta": None,
        }

    day_target = resolution.target_for(target_date.isoformat())
    return {
        "date": target_date.isoformat(),
        "day_type": day_target.day_type,
        **day_target.macros.as_dict(),
        "source": "explicit" if day_target.explicit else "resolved",
        "targets_null_reason": None,
        "week_budget_delta": resolution.week_budget_delta,
        "infeasible": resolution.infeasible,
    }
