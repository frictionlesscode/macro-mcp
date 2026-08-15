"""Tests for server.py's own logic (the health probe), not the tool wiring -- that's covered
end-to-end by scripts/mcp_smoke.py against a real running server.

The garmin-mcp reachability probe and its tests were removed along with the rest of the
garmin-mcp bridge (SPEC.md "Charter change (2026-08-14)") -- this server no longer talks to
garmin-mcp at all, so there's nothing left to probe.
"""

from __future__ import annotations

from macro_mcp import server


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
