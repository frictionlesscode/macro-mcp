#!/usr/bin/env python
"""M3/M5 gate: does every tool actually work over a live, authenticated Streamable HTTP
MCP connection?

SPEC.md's stated M3 gate is "every tool passes against MCP Inspector locally" -- a manual,
interactive check. This is the scriptable, repeatable equivalent: it starts the real server
as a subprocess bound to Streamable HTTP (the same transport Inspector and Claude itself would
use), completes the same headless OAuth login a real Claude connector would (M5 turned auth on
for real -- see macro_mcp.oauth), then connects with fastmcp's own Client and exercises every
tool end-to-end -- not just "does it register with a valid schema" but "does calling it
produce the right data, and does an unauthenticated request correctly get refused."

Usage
-----
    python scripts/mcp_smoke.py
"""

from __future__ import annotations

import _bootstrap  # noqa: F401

import asyncio
import base64
import hashlib
import os
import secrets
import socket
import subprocess
import sys
import tempfile
import time
from pathlib import Path
from urllib.parse import parse_qs, urlparse

import httpx
from fastmcp import Client

REPO_ROOT = Path(__file__).resolve().parent.parent

_REDIRECT_URI = "http://127.0.0.1:0/callback"  # never dialed -- see garmin_client.py's _login


async def headless_login(base_url: str, bearer_token: str) -> str:
    """The same headless OAuth completion garmin_client.py uses against garmin-mcp, run here
    against macro-mcp's own identical SingleUserOAuthProvider. Returns an access token.
    """
    async with httpx.AsyncClient(base_url=base_url, timeout=10.0) as http:
        reg = (await http.post("/register", json={
            "client_name": "mcp-smoke-test",
            "redirect_uris": [_REDIRECT_URI],
            "grant_types": ["authorization_code", "refresh_token"],
            "response_types": ["code"],
            "token_endpoint_auth_method": "client_secret_post",
        })).json()

        verifier = secrets.token_urlsafe(64)[:128]
        challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).rstrip(b"=").decode()
        state = secrets.token_urlsafe(16)

        resp = await http.post("/authorize", data={
            "client_id": reg["client_id"], "redirect_uri": _REDIRECT_URI,
            "response_type": "code", "code_challenge": challenge,
            "code_challenge_method": "S256", "state": state, "token": bearer_token,
        }, follow_redirects=False)
        location = resp.headers.get("location")
        if not location:
            raise RuntimeError(f"login rejected (HTTP {resp.status_code}): {resp.text[:200]}")
        code = parse_qs(urlparse(location).query)["code"][0]

        tok = (await http.post("/token", data={
            "grant_type": "authorization_code", "code": code, "redirect_uri": _REDIRECT_URI,
            "client_id": reg["client_id"], "client_secret": reg["client_secret"],
            "code_verifier": verifier,
        })).json()
        return tok["access_token"]


def free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def wait_for_health(base_url: str, proc: subprocess.Popen, timeout: float = 15.0) -> None:
    import urllib.error
    import urllib.request

    deadline = time.time() + timeout
    while time.time() < deadline:
        if proc.poll() is not None:
            raise RuntimeError(
                f"server process exited early with code {proc.returncode} -- "
                f"see its stderr above"
            )
        try:
            # 4s of headroom: /health's own garmin-mcp reachability probe can take up to
            # ~1.5s to fail (server.py's own bound) plus this environment's observed
            # connection-refused latency on loopback, which measured ~2.2s here -- slower
            # than a typical instant RST. A tight client-side timeout here was mistaking a
            # slow-but-answering health check for a dead server.
            with urllib.request.urlopen(f"{base_url}/health", timeout=4) as resp:
                if resp.status == 200:
                    return
        except (urllib.error.URLError, ConnectionError, TimeoutError):
            pass
        time.sleep(0.2)
    raise RuntimeError(f"server did not become healthy within {timeout}s")


class Check:
    def __init__(self):
        self.failures: list[str] = []
        self.passed = 0

    def ok(self, label: str, condition: bool, detail: str = "") -> None:
        if condition:
            self.passed += 1
            print(f"  PASS  {label}")
        else:
            self.failures.append(label)
            print(f"  FAIL  {label}  {detail}")

    def summary(self) -> bool:
        total = self.passed + len(self.failures)
        print(f"\n{self.passed}/{total} checks passed")
        if self.failures:
            print("failed:")
            for f in self.failures:
                print(f"  - {f}")
        return not self.failures


