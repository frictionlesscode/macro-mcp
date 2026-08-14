# Self-hosted setup notes

Generic guidance for exposing this server publicly so it can be added as a Claude custom
connector, plus a few operational notes worth knowing before you run this long-term.
Everything below uses placeholder values — `<your-machine>`, `example.ts.net`, etc. — swap in
your own. This mirrors [garmin-mcp's own setup doc](../../ClaudeGarminConnect/garmin-mcp/docs/self-hosted-setup.md)
closely, since the two servers use the identical auth mechanism and are meant to run side by
side on the same machine.

## Exposing the server publicly (Tailscale Funnel example)

Claude's connector UI needs a real HTTPS URL to reach your server's `/mcp` endpoint —
`127.0.0.1` or a bare LAN IP won't work. If you already have Tailscale Funnel running for
garmin-mcp, this is the same tailnet node — you're adding a second *Funnel listener port* on
that node, not setting up Funnel from scratch, and **not** the same port garmin-mcp uses.

Funnel maps a local port to a public HTTPS listener with `--https=<port> <local-port>`.
**Despite older Tailscale docs/folklore suggesting Funnel only supports 443/8443/943, an
arbitrary high port worked fine when verified live** (`--https=18082` here) — don't assume
you're restricted to those three without testing; the actual constraint, if any, turned out
to be on the *calling* side (see the port-choice note in step 2), not Funnel itself.

1. Tailscale should already be installed and signed in if garmin-mcp is running. If not,
   `tailscale up` first.
2. Point a Funnel listener port at the port `compose.yml` publishes for this server (`18081`
   by default). **Picking the port number matters more than it looks:**
   - `443` is almost certainly taken by garmin-mcp already.
   - A "standard alternate" like `8443` can silently collide with something unrelated already
     bound to that port on your machine -- verified live during setup: an already-running Java
     process owned 8443 here, and requests to the funneled URL silently hit *that* service
     instead of erroring. Check first: `netstat -ano | findstr :8443` (Windows) /
     `lsof -i :8443` (Linux/macOS) -- don't trust `tailscale funnel status` reporting the
     mapping as configured as proof the port is actually free.
   - **A non-standard high port can be silently unreachable from a real external caller even
     though it works perfectly from this machine.** Also verified live: port `943` passed
     every check run *from this host* (including a full OAuth login through the public URL),
     but Claude's own connector infrastructure failed to reach it at all -- no request for it
     ever appeared in the container's logs, meaning it was blocked before ever reaching this
     machine (most likely an egress/allowlist restriction on the calling side, not a Funnel or
     DNS problem). Switching to a different high port (this server's own local port + 1, e.g.
     `18081` -> `18082`) resolved it. If a given port silently fails only for a *real remote
     caller* and every host-side test looks fine, suspect the port choice, not the tunnel.

   ```bash
   tailscale funnel --bg --https=18082 18081
   ```

3. Confirm both services are live:

   ```bash
   tailscale funnel status
   ```

   You should see two entries — garmin-mcp's (port 443) and this one.

4. Set `MCP_PUBLIC_URL` in `.env` to the full URL Funnel gives you, **including the port**
   (e.g. `https://<your-machine>.example.ts.net:18082`) and restart the container — OAuth's
   issuer/redirect URLs are derived from this value, not from `127.0.0.1`.
5. Verify from a network that isn't the host itself (phone on cellular, a different machine)
   that `<MCP_PUBLIC_URL>/health` returns a real response, and -- the check that actually
   caught the port-943 problem -- **try the real connector flow itself**, not just `/health`.
   A host-side check alone can look completely correct (this host successfully completed a
   full OAuth login against port 943's URL, including reading past a local certificate-trust
   quirk in the process) while a genuine outside caller still can't reach the port at all.

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
Getting this wrong doesn't corrupt anything — `get_expenditure` just reports `tdee: null`
with a connection-error reason instead of a number, per its degrade-honestly design — but
it's worth getting right so real weight data actually flows through.

## Companion Skill

Install [macro-coach](../../macro-coach) via Claude's Skills UI and add this server's MCP
connector with the bearer token from your `.env`. It's the minimal version for now — logging
food well, not target-setting or coaching judgment (see its README for what it does and
doesn't cover yet).

## The actual M5 gate

Everything above gets you to "reachable and authenticated." The milestone's real gate is
narrower and more concrete: **photograph a meal on your phone through Claude and confirm it
lands in the database with correct macros and a stated confidence.** That's a real end-to-end
check only you can run — it needs your phone, your Claude app, and your food. Once the
connector's added (Claude → Settings → Connectors → Add custom connector →
`<MCP_PUBLIC_URL>/mcp`, sign in with your `MCP_BEARER_TOKEN`), try it and see what comes back.
