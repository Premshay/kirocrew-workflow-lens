"""DeepSeek Harness Agent Teams, read as Workflow Lens runs.

dsh's experimental Agent Teams turn one session into a Lead that creates named
teammates, messages them and shares a task board with them. When KiroCrew drives
dsh over ACP, none of that reaches the gateway: the ACP surface carries the
Lead's own turn only, so the teammates work out of sight -- the same gap this app
closes for Claude Code workflows.

dsh records all of it in the Lead's session log, which is the source here:
``team/member`` events carry the roster, ``team/task`` the board, and
``team/message/queued`` / ``team/message/delivered`` the mailbox. Each teammate
is a session of its own in the same home, so its last step and its answer come
from its own log. Logs are zstd-compressed JSON lines, written as one frame per
flush; the stdlib decoder (Python 3.14) reads all frames.

Read-only, like the rest of the app. A parse is cached by file size and mtime,
so a fifteen-second poll decodes only the logs that changed since the last one.
"""

from __future__ import annotations

import json
import os
import time
from pathlib import Path
from typing import Any

#: Prefix that keeps a team's run id apart from a Claude Code run id.
RUN_PREFIX = "dsh-"

#: Environment override: dsh homes to read, separated by os.pathsep. Set but
#: empty means none. Unset means every ``~/.local/share/dsh-*`` home whose
#: patch layer turns Agent Teams on -- a home without it cannot hold a team, and
#: skipping it keeps the busy shared seat's logs out of every poll.
HOMES_ENV = "WORKFLOW_LENS_DSH_HOMES"

#: Marker that a home's patch layer mounts the Team packages.
TEAM_PACKAGE = "dsh-experimental-agent-team"

#: A Lead untouched for longer than this is not listed.
MAX_AGE_SECONDS = 14 * 24 * 3600

#: An open turn silent for longer than this belongs to a process that died
#: before it could log the turn's end; the same bound the reader uses for a
#: stale Claude agent.
STALE_AFTER_SECONDS = 900

_PREVIEW_CHARS = 160
_MESSAGES_SHOWN = 30

_cache: dict[str, tuple[tuple[int, int], dict[str, Any]]] = {}


def team_homes() -> list[Path]:
    """The dsh homes to read, per :data:`HOMES_ENV`."""
    raw = os.environ.get(HOMES_ENV)
    if raw is not None:
        return [Path(p).expanduser() for p in raw.split(os.pathsep) if p.strip()]
    homes: list[Path] = []
    for home in sorted((Path.home() / ".local" / "share").glob("dsh-*")):
        try:
            patch = (home / "cordis.patch.yml").read_text(encoding="utf-8")
        except OSError:
            continue
        if TEAM_PACKAGE in patch and (home / "sessions").is_dir():
            homes.append(home)
    return homes


def _events(log: Path) -> list[dict[str, Any]]:
    """Every event in one session log; a torn final frame is skipped."""
    from compression import zstd

    raw = log.read_bytes()
    try:
        text = zstd.decompress(raw).decode("utf-8", errors="replace")
    except zstd.ZstdError:
        # The writer may be mid-frame. Decode frame by frame and keep what is
        # whole, rather than losing the whole log to its last flush.
        magic = b"\x28\xb5\x2f\xfd"
        starts: list[int] = []
        at = raw.find(magic)
        while at != -1:
            starts.append(at)
            at = raw.find(magic, at + 4)
        parts: list[str] = []
        for a, b in zip(starts, starts[1:] + [len(raw)]):
            try:
                parts.append(zstd.decompress(raw[a:b]).decode("utf-8", errors="replace"))
            except zstd.ZstdError:
                continue
        text = "".join(parts)
    events: list[dict[str, Any]] = []
    for line in text.splitlines():
        try:
            event = json.loads(line)
        except ValueError:
            continue
        if isinstance(event, dict):
            events.append(event)
    return events


def _text_of(message: dict[str, Any]) -> str:
    """The visible text of one assistant message, reasoning excluded."""
    parts = []
    for block in message.get("content") or []:
        if isinstance(block, dict) and block.get("type") == "text":
            parts.append(str(block.get("text") or ""))
    return "".join(parts).strip()


