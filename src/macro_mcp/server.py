"""FastMCP app and tool registration.

Mirrors garmin-mcp's server.py conventions (same fastmcp version, @mcp.tool /
@mcp.custom_route pattern, streamable-http invocation) so the two servers stay easy to run and
reason about side by side.

    python -m macro_mcp.server

Scope: food logging, the personal library, stored targets, intake-vs-target reporting, trend
statistics, SVG charting, body composition, and progress photos (with a token-gated
/dashboard page combining photos with weight and body-fat trend).

Deliberately absent -- TDEE estimation, goals, and target derivation. Those were built and
then removed; see SPEC.md "Charter change (2026-08-14)". Claude owns the goal, the plan, the
cadence, and the numbers; this server records what it was told and measures what happened
against it. If a capability would require the server to hold an opinion about nutrition, it
does not belong here.
"""

from __future__ import annotations

import base64
import hmac
import logging
import os
from contextlib import contextmanager
from importlib.metadata import PackageNotFoundError, version as pkg_version
from typing import Any, Iterator

from fastmcp import FastMCP
from starlette.requests import Request
from starlette.responses import HTMLResponse, JSONResponse, PlainTextResponse, Response

from macro_mcp import body, body_photos, charts, dashboard, foods, targets as targets_mod, trends
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


@mcp.custom_route("/health", methods=["GET"], include_in_schema=False)
async def health(request: Request) -> JSONResponse:
    return JSONResponse({
        "status": "ok",
        "version": _server_version(),
        "db": _db_status(),
    })


def _dashboard_authorized(request: Request) -> bool:
    """The dashboard is reachable over the same public Tailscale Funnel as the MCP
    endpoint (SPEC.md's "Locked decisions"), so it needs its own gate: a plain browser GET
    has no OAuth bearer to check. DASHBOARD_TOKEN is deliberately separate from
    MCP_BEARER_TOKEN -- a leaked photo-viewing link (pasted into a chat, sitting in browser
    history) shouldn't also be a working MCP credential. Unset means the dashboard is
    disabled, not open -- fail closed, not open-by-default.
    """
    configured = os.environ.get("DASHBOARD_TOKEN", "")
    if not configured:
        return False
    supplied = request.query_params.get("token", "")
    return hmac.compare_digest(supplied, configured)


@mcp.custom_route("/dashboard", methods=["GET"], include_in_schema=False)
async def dashboard_page(request: Request) -> Response:
    if not os.environ.get("DASHBOARD_TOKEN"):
        return PlainTextResponse(
            "dashboard disabled: set DASHBOARD_TOKEN to enable it", status_code=503
        )
    if not _dashboard_authorized(request):
        return PlainTextResponse("unauthorized", status_code=401)

    angle = request.query_params.get("angle", "front")
    if angle not in ("front", "side", "back"):
        return PlainTextResponse(f"unknown angle {angle!r}", status_code=400)
    try:
        days = int(request.query_params.get("days", "90"))
    except ValueError:
        return PlainTextResponse("days must be an integer", status_code=400)

    token = request.query_params["token"]
    with _db() as conn:
        html = await dashboard.render_page(conn, angle, days, token)
    return HTMLResponse(html)


@mcp.custom_route("/dashboard/photo", methods=["GET"], include_in_schema=False)
async def dashboard_photo(request: Request) -> Response:
    if not _dashboard_authorized(request):
        return PlainTextResponse("unauthorized", status_code=401)

    day = request.query_params.get("day", "")
    angle = request.query_params.get("angle", "front")
    with _db() as conn:
        try:
            data, _status = body_photos.aligned_jpeg_bytes(conn, day, angle)
        except Exception as exc:  # noqa: BLE001 -- a bad/missing day must 404, never 500
            return PlainTextResponse(str(exc), status_code=404)
    return Response(content=data, media_type="image/jpeg")


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
    trends recompute from what's stored, they don't need to be told.
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
    only complete days count toward trend statistics), "partial", or "unlogged".
    Defaults to today. Marking a day complete is an assertion, not inferred
    automatically from having logged something.
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
def get_day(date: str | None = None) -> dict:
    """Everything logged on a day, with totals and how they compare to that
    day's stored targets. Defaults to today.

    `targets` is exactly what was set via set_targets for this date -- this
    server never derives targets, so if none were set this returns null with a
    reason rather than computing something. `remaining` is signed: negative
    means over target; `over` lists which macros are currently exceeded.
    """
    with _db() as conn:
        day = foods.get_day(conn, date)
        stored = targets_mod.get_targets(conn, day["date"])

    day["targets"] = stored["targets"]
    day["targets_null_reason"] = stored["targets_null_reason"]
    day["target_note"] = stored["note"]
    comparison = targets_mod.compare(day["totals"], stored["targets"])
    if comparison:
        day["remaining"] = comparison["remaining"]
        day["over"] = comparison["over"]
    else:
        day["remaining"] = None
        day["over"] = None
    return day


