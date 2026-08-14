# macro-mcp — Build Spec

A remote MCP server for nutrition logging and adaptive expenditure estimation, designed to run
alongside [garmin-mcp](../../ClaudeGarminConnect/garmin-mcp) and be driven from Claude on mobile.

---

## Objective

Claude acts as a nutrition coach. This server is the data plane **plus the deterministic math**:
food logs, adaptive TDEE estimation, and macro target resolution. Coaching judgment — what your
goals mean, how to interpret a stall, what to eat — lives in a separate `macro-coach` Skill.

Body weight, body-fat pushes, activity, sleep, and training load all come from `garmin-mcp`. This
server does not hold Garmin credentials and does not talk to Garmin directly.

## Division of labor

The governing principle, which every design decision below follows:

> **Claude decides and writes. The server computes and detects. The server never silently reallocates.**

| Owner | Responsibility |
|---|---|
| Server | Trend weight. Expenditure estimate. Weekly energy budget. Turning a day label into grams. Detecting that a week doesn't sum, that a planned session didn't happen, or that a goal's stop condition has tripped. |
| Skill (Claude) | Goal setting and onboarding. Estimating macros from text and photos. Which days are heavy/moderate/rest. How to redistribute a skipped day. Whether to accept a proposal. All interpretation. |

## Locked decisions — do not re-litigate

| Decision | Choice |
|---|---|
| Language | Python 3.11+ |
| MCP framework | FastMCP, **Streamable HTTP transport** |
| Local store | SQLite |
| Packaging | Docker, bound to `127.0.0.1:18081` only (18080 is garmin-mcp) |
| Public exposure | Existing reverse tunnel, configured outside this repo |
| Auth | OAuth 2.1 + Dynamic Client Registration, **copied** from garmin-mcp (not shared, not gateway) |
| Host | Owner's always-on PC |
| Body/activity source | `garmin-mcp`, called server-to-server as an MCP client |
| Expenditure math | Deterministic, in-server, unit-tested |
| Coaching logic | `macro-coach` Skill, not here |
| Charts | Skill-owned pinned HTML template; Claude injects the series the server returns |
| Tracked nutrients | kcal, protein, carbohydrate, fat, **fiber** |
| Food photos | Read by Claude in-chat, **never stored** |
| Food data | Personal library, plus Open Food Facts for barcodes and branded items |
| Target shape | Weekly energy budget, distributed to per-day targets by Claude-assigned day type |
| Guardrails | **None blocking.** Implied rate of change is always reported alongside any target written. |
| Scheduling | Nightly recompute + staged weekly proposal. No unsolicited messages. |
| Multi-user | No. Single user, single account. |
| Audience | Publishable open source, same treatment garmin-mcp got |

## Non-goals

- **No coaching thresholds, philosophy, or programming rules in the server.** Those live in the Skill.
- **No body weight as system of record.** garmin-mcp owns weight. This server reads it.
- **No image storage.** No plate photos, no label photos.
- **No REST API in v1.** MCP only. Revisit when something concrete needs it.
- **No web UI in v1.** Optional late milestone if browsing turns out to beat asking.
- **No alcohol column.** Owner does not drink. Schema leaves room; adding it is a one-line migration.
- **No micronutrients beyond fiber.** No sodium, sugar, or saturated fat in v1.
- **No blocking safety limits.** Explicitly declined by the owner.

---

## Critical instructions

**Verify library APIs against the installed package, not from memory.** This has already bitten
once during planning: GarminDB's `weight` table turned out to hold only `(day, weight)` — no body
composition at all — invalidating an assumption that body-fat data was already available locally.
Inspect the installed package before writing any call.

**Never return a fabricated or interpolated value.** This rule is inherited from garmin-mcp and
matters more here, because a plausible-but-wrong expenditure estimate silently sets wrong macros
for weeks. Every tool that cannot answer returns `null` plus a `*_reason` field explaining why.
An LLM asked for a TDEE will always produce a confident number; the server's job is to refuse when
the data doesn't support one.

**A day with no logged food is unknown, not zero.** Treating an unlogged day as a low-intake day
makes every forgotten dinner read as a deficit and drags the expenditure estimate down. Unlogged
and partially-logged days are excluded from the fit and flagged.

**Build to the milestone gates below. Stop at each gate and report.** M1 and M2 carry all the real
risk — M1 because estimation accuracy is the foundation everything else rests on, M2 because the
smoothing constant is the highest-stakes parameter in the system. Do not build past them unverified.

