"""macro-mcp as an MCP client of garmin-mcp: reads trend weight for the /dashboard route.

This is deliberately narrow -- SPEC.md's charter change (2026-08-14) retired the general
garmin-mcp bridge that used to feed the (now-deleted) expenditure engine. This module exists
only because the body-photo dashboard wants a weight line next to the photos and body-fat %,
and weight is a locked non-goal here (garmin-mcp owns it -- see SPEC.md "Locked decisions").
It reads one trend and nothing else; it does not write, and nothing downstream derives a
target or a plan from what it returns.

garmin-mcp's own auth (SPEC.md there: OAuth 2.1 + Dynamic Client Registration) has no static-
bearer shortcut, even for a same-host client -- its `/authorize` endpoint gates on
MCP_BEARER_TOKEN but only via a real OAuth authorization-code exchange, the same flow a human
in Claude's connector UI would complete via a login form. This module does that flow headlessly
(no browser, no human): register a client once, submit MCP_BEARER_TOKEN as the login-form POST
the human flow would send, exchange the resulting code for an access/refresh token pair, cache
it, and refresh it before it expires. Every step here was verified against the real, running
garmin-mcp instance during M4 (register -> 201, authorize -> 302 with a code, code -> token
exchange, refresh_token grant -> new access token, and a live get_body_trend call returning
real data) -- not written from the OAuth spec alone; this module is that same, proven client,
narrowed down to the one call the dashboard needs.

This module never sees a Garmin credential. It holds a garmin-mcp access token, which is a
macro-mcp-to-garmin-mcp secret -- a different, narrower thing than the Garmin login itself.

Degrades honestly: any failure -- garmin-mcp unreachable, login rejected, a tool error --
raises GarminBridgeError with a specific, stated reason. dashboard.py catches it and renders
the weight panel as unavailable with that reason, never a fabricated or estimated number.
"""

from __future__ import annotations

import base64
import hashlib
import json
import os
import secrets
import time
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlparse

import httpx
from fastmcp import Client

#: How much slack to leave before an access token's stated expiry when deciding whether to
#: still use it. Refreshing a little early avoids a request racing an expiry that lands
#: mid-call.
_EXPIRY_SKEW_SECONDS = 30

_REDIRECT_URI = "http://127.0.0.1:0/callback"  # never actually dialed -- see _login()


class GarminBridgeError(RuntimeError):
    """garmin-mcp is unreachable, rejected login, or returned an error. Always carries a
    specific, user-facing reason -- callers surface it verbatim rather than a generic message.
    """


def _mcp_url() -> str:
    return os.environ.get("GARMIN_MCP_URL", "http://127.0.0.1:18080/mcp")


def _base_url() -> str:
    url = _mcp_url()
    return url[:-4] if url.endswith("/mcp") else url


def _bearer_token() -> str:
    token = os.environ.get("GARMIN_MCP_TOKEN", "")
    if not token:
        raise GarminBridgeError(
            "GARMIN_MCP_TOKEN is not set -- macro-mcp has no credential to log into "
            "garmin-mcp with"
        )
    return token


def _token_cache_path() -> Path:
    return Path(os.environ.get("GARMIN_MCP_OAUTH_STATE_PATH", "./data/garmin_mcp_oauth.json"))


@dataclass
class _TokenSet:
    client_id: str
    client_secret: str
    access_token: str
    refresh_token: str | None
    expires_at: float  # epoch seconds

    def is_fresh(self) -> bool:
        return time.time() < (self.expires_at - _EXPIRY_SKEW_SECONDS)


def _load_cached() -> _TokenSet | None:
    path = _token_cache_path()
    if not path.exists():
        return None
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
        return _TokenSet(**raw)
    except (json.JSONDecodeError, OSError, TypeError):
        return None  # corrupt cache just means logging in again, not a crash


def _save_cached(tokens: _TokenSet) -> None:
    path = _token_cache_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(asdict(tokens)), encoding="utf-8")
    tmp.replace(path)  # atomic on both POSIX and Windows


def _wrap_request_error(step: str, exc: Exception) -> GarminBridgeError:
    return GarminBridgeError(f"garmin-mcp bridge: {step} failed ({exc.__class__.__name__}: {exc})")


async def _register_client(http: httpx.AsyncClient) -> tuple[str, str]:
    try:
        resp = await http.post(
            "/register",
            json={
                "client_name": "macro-mcp",
                "redirect_uris": [_REDIRECT_URI],
                "grant_types": ["authorization_code", "refresh_token"],
                "response_types": ["code"],
                "token_endpoint_auth_method": "client_secret_post",
            },
        )
    except httpx.HTTPError as exc:
        raise _wrap_request_error("client registration", exc) from exc
    if resp.status_code != 201:
        raise GarminBridgeError(
            f"garmin-mcp bridge: client registration rejected (HTTP {resp.status_code}): "
            f"{resp.text[:200]}"
        )
    body = resp.json()
    return body["client_id"], body["client_secret"]


