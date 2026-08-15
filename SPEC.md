# macro-mcp — Build Spec

A self-hosted MCP server that tracks what you eat against macro targets, and reports how it's
going. Driven from Claude, including on mobile.

---

## Charter

**macro-mcp owns:**

- **Stored macro targets** — whatever Claude sets, per date. Storage only.
- **Food logging** — by description or photo, with per-item provenance and confidence.
- **Food lookup** — the personal library and (later) barcode/branded search, in service of
  logging.
- **Intake vs. target calculation** — totals, remaining, over/under.
- **Trend computation** — rolling averages, adherence, variance, patterns over time.
- **Charting** — server-rendered SVG, so charts are consistent and identical everywhere.
- **Progress photos** — stored on disk, pose-aligned when possible, viewable through a
  token-gated `/dashboard` page alongside weight and body-fat % trend. Added 2026-08-15; see
  "Progress photos and dashboard" below.

**macro-mcp explicitly does not own:**

- **TDEE / energy expenditure.** Comes from Garmin activity data, the user's own history, and
  Claude's memory of the conversation.
- **Goals.** The user states a goal in conversation; Claude holds it.
- **Plans, programmes, or cadence.** Which day is a training day, when a refeed lands, when a
  cut ends — all Claude's.
- **Any derivation of what the user *should* eat.** The server never computes a target. It
  stores the number it was given and reports what happened against it.
- **Weight as system of record.** garmin-mcp still owns it (see "Locked decisions"). The
  dashboard *reads* a trend from garmin-mcp purely for display — see "Progress photos and
  dashboard" for why that one narrow read doesn't reopen the M4 bridge this charter removed.

The dividing line: **the server records and measures; Claude decides.** If a capability
requires an opinion about nutrition or training, it does not belong here.

### Charter change (2026-08-14) — what this replaced, and why

An earlier version of this spec had the server derive targets: `set_goal` took a rate and
per-pound protein/fat ratios, an expenditure engine estimated TDEE from energy balance, and a
resolver distributed a weekly calorie budget across day types using relative `carb_weight`
values. It was built, tested, and worked.

It was removed because it violated this project's own stated principle (*"Claude decides and
writes. The server computes and detects."*). Computing a weekly budget from a goal rate and
turning it into a day's carbohydrate number looks like arithmetic, but it is a nutritional
decision wearing arithmetic's clothes — and it made the server the owner of a decision the
user and Claude were better placed to make.

The concrete failure that surfaced it: a fat-cycling protocol (training / PSMF / refeed days
with fat at 32 / 20 / 50 g) **could not be expressed at all**, because the engine held fat
flat by construction and let only carbs vary. The first proposed fix was to add a
per-day-type fat map — which would have layered a second mechanism on top of the first while
leaving the underlying problem (the server holding a philosophy) untouched.

Deleted outright: `expenditure.py`, the old `targets.py` resolver, `goals.py`,
`garmin_client.py`, `scripts/simulate.py`, and the `goal` / `day_type` / `training_plan` /
`day_plan` / `proposal` tables. Not deprecated — removed. Their design notes survive only in
this section and in git history.

### Progress photos and dashboard (2026-08-15)

Added after the charter change above, not part of it — a distinct capability, not a walk-back.
The user wants a visual time series of their own body (photos aligned frame to frame, next to
weight and body-fat % trend) the same way `get_trend`/`render_trend` already give a visual
time series of macro adherence. Three decisions were made explicitly rather than defaulted:

- **Storage lives here, not a separate service.** macro-mcp already owns `body_comp`; a photo
  is another body-tracking data type, and a second self-hosted service would mean a second
  thing to deploy, secure, and keep alive for one feature.
- **Alignment is automatic (pose landmarks), not manual tap-points.** `body_photos.py` detects
  shoulder/hip midpoints via MediaPipe and rotates/scales/translates each photo onto a shared
  canvas. This is a real ML dependency (`pyproject.toml`'s `photos` extra, ~200MB+, no
  guaranteed wheel on every architecture) — its absence, or a failed detection on one photo,
  degrades to storing that photo unaligned and flagged with a reason. It never blocks the
  save and never refuses to run the server. mediapipe's classic bundled-weights Pose API
  (no model download needed) was removed as of mediapipe 1.x — confirmed against the real
  installed package, not assumed from old docs — so this uses the newer Tasks API, which
  needs a model file (`POSE_MODEL_PATH`) fetched once at Docker build time so the running
  container needs no outbound internet access for it.
