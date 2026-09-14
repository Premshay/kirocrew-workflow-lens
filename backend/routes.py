"""Gateway routes for the Workflow Lens app.

Claude Code writes a dynamic workflow's state next to the session that started
it, and its subagents to a sibling tree. Nothing in Kiro Crew reads either: the
ACP projection that would have surfaced them receives no events at all, verified
against a live three-agent run on 2026-09-11 whose transcript held zero
``async_task_*`` updates. The files are the only record that carries the work, so
this serves them.

Read-only by construction. Every handler answers from disk; there is no write
path, and the app declares no storage.
"""

from __future__ import annotations

import importlib.util
import sys
import time
from pathlib import Path
from typing import Any

from aiohttp import web
from kiro_crew.apps.context import AppContext
from kiro_crew.apps.route_registry import AppRoute


def _reader() -> Any:
    """The reader module that lives beside this file.

    Loaded by path rather than imported by name: an installed app is a COPY
    under the data home, so its backend package is not on ``sys.path`` and the
    module name is not importable. Cached on first use.
    """
    global _READER_MODULE
    if _READER_MODULE is not None:
        return _READER_MODULE
    path = Path(__file__).resolve().parent / "reader.py"
    spec = importlib.util.spec_from_file_location("workflow_lens_reader", path)
    if spec is None or spec.loader is None:  # pragma: no cover - packaging error
        raise RuntimeError(f"workflow-lens reader missing at {path}")
    module = importlib.util.module_from_spec(spec)
    sys.modules.setdefault("workflow_lens_reader", module)
    spec.loader.exec_module(module)
    _READER_MODULE = module
    return module


_READER_MODULE: Any = None


def _require_user(request: web.Request) -> None:
    if request.get("user") is None:
        raise web.HTTPUnauthorized(text="dashboard session required")


def _decorate(run: dict[str, Any], now: float) -> dict[str, Any]:
    """Attach the derived views the page renders but the reader does not store."""
    reader = _reader()
    shape = reader.infer_shape(run)
    agents = []
    for agent in run.get("agents") or []:
        state, idle = reader._agent_state(agent, now)
        agents.append({**agent, "state": state, "idle": idle})
    return {
        **run,
        "agents": agents,
        "live_agents": sum(1 for a in agents if a["state"] == "live"),
        "patterns": shape["patterns"],
        "evidence": shape["evidence"],
        "phase_shape": shape["phases"],
        "mermaid": reader.mermaid(run, now=now),
    }


async def _list_runs(request: web.Request, _ctx: AppContext) -> web.Response:
    _require_user(request)
    reader = _reader()
    try:
        limit = max(1, min(50, int(request.query.get("limit", "20"))))
    except ValueError:
        raise web.HTTPBadRequest(text="limit must be a number") from None
    now = time.time()
    runs = [_decorate(run, now) for run in reader.discover_runs()[:limit]]
    return web.json_response(
        {
            "runs": runs,
            "generated_at": now,
            "stale_after_seconds": reader.STALE_AFTER_SECONDS,
        }
    )


async def _get_run(request: web.Request, _ctx: AppContext) -> web.Response:
    _require_user(request)
    reader = _reader()
    run_id = request.match_info["run_id"]
    now = time.time()
    for run in reader.discover_runs():
        if run["run_id"] == run_id:
            return web.json_response({"run": _decorate(run, now), "generated_at": now})
    raise web.HTTPNotFound(text=f"no run named {run_id}")


async def _get_result(request: web.Request, _ctx: AppContext) -> web.Response:
    """What the workflow returned, rendered as text.

    Its own route because the list payload only ANNOUNCES a result: a poll
    every fifteen seconds must not carry a quarter-megabyte report per run that
    nobody has opened. Served whole when someone does.
    """
    _require_user(request)
    reader = _reader()
    run_id = request.match_info["run_id"]
    text = reader.find_result(run_id)
    if text is None:
        raise web.HTTPNotFound(text=f"no run named {run_id}")
    return web.json_response(
        {
            "run_id": run_id,
            "text": text,
            "chars": len(text),
        }
    )


async def _get_agent_output(request: web.Request, _ctx: AppContext) -> web.Response:
    """One agent's own output, read from its transcript.

    Separate from the run's result because they answer different questions, and
    because a killed run has no result at all: its agents' outputs are then the
    only thing that survives it.
    """
    _require_user(request)
    reader = _reader()
    run_id = request.match_info["run_id"]
    agent_id = request.match_info["agent_id"]
    text = reader.find_agent_output(run_id, agent_id)
    if text is None:
        raise web.HTTPNotFound(text=f"no agent {agent_id} in {run_id}")
    return web.json_response(
        {
            "run_id": run_id,
            "agent_id": agent_id,
            "text": text,
            "chars": len(text),
        }
    )


def register_routes(_: AppContext) -> list[AppRoute]:
    return [
        AppRoute("GET", "/runs", _list_runs),
        AppRoute("GET", "/runs/{run_id}", _get_run),
        AppRoute("GET", "/runs/{run_id}/result", _get_result),
        AppRoute("GET", "/runs/{run_id}/agents/{agent_id}/output", _get_agent_output),
    ]
