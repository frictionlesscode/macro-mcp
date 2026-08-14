# macro-mcp

A self-hosted [MCP](https://modelcontextprotocol.io) server that turns Claude into a nutrition
log and macro coach: you tell it what you ate (or photograph it), and it stores the numbers,
estimates your real energy expenditure from how your weight actually responds, and resolves
that into daily macro targets.

It's a companion to [garmin-mcp](https://github.com/frictionlesscode/garmin-mcp) — it pulls
your real weight trend from there rather than storing body weight itself, so the two servers
own distinct halves of the picture and never disagree about your weight.

Like garmin-mcp, this is the data-and-math plane only. It computes; it doesn't coach. The
judgment layer — how to interpret a stall, what protein ratio suits a given goal, when to end
a cut — lives in the [macro-coach](https://github.com/frictionlesscode/macro-coach) Skill and
in the conversation, not in this codebase.

## Benefits

- **Log food by talking or by photo.** No app, no barcode-scanning ritual, no searching a
  database for "chicken breast, raw, boneless" — describe it or show it, and it's logged.
- **Expenditure derived from your own data, not a formula.** No Harris-Benedict, no activity
  multiplier, no "sedentary/moderate/active" dropdown. TDEE comes from energy balance: what
  you actually ate versus how your trend weight actually moved. Training is already reflected
  in that response, so exercise calories are never added on top.
- **It refuses to guess.** Not enough logged days, too few weigh-ins, garmin-mcp unreachable —
  every one of those returns `null` with a specific stated reason rather than a plausible
  number. A confidently wrong TDEE silently sets wrong macros for weeks; that failure mode is
  designed out.
- **Unlogged days are unknown, not zero.** A day you forgot to log never counts as a
  zero-calorie day, which would otherwise drag the expenditure estimate down every time life
  got in the way.
- **A personal food library that gets faster over time.** Save a food once and repeats log
  with exact stored numbers instead of a fresh estimate that wobbles day to day.
- **Self-hosted, single-user.** Your food log lives in a SQLite file on your own machine.

## Tools

**Logging:** `log_food`, `log_from_library`, `edit_entry`, `edit_item`, `delete_entry`,
`delete_item`, `set_day_status`

**Library:** `save_food`, `save_recipe`, `search_library`

**Reads:** `get_day`, `get_targets`, `get_intake_trend`, `get_expenditure`, `get_goal`,
`get_body_comp`

**Goals and planning:** `set_goal`, `set_training_plan`, `set_day_plan`, `log_body_comp`

Every tool's docstring documents what its `null`s mean and where its numbers come from.

## How the math works

**Expenditure.** Trend weight is an exponentially-weighted moving average over your weigh-ins
(time-aware, so a missed day doesn't lag the trend). TDEE is then energy balance over a
trailing window:

```
TDEE ≈ mean_daily_intake + (Δtrend_weight × kcal_per_lb) / days
```

restricted to days you marked `complete`, weighted toward recent data. Below a configurable
minimum of usable days it returns `null` rather than a low-confidence guess.

**Targets.** The weekly energy budget is the anchor, because per-day targets vary:

```
weekly_budget = 7 × TDEE + (goal_rate_lb_per_week × kcal_per_lb)
```

(`goal_rate_lb_per_week` is negative for losing — so the `+` correctly *lowers* the budget on
a cut.) Protein and fat are flat every day, scaled off trend weight by ratios **you** set;
carbohydrate absorbs the remainder and is distributed across the week by each day's training
type, so heavy days get more carbs and rest days fewer without changing the week's total.

If your protein and fat floors alone exceed the weekly budget, that's reported explicitly as
infeasible with the exact shortfall — the server won't quietly lower your floors to make the
arithmetic work, and it won't block an aggressive target either. It reports; you decide.

See [SPEC.md](SPEC.md) for the full derivation and the reasoning behind each choice.

## Setup

### Prerequisites

- Docker, and a machine that can stay online.
- A running [garmin-mcp](https://github.com/frictionlesscode/garmin-mcp) instance (for weight
  data). Without it the server runs fine, but `get_expenditure` and `get_targets` will
  honestly report that weight data is unavailable.

### 1. Configure

```bash
cp .env.example .env
```

| Var | What it's for |
|---|---|
| `MCP_BEARER_TOKEN` | This server's login password for its OAuth flow. Pick a long random string. |
| `MCP_PUBLIC_URL` | The externally-reachable URL you'll expose this at. Required for correct OAuth redirect URLs — `127.0.0.1` won't work once tunneled. |
| `GARMIN_MCP_URL` | Your garmin-mcp instance. Use `host.docker.internal` rather than `127.0.0.1` when running in Docker — see `.env.example`. |
| `GARMIN_MCP_TOKEN` | Must match garmin-mcp's own `MCP_BEARER_TOKEN`. |
| `TZ` | Your local timezone. Determines the midnight day boundary. |

### 2. Run

```bash
docker compose up --build -d
curl http://127.0.0.1:18081/health
```

`/health` is unauthenticated and reports version, database status, and whether garmin-mcp is
reachable. Every other endpoint requires a real OAuth access token.

### 3. Expose it and connect Claude

Claude's connector UI requires OAuth 2.1 with Dynamic Client Registration — it won't accept a
static API key. This server implements that (the provider is copied from garmin-mcp, same
mechanism), gated behind `MCP_BEARER_TOKEN` as a one-time login password.

**Claude's backend only egresses on port 443.** A tunnel on any other port will fail silently —
the request never arrives, and nothing appears in your logs. If garmin-mcp already occupies
443 on the same host, put this server on a *path* under that same port rather than a different
port. See [docs/self-hosted-setup.md](docs/self-hosted-setup.md) for a worked example.

Then add a custom connector in Claude pointing at `<MCP_PUBLIC_URL>/mcp` and sign in with your
`MCP_BEARER_TOKEN`.

### 4. Install the Skill

[macro-coach](https://github.com/frictionlesscode/macro-coach) teaches Claude how to use these
tools correctly. Strongly recommended — the tools return honest `null`s and confidence levels
that a model will otherwise misread.

## What's built

Food logging, the personal library, body composition, the expenditure engine, and the
targets/goals engine are all implemented and tested end to end against real data.

Not yet built, and deliberately not stubbed: `get_weekly_review` (a composite weekly summary),
the staged-proposal flow (goal and target changes currently apply immediately rather than
waiting for your approval), nightly recompute, barcode/branded-food lookup, and chart
rendering. `SPEC.md`'s milestone list tracks these.

## FAQ

**Why doesn't it add my exercise calories to my target?**
Because expenditure is derived from how your weight actually responded to what you ate, and
that response already includes your training. Adding exercise calories on top would
double-count them. This is the same reasoning MacroFactor uses, and it's why the server never
takes activity data as an input to the TDEE calculation — Garmin data is context for
*interpretation*, not a term in the equation.

**Why is my TDEE `null`?**
Check `tdee_null_reason` — it names the specific cause: not enough days marked `complete`, too
few weigh-ins spanning too short a window, or garmin-mcp being unreachable. The estimate needs
roughly two to three weeks of consistent logging and weigh-ins before it's trustworthy, and it
says so rather than pretending otherwise.

**Does under-logging break it?**
Less than you'd think, and this is worth understanding. If you consistently under-report by
15%, the expenditure estimate comes out 15% low — and the target derived from it comes out
correspondingly low, so you still hit your goal rate. The absolute number is wrong but the
system works. What *does* break it is bias that **changes** over time, since the algorithm
reads that as a metabolic shift. Consistency matters more than accuracy.

**Can it write my nutrition data back to Garmin Connect?**
No. The `garminconnect` client has no write path for nutrition data at all — verified by
inspecting its entire public API surface. Garmin normally populates that field via
MyFitnessPal.

**Why does `push_to_garmin` on `log_body_comp` do nothing?**
It was tested live against a real account. `add_body_composition` uploads a synthetic FIT file
through Garmin's device-data pipeline; the upload is accepted (`"File processed"`, HTTP 200)
but produces no observable change to the actual data — tested both against a date with an
existing weigh-in and a clean date. Rather than claim a success that wasn't verified, the flag
is accepted and honestly reported as a no-op. See SPEC.md's M4 section.

**Does this send my food data anywhere?**
Only to whatever MCP client you connect. The database is a local SQLite file; the only
outbound calls are to your own garmin-mcp instance.

**Am I locked in?**
No. `scripts/export_csv.py` dumps every table to CSV, and it shipped in the first milestone
specifically so it'd be tested rather than written a year later and never run.

## Repo structure

```
macro-mcp/
├── src/macro_mcp/
│   ├── server.py         # FastMCP app, tool registration
│   ├── oauth.py / auth.py # OAuth 2.1 + DCR provider (copied from garmin-mcp)
│   ├── garmin_client.py  # MCP client for garmin-mcp
│   ├── expenditure.py    # trend weight + TDEE engine
│   ├── targets.py        # weekly budget -> per-day grams
│   ├── goals.py          # goal lifecycle, training/day plans
│   ├── foods.py          # logging, library, recipes
│   ├── body.py           # body composition
│   ├── store.py          # SQLite schema and access
│   └── models.py         # shared types and validation
├── scripts/
│   ├── log_cli.py        # local logging CLI
│   ├── calibrate.py      # estimation-accuracy harness
│   ├── simulate.py       # expenditure-engine validation
│   ├── mcp_smoke.py      # end-to-end MCP check
│   └── export_csv.py
├── docs/self-hosted-setup.md
├── SPEC.md               # design spec, locked decisions, milestone log
└── compose.yml
```

## Development

```bash
python -m venv .venv
.venv\Scripts\activate      # or: source .venv/bin/activate
pip install -e ".[dev]"
pytest -q
```

`scripts/simulate.py` validates the expenditure engine against synthetic data with a known
true TDEE — including realistic water-weight noise and missed logging days — and reports how
the smoothing constant trades noise rejection against responsiveness to real change.
`scripts/mcp_smoke.py` starts a real server, completes a real OAuth login, and exercises every
tool over the wire.

## Backup

The SQLite file (`./data/macro.db`) is the entire system of record for food, library, recipes,
goals, and body composition. Back it up like anything you can't regenerate.

## License

MIT — see [LICENSE](LICENSE).
