"""Tests for reading DeepSeek Harness Agent Teams as runs."""

from __future__ import annotations

import importlib.util
import json
import time
from compression import zstd
from pathlib import Path

import pytest

_BACKEND = Path(__file__).resolve().parents[1] / "backend"

LEAD = "11111111-1111-1111-1111-111111111111"
ALPHA = "22222222-2222-2222-2222-222222222222"
BETA = "33333333-3333-3333-3333-333333333333"


@pytest.fixture()
def teams():
    spec = importlib.util.spec_from_file_location("dsh_teams_under_test", _BACKEND / "dsh_teams.py")
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _write(home: Path, sid: str, events: list[dict], *, frames: int = 1) -> Path:
    """A session log the way dsh writes it: JSON lines in zstd frames."""
    log = home / "sessions" / "--work--" / sid / "session.v4.jsonl.zstd"
    log.parent.mkdir(parents=True, exist_ok=True)
    lines = [json.dumps(e) + "\n" for e in events]
    size = max(1, len(lines) // frames)
    chunks = [lines[i : i + size] for i in range(0, len(lines), size)]
    log.write_bytes(b"".join(zstd.compress("".join(c).encode()) for c in chunks))
    return log


def _t(seconds_ago: float = 0) -> int:
    return int((time.time() - seconds_ago) * 1000)


def _answer(text: str, ago: float = 0) -> list[dict]:
    return [
        {"type": "turn/start", "time": _t(ago + 1), "data": {"turn": 1}},
        {
            "type": "assistant/message",
            "time": _t(ago),
            "data": {
                "message": {"content": [{"type": "reasoning", "text": "x"}, {"type": "text", "text": text}]},
                "usage": {"totalTokens": 100},
            },
        },
        {"type": "turn/end", "time": _t(ago), "data": {"turn": 1}},
    ]


def _member(mid: str, name: str, phase: str = "active") -> dict:
    return {
        "type": "team/member",
        "time": _t(5),
        "data": {"teamId": LEAD, "member": {"id": mid, "name": name, "description": name, "phase": phase}},
    }


def _seed(home: Path, *, lead_open: bool = False) -> None:
    lead = [
        {"type": "session", "id": LEAD, "cwd": "/work/repo", "delegationDepth": 0},
        {"type": "turn/start", "time": _t(10), "data": {"turn": 1}},
        {"type": "tool/call", "time": _t(9), "data": {"name": "spawn_teammate", "arguments": "{}"}},
        _member(ALPHA, "alpha"),
        _member(BETA, "beta"),
        {
            "type": "team/task",
            "time": _t(8),
            "data": {"teamId": LEAD, "task": {"id": "1", "revision": 1, "subject": "Read A", "status": "in_progress", "ownerId": ALPHA, "blockedBy": [], "writeScopes": ["a.py"]}},
        },
        {
            "type": "team/task",
            "time": _t(7),
            "data": {"teamId": LEAD, "task": {"id": "1", "revision": 2, "subject": "Read A", "status": "completed", "ownerId": ALPHA, "blockedBy": [], "writeScopes": ["a.py"]}},
        },
        {
            "type": "team/task",
            "time": _t(7),
            "data": {"teamId": LEAD, "task": {"id": "2", "revision": 3, "subject": "Gone", "status": "deleted", "blockedBy": [], "writeScopes": []}},
        },
        {
            "type": "team/message/queued",
            "time": _t(6),
            "data": {"teamId": LEAD, "message": {"id": "m1", "senderId": ALPHA, "senderName": "alpha", "targetId": BETA, "content": [{"type": "text", "text": "check b.py"}]}},
        },
        {"type": "team/message/delivered", "time": _t(6), "data": {"teamId": LEAD, "messageId": "m1"}},
        {
            "type": "assistant/message",
            "time": _t(4),
            "data": {"message": {"content": [{"type": "text", "text": "Both done: ALPHA-OK BETA-OK"}]}, "usage": {"totalTokens": 50}},
        },
    ]
    if not lead_open:
        lead.append({"type": "turn/end", "time": _t(4), "data": {"turn": 1}})
    _write(home, LEAD, lead, frames=3)
    _write(
        home,
        ALPHA,
        [{"type": "session", "id": ALPHA, "delegationDepth": 1},
         {"type": "subagent/descriptor", "data": {"agentModel": "deepseek-flash"}}]
        + _answer("ALPHA-OK", 5),
    )
    _write(
        home,
        BETA,
        [{"type": "session", "id": BETA, "delegationDepth": 1},
         {"type": "subagent/descriptor", "data": {"agentModel": "deepseek-flash"}}]
        + _answer("BETA-OK", 5),
    )


def test_a_lead_with_teammates_reads_as_one_run(teams, tmp_path, monkeypatch) -> None:
    _seed(tmp_path)
    monkeypatch.setenv(teams.HOMES_ENV, str(tmp_path))

    runs = teams.discover_team_runs()

    # One run: the teammates' own sessions are members, not runs of their own.
    assert len(runs) == 1
    run = runs[0]
    assert run["run_id"] == f"dsh-{LEAD}"
    assert run["status"] == "completed"
    # The sum re-counts each step's context, so it is not labelled plain "Tokens".
    assert run["tokens_label"] == "Context tokens (summed per step)"
    assert [(a["label"], a["phase"]) for a in run["agents"]] == [
        ("Lead", "Lead"), ("alpha", "Teammates"), ("beta", "Teammates"),
    ]
    assert all(a["returned"] for a in run["agents"])
    assert run["agents"][1]["model"] == "deepseek-flash"
    # Every member's usage is counted, not only the Lead's.
    assert run["tokens"] == 250
    assert run["has_result"] and run["result_preview"] == "Both done: ALPHA-OK BETA-OK"


def test_the_board_keeps_each_task_latest_revision_and_drops_deleted(teams, tmp_path, monkeypatch) -> None:
    _seed(tmp_path)
    monkeypatch.setenv(teams.HOMES_ENV, str(tmp_path))

    team = teams.discover_team_runs()[0]["team"]

    assert team["tasks"] == [
        {"id": "1", "subject": "Read A", "status": "completed", "owner": "alpha", "blocked_by": [], "write_scopes": ["a.py"]}
    ]
    assert team["messages"] == [
        {"from": "alpha", "to": "beta", "text": "check b.py", "delivered": True}
    ]


def test_silent_open_turn_is_incomplete_not_proven_stopped(teams, tmp_path, monkeypatch) -> None:
    _seed(tmp_path, lead_open=True)
    monkeypatch.setenv(teams.HOMES_ENV, str(tmp_path))

    assert teams.discover_team_runs()[0]["status"] == "running"
    later = time.time() + teams.STALE_AFTER_SECONDS + 60
    run = teams.discover_team_runs(now=later)[0]
    assert run["status"] == "incomplete"
    assert not run["agents"][0]["turn_open"]


def test_outputs_are_served_per_member_and_refuse_strangers(teams, tmp_path, monkeypatch) -> None:
    _seed(tmp_path)
    monkeypatch.setenv(teams.HOMES_ENV, str(tmp_path))
    run_id = f"dsh-{LEAD}"

    assert teams.find_team_result(run_id) == "Both done: ALPHA-OK BETA-OK"
    assert teams.find_team_agent_output(run_id, ALPHA) == "ALPHA-OK"
    assert teams.find_team_agent_output(run_id, "../etc") is None
    assert teams.find_team_result("dsh-unknown") is None


def test_a_resumed_turn_shows_its_own_output_not_the_previous_answer(teams, tmp_path, monkeypatch) -> None:
    _seed(tmp_path)
    resumed = [
        {"type": "turn/start", "time": _t(3), "data": {"turn": 2}},
        {
            "type": "assistant/message",
            "time": _t(2),
            "data": {"message": {"content": [{"type": "text", "text": "Resumed: running the suite"}]}},
        },
    ]
    log = next(tmp_path.glob(f"sessions/*/{LEAD}/session.v4.jsonl.zstd"))
    from compression import zstd

    log.write_bytes(log.read_bytes() + zstd.compress(("\n".join(json.dumps(e) for e in resumed) + "\n").encode()))
    monkeypatch.setenv(teams.HOMES_ENV, str(tmp_path))
    run_id = f"dsh-{LEAD}"

    assert teams.find_team_agent_output(run_id, LEAD) == "Resumed: running the suite"
    # The run's result is still the last completed turn's answer.
    assert teams.find_team_result(run_id) == "Both done: ALPHA-OK BETA-OK"
    assert teams.find_team_result("wf_claude_run") is None


def test_a_session_without_teammates_is_not_a_team(teams, tmp_path, monkeypatch) -> None:
    _write(tmp_path, LEAD, [{"type": "session", "id": LEAD, "delegationDepth": 0}] + _answer("hi"))
    monkeypatch.setenv(teams.HOMES_ENV, str(tmp_path))

    assert teams.discover_team_runs() == []


def test_unanswered_approval_is_waiting_and_not_returned(teams, tmp_path):
    events = [
        {"type": "session", "id": LEAD},
        {"type": "turn/start", "time": _t(), "data": {}},
        {"type": "tool/call", "time": _t(), "data": {"name": "bash"}},
        {"type": "approval/asked", "time": _t(), "data": {"id": "ask-1"}},
    ]
    summary = teams._summary(_write(tmp_path, LEAD, events))
    agent = teams._agent("Lead", LEAD, summary, "Lead", time.time())
    assert agent["activity_state"] == "waiting_approval"
    assert agent["last_step"].startswith("Awaiting tool approval")
    assert not agent["returned"]
    later = time.time() + teams.STALE_AFTER_SECONDS + 60
    stale = teams._agent("Lead", LEAD, summary, "Lead", later)
    assert stale["activity_state"] == "no_recent_activity"
    assert stale["last_step"].startswith("Unanswered tool approval; no recent activity")
    assert not stale["returned"]


def test_answered_approval_does_not_remain_waiting(teams, tmp_path):
    events = [
        {"type": "session", "id": LEAD},
        {"type": "turn/start", "time": _t(), "data": {}},
        {"type": "approval/asked", "time": _t(), "data": {"id": "ask-1"}},
        {"type": "approval/decided", "time": _t(), "data": {"id": "ask-1"}},
    ]
    summary = teams._summary(_write(tmp_path, LEAD, events))
    agent = teams._agent("Lead", LEAD, summary, "Lead", time.time())
    assert agent["activity_state"] == "recent_activity"
    stale = teams._agent("Lead", LEAD, summary, "Lead", time.time() + teams.STALE_AFTER_SECONDS + 60)
    assert stale["activity_state"] == "no_recent_activity"
    assert not stale["returned"]


def test_default_homes_are_only_those_that_mount_teams(teams, tmp_path, monkeypatch) -> None:
    share = tmp_path / ".local" / "share"
    for name, patch in (("dsh-team", "name: '@deepseek-ai/dsh-experimental-agent-team'"), ("dsh-plain", "[]")):
        (share / name / "sessions").mkdir(parents=True)
        (share / name / "cordis.patch.yml").write_text(patch)
    monkeypatch.delenv(teams.HOMES_ENV)
    monkeypatch.setattr(teams.Path, "home", staticmethod(lambda: tmp_path))

    assert [h.name for h in teams.team_homes()] == ["dsh-team"]


@pytest.mark.asyncio
async def test_the_list_route_merges_teams_with_workflow_runs(tmp_path, monkeypatch) -> None:
    import sys

    sys.path.insert(0, str(_BACKEND.parent))
    spec = importlib.util.spec_from_file_location("workflow_lens_routes_teams", _BACKEND / "routes.py")
    assert spec and spec.loader
    routes = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(routes)
    home = tmp_path / "dsh"
    _seed(home)
    monkeypatch.setenv("WORKFLOW_LENS_DSH_HOMES", str(home))
    monkeypatch.setattr(routes._reader(), "DEFAULT_PROJECTS_ROOT", tmp_path / "claude")

    class Req(dict):
        query: dict = {}
        match_info: dict = {}

    req = Req(user="operator")
    body = json.loads((await routes._list_runs(req, None)).body)

    assert [r["run_id"] for r in body["runs"]] == [f"dsh-{LEAD}"]
    run = body["runs"][0]
    # Finished teammates read as finished even though they wrote seconds ago.
    assert run["live_agents"] == 0
    assert {a["state"] for a in run["agents"]} == {"stale"}

    req.match_info = {"run_id": f"dsh-{LEAD}", "agent_id": BETA}
    out = json.loads((await routes._get_agent_output(req, None)).body)
    assert out["text"] == "BETA-OK"
