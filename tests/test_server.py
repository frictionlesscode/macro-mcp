"""Tests for server.py's own logic (the health probes), not the tool wiring -- that's
covered end-to-end by scripts/mcp_smoke.py against a real running server.
"""

from __future__ import annotations

import asyncio

import pytest

from macro_mcp import server


def test_garmin_mcp_status_never_raises_on_malformed_url(monkeypatch):
    """Regression test: a malformed GARMIN_MCP_URL (e.g. an out-of-range port number) raised
    OverflowError from well below httpx's own exception hierarchy during M5's Docker
    verification, uncaught, crashing the /health request entirely. A health check must
    degrade to reachable: false, never itself fail.
    """
    monkeypatch.setenv("GARMIN_MCP_URL", "http://127.0.0.1:99999/mcp")  # out of the 0-65535 range
    result = asyncio.run(server._garmin_mcp_status())
    assert result["reachable"] is False
    assert "error" in result


def test_garmin_mcp_status_reports_unreachable_for_a_dead_port(monkeypatch):
    monkeypatch.setenv("GARMIN_MCP_URL", "http://127.0.0.1:1/mcp")
    result = asyncio.run(server._garmin_mcp_status())
    assert result["reachable"] is False


def test_db_status_reports_ok_for_a_working_db(monkeypatch, tmp_path):
    monkeypatch.setenv("SQLITE_PATH", str(tmp_path / "health_check.db"))
    result = server._db_status()
    assert result["ok"] is True


def test_db_status_never_raises_when_path_is_unwritable(monkeypatch, tmp_path):
    # A path with a *file* as one of its parent components can never be created as a
    # directory -- mkdir(parents=True) raises the same way on Windows and POSIX, unlike
    # POSIX-specific paths like /dev/null/... which Windows just treats as a normal
    # (creatable) path instead of a real blocking device file.
    blocker = tmp_path / "blocker"
    blocker.write_text("not a directory")
    monkeypatch.setenv("SQLITE_PATH", str(blocker / "health.db"))
    result = server._db_status()
    assert result["ok"] is False
    assert "error" in result