@mcp.tool
def get_intake_trend(days: int = 28) -> dict:
    """Daily intake over a trailing window, with each day's logging status
    attached. Unlogged days come back with null macros, never zero -- a day
    that wasn't logged is unknown, not a zero-calorie day.
    """
    with _db() as conn:
        return foods.get_intake_trend(conn, days)


# --- targets -----------------------------------------------------------------------


@mcp.tool
def set_targets(targets: list[dict]) -> dict:
    """Store macro targets for one or more dates, replacing any already set.

    Each entry needs `date`, `protein_g`, `carb_g`, `fat_g`; optional `kcal`,
    `fiber_g`, `note`. If `kcal` is omitted it's derived from the macros via
    Atwater (4/4/9) and reported back -- a target is a specification with one
    well-defined energy content. (Logged food is the opposite: its stated
    calories are never rewritten, only flagged.)

    Bulk on purpose. This server holds no notion of day types, recurrence, or
    training plans -- you decide the cadence and write each date explicitly, so
    a month-long protocol should be one call rather than thirty. The whole
    batch is validated before anything is written.
    """
    with _db() as conn:
        return targets_mod.set_targets(conn, targets)


@mcp.tool
def get_targets(date: str | None = None) -> dict:
    """The macro targets stored for a date, or null with a reason if none were
    set. Defaults to today. Nothing is derived -- this returns exactly what was
    written by set_targets.
    """
    with _db() as conn:
        return targets_mod.get_targets(conn, date)


@mcp.tool
def delete_targets(date: str) -> dict:
    """Remove the stored targets for a date. `existed` says whether there were
    any to remove, so a no-op is distinguishable from a real deletion.
    """
    with _db() as conn:
        return targets_mod.delete_targets(conn, date)


# --- trends and charts -----------------------------------------------------------------


@mcp.tool
def get_trend(days: int = 28, metrics: list[str] | None = None) -> dict:
    """Intake vs targets over a trailing window, with adherence statistics.

    Returns a per-metric series (intake and target per date), plus averages and
    adherence: how often intake landed over, under, or within a tolerance band
    of target, with the signed mean deviation (bias) reported separately from
    the absolute mean (scatter) -- a steady small overshoot and wild swings
    averaging to zero are different problems.

    Unlogged days are excluded from every statistic rather than counted as
    zero. Below a minimum number of complete days the statistics come back null
    with a reason instead of a figure computed from too little data; `coverage`
    always reports how many days actually contributed.
    """
    chosen = metrics or list(MACRO_FIELDS)
    with _db() as conn:
        points = foods.get_intake_trend(conn, days=days)["points"]
        if points:
            by_day = targets_mod.get_targets_range(conn, points[0]["date"], points[-1]["date"])
        else:
            by_day = {}
    return trends.compute(points, by_day, metrics=chosen)


