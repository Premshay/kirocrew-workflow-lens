"""Project KiroCrew's authorized live workflow view into Lens cards."""

from __future__ import annotations

import json
import logging
from collections.abc import Callable
from datetime import datetime
from typing import Any

from aiohttp import web
from kiro_crew.dashboard.handlers import workflows as workflow_api

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


async def live_records(request: web.Request) -> list[dict[str, Any]]:
    response = await workflow_api.api_workflow_runs(request)
    if response.status != 200:
        if response.status == 403:
            raise web.HTTPForbidden(text="Native workflow access refused")
        raise web.HTTPServiceUnavailable(text="Native workflows unavailable; check gateway workflow service")
    inventory = json.loads(response.body)["runs"]
    service = request.app["state"].workflow_service
    records = []
    for run in inventory:
        snapshot = service.result(run["run_id"])
        if snapshot is None or snapshot.get("memory_mode", "persistent") != "persistent":
            continue
        record = {
            key: snapshot.get(key)
            for key in (
                "run_id", "name", "status", "error", "events", "result",
                "agent_results", "partial_results", "execution_context", "session_key",
            )
        }
        record["agent_results"] = record["agent_results"] or record.pop("partial_results") or {}
        records.append(record)
    redacted = await workflow_api._json_response_off_loop(records)
    return json.loads(redacted.body)


def _project(record: dict[str, Any], render: Callable[[Any], str]) -> dict[str, Any]:
    phases: list[dict[str, Any]] = []
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
                phases.append({"title": title, "detail": "", "entered": True})
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
    if phases and record.get("status") == "running":
        summary = f"Current phase: {phases[-1]['title']}." + (f" Last log: {summary}" if summary else "")
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


def discover_runs(render: Callable[[Any], str], records: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [_project(record, render) for record in records]


def find_result(run_id: str, render: Callable[[Any], str], records: list[dict[str, Any]]) -> str | None:
    record = next((r for r in records if RUN_PREFIX + r["run_id"] == run_id), None)
    return render(record.get("result")) if record else None


def find_agent_output(run_id: str, agent_id: str, render: Callable[[Any], str], records: list[dict[str, Any]]) -> str | None:
    record = next((r for r in records if RUN_PREFIX + r["run_id"] == run_id), None)
    if record is None:
        return None
    for event in record.get("events") or []:
        data = event.get("data") or {}
        if event.get("type") == "agent_started" and data.get("agent_id") == agent_id:
            output = (record.get("agent_results") or {}).get(str(data["call_index"]))
            return render(output) if output is not None else None
    return None
