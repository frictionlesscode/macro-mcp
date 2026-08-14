"""Unit tests for the pure logic in garmin_client.py, plus one opt-in integration test.

The OAuth handshake itself (register -> authorize -> token exchange -> refresh) was verified
live against the real, running garmin-mcp instance during M4's build -- see SPEC.md's M4
section for what was checked and what it returned. That live verification isn't something a
committed test can safely re-run unattended (it depends on a real garmin-mcp instance and a
real secret token being present in the environment this suite runs in), so what's covered here
is: the token-freshness/caching logic in isolation (fully mockable, no network), and one
`integration`-marked test that exercises the real thing end to end but is skipped unless a
real environment is explicitly configured for it.
"""

from __future__ import annotations

import json
import os
import time

import pytest

from macro_mcp import garmin_client
from macro_mcp.garmin_client import GarminBridgeError, _TokenSet


# --- pure logic: token freshness, URL derivation, caching -----------------------


def test_token_is_fresh_before_expiry():
    tokens = _TokenSet("cid", "secret", "access", "refresh", expires_at=time.time() + 3600)
    assert tokens.is_fresh()


def test_token_is_not_fresh_past_expiry():
    tokens = _TokenSet("cid", "secret", "access", "refresh", expires_at=time.time() - 1)
    assert not tokens.is_fresh()


def test_token_freshness_respects_the_expiry_skew_buffer():
    """A token expiring in 10s should already read as not-fresh (the skew buffer is 30s) --
    otherwise a request could start against a token that expires mid-flight.
    """
    tokens = _TokenSet("cid", "secret", "access", "refresh", expires_at=time.time() + 10)
    assert not tokens.is_fresh()


def test_base_url_strips_trailing_mcp_suffix(monkeypatch):
    monkeypatch.setenv("GARMIN_MCP_URL", "http://127.0.0.1:18080/mcp")
    assert garmin_client._base_url() == "http://127.0.0.1:18080"


def test_base_url_leaves_non_mcp_suffixed_url_alone(monkeypatch):
    monkeypatch.setenv("GARMIN_MCP_URL", "http://127.0.0.1:18080")
    assert garmin_client._base_url() == "http://127.0.0.1:18080"


def test_mcp_url_has_a_sane_default(monkeypatch):
    monkeypatch.delenv("GARMIN_MCP_URL", raising=False)
    assert garmin_client._mcp_url() == "http://127.0.0.1:18080/mcp"


def test_bearer_token_missing_raises_with_actionable_message(monkeypatch):
    monkeypatch.delenv("GARMIN_MCP_TOKEN", raising=False)
    with pytest.raises(GarminBridgeError, match="GARMIN_MCP_TOKEN"):
        garmin_client._bearer_token()


def test_bearer_token_returns_configured_value(monkeypatch):
    monkeypatch.setenv("GARMIN_MCP_TOKEN", "the-secret")
    assert garmin_client._bearer_token() == "the-secret"


# --- token cache round-trip --------------------------------------------------------


def test_cache_round_trips_a_token_set(tmp_path, monkeypatch):
    cache_path = tmp_path / "garmin_oauth.json"
    monkeypatch.setenv("GARMIN_MCP_OAUTH_STATE_PATH", str(cache_path))

    tokens = _TokenSet("cid", "secret", "access-tok", "refresh-tok", expires_at=1234.5)
    garmin_client._save_cached(tokens)

    loaded = garmin_client._load_cached()
    assert loaded == tokens


def test_cache_miss_returns_none(tmp_path, monkeypatch):
    monkeypatch.setenv("GARMIN_MCP_OAUTH_STATE_PATH", str(tmp_path / "does-not-exist.json"))
    assert garmin_client._load_cached() is None


def test_corrupt_cache_is_treated_as_a_miss_not_a_crash(tmp_path, monkeypatch):
    cache_path = tmp_path / "garmin_oauth.json"
    cache_path.write_text("not valid json{{{", encoding="utf-8")
    monkeypatch.setenv("GARMIN_MCP_OAUTH_STATE_PATH", str(cache_path))
    assert garmin_client._load_cached() is None


def test_cache_write_is_atomic_no_tmp_file_left_behind(tmp_path, monkeypatch):
    cache_path = tmp_path / "garmin_oauth.json"
    monkeypatch.setenv("GARMIN_MCP_OAUTH_STATE_PATH", str(cache_path))
    garmin_client._save_cached(_TokenSet("cid", "secret", "a", "r", expires_at=1.0))
    assert cache_path.exists()
    assert not cache_path.with_suffix(".tmp").exists()


# --- error wrapping -----------------------------------------------------------------


def test_wrap_request_error_names_the_step_and_exception_type():
    err = garmin_client._wrap_request_error("token exchange", ConnectionError("boom"))
    assert "token exchange" in str(err)
    assert "ConnectionError" in str(err)
    assert "boom" in str(err)


# --- integration: the real thing, opt-in only ---------------------------------------


def _integration_configured() -> bool:
    return bool(os.environ.get("GARMIN_MCP_TOKEN")) and os.environ.get(
        "MACRO_MCP_RUN_GARMIN_INTEGRATION_TEST"
    ) == "1"


@pytest.mark.integration
@pytest.mark.skipif(
    not _integration_configured(),
    reason="set GARMIN_MCP_TOKEN and MACRO_MCP_RUN_GARMIN_INTEGRATION_TEST=1 to run this "
           "against a real, running garmin-mcp instance",
)
def test_get_weight_points_against_real_garmin_mcp():
    """Read-only: calls garmin-mcp's get_body_trend, nothing more. Verified manually during
    M4's build to return real data (12 points, matching the live account) -- this test exists
    so that verification is repeatable, not a one-time manual check. Uses asyncio.run()
    directly rather than pulling in pytest-asyncio for one opt-in test.
    """
    import asyncio

    points = asyncio.run(garmin_client.get_weight_points(days=30))
    assert isinstance(points, list)
    if points:  # the account may have zero weigh-ins in some future test environment
        assert {"date", "weight_lb"} <= set(points[0].keys())