- **The dashboard reads weight from garmin-mcp, narrowly.** Weight is a locked non-goal here
  (garmin-mcp owns it). Rather than skip weight or duplicate it into macro-mcp's own storage,
  `garmin_weight.py` restores the exact, already-verified-live OAuth client the M4 bridge
  used — narrowed to the one read the dashboard needs (`get_body_trend`), never a write, and
  nothing downstream derives a target or plan from it. It exists only because the dashboard's
  weight panel would otherwise be permanently null; it is not a reopening of the general
  garmin-mcp bridge the charter change removed. No macro/target/trend logic calls it —
  `dashboard.py` is its only caller.

The dashboard itself is a second, deliberately separate secret (`DASHBOARD_TOKEN`, distinct
from `MCP_BEARER_TOKEN`) gating a plain HTTP page — see "Locked decisions" and Config. It is
reachable over the same Tailscale Funnel as the MCP endpoint, which is genuinely more exposed
than a photo of your own body ought to be; the separate token means a leaked dashboard link
(pasted into a chat, sitting in browser history) is not also a working MCP credential, and an
unset token disables the dashboard rather than defaulting it open.

---

## Locked decisions

| Decision | Choice |
|---|---|
| Language | Python 3.11+ |
| MCP framework | FastMCP, Streamable HTTP transport |
| Local store | SQLite |
| Packaging | Docker, bound to `127.0.0.1:18081` only |
| Public exposure | Tailscale Funnel on port 443 (see `docs/self-hosted-setup.md`) |
| Auth | OAuth 2.1 + Dynamic Client Registration, copied from garmin-mcp |
| Targets | **Stored, never derived.** Claude sets them; the server keeps them. |
| Tracked nutrients | kcal, protein, carbohydrate, fat, fiber |
| Food photos | Read by Claude in-chat, never stored |
| Progress photos | **Stored** on disk (`PHOTO_DIR`), pose-aligned when possible — see "Progress photos and dashboard" |
| Weight (dashboard only) | Read-only, live, from garmin-mcp — never stored here, never a system of record |
| Charts | **Server-rendered inline SVG**, no external libraries |
| Dashboard | Token-gated HTML page (`DASHBOARD_TOKEN`, separate from `MCP_BEARER_TOKEN`), same Funnel as MCP |
| Guardrails | None blocking. The server reports; it does not refuse or clamp. |
| Multi-user | No. Single user, single account. |
| Audience | Publishable open source |

## Non-goals

- No TDEE estimation, goal tracking, or target derivation (see Charter).
- No coaching thresholds or nutritional opinions of any kind.
- No body weight as system of record — garmin-mcp owns it; the dashboard only reads a trend
  for display (see "Progress photos and dashboard").
- No storage of *food* photos — those are still read in-chat only and never saved. Progress
  photos are a separate, deliberately different capability; see above.
- No REST API beyond the dashboard's own read-only HTML/image routes. Every write is MCP only.
- No alcohol column; no micronutrients beyond fiber.

---

## Critical instructions

**Never return a fabricated or interpolated value.** Every tool that cannot answer returns
`null` plus a `*_reason` explaining why. This applies to trend statistics as much as anything
else: a rolling average over three days of a twenty-eight-day window is not an average, it is
a guess with error bars nobody asked for. Suppress and say so.

**A day with no logged food is unknown, not zero.** Unlogged days never count as zero-intake
days in any average, adherence rate, or chart. They render as gaps, not as points at zero.

**A `null` describes a data state, not a missing feature.** Phrase every reason in terms of
what data is absent or what the user hasn't set yet — never in terms of build state. A message
reading "not implemented until M3" survived past the milestone that implemented it and was
read, reasonably, as proof the feature didn't exist.

**Targets are specifications; logged food is testimony.** A target's `kcal` may be derived
from its macros via Atwater when not supplied — 190P/60C/32F has exactly one energy content.
A logged food's stated calories are never rewritten, only flagged when they disagree with
their macros. These rules are deliberately opposite.

**Never commit secrets.** `.env`, the SQLite file, and any token are gitignored.

---

## Data model