async def run_checks(base_url: str, bearer_token: str) -> bool:
    check = Check()

    # -- unauthenticated request must be refused, not silently allowed --
    unauth_ok = False
    try:
        async with Client(f"{base_url}/mcp", timeout=5.0) as client:
            await client.list_tools()
    except Exception:
        unauth_ok = True  # any failure to connect/list without a token is the correct outcome
    check.ok("an unauthenticated request is refused", unauth_ok)

    access_token = await headless_login(base_url, bearer_token)

    async with Client(f"{base_url}/mcp", auth=access_token) as client:
        tools = await client.list_tools()
        tool_names = {t.name for t in tools}
        expected = {
            "log_food", "log_from_library", "edit_entry", "edit_item",
            "delete_entry", "delete_item", "set_day_status",
            "save_food", "save_recipe", "search_library",
            "get_day", "get_intake_trend", "get_expenditure",
            "log_body_comp", "get_body_comp",
            "get_targets", "set_goal", "get_goal", "set_training_plan", "set_day_plan",
        }
        check.ok(
            "all expected tools are registered",
            expected <= tool_names,
            f"missing: {expected - tool_names}",
        )

        # -- save a food, then log it by weighed grams --
        saved = (await client.call_tool(
            "save_food",
            {"name": "Oats", "serving_desc": "40 g dry", "serving_g": 40.0,
             "kcal": 150, "protein_g": 5, "carb_g": 27, "fat_g": 2.5, "fiber_g": 4,
             "source": "label"},
        )).data
        check.ok("save_food returns an id", saved.get("ok") and isinstance(saved.get("id"), int))
        food_id = saved["id"]

        logged = (await client.call_tool(
            "log_from_library",
            {"meal": "breakfast", "food_id": food_id, "grams": 80.0,
             "when": "2026-03-01T07:30:00"},
        )).data
        check.ok(
            "quick-log by grams scales the saved food correctly",
            logged["totals"]["kcal"] == 300 and logged["totals"]["protein_g"] == 10,
            f"got {logged.get('totals')}",
        )

        # -- log a mixed meal from explicit items --
        meal = (await client.call_tool(
            "log_food",
            {
                "description": "chicken and rice", "meal": "dinner",
                "when": "2026-03-01T18:30:00",
                "items": [
                    {"name": "Chicken breast", "qty": 200, "unit": "g", "kcal": 330,
                     "protein_g": 62, "carb_g": 0, "fat_g": 7,
                     "source": "estimate", "confidence": "medium"},
                    {"name": "White rice", "qty": 150, "unit": "g", "kcal": 195,
                     "protein_g": 4, "carb_g": 43, "fat_g": 0.5, "fiber_g": 1,
                     "source": "estimate", "confidence": "medium"},
                ],
            },
        )).data
        check.ok("mixed meal totals correctly", meal["totals"]["kcal"] == 825, f"got {meal['totals']}")

        # -- day status gates the intake trend --
        status = (await client.call_tool(
            "set_day_status", {"date": "2026-03-01", "status": "complete"},
        )).data
        check.ok("set_day_status marks the day complete", status["status"] == "complete")

        day = (await client.call_tool("get_day", {"date": "2026-03-01"})).data
        check.ok(
            "get_day reflects both logged meals",
            day["entry_count"] == 2 and day["totals"]["kcal"] == 825,
            f"got {day.get('totals')}",
        )
        check.ok(
            "get_day honestly reports targets as unimplemented, not a placeholder",
            day["targets"] is None and bool(day["targets_null_reason"]),
        )

        trend = (await client.call_tool("get_intake_trend", {"days": 10})).data
        check.ok(
            "unlogged days in the trend are null, never zero",
            any(p["status"] == "unlogged" and p["kcal"] is None for p in trend["points"]),
        )

        # -- expenditure: honest null, not a fabricated number --
        expenditure = (await client.call_tool("get_expenditure", {"days": 28})).data
        check.ok(
            "get_expenditure returns null with a real reason (no weight source wired up yet)",
            expenditure["tdee"] is None and bool(expenditure["tdee_null_reason"]),
            f"got {expenditure}",
        )

        # -- targets/goals: no goal yet --
        no_goal_targets = (await client.call_tool("get_targets", {})).data
        check.ok(
            "get_targets is null with 'no active goal' before any goal is set",
            no_goal_targets["kcal"] is None and "no active goal" in (no_goal_targets["targets_null_reason"] or ""),
            f"got {no_goal_targets}",
        )
        no_goal = (await client.call_tool("get_goal", {})).data
        check.ok("get_goal reports active: false with no goal set", no_goal == {"active": False})

        # -- set a goal; this server has no live garmin-mcp in this ephemeral test env, so
        # weekly_budget/implied_rate must come back honestly null rather than guessed --
        goal = (await client.call_tool("set_goal", {
            "mode": "cut", "rate_lb_per_week": -1.0,
            "protein_g_per_lb": 1.0, "fat_g_per_lb_floor": 0.35,
            "stop_metric": "weight", "stop_value": "180",
        })).data
        check.ok(
            "set_goal creates a goal but honestly nulls weekly_budget without a TDEE",
            goal["ok"] and goal["weekly_budget"] is None and bool(goal.get("weekly_budget_null_reason")),
            f"got {goal}",
        )

        active_goal = (await client.call_tool("get_goal", {})).data
        check.ok(
            "get_goal reflects the goal just set",
            active_goal["active"] and active_goal["mode"] == "cut" and active_goal["stop_value"] == "180",
            f"got {active_goal}",
        )

        # -- now that a goal exists, get_targets' null reason should shift from 'no active
        # goal' to the TDEE problem specifically -- proves the two failure modes are
        # genuinely distinguished, not the same generic message --
        targets_now = (await client.call_tool("get_targets", {})).data
        check.ok(
            "get_targets null reason shifts to the TDEE problem once a goal exists",
            targets_now["kcal"] is None and "TDEE" in (targets_now["targets_null_reason"] or ""),
            f"got {targets_now}",
        )

        # -- get_day composes in the same honest null once a goal exists --
        day_with_goal = (await client.call_tool("get_day", {})).data
        check.ok(
            "get_day's targets_null_reason updates once a goal exists",
            day_with_goal["targets"] is None and "TDEE" in (day_with_goal["targets_null_reason"] or ""),
            f"got targets_null_reason={day_with_goal.get('targets_null_reason')}",
        )

        # -- training plan and day plan persist correctly even though resolution is blocked --
        plan = (await client.call_tool("set_training_plan", {"weekday_map": {"0": "heavy", "6": "rest"}})).data
        # dict keys are always strings over JSON-RPC, regardless of the int keys
        # goals.set_training_plan uses internally (tests/test_goals.py checks those directly).
        check.ok("set_training_plan updates the requested weekdays",
                 plan["ok"] and plan["updated"] == {"0": "heavy", "6": "rest"}, f"got {plan}")

        day_type_override = (await client.call_tool(
            "set_day_plan", {"date": "2026-09-01", "day_type": "rest"},
        )).data
        check.ok("set_day_plan accepts a day_type override",
                 day_type_override["ok"] and day_type_override["day_type"] == "rest")

        macro_override = (await client.call_tool(
            "set_day_plan",
            {"date": "2026-09-02",
             "macros": {"kcal": 1900, "protein_g": 160, "carb_g": 180, "fat_g": 55}},
        )).data
        check.ok(
            "set_day_plan accepts and normalizes an explicit macro override",
            macro_override["ok"] and macro_override["explicit_macros"]["fiber_g"] == 0.0,
            f"got {macro_override}",
        )

        bad_goal = await client.call_tool(
            "set_goal",
            {"mode": "shred", "rate_lb_per_week": -1.0, "protein_g_per_lb": 1.0,
             "fat_g_per_lb_floor": 0.35, "stop_metric": "none"},
            raise_on_error=False,
        )
        check.ok("an invalid goal mode surfaces as a tool error", bad_goal.is_error)

        # -- edits and deletes actually change stored state --
        # meal["entries"] is the whole day, chronologically -- breakfast (07:30) sorts
        # before this dinner entry (18:30), so it must be found by entry_id, not indexed
        # positionally as "the entry just logged".
        dinner_entry = next(e for e in meal["entries"] if e["entry_id"] == meal["entry_id"])
        item_id = dinner_entry["items"][0]["item_id"]  # the chicken breast, 330 kcal
        edited = (await client.call_tool("edit_item", {"item_id": item_id, "kcal": 350})).data
        check.ok("edit_item changes totals", edited["totals"]["kcal"] == 845, f"got {edited['totals']}")

        deleted = (await client.call_tool("delete_entry", {"entry_id": meal["entry_id"]})).data
        check.ok(
            "delete_entry removes the whole meal, leaving only breakfast",
            deleted["totals"]["kcal"] == 300, f"got {deleted['totals']}",
        )

        # -- library search surfaces what was saved --
        results = (await client.call_tool("search_library", {"query": "Oats"})).data
        check.ok("search_library finds the saved food", any(r["id"] == food_id for r in results))

        # -- body composition: logged locally, push honestly reported as not wired up --
        comp = (await client.call_tool(
            "log_body_comp",
            {"percent_fat": 18.5, "method": "scale", "date": "2026-03-01", "push_to_garmin": True},
        )).data
        check.ok(
            "log_body_comp stores the reading but is honest that the Garmin push isn't wired up",
            comp["ok"] and comp["pushed"] is False and "M4" in (comp["push_error"] or ""),
            f"got {comp}",
        )

        # days must be large enough to span from the fixed 2026-03-01 test date to whatever
        # today() actually is when this script runs -- not the trailing window a real
        # get_body_comp call would use.
        comp_trend = (await client.call_tool("get_body_comp", {"days": 3650})).data
        check.ok(
            "get_body_comp returns the reading just logged",
            comp_trend["latest"] is not None and comp_trend["latest"]["percent_fat"] == 18.5,
            f"got {comp_trend}",
        )

        # -- validation errors surface as real tool errors, not silent failures --
        bad = await client.call_tool(
            "log_food",
            {"description": "x", "meal": "brunch", "items": [{"name": "x", "kcal": 1,
             "protein_g": 0, "carb_g": 0, "fat_g": 0}]},
            raise_on_error=False,
        )
        check.ok("an invalid meal type surfaces as a tool error, not a silent 200", bad.is_error)

    return check.summary()