async def _login(http: httpx.AsyncClient) -> _TokenSet:
    """The headless equivalent of a human submitting garmin-mcp's login form.

    Note: no `scope` field is sent -- garmin-mcp's OAuth provider registers clients with no
    default scope, and passing an empty string for `scope` at /authorize is rejected as
    "not registered with that scope" (verified live). Omitting the field entirely is what
    the browser-based flow effectively does too, since the login form has no scope input.
    """
    bearer = _bearer_token()
    client_id, client_secret = await _register_client(http)

    verifier = secrets.token_urlsafe(64)[:128]
    challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).rstrip(b"=").decode()
    state = secrets.token_urlsafe(16)

    try:
        resp = await http.post(
            "/authorize",
            data={
                "client_id": client_id,
                "redirect_uri": _REDIRECT_URI,
                "response_type": "code",
                "code_challenge": challenge,
                "code_challenge_method": "S256",
                "state": state,
                "token": bearer,
            },
            follow_redirects=False,
        )
    except httpx.HTTPError as exc:
        raise _wrap_request_error("authorization", exc) from exc

    location = resp.headers.get("location")
    if not location:
        # Most likely cause: GARMIN_MCP_TOKEN doesn't match garmin-mcp's configured
        # MCP_BEARER_TOKEN -- the login form re-renders with an error instead of redirecting.
        raise GarminBridgeError(
            f"garmin-mcp bridge: login rejected (HTTP {resp.status_code}) -- check that "
            f"GARMIN_MCP_TOKEN matches garmin-mcp's MCP_BEARER_TOKEN"
        )

    qs = parse_qs(urlparse(location).query)
    code = qs.get("code", [None])[0]
    returned_state = qs.get("state", [None])[0]
    if not code or returned_state != state:
        raise GarminBridgeError("garmin-mcp bridge: authorization response missing code or state mismatch")

    return await _exchange(http, client_id, client_secret, grant_type="authorization_code",
                           code=code, redirect_uri=_REDIRECT_URI, code_verifier=verifier)


async def _refresh(http: httpx.AsyncClient, tokens: _TokenSet) -> _TokenSet:
    if not tokens.refresh_token:
        raise GarminBridgeError("garmin-mcp bridge: no refresh token cached")
    return await _exchange(
        http, tokens.client_id, tokens.client_secret,
        grant_type="refresh_token", refresh_token=tokens.refresh_token,
    )


async def _exchange(
    http: httpx.AsyncClient, client_id: str, client_secret: str, **grant_fields: str
) -> _TokenSet:
    try:
        resp = await http.post(
            "/token",
            data={"client_id": client_id, "client_secret": client_secret, **grant_fields},
        )
    except httpx.HTTPError as exc:
        raise _wrap_request_error("token exchange", exc) from exc
    if resp.status_code != 200:
        raise GarminBridgeError(
            f"garmin-mcp bridge: token exchange rejected (HTTP {resp.status_code}): "
            f"{resp.text[:200]}"
        )
    body = resp.json()
    return _TokenSet(
        client_id=client_id,
        client_secret=client_secret,
        access_token=body["access_token"],
        refresh_token=body.get("refresh_token"),
        expires_at=time.time() + float(body.get("expires_in", 3600)),
    )


async def _ensure_token(http: httpx.AsyncClient) -> _TokenSet:
    cached = _load_cached()
    if cached and cached.is_fresh():
        return cached

    if cached and cached.refresh_token:
        try:
            tokens = await _refresh(http, cached)
            _save_cached(tokens)
            return tokens
        except GarminBridgeError:
            pass  # refresh token itself may be expired/revoked -- fall through to a fresh login

    tokens = await _login(http)
    _save_cached(tokens)
    return tokens


async def get_weight_points(days: int = 90) -> list[dict[str, Any]]:
    """Trend weight points from garmin-mcp's get_body_trend, as {date, weight_lb} -- verified
    live against the real instance, no reshaping needed. Used only by dashboard.py.
    """
    async with httpx.AsyncClient(base_url=_base_url(), timeout=15.0) as http:
        tokens = await _ensure_token(http)

    try:
        async with Client(_mcp_url(), auth=tokens.access_token) as client:
            result = await client.call_tool("get_body_trend", {"days": days})
    except Exception as exc:  # noqa: BLE001 -- any transport/tool failure degrades honestly
        raise _wrap_request_error("get_body_trend call", exc) from exc

    data = result.structured_content
    if not isinstance(data, dict) or "points" not in data:
        raise GarminBridgeError("garmin-mcp bridge: get_body_trend returned an unexpected shape")
    return data["points"]