def _preview(text: str) -> str:
    line = " ".join(text.split())
    return line if len(line) <= _PREVIEW_CHARS else line[: _PREVIEW_CHARS - 1] + "…"


def _summarize(log: Path) -> dict[str, Any]:
    """What one session log says, reduced to what a run needs."""
    header: dict[str, Any] = {}
    model = ""
    title = ""
    open_turn = False
    last_time = 0.0
    last_text = ""
    last_tool = ""
    answer = ""
    turn_text = ""
    tokens = 0
    tool_calls = 0
    members: dict[str, dict[str, Any]] = {}
    member_order: list[str] = []
    tasks: dict[str, dict[str, Any]] = {}
    messages: dict[str, dict[str, Any]] = {}
    message_order: list[str] = []
    delivered: set[str] = set()
    approvals: set[str] = set()
    for event in _events(log):
        kind = event.get("type")
        data = event.get("data") if isinstance(event.get("data"), dict) else {}
        stamp = event.get("time")
        if isinstance(stamp, (int, float)):
            last_time = max(last_time, stamp / 1000)
        if kind == "session":
            header = event
        elif kind == "subagent/descriptor":
            model = model or str(data.get("agentModel") or "")
        elif kind == "request/header":
            config = (data.get("header") or {}).get("config") or {}
            model = str(config.get("model") or model)
        elif kind == "session/title":
            if (data.get("source") or {}).get("kind") != "fallback":
                title = str(data.get("title") or "")
        elif kind == "turn/start":
            open_turn = True
            turn_text = ""
        elif kind == "turn/end":
            open_turn = False
            approvals.clear()
            if turn_text:
                answer = turn_text
        elif kind == "assistant/message":
            text = _text_of(data.get("message") or {})
            if text:
                last_text = text
                turn_text = text
            usage = data.get("usage") or {}
            if isinstance(usage.get("totalTokens"), int):
                tokens += usage["totalTokens"]
        elif kind == "tool/call":
            tool_calls += 1
            args = " ".join(str(data.get("arguments") or "").split())
            last_tool = f"{data.get('name') or ''} {args[:80]}".strip()
        elif kind == "approval/asked":
            approvals.add(str(data.get("id") or ""))
        elif kind == "approval/decided":
            approvals.discard(str(data.get("id") or ""))
        elif kind == "team/member":
            member = data.get("member") or {}
            mid = str(member.get("id") or "")
            if mid:
                if mid not in members:
                    member_order.append(mid)
                members[mid] = member
        elif kind == "team/task":
            task = data.get("task") or {}
            if task.get("id"):
                tasks[str(task["id"])] = task
        elif kind == "team/message/queued":
            message = data.get("message") or {}
            msg_id = str(message.get("id") or "")
            if msg_id and msg_id not in messages:
                message_order.append(msg_id)
                messages[msg_id] = message
        elif kind == "team/message/delivered":
            delivered.add(str(data.get("messageId") or ""))
    return {
        "id": str(header.get("id") or log.parent.name),
        "cwd": str(header.get("cwd") or ""),
        "root": not header.get("delegationDepth"),
        "model": model,
        "title": title,
        "open_turn": open_turn,
        "last_time": last_time,
        "last_text": last_text,
        "last_tool": last_tool,
        "answer": answer,
        "turn_text": turn_text if open_turn else "",
        "tokens": tokens,
        "tool_calls": tool_calls,
        "pending_approvals": len(approvals),
        "members": [members[m] for m in member_order],
        "tasks": list(tasks.values()),
        "messages": [
            {**messages[m], "delivered": m in delivered} for m in message_order
        ],
    }


def _summary(log: Path) -> dict[str, Any] | None:
    """:func:`_summarize`, cached on the log's size and mtime."""
    try:
        stat = log.stat()
    except OSError:
        return None
    key = (stat.st_size, stat.st_mtime_ns)
    hit = _cache.get(str(log))
    if hit is not None and hit[0] == key:
        return hit[1]
    try:
        summary = _summarize(log)
    except OSError:
        return None
    summary["mtime"] = stat.st_mtime
    _cache[str(log)] = (key, summary)
    return summary


