"""Route-level tests for the Workflow Lens app."""

from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path

import pytest
from aiohttp import web

_BACKEND = Path(__file__).resolve().parents[1] / "backend"


@pytest.fixture()
def routes_module():
    sys.path.insert(0, str(_BACKEND.parent))
    spec = importlib.util.spec_from_file_location(
        "workflow_lens_routes", _BACKEND / "routes.py"
    )
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class FakeRequest(dict):
    def __init__(self, *, user=True, query=None, run_id=""):
        super().__init__()
        if user:
            self["user"] = "operator"
        self.query = query or {}
        self.match_info = {"run_id": run_id}


def _seed(tmp_path: Path) -> None:
    workflows = tmp_path / "-home-user-repo" / "sess-1" / "workflows"
    workflows.mkdir(parents=True)
    (workflows / "wf_one.json").write_text(
        json.dumps(
            {
                "runId": "wf_one",
                "workflowName": "demo",
                "status": "completed",
                "phases": [{"title": "Read", "detail": ""}],
                "workflowProgress": [
                    {"type": "workflow_agent", "index": i, "label": f"read:{i}"}
                    for i in range(3)
                ],
            }
        ),
        encoding="utf-8",
    )
    # Sidecars, because phase attribution comes from them: an agent the run has
    # announced but not yet spawned has no phase, and a pattern cannot be read
    # off a run whose agents belong to no phase.
    sidecars = workflows.parent / "subagents" / "workflows" / "wf_one"
    sidecars.mkdir(parents=True)
    for i in range(3):
        (sidecars / f"agent-{i}.meta.json").write_text(
            json.dumps(
                {"description": f"read:{i}", "workflowPhase": "Read", "model": "opus"}
            ),
            encoding="utf-8",
        )


@pytest.mark.asyncio
async def test_listing_requires_a_dashboard_session(routes_module) -> None:
    with pytest.raises(web.HTTPUnauthorized):
        await routes_module._list_runs(FakeRequest(user=False), None)


@pytest.mark.asyncio
async def test_listing_decorates_each_run_with_its_derived_views(
    routes_module, tmp_path, monkeypatch
) -> None:
    _seed(tmp_path)
    reader = routes_module._reader()
    monkeypatch.setattr(reader, "DEFAULT_PROJECTS_ROOT", tmp_path)

    response = await routes_module._list_runs(FakeRequest(), None)
    body = json.loads(response.body)

    assert len(body["runs"]) == 1
    run = body["runs"][0]
    # The page renders these; the reader stores none of them.
    assert run["patterns"] == ["fan-out"]
    assert run["mermaid"].startswith("flowchart LR")
    # Just-written sidecars are live by definition -- liveness is read off file
    # times, so this also proves the decoration is not echoing a stored field.
    assert run["live_agents"] == 3
    assert all(a["state"] == "live" and a["idle"] for a in run["agents"])
    assert body["stale_after_seconds"] == reader.STALE_AFTER_SECONDS


@pytest.mark.asyncio
async def test_an_unknown_run_is_a_404_not_an_empty_body(
    routes_module, tmp_path, monkeypatch
) -> None:
    _seed(tmp_path)
    monkeypatch.setattr(routes_module._reader(), "DEFAULT_PROJECTS_ROOT", tmp_path)

    with pytest.raises(web.HTTPNotFound):
        await routes_module._get_run(FakeRequest(run_id="wf_missing"), None)


@pytest.mark.asyncio
async def test_the_list_announces_a_result_without_carrying_it(
    routes_module, tmp_path, monkeypatch
) -> None:
    """A fifteen-second poll must not ship every run's full output."""
    _seed(tmp_path)
    workflows = tmp_path / "-home-user-repo" / "sess-1" / "workflows"
    payload = json.loads((workflows / "wf_one.json").read_text())
    payload["result"] = {"verdict": "Approved. " + "detail " * 400}
    (workflows / "wf_one.json").write_text(json.dumps(payload), encoding="utf-8")
    monkeypatch.setattr(routes_module._reader(), "DEFAULT_PROJECTS_ROOT", tmp_path)

    response = await routes_module._list_runs(FakeRequest(), None)
    run = json.loads(response.body)["runs"][0]

    assert run["has_result"] is True
    assert run["result_preview"].startswith("Approved.")
    assert run["result_chars"] > 2000
    assert "text" not in run
    # The preview is a fixed-cost line, not a share of the output: 400 repeats
    # went into the result and only the first handful can reach the payload.
    assert len(run["result_preview"]) <= 160
    assert response.body.decode().count("detail") < 40


