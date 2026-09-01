# macro-mcp

> **Unofficial personal project.** Not affiliated with or endorsed by any nutrition, fitness, or device company. It runs entirely on your own machine; the only optional outbound call is to your own [garmin-mcp](https://github.com/frictionlesscode/garmin-mcp) instance for a weight trend.

A self-hosted [MCP](https://modelcontextprotocol.io) server that turns Claude into a nutrition
log: you tell it what you ate (or photograph it), it logs the numbers, and it tracks intake
against whatever macro targets you set — trend statistics, adherence, and server-rendered
charts. It also stores progress photos, pose-aligned frame to frame, viewable on a small
self-hosted dashboard next to your weight and body-fat trend.

It deliberately does **not** decide what you should eat. TDEE, goals, training-day cadence,
and target derivation all live in the conversation and in Claude's own judgment — this server
records what it's told and measures what happened against it. See
[SPEC.md's Charter](SPEC.md#charter) for the full reasoning, including the design this
replaced and why.

Like [garmin-mcp](https://github.com/frictionlesscode/garmin-mcp), this is the data-and-math
plane only. The judgment layer lives in the
[macro-coach](https://github.com/frictionlesscode/macro-coach) Skill and in conversation, not
in this codebase.

## Benefits

- **Log food by talking or by photo.** No app, no barcode-scanning ritual — describe it or
  show it, and it's logged. (Food photos are read in-chat only, never stored — see "Progress
  photos" below for the photos this server *does* keep.)
- **You set the targets; the server just holds them.** No formula guesses your calories for
  you. `set_targets` takes exact macros per date — a fat-cycling protocol, a refeed, a single
  one-off day — Claude and you decide the numbers, the server stores and measures against them.
- **It refuses to guess.** Too few logged days for a trend statistic, no target set for a date
  — every one of those returns `null` with a specific stated reason rather than a plausible
  number.
- **Unlogged days are unknown, not zero.** A day you forgot to log never counts as a
  zero-calorie day in any average, adherence rate, or chart.
- **A personal food library that gets faster over time.** Save a food once and repeats log
  with exact stored numbers instead of a fresh estimate that wobbles day to day.
- **Progress photos, aligned automatically.** Pose-landmark detection lines up your torso frame
  to frame, so small changes are visible in a slideshow instead of buried under differences in
  distance, tilt, and framing.
- **Self-hosted, single-user.** Your food log and photos live on your own machine.

## Tools

**Logging:** `log_food`, `log_from_library`, `edit_entry`, `edit_item`, `delete_entry`,
`delete_item`, `set_day_status`

**Library:** `save_food`, `save_recipe`, `search_library`

**Targets:** `set_targets`, `get_targets`, `delete_targets`

**Reads and trends:** `get_day`, `get_intake_trend`, `get_trend`, `render_trend`

**Body:** `log_body_comp`, `get_body_comp`, `log_body_photo`, `get_body_photo`,
`list_body_photos`, `delete_body_photo`

Every tool's docstring documents what its `null`s mean and where its numbers come from.

## Setup

### Prerequisites

- Docker, and a machine that can stay online.
- (Optional) A running [garmin-mcp](https://github.com/frictionlesscode/garmin-mcp) instance,
  only if you want the `/dashboard` page's weight line — see "Progress photos and dashboard"
  in SPEC.md for why that's the one place this server talks to garmin-mcp.

### 1. Configure

```bash
cp .env.example .env
```

| Var | What it's for |
|---|---|
| `MCP_BEARER_TOKEN` | This server's login password for its OAuth flow. Pick a long random string. |
| `MCP_PUBLIC_URL` | The externally-reachable URL you'll expose this at. Required for correct OAuth redirect URLs — `127.0.0.1` won't work once tunneled. |
| `DASHBOARD_TOKEN` | Separate secret gating `/dashboard`. Unset disables the dashboard entirely — it fails closed, not open. |
| `TZ` | Your local timezone. Determines the midnight day boundary. |
| `GARMIN_MCP_URL`, `GARMIN_MCP_TOKEN` | Optional. Only used by `/dashboard`'s weight panel. Without them the dashboard still works, just without a weight line. |

### 2. Run

```bash
docker compose up --build -d
curl http://127.0.0.1:18081/health
```

`/health` is unauthenticated and reports version and database status. Every MCP tool requires
a real OAuth access token; `/dashboard` requires `DASHBOARD_TOKEN` as a query parameter
instead (it's a plain browser page, not an MCP client).

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
tools correctly. Strongly recommended — the tools return honest `null`s that a model will
otherwise misread as a missing feature.

### 5. View the dashboard

`<MCP_PUBLIC_URL>/dashboard?token=<DASHBOARD_TOKEN>` — weight and body-fat % trend charts, plus
a slideshow of aligned progress photos. Bookmark the URL with the token included; there's no
separate login.

## Progress photos

`log_body_photo` stores a photo per `(date, angle)` — `front`, `side`, or `back` — and tries to
detect a pose (shoulders, hips) so `/dashboard`'s slideshow can rotate/scale/crop each photo
onto a shared frame. That detection is a real ML dependency
([MediaPipe](https://github.com/google/mediapipe), `pyproject.toml`'s `photos` extra, on by
default in the Docker image) and doesn't have a guaranteed wheel on every architecture. If it's
missing, or it can't find a confident pose in a given photo, the photo is still stored and
still shown — just unaligned, and the dashboard says so rather than pretending otherwise.

No MCP tool returns the photo itself into chat — only metadata (dimensions, alignment status,
your note). View the actual images at `/dashboard`.

## What's built

Food logging, the personal library, stored targets, trend statistics, SVG charting, body
composition, and progress photos with pose alignment are all implemented and tested end to
end against real data (`scripts/mcp_smoke.py`).

Not yet built: Open Food Facts barcode/branded-food lookup, feeding the personal library from
an external database. `SPEC.md`'s milestone list tracks this.

## FAQ

**Why doesn't the server tell me what my calories should be?**
Because that's a nutritional opinion, not arithmetic — see `SPEC.md`'s "Charter change" section
for the concrete failure that led to removing an earlier version that tried. Claude and you set
the number; the server stores it and reports what happened against it.

**Why is `targets` `null` on `get_day`?**
Nothing was set for that date yet. Call `set_targets` for it — nothing here derives a target
automatically.

**Does under-logging break the trend statistics?**
Unlogged and partial days are excluded from every average and adherence calculation, never
counted as zero. What *does* skew things is under-*reporting* on days you do log — the numbers
are only as honest as what went in.

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

**Is `/dashboard` safe to expose the way the MCP endpoint is?**
It's gated by its own secret (`DASHBOARD_TOKEN`), deliberately separate from your MCP bearer
token — a leaked dashboard link (pasted into a chat, sitting in browser history) can't be used
to authenticate as your MCP client. Leaving `DASHBOARD_TOKEN` unset disables the page entirely.

**Does this send my food or photo data anywhere?**
Only to whatever MCP client you connect, and — for `/dashboard`'s weight panel only — a
read-only call to your own garmin-mcp instance. The database and photo files are local; no
photo is ever uploaded anywhere by this server.

**Am I locked in?**
No. `scripts/export_csv.py` dumps every table to CSV.

## Repo structure

```
macro-mcp/
├── src/macro_mcp/
│   ├── server.py         # FastMCP app, tool + HTTP route registration
│   ├── oauth.py / auth.py # OAuth 2.1 + DCR provider (copied from garmin-mcp)
│   ├── foods.py          # logging, library, recipes, get_day
│   ├── targets.py        # stored targets (no derivation)
│   ├── trends.py         # rolling averages, adherence, coverage
│   ├── charts.py         # dependency-free SVG rendering
│   ├── body.py           # body composition
│   ├── body_photos.py    # progress photos: storage + pose-landmark alignment
│   ├── garmin_weight.py  # narrow, read-only weight fetch for the dashboard only
│   ├── dashboard.py      # renders /dashboard's HTML
│   ├── store.py          # SQLite schema and access
│   └── models.py         # shared types and validation
├── models/                # pose_landmarker_lite.task, fetched at Docker build time
├── scripts/
│   ├── log_cli.py        # local logging CLI
│   ├── calibrate.py      # estimation-accuracy harness
│   ├── mcp_smoke.py      # end-to-end MCP + dashboard check
│   └── export_csv.py
├── docs/self-hosted-setup.md
├── SPEC.md               # design spec, locked decisions, milestone log
└── compose.yml
```

## Development

```bash
python -m venv .venv
.venv\Scripts\activate      # or: source .venv/bin/activate
pip install -e ".[dev,photos]"
pytest -q
```

`scripts/mcp_smoke.py` starts a real server, completes a real OAuth login, and exercises every
tool and HTTP route over the wire — including `log_body_photo` with real pose detection if the
`photos` extra and its model file (`models/pose_landmarker_lite.task`) are present.

## Backup

`./data/macro.db` and `./data/photos/` are the entire system of record — food, library,
recipes, targets, body composition, and progress photos. Back both up like anything you can't
regenerate.

## License

MIT — see [LICENSE](LICENSE).
