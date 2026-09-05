# Self-hosted setup notes

Generic guidance for exposing this server publicly so it can be added as a Claude custom
connector, plus a few operational notes worth knowing before you run this long-term.
Everything below uses placeholder values — `<your-machine>`, `example.ts.net`, etc. — swap in
your own. This mirrors [garmin-mcp's own setup doc](https://github.com/frictionlesscode/garmin-mcp/blob/main/docs/self-hosted-setup.md)
closely, since the two servers use the identical auth mechanism and are meant to run side by
side on the same machine.

## Exposing the server publicly (Tailscale Funnel)

Claude's connector UI needs a real HTTPS URL to reach your server's `/mcp` endpoint —
`127.0.0.1` or a bare LAN IP won't work.

**Two hard constraints drive this entire section. Both were learned the expensive way:**

1. **Claude's backend only egresses on port 443.** Any other port fails *silently* — the
   request never arrives and **nothing appears in your logs**, which looks identical to a
   server bug. Verified live: ports `943` and `18082` each passed every host-side check
   including a complete OAuth login through the public URL, while Claude's connector couldn't
   reach them at all. ([Reference](https://www.brendanlong.com/debugging-claude-ai-mcp-connectors.html).)
   Note this means Funnel *will* happily configure an arbitrary port for you — that's not
   evidence it works.
2. **OAuth protected-resource discovery is domain-root-relative**, so two MCP servers sharing
   one hostname collide. Details and the workaround below.

Since garmin-mcp already occupies port 443 on this tailnet node, macro-mcp goes on a **path
under that same port**, not a different port.

### Path-based sharing on port 443

```bash
tailscale funnel --bg --set-path=/macro 18081
```

That alone is **not sufficient**, and the failure is confusing: Claude reports an invalid or
expired token and never shows a login prompt, while your logs show a bare `POST /mcp` → 401
with no `/register` or `/authorize` behind it — i.e. it gave up before trying to authenticate.

The cause is a prefix mismatch:

- On a 401, the server advertises where to authenticate via a `WWW-Authenticate` header
  pointing at `/.well-known/oauth-protected-resource/macro/mcp` — a **domain-root** path.
- The domain root is proxied to garmin-mcp, so the client follows that pointer to the *wrong
  server* and gets a 404.
- Worse, FastMCP registers that metadata route *including* the `/macro` prefix, while
  `--set-path` **strips** the prefix before forwarding — so the route could never match even
  if routing were correct.

Fix it with two mappings that pass those specific paths through **unstripped**, by giving the
target as a full URL including its path:

```bash
tailscale funnel --bg --set-path=/.well-known/oauth-protected-resource/macro/mcp \
  http://127.0.0.1:18081/.well-known/oauth-protected-resource/macro/mcp

tailscale funnel --bg --set-path=/.well-known/oauth-authorization-server/macro \
  http://127.0.0.1:18081/.well-known/oauth-authorization-server
```

The second covers RFC 8414's root-relative discovery form
(`/.well-known/oauth-authorization-server/<path>`), which clients typically try **before** the
path-appended form — without it, discovery 404s even though the appended form works.

`tailscale funnel status` should then show:

```
|-- /                                               proxy http://127.0.0.1:18080
|-- /macro                                          proxy http://127.0.0.1:18081
|-- /.well-known/oauth-authorization-server/macro   proxy http://127.0.0.1:18081/.well-known/oauth-authorization-server
|-- /.well-known/oauth-protected-resource/macro/mcp proxy http://127.0.0.1:18081/.well-known/oauth-protected-resource/macro/mcp
```

Set `MCP_PUBLIC_URL` to the path-inclusive URL (e.g.
`https://<your-machine>.example.ts.net/macro`) and restart — OAuth's issuer and redirect URLs
derive from it. Connect Claude to `<MCP_PUBLIC_URL>/mcp`.

> **Scaling note.** These mappings compensate for a real architectural constraint rather than
> removing it. A third MCP server on this hostname would need its own set. At that point, give
> each server its own Tailscale hostname instead.

### Verifying (test the discovery chain, not just `/health`)

`/health` passing proves almost nothing here — it was returning 200 the entire time the
connector was broken. Walk the chain a client actually walks:

```bash
# 1. unauthenticated call must 401 *and* advertise where to authenticate
curl -sk -i -X POST https://<host>/macro/mcp -H 'Content-Type: application/json' -d '{}' \
  | grep -i www-authenticate

# 2. the URL from that header must return 200 JSON (not 404 from the other server)
curl -sk https://<host>/.well-known/oauth-protected-resource/macro/mcp

# 3. RFC 8414 root-relative discovery must return 200
curl -sk -o /dev/null -w '%{http_code}\n' \
  https://<host>/.well-known/oauth-authorization-server/macro

# 4. confirm the other server is still intact at root
curl -sk https://<host>/health
```

If a connector attempt fails, `docker logs macro-mcp` distinguishes the cases precisely:
`/register` or `/authorize` appearing means discovery worked and it's a genuine auth problem;
a bare `POST /mcp` → 401 with nothing before it means discovery failed or the client is
reusing a cached credential.

### Stale cached credentials

Changing `MCP_PUBLIC_URL` changes the OAuth issuer, invalidating any token Claude already
holds — and Claude will keep retrying the dead token rather than re-authenticating. **Delete
the connector entirely and re-add it**; editing it in place may not clear the cache. Clearing
the server side too (`rm ./data/oauth_state.json`, then `docker compose up -d`) guarantees a
clean slate.

### Other options

Any reverse tunnel or reverse proxy that terminates HTTPS and forwards to the container's
published port works the same way — Cloudflare Tunnel and ngrok are common alternatives. The
only requirement is a stable public HTTPS URL to put in `MCP_PUBLIC_URL`.

## Picking a host port

`compose.yml` defaults to publishing `127.0.0.1:18081:8080` — chosen specifically not to
collide with garmin-mcp's `18080`. If `18081` is already taken by something else, change the
host-side number in `compose.yml` (leave the container-side `8080` alone) and update your
tunnel to match.

## Reaching garmin-mcp from inside the container

`GARMIN_MCP_URL` needs `host.docker.internal`, not `127.0.0.1`, when this server is running
in Docker — see the comment in `.env.example` for why. `compose.yml` already adds the
`extra_hosts` mapping this needs on Linux; Docker Desktop (Windows/Mac) provides it natively.
Getting this wrong doesn't corrupt anything — `/dashboard`'s weight panel just reports the
trend unavailable with a connection-error reason instead of a chart, per `garmin_weight.py`'s
degrade-honestly design — but it's worth getting right so real weight data actually shows up.
(This is the *only* thing macro-mcp reads from garmin-mcp; see SPEC.md's "Progress photos and
dashboard" for why that narrow read exists after the general bridge was removed.)

## Fetching the pose-landmark model (non-Docker runs)

The Docker image fetches this automatically at build time (see `Dockerfile`). Running the
server bare (`python -m macro_mcp.server`, e.g. for local dev) needs the same file at
`./models/pose_landmarker_lite.task`, or `log_body_photo` will store photos unaligned:

```bash
mkdir -p models
curl -fsSL -o models/pose_landmarker_lite.task \
  https://storage.googleapis.com/mediapipe-models/pose_landmarker/pose_landmarker_lite/float16/latest/pose_landmarker_lite.task
```

Not fetching it isn't a failure mode worth avoiding at all costs — `body_photos.py` degrades to
storing photos unaligned with a stated reason rather than refusing to run.

## Surviving a reboot

Four things have to come back independently:

| Piece | How it persists |
|---|---|
| Tailscale service | Installs as an auto-start service. |
| Funnel path mappings | Persisted by `--bg`; survives restarts without re-running. |
| Containers | `restart: unless-stopped` in `compose.yml` brings both back once Docker is up. |
| Docker itself | **The weak link — see below.** |

On Windows, Docker Desktop's `AutoStart` setting fires **at user login, not at boot** (the
underlying `com.docker.service` is typically `Manual`/`Stopped` until then). A machine that
reboots and sits at the lock screen leaves every container down, and the connector simply
fails until someone logs in.

If this host is one you actually log into, that's fine. For genuinely unattended operation
you need either auto-login, or Docker Engine running as a real service rather than Docker
Desktop. Both are deliberate tradeoffs worth choosing rather than discovering after an
unattended reboot.

Check yours:

```powershell
(Get-CimInstance Win32_Service -Filter "Name='com.docker.service'").StartMode
docker inspect -f '{{.HostConfig.RestartPolicy.Name}}' macro-mcp
tailscale funnel status
```

## Companion Skill

Install [macro-coach](https://github.com/frictionlesscode/macro-coach) via Claude's Skills UI
and add this server's MCP connector with the bearer token from your `.env`. Strongly
recommended: the tools return honest `null`s and confidence levels that a model will otherwise
misread.

## Confirming it actually works

Everything above gets you to "reachable and authenticated." The real test is narrower and more
concrete: **photograph a meal through Claude and confirm it lands in the database with correct
macros and a stated confidence.** Once the connector's added (Claude → Settings → Connectors →
Add custom connector → `<MCP_PUBLIC_URL>/mcp`, sign in with your `MCP_BEARER_TOKEN`), try it
and see what comes back.

Separately, confirm the dashboard: visit `<MCP_PUBLIC_URL>/dashboard?token=<DASHBOARD_TOKEN>`
in a browser. It shares the same host/port/Funnel path as `/mcp`, so no extra tunnel
configuration is needed — if `/mcp` is reachable, `/dashboard` is too. A request with no token,
or the wrong one, should 401; with the right token it should render even with zero photos
logged yet (the charts and slideshow just report "no data in this window" honestly).