**Never commit secrets.** `.env`, the SQLite file, and the garmin-mcp access token are gitignored.

---

## Known properties worth understanding before building

**Under-logging is self-correcting, until it isn't.** If the owner consistently under-reports by
15%, the expenditure estimate comes out 15% low — and the target derived from it is correspondingly
low, so the goal rate is still achieved. The absolute number is wrong but the system works. This
breaks when logging *bias changes* (getting stricter or lazier), which the algorithm reads as a
metabolic shift. The Skill should watch adherence *quality*, not just adherence.

**The trend signal is noisy relative to the effect being measured.** A realistic weigh-in series can move ~6 lb across three consecutive days on water shifts alone. Meanwhile the actual weekly signal being extracted is on the order of 1–2 lb. Smoothing is
not a nicety here; a naive linear fit over a short window is dominated by whichever day happened to
be a water spike.

**Energy density is a tunable constant, not 3500.** The 3500 kcal/lb figure assumes fat. An owner
doing progressive strength work while cutting is changing body composition, not just fat mass, so
the true conversion drifts. Make it configuration, and let the engine be calibrated against
observed data rather than asserting the textbook number.

---

## Data model

```
food_entry      id, logged_at (tz-aware), day (date), meal, description,
                kcal, protein_g, carb_g, fat_g, fiber_g,
                source (label|barcode|library|estimate), confidence, library_food_id?, planned (bool)

day_log         day (PK), status (complete|partial|unlogged), notes
                -- status drives inclusion in the expenditure fit

library_food    id, name, brand?, barcode?, serving_desc, serving_g,
                kcal, protein_g, carb_g, fat_g, fiber_g, source, times_used, last_used

recipe          id, name, servings, ingredients[] -> library_food + qty

body_comp       day (PK), percent_fat, method (scale|calipers|dexa|estimate),
                pushed_to_garmin (bool), push_error?
                -- system of record. Weight is NOT stored here; garmin-mcp owns it.

goal            id, mode (cut|bulk|maintain), rate_lb_per_week,
                protein_g_per_lb, fat_g_per_lb_floor,
                stop_metric (weight|bodyfat|date|none), stop_value,
                start_weight_lb?, start_percent_fat?,
                successor_goal_id?, status (active|met|superseded|abandoned),
                started_on, ended_on
                -- protein_g_per_lb/fat_g_per_lb_floor added when targets.py was built:
                -- the tool contract sketch below didn't say where these come from, and
                -- hardcoding a ratio in the server would be exactly the kind of nutritional
                -- stance SPEC's own non-goals list rules out -- so they're Claude-set,
                -- required, no server default. start_weight_lb/start_percent_fat are
                -- snapshotted at set_goal time so get_goal's progress/projected_completion
                -- have a baseline that survives later garmin-mcp data gaps.

training_plan   weekday (0-6), day_type       -- recurring pattern, written by Claude
day_plan        day (PK), day_type, explicit_macros?, source (plan|override|reconciled)

day_type        name (rest|moderate|heavy|...), energy_weight, carb_weight
                -- seeded at onboarding, tunable

proposal        id, kind (target|transition|reconciliation), created_on, payload,
                status (pending|accepted|declined), decided_on
```

Weight is deliberately absent. Body fat is deliberately present — garmin-mcp declared circumference
and body composition a non-goal, and goals here can terminate on BF%, so this server owns it.

---

## The expenditure engine

Two stages, both deterministic and unit-tested.

**Trend weight.** Exponentially-weighted (or Kalman) smoothing of daily weigh-ins pulled from
garmin-mcp. The smoothing constant is configuration and is the parameter most worth tuning.

**Expenditure.** Energy balance over a rolling window:

```
TDEE ≈ mean_daily_intake + (Δtrend_weight × kcal_per_lb) / days
```

...restricted to days marked `complete`, with recent data weighted more heavily, and returning a
confidence derived from data density and residual variance. Below a configured minimum of usable
days, it returns `null` with a reason — it does not return a low-confidence guess.

Notably, **activity data is not an input.** Expenditure is inferred from energy balance, so
training is already reflected in the observed weight response. Garmin activity, training load,
HRV, and sleep are *context for interpretation* — is the deficit hurting recovery, is a stall
actually water retention from a heavy session — not terms in the equation. Do not add exercise
calories on top of targets.

## Targets