@mcp.tool
def render_trend(days: int = 28, metric: str = "kcal", chart: str = "line") -> dict:
    """Render a trend as an inline SVG chart, ready to display directly.

    `chart="line"` plots intake over time against the target as a dashed
    reference line with a tolerance band. `chart="deviation"` plots per-day
    distance from target as bars, coloured over/under/on-target.

    Unlogged days are gaps in the line, never points at zero. Every data point
    carries a native hover tooltip, so the chart is interactive without any
    JavaScript. Returns `svg: null` with a reason when there's nothing
    plottable.
    """
    if chart not in ("line", "deviation"):
        raise ValueError(f"chart must be 'line' or 'deviation'; got {chart!r}")

    with _db() as conn:
        points = foods.get_intake_trend(conn, days=days)["points"]
        if points:
            by_day = targets_mod.get_targets_range(conn, points[0]["date"], points[-1]["date"])
        else:
            by_day = {}
    computed = trends.compute(points, by_day, metrics=[metric])
    series = computed["series"][metric]

    coverage = computed["coverage"]
    subtitle = (f"{coverage['days_complete']} complete / "
                f"{coverage['days_unlogged']} unlogged of {days}d")

    if chart == "line":
        out = charts.line_chart(series, metric, subtitle=subtitle)
    else:
        out = charts.deviation_bars(series, metric, subtitle=subtitle)
    out["metric"] = metric
    out["coverage"] = coverage
    return out


# --- body composition --------------------------------------------------------------


@mcp.tool
def log_body_comp(
    percent_fat: float,
    method: str = "scale",
    date: str | None = None,
    push_to_garmin: bool = False,
) -> dict:
    """Log a body-fat percentage reading. Tracking only -- nothing in this
    server derives from it. `method` should reflect the real source ("scale",
    "calipers", "dexa", "estimate"); don't default to "scale" if the user
    didn't say how they measured it.

    push_to_garmin is accepted but deliberately not enabled -- it was tested
    live and found to have no observable effect on Garmin's data. Requesting it
    returns pushed: false with push_error explaining why, rather than claiming
    an unverified success.
    """
    with _db() as conn:
        return body.log_body_comp(conn, percent_fat, method, date, push_to_garmin)


@mcp.tool
def get_body_comp(days: int = 90) -> dict:
    """Body-fat percentage history over a trailing window."""
    with _db() as conn:
        return body.get_body_comp(conn, days)


# --- progress photos ---------------------------------------------------------------


@mcp.tool
def log_body_photo(
    image_base64: str,
    angle: str = "front",
    date: str | None = None,
    note: str | None = None,
) -> dict:
    """Store a progress photo. `image_base64` is the raw image bytes (any common format --
    JPEG, PNG, HEIC, etc.), base64-encoded with no "data:" URI prefix. `angle` is
    "front", "side", or "back" -- one photo per date+angle, a later save for the same pair
    replaces it. Defaults to today.

    The server tries to detect a pose (shoulders, hips) so the dashboard can align this
    photo with the rest of the series; `align_status` in the response says whether that
    worked, and `align_reason` explains why not when it didn't (no clear full-body pose,
    or pose detection unavailable on this host). Either way the photo is still stored and
    still viewable -- alignment only affects whether the dashboard slideshow can line it up
    frame to frame, it's never a condition for saving. There's no MCP tool to read the
    image back into chat; view photos at the /dashboard page instead.
    """
    try:
        raw = base64.b64decode(image_base64, validate=True)
    except Exception as exc:
        raise ValueError(f"image_base64 is not valid base64: {exc}") from exc
    with _db() as conn:
        return body_photos.save_photo(conn, raw, angle=angle, day=date, note=note)


@mcp.tool
def get_body_photo(date: str | None = None, angle: str = "front") -> dict:
    """Metadata for a stored progress photo -- dimensions, alignment status, note. Not
    the image itself (see log_body_photo's note on viewing via /dashboard). Defaults to
    today. Returns `photo: null` with a reason if nothing is stored for that date+angle.
    """
    with _db() as conn:
        return body_photos.get_photo(conn, date, angle)


@mcp.tool
def list_body_photos(angle: str = "front", start: str | None = None, end: str | None = None) -> dict:
    """Which dates have a stored photo for a given angle, in a date range (defaults to
    the trailing 90 days). Metadata only, same as get_body_photo.
    """
    with _db() as conn:
        return body_photos.list_photos(conn, angle, start, end)


@mcp.tool
def delete_body_photo(date: str, angle: str = "front") -> dict:
    """Remove a stored photo (and its file on disk). `existed` says whether there was
    one to remove.
    """
    with _db() as conn:
        return body_photos.delete_photo(conn, date, angle)


if __name__ == "__main__":
    mcp.run(
        transport="streamable-http",
        host=os.environ.get("MCP_HOST", "127.0.0.1"),
        port=_port,
    )