def _logs(home: Path) -> dict[str, Path]:
    """Session id -> log path for every session in *home*."""
    found: dict[str, Path] = {}
    for log in (home / "sessions").glob("*/*/session.v4.jsonl.zstd"):
        found[log.parent.name] = log
    return found


def _agent(name: str, member_id: str, summary: dict[str, Any] | None, phase: str,
           now: float, failed: bool = False, error: str = "") -> dict[str, Any]:
    """One Lens agent row."""
    if summary is None:
        return {
            "agent_id": member_id, "label": name, "phase": phase, "model": "",
            "spawn_depth": None, "shape": "", "last_active_at": None,
            "last_step": error, "last_tool": "", "has_output": False,
            "returned": False, "turn_open": False,
        }
    last = summary["last_time"] or summary["mtime"]
    working = summary["open_turn"] and not failed and now - last < STALE_AFTER_SECONDS
    activity_state = (
        "no_recent_activity" if summary["open_turn"] and not working
        else "waiting_approval" if summary["open_turn"] and summary["pending_approvals"]
        else "recent_activity" if working
        else "no_recent_activity" if summary["open_turn"]
        else "ended"
    )
    prefix = (
        "Awaiting tool approval" if activity_state == "waiting_approval"
        else "Unanswered tool approval; no recent activity" if activity_state == "no_recent_activity" and summary["pending_approvals"]
        else "No recent activity; completion unconfirmed" if activity_state == "no_recent_activity"
        else "Recent activity" if working
        else ""
    )
    step = error or (f"{prefix} · {summary['last_tool']}" if prefix else _preview(summary["last_text"]))
    return {
        "agent_id": member_id,
        "label": name,
        "phase": phase,
        "model": summary["model"],
        "spawn_depth": 0 if phase == "Lead" else 1,
        "shape": "",
        "last_active_at": last,
        "last_step": step,
        "activity_state": activity_state,
        "last_tool": summary["last_tool"] if working else "",
        "has_output": bool(summary["answer"] or summary["last_text"]),
        "returned": not summary["open_turn"] and not failed and bool(summary["answer"]),
        "turn_open": working,
    }


