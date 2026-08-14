"""FastMCP app and tool registration -- M3: local only, no auth, no Docker.

Mirrors garmin-mcp's server.py conventions (same fastmcp version, @mcp.tool / @mcp.custom_route
pattern, streamable-http invocation) so the two servers stay easy to run and reason about side
by side.

Run directly for local dev / MCP Inspector, or via `scripts/mcp_smoke.py`'s automated check:

    python -m macro_mcp.server

Scope note: this now exposes food logging, the personal library, the expenditure engine, body
composition, and the full targets/goals engine (weekly-budget resolution, day planning, goal
lifecycle -- see targets.py/goals.py, SPEC.md's M5.5). It does NOT yet expose get_weekly_review
or the proposal table (get_proposals/accept_proposal/decline_proposal) -- both belong with M7's
nightly recompute, which doesn't exist yet; tracked as follow-up work, not silently dropped.
"""

from __future__ import annotations

import dataclasses
import logging
import os
from contextlib import contextmanager
from importlib.metadata import PackageNotFoundError, version as pkg_version
from typing import Any, Iterator

import httpx
from fastmcp import FastMCP
from starlette.requests import Request
from starlette.responses import JSONResponse

from macro_mcp import body, expenditure, foods, garmin_client, goals
from macro_mcp.models import MACRO_FIELDS
from macro_mcp.oauth import SingleUserOAuthProvider
from macro_mcp.store import db_path, open_db

logging.basicConfig(level=os.environ.get("LOG_LEVEL", "INFO"))

_port = int(os.environ.get("MCP_PORT", "8000"))
_public_url = os.environ.get("MCP_PUBLIC_URL", f"http://127.0.0.1:{_port}")

auth_provider = SingleUserOAuthProvider(base_url=_public_url)

mcp = FastMCP(name="macro-mcp", auth=auth_provider)


@contextmanager
def _db() -> Iterator[Any]:
    """A fresh connection per tool call. Simple and safe at this data volume (SPEC.md:
    SQLite, single user) -- no pooling, no cross-call shared state to reason about.
    """
    conn = open_db()
    try:
        yield conn
    finally:
        conn.close()


async def _current_expenditure(days: int = 28) -> expenditure.ExpenditureResult:
    """Shared by get_expenditure, get_targets, get_day, set_goal, and get_goal -- one place
    that knows how to talk to garmin-mcp and degrade honestly, so every tool that needs a
    TDEE/trend-weight gets the identical fail-closed behavior rather than five copies of it.
    """
    with _db() as conn:
        intake_points = foods.get_intake_trend(conn, days=days)["points"]
    try:
        # More history than the analysis window, so the trend's EWMA has room to warm up
        # before the window it's actually judged over begins -- see garmin_client's own
        # docstring on get_weight_points.
        weight_points = await garmin_client.get_weight_points(days=days + 60)
    except garmin_client.GarminBridgeError as exc:
        result = expenditure.compute_expenditure(intake_points, [], window_days=days)
        return dataclasses.replace(result, tdee_null_reason=str(exc))
    return expenditure.compute_expenditure(intake_points, weight_points, window_days=days)


def _latest_percent_fat(conn) -> float | None:
    latest = body.get_body_comp(conn, days=90)["latest"]
    return latest["percent_fat"] if latest else None


def _server_version() -> str:
    try:
        return pkg_version("macro-mcp")
    except PackageNotFoundError:
        return "0.0.0-dev"


def _db_status() -> dict:
    try:
        with _db() as conn:
            conn.execute("SELECT 1").fetchone()
        return {"ok": True, "path": str(db_path())}
    except Exception as exc:  # noqa: BLE001 -- health check must never raise
        return {"ok": False, "path": str(db_path()), "error": str(exc)}


async def _garmin_mcp_status() -> dict:
    """A lightweight reachability probe -- hits garmin-mcp's own unauthenticated /health,
    not a full OAuth login (that's real work, not appropriate to redo on every health check).

    Kept short deliberately: a health check that blocks for several seconds on one
    downstream dependency being slow to fail is itself a problem, independent of how long
    that dependency actually takes to answer.
    """
    base = garmin_client._base_url()
    try:
        async with httpx.AsyncClient(timeout=1.5) as http:
            resp = await http.get(f"{base}/health")
        return {"reachable": resp.status_code == 200, "url": base, "status_code": resp.status_code}
    except Exception as exc:  # noqa: BLE001 -- a malformed GARMIN_MCP_URL (e.g. a bad port
        # number) can raise well below httpx's own exception hierarchy (OverflowError from
        # asyncio's socket layer, observed live during M5's Docker verification) -- like
        # _db_status above, this health check must never itself raise, no matter why the
        # downstream is unreachable.
        return {"reachable": False, "url": base, "error": f"{exc.__class__.__name__}: {exc}"}


