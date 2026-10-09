"""Project KiroCrew's durable workflow inventory into Lens's read-only views."""

from __future__ import annotations

import logging
from collections.abc import Callable
from datetime import datetime
from typing import Any

from kiro_crew.workflows.store import WorkflowRunStore

RUN_PREFIX = "kirocrew:"
TERMINAL = {"finished", "completed", "failed", "cancelled", "killed", "error", "aborted"}
logger = logging.getLogger(__name__)


def _stamp(value: str | None) -> float:
    if not value:
        return 0.0
    try:
        return datetime.fromisoformat(value).timestamp()
    except ValueError:
        logger.warning("Native workflow event has an invalid timestamp; event time is unknown")
        return 0.0


def _records() -> list[dict[str, Any]]:
    return WorkflowRunStore().load_all()


def _find(run_id: str) -> dict[str, Any] | None:
    return next((r for r in _records() if RUN_PREFIX + r["run_id"] == run_id), None)


def _project(record: dict[str, Any], render: Callable[[Any], str]) -> dict[str, Any]:
    phases: list[dict[str, str]] = []
    agents: dict[str, dict[str, Any]] = {}
    timestamps: list[float] = []
    duration_ms = None
    summary = str(record.get("error") or "")
    results = record.get("agent_results") or {}
    for event in record.get("events") or []:
        data = event.get("data") or {}
        stamp = _stamp(event.get("ts"))
        timestamps.append(stamp)
        kind = event.get("type")
        if kind == "phase_started":
            title = str(data.get("title") or "")
            if title and not any(p["title"] == title for p in phases):
                phases.append({"title": title, "detail": ""})
        elif kind == "agent_started":
            agent_id = str(data["agent_id"])
            output = results.get(str(data["call_index"]))
            agents[agent_id] = {
                "agent_id": agent_id,
                "label": str(data.get("label") or agent_id),
                "phase": str(data.get("phase") or ""),
                "model": "",
                "last_active_at": stamp,
                "last_step": "",
                "last_tool": "",
                "returned": False,
                "has_output": output is not None,
                "native_active": True,
            }
        elif kind in {"agent_progress", "agent_finished"}:
            agent = agents.get(str(data.get("agent_id")))
            if agent is not None:
                agent["last_active_at"] = stamp
                if kind == "agent_finished":
                    agent["native_active"] = False
                    agent["returned"] = data.get("ok") is True
                    agent["last_step"] = str(data.get("error") or data.get("result_summary") or "")
                else:
                    agent["last_tool"] = str(data.get("last_tool") or "")
        elif kind == "log" and not record.get("error"):
            summary = str(data.get("message") or "")
        elif kind == "run_finished" and data.get("duration_s") is not None:
            duration_ms = float(data["duration_s"]) * 1000
    if record.get("status") in TERMINAL or record.get("status") == "paused":
        for agent in agents.values():
            agent["native_active"] = False
    result = render(record.get("result"))
    context = record.get("execution_context") or {}
    return {
        "run_id": RUN_PREFIX + record["run_id"],
        "source": "kirocrew",
        "name": str(record.get("name") or record["run_id"]),
        "status": {"finished": "completed", "cancelled": "aborted"}.get(record.get("status"), record.get("status")),
        "summary": summary,
        "project": str(context.get("cwd") or ""),
        "session_id": str(record.get("session_key") or ""),
        "phases": phases,
        "agents": list(agents.values()),
        "tokens": None,
        "tool_calls": None,
        "duration_ms": duration_ms,
        "updated_at": max(timestamps, default=0),
        "resumed_after_status": False,
        "ordering": {},
        "has_result": bool(result),
        "result_preview": " ".join(result.split())[:160],
        "result_chars": len(result),
    }


def discover_runs(render: Callable[[Any], str]) -> list[dict[str, Any]]:
    return [_project(record, render) for record in _records()]


def find_result(run_id: str, render: Callable[[Any], str]) -> str | None:
    record = _find(run_id)
    return render(record.get("result")) if record else None


def find_agent_output(run_id: str, agent_id: str, render: Callable[[Any], str]) -> str | None:
    record = _find(run_id)
    if record is None:
        return None
    for event in record.get("events") or []:
        data = event.get("data") or {}
        if event.get("type") == "agent_started" and data.get("agent_id") == agent_id:
            output = (record.get("agent_results") or {}).get(str(data["call_index"]))
            return render(output) if output is not None else None
    return None