The weekly energy budget is the anchor, because per-day targets vary:

```
weekly_budget = 7 × TDEE + (goal_rate_lb_per_week × kcal_per_lb)
```

`goal_rate_lb_per_week` uses the same sign convention as `get_expenditure`'s
`trend_lb_per_week` (and garmin-mcp's `get_body_trend`): **negative = losing**. The `+` here is
not a typo — with that convention, a cut's negative rate has to *lower* the budget, and
`7×TDEE − (negative × kcal_per_lb)` would raise it instead. This was caught by `targets.py`'s
own tests during the targets-engine build (M5.5, below), the same class of mistake the
sign-convention comment in `expenditure.py` was written to prevent.

Claude assigns a `day_type` per date (from `training_plan`, or an explicit override via
`day_plan`). The server resolves `day_type + weekly_budget → grams`. **The concrete algorithm,
locked when `targets.py` was built** (the milestone list only stated the principle — protein
flat, fat floor, carb carries the variance — not a formula; this is that formula, made explicit
so it's no longer ambiguous):

1. **Total weekly kcal is fixed by TDEE, not by day type.** `day_type.energy_weight` exists in
   the schema and is **reserved, unused by v1** — cycling total daily calories up on heavy days
   is a real, defensible practice, but it's a nutritional *stance*, not mechanical plumbing, and
   the server doesn't take stances (see non-goals). If a future version wants that, it's an
   explicit, documented addition, not something v1 does silently. `energy_weight` staying in the
   schema unused, rather than being deleted, is a deliberate choice: it's the extension point for
   whoever builds that later, not dead weight.
2. **Protein is flat across the week:** `protein_g = protein_g_per_lb × trend_weight_lb`, using
   the same `trend_weight_lb` `get_expenditure` already computed (if expenditure is `null`,
   targets are `null` too, with the same reason — there's no independent weight fetch here, by
   design, so the two can't disagree).
3. **Fat is a flat floor:** `fat_g = fat_g_per_lb_floor × trend_weight_lb`, same basis.
4. **`protein_g_per_lb` and `fat_g_per_lb_floor` are Claude-set, required, no server default.**
   Any ratio the server picked on its own would be exactly the kind of coaching judgment the
   non-goals list rules out of this codebase — they're parameters on `set_goal`, chosen the same
   way Claude would choose them in conversation with the user.
5. **Carbohydrate absorbs the entire remainder, distributed across the week by
   `day_type.carb_weight`:**
   ```
   protein_fat_weekly_kcal = Σ over 7 days (protein_g×4 + fat_g×9)     [flat, so just ×7]
   carb_pool_kcal = weekly_budget − protein_fat_weekly_kcal
   carb_g[day] = max(0, carb_pool_kcal) × (carb_weight[day] / Σ carb_weight over the week) / 4
   ```
   This is the "carry the variance" day-to-day differentiation — a heavy day's higher
   `carb_weight` pulls more of the week's fixed carb pool onto that day, a rest day's lower
   weight pulls less, while protein and fat stay identical every day.
6. **Infeasibility is detected, not silently absorbed.** If `carb_pool_kcal < 0` — the flat
   protein+fat floors alone already exceed the weekly budget — carbs clamp to 0 and the result
   carries an explicit `infeasible: true` plus the shortfall in kcal. The server does not lower
   protein or fat to make the week balance; SPEC's own guardrails decision means it doesn't
   block an aggressive target either. It reports the conflict and leaves the call to Claude.
7. **Week boundary is Monday-start, ISO (`date.weekday()`, Monday=0..Sunday=6)** — picked
   because it's the one Python's stdlib gives for free with no reinterpretation, not because it
   matches any particular convention elsewhere in the stack (`get_zone_summary` in garmin-mcp
   uses Sunday-start, matching that account's own profile setting — the two aren't meant to
   agree, they're unrelated boundaries for unrelated purposes).

If the week's seven resolved days don't sum to the budget for any reason other than the
infeasible case above (there shouldn't be one, given the algorithm above — this is a defensive
check, not an expected path), the server reports the delta rather than silently rebalancing.

**Adherence is judged weekly, not daily.** The Skill must say so; a "over by 400" day inside a
balanced week is not a miss.

**Past days freeze when the day closes.** Redistribution only touches remaining days in the current
week. Otherwise adherence history is unfalsifiable.

---

## Milestones

### M1 — Prove estimation, not plumbing

No MCP, no Docker, no auth. SQLite schema, service functions, and a CLI to log meals and dump a day.

The real risk is not the API — it is whether Claude's photo-and-text estimation is good enough to
build on. So M1 includes a **calibration harness**: ~20 real items whose labels are known, estimated
from photo alone, error quantified as a distribution. The owner uses a food scale, so portion mass
is usually known — meaning the harness should measure *identification and composition* error with
mass given, and separately measure portion error on the unweighted case (restaurant meals) since
those are the entries that will actually be uncertain in practice.

**Gate:** three real days logged through the CLI. Estimation error reported as a distribution with
sample size, not a summary adjective. That number determines how hard barcode lookup must work in M6.

### M2 — Expenditure engine, offline

Trend weight, energy-balance expenditure, confidence, and explicit unlogged-day handling.

Testable now without waiting weeks: simulate a subject with a known true TDEE of 2,600 eating 2,100,
inject realistic water-weight noise (calibrated to plausible ±6 lb day-to-day swings) and missed logging days, and verify recovery of 2,600 within tolerance.

**Gate:** known TDEE recovered from synthetic series within stated tolerance; sparse input returns
`null` with a reason; the smoothing constant's sensitivity is documented, not just chosen.

### M3 — MCP server, local

FastMCP over Streamable HTTP. Full tool surface below. No auth, no Docker yet.

**Gate:** every tool passes against MCP Inspector.

### M4 — Garmin bridge — done

macro-mcp authenticates to garmin-mcp as an MCP client and pulls trend weight for the
expenditure engine. Degrades honestly when garmin-mcp is unreachable or login fails —
`get_expenditure` returns `tdee: null` with the specific reason, never a stale-but-unlabeled
or fabricated number.

**Auth mechanism, found by reading garmin-mcp's own oauth.py rather than assuming:**
garmin-mcp requires full OAuth 2.1 + Dynamic Client Registration on every connection — there
is no static-bearer shortcut, even for a same-host client. `garmin_client.py` completes this
headlessly: register a client once, submit `GARMIN_MCP_TOKEN` as the same login-form POST a
human in Claude's connector UI would send, exchange the resulting code for an access/refresh
token pair, cache it, refresh before expiry. Verified against the live, running garmin-mcp
instance end to end (register → 201, authorize → 302 with a code, code → token exchange,
refresh_token grant → new access token, and a live `get_body_trend` call returning real data),
not written from the OAuth spec alone.

**Both open questions answered empirically, in writing:**

1. **Body-fat push — does not work with the installed `garminconnect==0.3.2`, do not enable it.**
   `add_body_composition` doesn't call a "set today's body-fat" API field — it synthesizes a
   FIT file (the binary format a real device produces) with an anonymous, unidentified device
   and uploads it through Garmin Connect's generic device-data ingestion pipeline, the same one
   a real scale's sync uses. Tested live against two cases: (a) a date with an existing
   same-day MANUAL weigh-in already on file, using the *same* weight value to make the test
   safe regardless of outcome, and (b) a date with zero existing data. Both uploads were
   accepted at the ingestion layer (`"File processed"`, HTTP 200) but produced **zero
   observable effect** — no field updated, no duplicate created, confirmed via the live API
   immediately and a few seconds later. This makes the original duplication question moot:
   there's nothing to clobber because the write doesn't appear to take effect at all against
   Garmin's current backend with this library version (a known risk class for this project —
   see garmin-mcp's own README on the unofficial API changing under it). `log_body_comp`'s
   `push_to_garmin` stays a documented no-op (`PUSH_NOT_WIRED_REASON` in `body.py`) — not
   because the wiring is unbuilt, but because building it would mean silently claiming
   `pushed: true` for a write with no verified effect. Revisit only if a future
   `garminconnect` version or a different write path changes this.
2. **Nutrition write-back — does not exist, confirmed by exhaustive package inspection.** The
   installed `garminconnect` client has zero write methods for nutrition/food/meal data
   anywhere in its ~134-method public surface (checked every `add_/log_/set_/post_/write_/
   upload_`-prefixed method, and every method with `nutrition/meal/food/diet/calorie/macro`
   in its name) — only three read methods
   (`get_nutrition_daily_food_log`/`_meals`/`_settings`). No live call was needed; the
   capability simply isn't in the library. Confirms the README's own expectation (Garmin
   populates that field via MyFitnessPal). **Decision: macro-mcp will not attempt nutrition
   write-back.** Matches the locked-decisions table's default (macro-mcp owns nutrition), now
   evidenced rather than assumed.

**Gate:** met. `get_expenditure` verified running on real weight pulled live from garmin-mcp,
through the actual deployed server (12 real weight points → a real TDEE from real+synthetic
data; separately verified honest degrade on both garmin-mcp-unreachable and wrong-token cases).
Both open questions answered in writing above, with live evidence.

### M5 — Auth, Docker, mobile — auth/Docker done; phone gate is the owner's to run

OAuth provider (`oauth.py` + `auth.py`) copied from garmin-mcp -- same DCR/PKCE/login-gate
mechanism, same persistence-to-`/data` pattern, branding and default `OAUTH_STATE_PATH`
changed only. Container publishes `127.0.0.1:18081` (chosen not to collide with garmin-mcp's
`18080`). `/health` (unauthenticated) returns version, DB status, and garmin-mcp reachability.

**All verified live, not just built:**
- Full `docker build` + `docker compose up`, then confirmed via `/health` that the DB and the
  `host.docker.internal` route to garmin-mcp both work from inside the container.
- An unauthenticated MCP request is refused.
- A real headless OAuth login (register → authorize → token exchange, the same mechanism
  `garmin_client.py` uses against garmin-mcp, run here against this server's own provider)
  succeeds, and the resulting token makes a real authenticated tool call that persists data
  through the volume mount -- confirmed by reading the SQLite file back from the host after
  the container was stopped.
- `scripts/mcp_smoke.py` was extended to log in for real rather than connecting unauthenticated,
  plus a new check that an unauthenticated request is refused. 16/16 passing.

**One real bug found during Docker verification:** `_garmin_mcp_status()`'s health probe only
caught `httpx.HTTPError`. A malformed `GARMIN_MCP_URL` (an out-of-range port number, from the
verification's own test config) raised `OverflowError` from deep in asyncio's socket layer --
well outside httpx's exception hierarchy -- and crashed the `/health` endpoint entirely.
Fixed to catch broadly, matching `_db_status`'s existing "a health check must never raise"
principle; added as a permanent regression test in `test_server.py`.

**A minimal `macro-coach` Skill ships** (see `../macro-coach`) -- logging food well, honest
about `null`s, explicitly silent on target-setting since that engine doesn't exist yet.

**What's left is the owner's to do, not buildable from here:** exposing the container
publicly (Tailscale Funnel or equivalent -- a real, deliberate change to what's reachable
from the internet), adding it as a Claude connector, and the actual gate below. See
`docs/self-hosted-setup.md`.

**Gate:** photograph a meal on the phone; it lands in the DB with correct macros and a stated
confidence. Requires the owner's own phone, Claude app, and tunnel decision.

### M5.5 — Targets and goals engine — done

Not in the original milestone list -- inserted here because M3 scoped goal/target/day-plan
tools out for a real reason (the weekly-budget engine they depend on didn't exist yet) and
that gap was still open after M5. Numbered 5.5 rather than renumbering M6-M8, since nothing
past this point depends on where exactly it sits, only that it exists before M7's proposals
(which need a target to propose changes *to*) and M8's onboarding (which needs a goal to set
in the first place).

Builds `targets.py` (the weekly-budget-to-grams algorithm -- see "## Targets" above) and
`goals.py` (goal lifecycle: creation, supersession, progress tracking against a snapshotted
baseline). Wires `set_goal`, `get_goal`, `set_training_plan`, `set_day_plan`, `get_targets`
into the MCP server, and -- meaningfully -- retires the `targets: null` placeholder `get_day`
has returned since M1 in favor of real resolved targets when a goal exists.

**Explicit non-goals for this pass:** `get_weekly_review` (the composite convenience call)
and the full `proposal` table (`get_proposals`/`accept_proposal`/`decline_proposal`) stay
unbuilt -- both belong with M7, where nightly recompute and staged proposals give them
something real to compose over. `get_goal`'s `stop_condition_met` is reported honestly as a
computed fact but does **not** auto-transition `goal.status` to `'met'` -- that transition is
supposed to be a Claude-mediated proposal (M7), which doesn't exist yet, so an interim
`get_goal` call reports the fact and leaves the decision where M7 will eventually formalize it.

**Gate:** met. A full week resolves correctly end to end (weekly budget → day-type-weighted
grams → `get_day` showing real targets and `remaining`) against both a synthetic case
(`test_targets.py`/`test_goals.py`, including the infeasible-week case — clamped to 0 carbs,
shortfall reported, protein/fat floors left untouched, not silently rebalanced) and a real
goal set against real garmin-mcp weight data, verified through the actual running,
authenticated MCP server: a real `set_goal` call, real `set_training_plan`/`set_day_plan`
overrides, and `get_day` returning real `day_type: "heavy"`, real resolved
protein/carb/fat/kcal targets, and real `remaining` computed against actual logged totals.

**One real sign bug caught by the tests, not in review:** `weekly_budget`'s formula used `−`
where the "negative = losing" rate convention (matching `get_expenditure`) required `+` — as
written, a cut would have *raised* the budget instead of lowering it, silently inverting
every cut/bulk target. Caught because `test_weekly_budget_cut_is_lower_than_tdee` failed
outright, not because anyone eyeballed the arithmetic. See "## Targets" above for the fix and
the worked example now in both the code and here.

### M6 — Food library and barcode

Personal library built from logged entries; Open Food Facts barcode and branded search; recipes;
quick-repeat resolving to a prior exact entry rather than a fresh estimate.

**Gate:** a repeat meal logs in one conversational turn with `source: library` and identical numbers
to last time.

### M7 — Nightly recompute, proposals, reconciliation

Cron recalculates trend weight and expenditure nightly so tool calls are instant. Three staged
proposal types, all waiting silently to be asked about:

- **Target** — weekly, when the expenditure estimate has moved.
- **Reconciliation** — a planned training day with no recorded session ("Tuesday planned heavy, no
  session found, 350 kcal unallocated"). Claude decides the redistribution and writes it.
- **Transition** — a goal's stop condition has tripped; its successor is proposed.

**Gate:** a week of data produces a target proposal; accepting changes targets; declining leaves them
alone and records the decline. A deliberately skipped training day produces a reconciliation proposal.

### M8 — `macro-coach` Skill, complete

Onboarding interview that sets initial targets from goals and stats before any expenditure data
exists, and that handles every goal mode (cut to weight, cut to BF%, bulk to a BF ceiling, maintain)
rather than just the owner's current one. Interpretation rules. The pinned interactive chart
template. Explicit rules for reading macro data against `garmin-coach` — a stall with falling HRV
and rising training load is a different problem than a stall with clean recovery.

**Gate:** a cold "how's my cut going?" produces the right tool calls, a correct reading including
stated confidence, and a chart with working hover.

### Optional, later

Read-only web dashboard. CSV import from MacroFactor/MyFitnessPal. REST API. Only if something
concrete demands them.

---

## Tool contracts

All weights in **pounds**, food masses in **grams**, dates ISO `YYYY-MM-DD`, times
`America/New_York`, day boundary **midnight**. Every tool returns compact JSON — few fields, high
signal. The consumer is an LLM context window.

### Logging

```
log_food(description, meal, items[], when=None, planned=False)
  items[]: {name, qty, unit, kcal, protein_g, carb_g, fat_g, fiber_g, confidence, source}
  -> {ok, entry_id, day, day_totals, remaining_vs_target}

log_from_library(food_id | recipe_id, qty, meal, when=None) -> same shape
edit_entry(entry_id, ...) -> {ok, day, day_totals, recomputed}
delete_entry(entry_id)    -> {ok, day, day_totals}
set_day_status(day, status) -> {ok}   # complete | partial | unlogged
```

### Library

```
save_food(...) / save_recipe(...)     -> {id}
search_library(query, limit=10)       -> [{id, name, brand, serving_desc, macros, times_used}]
lookup_barcode(code)                  -> {found, name, brand, serving, macros, source} | {found: false}
search_foods(query, limit=10)         -> branded/OFF results, clearly marked by source
```

### Reads

```
get_day(date=today)
  -> {date, day_type, targets, totals, remaining, entries[], status, adherence_note}

get_targets(date=today)
  -> {date, day_type, kcal, protein_g, carb_g, fat_g, fiber_g, source, week_budget_delta}

get_intake_trend(days=28)
  -> {points: [{date, kcal, protein_g, carb_g, fat_g, fiber_g, status}],
      avg_kcal_complete_days, days_complete, days_partial, days_unlogged}

get_expenditure(days=28)
  -> {tdee, confidence, method, days_used, trend_weight_lb, trend_lb_per_week,
      kcal_per_lb_used, tdee_null_reason?}

get_goal()
  -> {mode, rate_lb_per_week, stop_metric, stop_value, progress, projected_completion,
      implied_rate_pct_bodyweight, successor?}

get_body_comp(days=90)
  -> {points: [{date, percent_fat, method}], latest, pushed_to_garmin}

get_weekly_review()
  -> {week, expenditure, trend, intake_summary, adherence, day_breakdown,
      garmin_context: {training_load, hrv, sleep, staleness}, pending_proposals[]}
```

`get_weekly_review` is the composite convenience call, same pattern and same rationale as
garmin-mcp's `get_readiness` — one round trip for the question the Skill asks most.

### Planning and goals

```
set_goal(mode, rate_lb_per_week, protein_g_per_lb, fat_g_per_lb_floor,
         stop_metric, stop_value, successor_goal_id=None)
  -> {ok, goal_id, weekly_budget, implied_rate_pct_bodyweight}
  -- protein_g_per_lb/fat_g_per_lb_floor added post-sketch: see "## Targets" for why these
  -- are required Claude-set parameters rather than server defaults. Supersedes any current
  -- active goal (status -> superseded, ended_on = today) unless successor_goal_id links this
  -- as a specific prior goal's planned next phase.

set_training_plan(weekday_map)          -> {ok, week_budget_delta}
set_day_plan(date, day_type | macros)   -> {ok, targets, week_budget_delta}
log_body_comp(date, percent_fat, method, push_to_garmin=False)
  -> {ok, pushed, push_error?}

get_proposals(kind=None)                -> [{id, kind, created_on, payload, rationale_inputs}]
accept_proposal(id) / decline_proposal(id, reason?) -> {ok, applied}
```

`set_goal` always returns `implied_rate_pct_bodyweight`. It never refuses a value. Reporting is not
gating — this is the agreed treatment of safety limits.

---

## Repo structure

```
macro-mcp/
├── src/macro_mcp/
│   ├── server.py        # FastMCP app, tool registration
│   ├── oauth.py         # OAuth 2.1 / DCR provider (copied from garmin-mcp)
│   ├── garmin_client.py # MCP client for garmin-mcp
│   ├── expenditure.py   # trend weight + TDEE engine
│   ├── targets.py       # weekly budget -> per-day resolution
│   ├── goals.py         # goal phases, stop conditions, transitions
│   ├── foods.py         # library, recipes, Open Food Facts
│   ├── store.py         # SQLite access
│   └── models.py        # return shapes
├── scripts/
│   ├── calibrate.py     # M1 estimation calibration harness
│   ├── simulate.py      # M2 synthetic-series validation
│   ├── nightly.py       # recompute + stage proposals
│   └── export_csv.py
├── tests/
├── Dockerfile
├── compose.yml
├── .env.example
├── SPEC.md
└── README.md
```

## Config (env)

```
SQLITE_PATH=/data/macro.db
MCP_BEARER_TOKEN=
MCP_PUBLIC_URL=
GARMIN_MCP_URL=http://127.0.0.1:18080/mcp
GARMIN_MCP_TOKEN=
KCAL_PER_LB=3500
TREND_SMOOTHING_ALPHA=0.15
EXPENDITURE_WINDOW_DAYS=28
EXPENDITURE_MIN_DAYS=14
OFF_ENABLED=true
TZ=America/New_York
LOG_LEVEL=INFO
```

## Error handling

- **garmin-mcp unreachable** — return weight-dependent fields as `null` with a reason naming the
  dependency. Never substitute a last-known value without labeling its age.
- **Open Food Facts miss or timeout** — return `{found: false}` and fall through to Claude
  estimation. Never invent a branded product's macros.
- **Insufficient data for expenditure** — `null` plus `tdee_null_reason`, never a low-confidence number.
- **Body-comp push failure** — the local record still commits. Push state is recorded separately so
  a Garmin outage never blocks goal tracking.

## Testing

Unit tests for the expenditure engine against synthetic series with known ground truth — this is the
most important test suite in the project. Unit tests for target resolution and weekly-budget
arithmetic. Integration tests with garmin-mcp mocked. One live integration test, marked and skipped
by default, that runs read-only against the real garmin-mcp instance.

## Backup

The SQLite file is the entire system of record for food, library, goals, and body composition. A
documented backup path and `scripts/export_csv.py` ship in M1, not as an afterthought — the owner
should never be trapped in this thing.