@mcp.custom_route("/health", methods=["GET"], include_in_schema=False)
async def health(request: Request) -> JSONResponse:
    return JSONResponse({
        "status": "ok",
        "version": _server_version(),
        "db": _db_status(),
        "garmin_mcp": await _garmin_mcp_status(),
    })


# --- logging -------------------------------------------------------------------


@mcp.tool
def log_food(
    description: str,
    meal: str,
    items: list[dict],
    when: str | None = None,
    planned: bool = False,
) -> dict:
    """Log a meal as one or more items. Each item in `items` needs at least
    name/kcal/protein_g/carb_g/fat_g; fiber_g defaults to 0. Optional per item:
    qty, unit, source ("label"|"barcode"|"library"|"estimate", default "estimate"),
    confidence ("high"|"medium"|"low", default "medium"). `when` is an ISO
    timestamp defaulting to now; the day it falls on is midnight-bounded local time.
    Set `planned=True` for a meal you intend to eat rather than one you just ate --
    it's tracked separately and never counts toward the day's actual totals or
    logging-completeness status.
    """
    with _db() as conn:
        return foods.log_food(conn, description, meal, items, when=when, planned=planned)


@mcp.tool
def log_from_library(
    meal: str,
    food_id: int | None = None,
    recipe_id: int | None = None,
    servings: float | None = None,
    grams: float | None = None,
    when: str | None = None,
    planned: bool = False,
) -> dict:
    """Log a previously-saved food or recipe -- the fast path for a repeat meal.
    Pass exactly one of food_id/recipe_id, and exactly one of servings/grams
    (recipes must be logged by servings; grams only works for a food saved with
    a known serving_g).
    """
    with _db() as conn:
        return foods.log_from_library(
            conn, meal, food_id=food_id, recipe_id=recipe_id,
            servings=servings, grams=grams, when=when, planned=planned,
        )


@mcp.tool
def edit_entry(
    entry_id: str,
    meal: str | None = None,
    description: str | None = None,
    when: str | None = None,
) -> dict:
    """Edit meal-level fields (meal type, description, or timestamp) of a
    previously logged meal, identified by the entry_id returned from log_food /
    log_from_library. To correct one component's macros, use edit_item instead.
    """
    with _db() as conn:
        return foods.edit_entry(conn, entry_id, meal=meal, description=description, when=when)


@mcp.tool
def edit_item(
    item_id: int,
    name: str | None = None,
    qty: float | None = None,
    unit: str | None = None,
    kcal: float | None = None,
    protein_g: float | None = None,
    carb_g: float | None = None,
    fat_g: float | None = None,
    fiber_g: float | None = None,
    source: str | None = None,
    confidence: str | None = None,
) -> dict:
    """Correct one logged food item (not a whole meal) by its item_id. Only the
    fields you pass are changed. Retroactive corrections are expected and cheap --
    the expenditure engine recomputes from scratch, it doesn't need to be told.
    """
    fields = {
        "name": name, "qty": qty, "unit": unit, "kcal": kcal, "protein_g": protein_g,
        "carb_g": carb_g, "fat_g": fat_g, "fiber_g": fiber_g,
        "source": source, "confidence": confidence,
    }
    fields = {k: v for k, v in fields.items() if v is not None}
    with _db() as conn:
        return foods.edit_item(conn, item_id, **fields)


@mcp.tool
def delete_entry(entry_id: str) -> dict:
    """Delete a whole logged meal (all its items) by entry_id."""
    with _db() as conn:
        return foods.delete_entry(conn, entry_id)


@mcp.tool
def delete_item(item_id: int) -> dict:
    """Delete a single logged item by item_id, leaving the rest of its meal intact."""
    with _db() as conn:
        return foods.delete_item(conn, item_id)


@mcp.tool
def set_day_status(date: str | None = None, status: str = "complete", notes: str | None = None) -> dict:
    """Mark how completely a day was logged: "complete" (nothing missing --
    only complete days count toward the expenditure estimate), "partial", or
    "unlogged". Defaults to today. Marking a day complete is an assertion, not
    inferred automatically from having logged something.
    """
    with _db() as conn:
        return foods.set_day_status(conn, date, status, notes)