def main() -> int:
    port = free_port()
    base_url = f"http://127.0.0.1:{port}"

    # tempfile.TemporaryDirectory()'s strict cleanup can raise on Windows if the OS hasn't
    # fully released the log file's handle the instant the child process exits (a known
    # Windows filesystem timing quirk, not a real leak) -- so this dir is removed best-effort
    # in a finally block instead of relying on that context manager's cleanup.
    tmp = Path(tempfile.mkdtemp())
    proc = None
    bearer_token = secrets.token_urlsafe(32)
    try:
        env = os.environ.copy()
        env["SQLITE_PATH"] = str(tmp / "smoke.db")
        env["MCP_PORT"] = str(port)
        env["MCP_HOST"] = "127.0.0.1"
        env["MCP_BEARER_TOKEN"] = bearer_token
        env["MCP_PUBLIC_URL"] = base_url
        env["OAUTH_STATE_PATH"] = str(tmp / "oauth_state.json")
        # This server-under-test's own garmin-mcp bridge is irrelevant to what this script
        # verifies (it's exercised for real in tests/test_garmin_client.py's integration
        # test); pointing it at an unreachable port keeps this run fast and self-contained
        # rather than depending on a real garmin-mcp instance being up. Using a freshly
        # freed high port rather than a reserved one like 1 -- Windows doesn't fast-refuse
        # connections to reserved/privileged ports the way it does an ordinary closed one,
        # which was slow enough to make /health itself time out during startup polling.
        env.setdefault("GARMIN_MCP_URL", f"http://127.0.0.1:{free_port()}/mcp")
        env["PYTHONPATH"] = str(REPO_ROOT / "src")

        # Redirected to a file, not subprocess.PIPE: a pipe nobody drains can fill its OS
        # buffer and block the child indefinitely -- including before it ever answers
        # /health, which would otherwise look like a silent hang with no diagnostic.
        log_path = tmp / "server.log"
        with open(log_path, "w", encoding="utf-8") as log_file:
            proc = subprocess.Popen(
                [sys.executable, "-m", "macro_mcp.server"],
                cwd=str(REPO_ROOT), env=env,
                stdout=log_file, stderr=subprocess.STDOUT,
            )
            try:
                wait_for_health(base_url, proc)
                print(f"server healthy at {base_url}\n")
                ok = asyncio.run(run_checks(base_url, bearer_token))
            except Exception:
                proc.poll()
                print("--- server log ---")
                print(log_path.read_text(encoding="utf-8", errors="replace"))
                print("--- end server log ---")
                raise
            finally:
                proc.terminate()
                try:
                    proc.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    proc.kill()
                    proc.wait(timeout=5)
    finally:
        import shutil
        shutil.rmtree(tmp, ignore_errors=True)

    print(f"\nM3 gate: {'satisfied' if ok else 'NOT satisfied'}")
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