def _run(home: Path, lead: dict[str, Any], logs: dict[str, Path], now: float) -> dict[str, Any]:
    """One team as a Lens run."""
    names = {lead["id"]: "Lead"}
    agents = [_agent("Lead", lead["id"], lead, "Lead", now)]
    tokens = lead["tokens"]
    tool_calls = lead["tool_calls"]
    updated = lead["mtime"]
    for member in lead["members"]:
        mid = str(member.get("id") or "")
        name = str(member.get("name") or mid[:8])
        names[mid] = name
        summary = _summary(logs[mid]) if mid in logs else None
        failed = member.get("phase") == "failed"
        agents.append(
            _agent(name, mid, summary, "Teammates", now, failed, str(member.get("error") or ""))
        )
        if summary is not None:
            tokens += summary["tokens"]
            tool_calls += summary["tool_calls"]
            updated = max(updated, summary["mtime"])
    working = any(a["turn_open"] for a in agents)
    tasks = [
        {
            "id": str(t.get("id") or ""),
            "subject": str(t.get("subject") or ""),
            "status": str(t.get("status") or ""),
            "owner": names.get(str(t.get("ownerId") or ""), ""),
            "blocked_by": [str(b) for b in t.get("blockedBy") or []],
            "write_scopes": [str(s) for s in t.get("writeScopes") or []],
        }
        for t in lead["tasks"]
        if t.get("status") != "deleted"
    ]
    messages = [
        {
            "from": str(m.get("senderName") or names.get(str(m.get("senderId") or ""), "")),
            "to": names.get(str(m.get("targetId") or ""), str(m.get("targetId") or "")[:8]),
            "text": _preview(_text_of({"content": m.get("content") or []})),
            "delivered": bool(m.get("delivered")),
        }
        for m in lead["messages"][-_MESSAGES_SHOWN:]
    ]
    done = sum(1 for t in tasks if t["status"] == "completed")
    teammates = len(agents) - 1
    summary_bits = [f"{teammates} teammate{'s' if teammates != 1 else ''}"]
    if tasks:
        summary_bits.append(f"{done} of {len(tasks)} tasks done")
    if lead["messages"]:
        summary_bits.append(f"{len(lead['messages'])} messages")
    result = lead["answer"] if not agents[0]["turn_open"] else ""
    if any(a.get("activity_state") == "waiting_approval" for a in agents):
        status = "waiting"
    elif working:
        status = "running"
    elif lead["open_turn"] or any(a.get("activity_state") == "no_recent_activity" for a in agents):
        status = "incomplete"
    elif any(t["status"] != "completed" for t in tasks):
        status = "incomplete"
    else:
        status = "completed"
    return {
        "run_id": f"{RUN_PREFIX}{lead['id']}",
        "name": lead["title"] or f"DeepSeek team · {Path(lead['cwd']).name or lead['id'][:8]}",
        "status": status,
        "summary": " · ".join(summary_bits),
        "project": lead["cwd"],
        "session_id": lead["id"],
        "source": "dsh-team",
        "home": str(home),
        "phases": [
            {"title": "Lead", "detail": "Plans the work, creates teammates and merges their results."},
            {"title": "Teammates", "detail": "Named DeepSeek agents in the Lead's process, sharing its working directory."},
        ],
        "agents": agents,
        "tokens": tokens,
        # Each step's totalTokens counts the whole context sent that step, so
        # this sum re-counts the same context every step: name it for what it is.
        "tokens_label": "Context tokens (summed per step)",
        "tool_calls": tool_calls,
        "duration_ms": None,
        "updated_at": updated,
        "resumed_after_status": False,
        "ordering": {"pipelined": [], "barriers": [], "reattempted": [], "rolling": []},
        "has_result": bool(result),
        "result_preview": _preview(result),
        "result_chars": len(result),
        "team": {"tasks": tasks, "messages": messages},
    }


def discover_team_runs(now: float | None = None) -> list[dict[str, Any]]:
    """Every recent Lead with at least one teammate, newest first."""
    now = time.time() if now is None else now
    runs: list[dict[str, Any]] = []
    for home in team_homes():
        logs = _logs(home)
        for sid, log in logs.items():
            try:
                if now - log.stat().st_mtime > MAX_AGE_SECONDS:
                    continue
            except OSError:
                continue
            summary = _summary(log)
            if summary is None or not summary["root"] or not summary["members"]:
                continue
            runs.append(_run(home, summary, logs, now))
    runs.sort(key=lambda r: r["updated_at"], reverse=True)
    return runs


def _find(run_id: str) -> tuple[dict[str, Any], dict[str, Path]] | None:
    if not run_id.startswith(RUN_PREFIX):
        return None
    sid = run_id[len(RUN_PREFIX):]
    for home in team_homes():
        logs = _logs(home)
        if sid in logs:
            summary = _summary(logs[sid])
            if summary is not None and summary["members"]:
                return summary, logs
    return None


def find_team_result(run_id: str) -> str | None:
    """The Lead's answer to its last completed turn, or None for no such team."""
    found = _find(run_id)
    if found is None:
        return None
    return found[0]["answer"]


def find_team_agent_output(run_id: str, agent_id: str) -> str | None:
    """One member's latest answer, the Lead included."""
    found = _find(run_id)
    if found is None:
        return None
    lead, logs = found
    if agent_id != lead["id"] and agent_id not in {str(m.get("id")) for m in lead["members"]}:
        return None
    if agent_id not in logs:
        return None
    summary = _summary(logs[agent_id])
    if summary is None:
        return None
    # A resumed session opens a new turn: its text so far is the output, not the
    # answer the previous turn ended on.
    return summary["turn_text"] or summary["answer"] or summary["last_text"]