```
food_entry    id, group_id, logged_at (tz-aware), day, meal, description, name, qty, unit,
              kcal, protein_g, carb_g, fat_g, fiber_g,
              source (label|barcode|library|estimate), confidence, library_food_id?, planned

day_log       day (PK), status (complete|partial|unlogged), notes
              -- 'complete' is the user's assertion, never inferred from entry count

day_target    day (PK), kcal, protein_g, carb_g, fat_g, fiber_g, note, set_at
              -- exactly what Claude set for that date. No day types, no weights, no
              -- recurrence, no derivation. Claude owns the cadence and writes each date.

library_food  id, name, brand?, barcode?, serving_desc, serving_g,
              kcal, protein_g, carb_g, fat_g, fiber_g, source, times_used, last_used

recipe        id, name, servings  +  recipe_ingredient -> library_food + qty

body_comp     day (PK), percent_fat, method (scale|calipers|dexa|estimate)
              -- Retained deliberately, though arguably outside the charter: garmin-mcp
              -- declared body composition a non-goal, so this is the only home for it.
              -- Tracking-only; nothing derives from it. Drop it if that stops being worth
              -- the exception.

body_photo    day + angle (PK, angle in front|side|back), file_path, width, height,
              landmarks_json?, align_status (pending|ok|failed), align_reason?, note, created_at
              -- file_path points at a JPEG under PHOTO_DIR; the image itself is never in the
              -- DB. landmarks_json caches the pose landmarks detected at save time so the
              -- dashboard doesn't re-run detection on every render.
```

`day_target` replaces `day_plan`. It holds the same shape `day_plan`'s `explicit_macros`
already held — the explicit path was the only part of the old model that survived contact
with a real protocol, so it became the whole model.

---

## Trend computation

Deterministic statistics over logged intake and stored targets. No opinions, no thresholds,
no verdicts — the numbers, and enough context to read them honestly.

- **Rolling averages** over a window, computed across `complete` days only.
- **Adherence**: per macro, how often intake landed over, under, or within a tolerance band of
  target; mean signed deviation (the bias) reported separately from mean absolute deviation
  (the scatter), because a consistent small overshoot and wild swings averaging to zero are
  different problems.
- **Coverage**: how many days in the window were complete / partial / unlogged, always
  returned alongside any statistic so a figure computed from four days is never mistaken for
  one computed from twenty-eight.
- **Suppression**: below a minimum number of usable days, statistics return `null` with a
  reason rather than a number.

Adherence is reported both daily and weekly. A single day over target inside an otherwise
balanced week is not the same signal as a persistent drift, and the tool surfaces both rather
than picking one.

## Charting

Server-generated inline SVG, built with plain string formatting. **No matplotlib, no charting
library, no CDN** — Claude's artifact sandbox blocks external scripts, so anything requiring a
library would have to inline hundreds of kilobytes of it.

- Line charts for intake vs. target over time; bar charts for per-day deviation.
- Unlogged days are gaps in the line, never zero-valued points.
- Target shown as a reference band, so over/under is visible without reading the axis.
- Native `<title>` elements per point give hover tooltips with no JavaScript.
- A `<style>` block with `prefers-color-scheme` so charts are legible in light and dark.
- Output is roughly 10–20 KB — small enough that returning one through a tool call is cheap,
  unlike a base64 raster image.

The server owns chart *design* so charts look identical every time rather than being
re-invented per conversation. `get_trend` (data + statistics) stays available separately for
when Claude needs numbers to reason about rather than a picture to show.

---

## Tool contracts

Dates ISO `YYYY-MM-DD`, times `America/New_York`, day boundary midnight. Compact JSON — the
consumer is an LLM context window.

### Targets

```
set_targets(targets: [{date, protein_g, carb_g, fat_g, kcal?, fiber_g?, note?}])
  -> {ok, count, dates, warnings?}
  -- Bulk by design. Claude owns the cadence, so it writes each date explicitly; accepting a
  -- list keeps a month-long protocol to one call instead of thirty. kcal derives via Atwater
  -- when omitted.

get_targets(date=today)          -> {date, targets, set_at, note} | targets: null + reason
delete_targets(date)             -> {ok, date}
```

### Logging

```
log_food(description, meal, items[], when=None, planned=False)
log_from_library(meal, food_id|recipe_id, servings|grams, when=None, planned=False)
edit_entry(entry_id, ...) / edit_item(item_id, ...)
delete_entry(entry_id) / delete_item(item_id)
set_day_status(date=today, status="complete", notes=None)
```

### Library

```
save_food(...) / save_recipe(...) / search_library(query, limit)
lookup_barcode(code)             -- M6
```

### Reads

```
get_day(date=today)
  -> {date, status, totals, targets, remaining, over_under, entries[], confidence_mix}

get_intake_trend(days=28)
  -> {points: [{date, status, kcal, protein_g, ...}], days_complete/partial/unlogged, ...}

get_trend(days=28, metrics=["kcal","protein_g",...])
  -> {points[], targets[], rolling[], adherence{}, coverage{}, suppressed_reason?}

render_trend(days=28, metric="kcal", chart="line")
  -> {svg, width, height, points_plotted, note?}
```

### Body composition

```
log_body_comp(percent_fat, method, date=None)
get_body_comp(days=90)
```

### Progress photos

