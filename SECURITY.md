# Security policy

## Reporting a vulnerability

Please **don't** open a public issue for anything security-sensitive. Use GitHub's private
vulnerability reporting instead — the repository's **Security** tab → **Report a
vulnerability** — which opens a private advisory visible only to you and the maintainer.

For low-risk hardening suggestions, a normal issue is fine.

There is no bounty and no SLA; this is a personal project maintained in spare time. Reports
will still be read and acted on as quickly as is practical.

## What this server holds

Everything sensitive lives in `.env` or the local data directory (`SQLITE_PATH` and its
siblings). Both are git-ignored — no secret or personal data is committed to this repo, and
none should ever be.

| Item | Location | Impact if it leaks |
|---|---|---|
| Nutrition log, weight, body-fat, and target history | the SQLite DB at `SQLITE_PATH` | Personal health data. |
| Progress photos | `PHOTO_DIR` (defaults next to the DB) | Personal images. |
| OAuth client + token state | `OAUTH_STATE_PATH` | A valid refresh token here is equivalent to being logged in to this server. Delete the file to invalidate everything. |
| `MCP_BEARER_TOKEN` | `.env` | Used once, at login, to obtain an OAuth access token. Rotate it and restart. |
| `DASHBOARD_TOKEN` | `.env` | Gates the `/dashboard` page. Leave unset to disable the dashboard entirely. Rotate and restart. |
| `GARMIN_MCP_TOKEN` | `.env` | Bearer for the optional read-only call to a companion `garmin-mcp` instance — scoped to that, not a Garmin credential. |

Recommended: `chmod 600 .env`, lock down the data directory, keep the container published to
`127.0.0.1` only, and let a tunnel or reverse proxy be the public-facing edge.

## Scope

In scope: authentication bypass, credential or token exposure, flaws in the OAuth flow, the
`/dashboard` token gate, or anything that lets a request reach stored data without a valid
access token.

Out of scope: issues that require already having the host's `.env` or data directory, and
the behavior of upstream services this optionally talks to.

## No warranty

This software is provided "as is", without warranty of any kind — see [LICENSE](LICENSE).