# --- library ---------------------------------------------------------------------


@mcp.tool
def save_food(
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
) -> dict:
    """Save a food to the personal library so it never needs re-estimating.
    Set serving_g (the mass of one serving) if you want this food loggable by
    grams later, not just by serving count.
    """
    with _db() as conn:
        return foods.save_food(
            conn, name, serving_desc, kcal, protein_g, carb_g, fat_g, fiber_g,
            brand=brand, barcode=barcode, serving_g=serving_g, source=source,
        )


@mcp.tool
def save_recipe(name: str, servings: float, ingredients: list[dict]) -> dict:
    """Save a recipe as a composition of library foods. Each ingredient dict
    needs library_food_id and exactly one of grams/servings.
    """
    with _db() as conn:
        return foods.save_recipe(conn, name, servings, ingredients)


@mcp.tool
def search_library(query: str = "", limit: int = 10) -> list[dict]:
    """Search saved foods by name or brand, most-used first -- the way a repeat
    meal should surface immediately instead of being re-estimated.
    """
    with _db() as conn:
        return foods.search_library(conn, query, limit)


# --- reads -----------------------------------------------------------------------


@mcp.tool
async def get_day(date: str | None = None) -> dict:
    """Everything logged on a day, with totals. Defaults to today. `targets`
    and `remaining` are populated when an active goal exists and TDEE/weight
    data is available; otherwise both stay null with `targets_null_reason`
    explaining why (e.g. "no active goal set", or the same reason
    get_expenditure would give if weight/TDEE data isn't there yet).
    """
    with _db() as conn:
        day = foods.get_day(conn, date)
        has_goal = goals.has_active_goal(conn)

    if not has_goal:
        day["targets_null_reason"] = "no active goal set (see set_goal)"
        day["day_type"] = None
        day["remaining"] = None
        return day

    exp = await _current_expenditure()
    with _db() as conn:
        target = goals.get_targets(conn, day["date"], tdee=exp.tdee, trend_weight_lb=exp.trend_weight_lb)

    day["day_type"] = target["day_type"]
    day["targets_null_reason"] = target["targets_null_reason"]
    if target["targets_null_reason"] is None:
        day["targets"] = {k: target[k] for k in MACRO_FIELDS}
        day["remaining"] = {
            k: round(target[k] - day["totals"][k], 1) for k in MACRO_FIELDS
        }
    else:
        day["targets"] = None
        day["remaining"] = None
    return day


@mcp.tool
def get_intake_trend(days: int = 28) -> dict:
    """Daily intake over a trailing window, with each day's logging status
    attached. Unlogged days come back with null macros, never zero -- a day
    that wasn't logged is unknown, not a zero-calorie day.
    """
    with _db() as conn:
        return foods.get_intake_trend(conn, days)


@mcp.tool
async def get_expenditure(days: int = 28) -> dict:
    """Estimate TDEE from energy balance: recency-weighted average intake over
    complete-logged days, adjusted by the trend-weight change over the same
    window. Returns tdee: null with a stated reason if there isn't enough
    data -- never a number the data doesn't support.

    Weight data comes from garmin-mcp over its own MCP connection. If
    garmin-mcp is unreachable or login fails, this degrades honestly: tdee
    comes back null with a reason naming the garmin-mcp problem specifically,
    never a fabricated or stale-but-unlabeled number.
    """
    result = await _current_expenditure(days)
    return result.as_dict()


# --- body composition --------------------------------------------------------------


@mcp.tool
def log_body_comp(
    percent_fat: float,
    method: str = "scale",
    date: str | None = None,
    push_to_garmin: bool = False,
) -> dict:
    """Log a body-fat percentage reading. macro-mcp is the system of record for
    this (garmin-mcp declared body composition a non-goal). push_to_garmin is
    accepted but deliberately not enabled -- tested live in SPEC.md's M4 and found
    to have no observable effect on Garmin's data. Requesting it returns
    pushed: false with push_error explaining why, rather than silently no-op'ing
    or claiming a success that wasn't verified.
    """
    with _db() as conn:
        return body.log_body_comp(conn, percent_fat, method, date, push_to_garmin)


@mcp.tool
def get_body_comp(days: int = 90) -> dict:
    """Body-fat percentage history over a trailing window."""
    with _db() as conn:
        return body.get_body_comp(conn, days)