@pytest.mark.asyncio
async def test_the_result_route_returns_the_rendered_output(
    routes_module, tmp_path, monkeypatch
) -> None:
    _seed(tmp_path)
    workflows = tmp_path / "-home-user-repo" / "sess-1" / "workflows"
    payload = json.loads((workflows / "wf_one.json").read_text())
    payload["result"] = {"verdict": "Approved.\n\nNo blocking findings."}
    (workflows / "wf_one.json").write_text(json.dumps(payload), encoding="utf-8")
    monkeypatch.setattr(routes_module._reader(), "DEFAULT_PROJECTS_ROOT", tmp_path)

    response = await routes_module._get_result(FakeRequest(run_id="wf_one"), None)
    body = json.loads(response.body)

    assert body["run_id"] == "wf_one"
    assert "No blocking findings." in body["text"]
    assert body["chars"] == len(body["text"])
    assert "limit" not in body


@pytest.mark.asyncio
async def test_the_result_route_requires_a_dashboard_session(routes_module) -> None:
    with pytest.raises(web.HTTPUnauthorized):
        await routes_module._get_result(FakeRequest(user=False, run_id="wf_one"), None)


@pytest.mark.asyncio
async def test_a_result_for_an_unknown_run_is_a_404(
    routes_module, tmp_path, monkeypatch
) -> None:
    _seed(tmp_path)
    monkeypatch.setattr(routes_module._reader(), "DEFAULT_PROJECTS_ROOT", tmp_path)

    with pytest.raises(web.HTTPNotFound):
        await routes_module._get_result(FakeRequest(run_id="wf_missing"), None)


def _agent_transcript(tmp_path: Path) -> str:
    """Give wf_one's first agent a transcript with a schema'd answer."""
    sidecars = tmp_path / "-home-user-repo" / "sess-1" / "subagents" / "workflows" / "wf_one"
    (sidecars / "agent-0.jsonl").write_text(
        json.dumps(
            {
                "type": "assistant",
                "message": {
                    "content": [
                        {"type": "text", "text": "Compiling."},
                        {
                            "type": "tool_use",
                            "name": "StructuredOutput",
                            "input": {"finding": "the gate reopens"},
                        },
                    ]
                },
            }
        )
        + "\n",
        encoding="utf-8",
    )
    return "agent-0"


@pytest.mark.asyncio
async def test_an_agents_output_is_served_from_its_own_transcript(
    routes_module, tmp_path, monkeypatch
) -> None:
    _seed(tmp_path)
    agent_id = _agent_transcript(tmp_path)
    monkeypatch.setattr(routes_module._reader(), "DEFAULT_PROJECTS_ROOT", tmp_path)

    request = FakeRequest(run_id="wf_one")
    request.match_info["agent_id"] = agent_id
    response = await routes_module._get_agent_output(request, None)
    body = json.loads(response.body)

    assert body["agent_id"] == agent_id
    assert "the gate reopens" in body["text"]
    assert body["chars"] == len(body["text"])


@pytest.mark.asyncio
async def test_the_list_announces_which_agents_answered(
    routes_module, tmp_path, monkeypatch
) -> None:
    """Announced in the poll, rendered only on request."""
    _seed(tmp_path)
    agent_id = _agent_transcript(tmp_path)
    monkeypatch.setattr(routes_module._reader(), "DEFAULT_PROJECTS_ROOT", tmp_path)

    response = await routes_module._list_runs(FakeRequest(), None)
    agents = json.loads(response.body)["runs"][0]["agents"]
    answered = [a for a in agents if a["has_output"]]

    assert [a["agent_id"] for a in answered] == [agent_id]
    assert "the gate reopens" not in response.body.decode()


@pytest.mark.asyncio
async def test_an_unknown_agent_is_a_404(routes_module, tmp_path, monkeypatch) -> None:
    _seed(tmp_path)
    monkeypatch.setattr(routes_module._reader(), "DEFAULT_PROJECTS_ROOT", tmp_path)

    request = FakeRequest(run_id="wf_one")
    request.match_info["agent_id"] = "agent-missing"
    with pytest.raises(web.HTTPNotFound):
        await routes_module._get_agent_output(request, None)


@pytest.mark.asyncio
async def test_an_agents_output_requires_a_dashboard_session(routes_module) -> None:
    request = FakeRequest(user=False, run_id="wf_one")
    request.match_info["agent_id"] = "agent-0"
    with pytest.raises(web.HTTPUnauthorized):
        await routes_module._get_agent_output(request, None)