```
log_body_photo(image_base64, angle="front", date=None, note=None)
  -> {ok, day, angle, width, height, align_status, align_reason?}
  -- base64 image, no "data:" prefix. One per (day, angle); a later save replaces it.
  -- align_status/align_reason are honest, not blocking -- the photo is stored either way.

get_body_photo(date=None, angle="front")  -> {day, angle, photo, photo_null_reason?}
list_body_photos(angle="front", start=None, end=None)  -> {angle, start, end, photos[]}
delete_body_photo(date, angle="front")  -> {ok, day, angle, existed}
```

No tool returns the image itself — metadata only. Viewing photos is `/dashboard`'s job, not
chat's; a base64 image round-tripped through a tool call would be an expensive way to show a
picture a browser can just fetch.

---

## Repo structure

```
macro-mcp/
├── src/macro_mcp/
│   ├── server.py      # FastMCP app, tool registration
│   ├── oauth.py       # OAuth 2.1 + DCR provider
│   ├── auth.py        # rate limiting
│   ├── foods.py       # logging, library, recipes, get_day
│   ├── targets.py     # stored targets (no derivation)
│   ├── trends.py      # rolling averages, adherence, coverage
│   ├── charts.py       # dependency-free SVG rendering
│   ├── body.py         # body composition
│   ├── body_photos.py  # progress photos: storage + pose-landmark alignment
│   ├── garmin_weight.py # narrow, read-only weight fetch for the dashboard only
│   ├── dashboard.py    # renders /dashboard's HTML (weight/bodyfat charts + slideshow)
│   ├── store.py        # SQLite schema and access
│   └── models.py       # shared types and validation
├── models/
│   └── pose_landmarker_lite.task  # fetched at Docker build time, gitignored -- see Dockerfile
├── scripts/
│   ├── log_cli.py     # local logging CLI
│   ├── calibrate.py   # estimation-accuracy harness
│   ├── mcp_smoke.py   # end-to-end MCP check
│   └── export_csv.py
├── docs/self-hosted-setup.md
└── compose.yml
```

## Config

```
SQLITE_PATH=/data/macro.db
OAUTH_STATE_PATH=/data/oauth_state.json
MCP_BEARER_TOKEN=
MCP_PUBLIC_URL=
TREND_MIN_DAYS=7          # below this, trend statistics suppress with a reason
OFF_ENABLED=true          # Open Food Facts lookup (M6)
TZ=America/New_York
LOG_LEVEL=INFO

# progress photos + dashboard
PHOTO_DIR=                # default: a "photos/" sibling of SQLITE_PATH
POSE_MODEL_PATH=          # default: ./models/pose_landmarker_lite.task
DASHBOARD_TOKEN=          # unset = /dashboard disabled (fails closed, not open)

# dashboard's narrow, read-only weight fetch -- see "Progress photos and dashboard".
# Reintroduced deliberately after the charter change removed the general garmin-mcp bridge;
# not a sign the bridge is coming back wholesale.
GARMIN_MCP_URL=
GARMIN_MCP_TOKEN=
GARMIN_MCP_OAUTH_STATE_PATH=/data/garmin_mcp_oauth.json
```

No `KCAL_PER_LB`, no smoothing constant, no expenditure window — those were specific to the
deleted expenditure engine and stay gone. `GARMIN_MCP_*` came back for the dashboard's weight
panel only (above); nothing else in this server touches garmin-mcp.

---

## Milestones

**Done:** food logging and the personal library; body composition; the MCP server over
Streamable HTTP; OAuth 2.1 + DCR auth; Docker packaging and public exposure; stored targets;
trend computation; SVG charting; progress photos with pose-landmark alignment; a token-gated
`/dashboard` combining photos with weight and body-fat % trend.

**Remaining:**

- **Food lookup expansion** — Open Food Facts barcode and branded search, feeding the personal
  library. The library exists; external lookup does not.

**Retired:** the expenditure engine, goal tracking, and target derivation, per the Charter
change above.

## Testing

Unit tests for storage, trend statistics, SVG generation, photo storage/alignment geometry,
and (indirectly) the dashboard's chart/photo assembly. `scripts/mcp_smoke.py` starts a real
server, completes a real OAuth login, and exercises every tool over the wire, plus the
dashboard's HTTP routes with and without a valid token — the check that matters, since it is
the only one that tests the transport and auth path a real client (or browser) uses.

Photo alignment's geometry (`_landmark_geometry`, `_affine_coeffs` in `body_photos.py`) is
unit-tested with synthetic landmark coordinates, independent of MediaPipe. Actual pose
detection is exercised live by `mcp_smoke.py` when the `photos` extra and its model file are
installed, but degrades to "stored, unaligned" rather than failing when they aren't — see
"Progress photos and dashboard".