# --- targets and goals ---------------------------------------------------------------


@mcp.tool
async def get_targets(date: str | None = None) -> dict:
    """Resolved macro targets for a day: weekly energy budget (from TDEE and the
    active goal's rate) distributed across the week by day type -- protein and
    fat flat every day, carbs carrying the week's day-to-day variance. Null
    with a reason if there's no active goal or TDEE/weight data isn't
    available yet -- never a guessed number. See get_day for the composite
    view (targets + what's actually been eaten + what's left).
    """
    exp = await _current_expenditure()
    with _db() as conn:
        return goals.get_targets(conn, date, tdee=exp.tdee, trend_weight_lb=exp.trend_weight_lb)


@mcp.tool
async def set_goal(
    mode: str,
    rate_lb_per_week: float,
    protein_g_per_lb: float,
    fat_g_per_lb_floor: float,
    stop_metric: str,
    stop_value: str | None = None,
    successor_goal_id: int | None = None,
) -> dict:
    """Start a new goal (cut/bulk/maintain), superseding whatever goal is
    currently active. protein_g_per_lb and fat_g_per_lb_floor are yours to
    set -- this server deliberately has no built-in nutritional stance on
    what ratio is right for a given goal or person; that judgment belongs in
    the conversation, not hardcoded here. rate_lb_per_week uses the same
    sign convention as get_expenditure's trend_lb_per_week: negative = losing.
    stop_metric "weight"/"bodyfat" need stop_value as that target number (as
    a string); "date" needs an ISO date; "none" needs no stop_value at all.
    Snapshots current weight/body-fat as the goal's baseline for later
    progress tracking (see get_goal) -- if TDEE/weight data isn't available
    right now, the snapshot and weekly_budget will be null, but the goal is
    still created; set it again later once data exists, or the baseline just
    stays unknown for this goal.
    """
    exp = await _current_expenditure()
    with _db() as conn:
        current_percent_fat = _latest_percent_fat(conn)
        return goals.set_goal(
            conn, mode, rate_lb_per_week, protein_g_per_lb, fat_g_per_lb_floor,
            stop_metric, stop_value,
            current_weight_lb=exp.trend_weight_lb, current_percent_fat=current_percent_fat,
            tdee=exp.tdee, successor_goal_id=successor_goal_id,
        )


@mcp.tool
async def get_goal() -> dict:
    """The current active goal, with progress toward its stop condition and a
    projected completion date extrapolated from the current trend rate.
    {"active": false} if no goal is set. stop_condition_met is reported
    honestly as a computed fact but does NOT automatically end or transition
    the goal -- that's a deliberate interim gap (SPEC.md M5.5): goal
    transitions are meant to be a Claude-mediated proposal once the nightly
    proposal system (M7) exists, not a silent automatic status change.
    """
    exp = await _current_expenditure()
    with _db() as conn:
        current_percent_fat = _latest_percent_fat(conn)
        return goals.get_goal(
            conn, current_weight_lb=exp.trend_weight_lb,
            current_percent_fat=current_percent_fat, trend_lb_per_week=exp.trend_lb_per_week,
        )


@mcp.tool
def set_training_plan(weekday_map: dict[str, str]) -> dict:
    """Set the recurring weekly training pattern used to assign a day_type to
    each date when there's no more specific override. Keys are weekday
    numbers as strings, "0"=Monday through "6"=Sunday (matching Python's
    date.weekday()), values are day_type names (e.g. "rest", "moderate",
    "heavy"). Only the weekdays you pass are changed; others keep their
    existing assignment.
    """
    with _db() as conn:
        return goals.set_training_plan(conn, {int(k): v for k, v in weekday_map.items()})


@mcp.tool
def set_day_plan(date: str, day_type: str | None = None, macros: dict | None = None) -> dict:
    """Override a specific date's day type or give it fully explicit macros,
    beating whatever set_training_plan's recurring pattern would otherwise
    assign. Pass exactly one of day_type or macros. Explicit macros are
    logged as-is for that day and excluded from the week's day-type-weighted
    carb distribution (they still count toward the week's total budget --
    see get_targets' week_budget_delta).
    """
    with _db() as conn:
        return goals.set_day_plan(conn, date, day_type=day_type, macros=macros)


if __name__ == "__main__":
    mcp.run(
        transport="streamable-http",
        host=os.environ.get("MCP_HOST", "127.0.0.1"),
        port=_port,
    )
