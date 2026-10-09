"""Native workflow inventory, completion and output routing."""

import json
from types import SimpleNamespace

import pytest
from aiohttp import web

from backend import native, reader, routes
from kiro_crew.workflows import store


def records():
    return store.WorkflowRunStore().load_all()


@pytest.fixture(autouse=True)
def route_records(monkeypatch):
    async def read(request):
        return records()

    monkeypatch.setattr(routes._native(), "live_records", read)


def seed(status="running"):
    root = store.default_workflows_dir() / "runs"
    root.mkdir(parents=True, exist_ok=True)
    record = {
        "run_id": "wf_one",
        "name": "Native demo",
        "status": status,
        "events": [
            {"ts": "2026-01-01T00:00:00+00:00", "type": "phase_started", "data": {"title": "Read"}},
            {"ts": "2026-01-01T00:00:01+00:00", "type": "agent_started", "data": {"agent_id": "a0", "label": "read:a", "phase": "Read", "call_index": 0}},
            {"ts": "2026-01-01T00:00:02+00:00", "type": "agent_started", "data": {"agent_id": "a1", "label": "read:b", "phase": "Read", "call_index": 1}},
            {"ts": "2026-01-01T00:00:03+00:00", "type": "agent_finished", "data": {"agent_id": "a0", "ok": True}},
        ],
        "result": {"verdict": "Approved"} if status == "finished" else None,
        "agent_results": {"0": "Read complete"},
    }
    (root / "wf_one.json").write_text(json.dumps(record))
    return record, root / "wf_one.json"


def test_inventory_keeps_native_ids_distinct_and_metrics_unknown():
    seed()
    run = native.discover_runs(reader.render_result, records())[0]
    assert run["run_id"] == "kirocrew:wf_one"
    assert run["source"] == "kirocrew"
    assert run["tokens"] is None and run["tool_calls"] is None
    assert run["phases"] == [{"title": "Read", "detail": "", "entered": True}]
    decorated = routes._decorate(run, 2000000000)
    assert decorated["live_agents"] == 1
    assert decorated["agents"][0]["state"] == "stale"
    assert decorated["agents"][0]["returned"] is True
    assert decorated["agents"][1]["state"] == "live"
    assert decorated["phase_shape"][0]["entered"] is True
    assert run["summary"].startswith("Current phase: Read.")
    assert not routes._reader().is_older(run, 2000000000)


@pytest.mark.parametrize("status", ["finished", "failed", "cancelled", "paused"])
def test_stopped_runs_never_leave_agents_live(status):
    seed(status)
    run = native.discover_runs(reader.render_result, records())[0]
    assert routes._decorate(run, run["updated_at"])["live_agents"] == 0
    if status != "paused":
        assert routes._reader().is_older(run, 2000000000)


def test_reader_follows_updates_without_writing_or_copying_records():
    record, path = seed()
    before = path.read_bytes()
    native.discover_runs(reader.render_result, records())
    assert path.read_bytes() == before
    record["status"] = "finished"
    path.write_text(json.dumps(record))
    assert native.discover_runs(reader.render_result, records())[0]["status"] == "completed"


def test_output_lookup_does_not_accept_path_traversal():
    seed("finished")
    assert native.find_result("kirocrew:../../wf_one", reader.render_result, records()) is None
    assert native.find_agent_output("kirocrew:wf_one", "../../a0", reader.render_result, records()) is None
    assert native.find_agent_output("kirocrew:wf_one", "a1", reader.render_result, records()) is None


def test_temporary_records_stay_excluded():
    record, path = seed()
    record["memory_mode"] = "temporary"
    path.write_text(json.dumps(record))
    assert native.discover_runs(reader.render_result, records()) == []
    assert native.find_result("kirocrew:wf_one", reader.render_result, records()) is None


def test_inventory_failures_propagate_instead_of_looking_empty(monkeypatch):
    def unavailable(self):
        raise OSError("Workflow inventory unavailable")

    monkeypatch.setattr(store.WorkflowRunStore, "load_all", unavailable)
    with pytest.raises(OSError, match="inventory unavailable"):
        native.discover_runs(reader.render_result, records())


class Request(dict):
    def __init__(self, run_id="kirocrew:wf_one", query=None):
        super().__init__(user="operator")
        self.query = query or {}
        self.match_info = {"run_id": run_id, "agent_id": "a0"}


@pytest.mark.asyncio
async def test_native_listing_and_outputs_use_existing_routes(monkeypatch):
    seed("finished")
    monkeypatch.setattr(routes._reader(), "discover_runs", lambda: [])
    monkeypatch.setattr(routes._teams(), "discover_team_runs", lambda now: [])
    listing = json.loads((await routes._list_runs(Request(query={"older": "1"}), None)).body)
    run = listing["runs"][0]
    assert run["name"] == "Native demo"
    assert run["has_result"] and run["agents"][0]["has_output"]
    assert "Read complete" not in json.dumps(listing)
    result = json.loads((await routes._get_result(Request(), None)).body)
    assert "Approved" in result["text"]
    output = json.loads((await routes._get_agent_output(Request(), None)).body)
    assert output["text"] == "Read complete"


@pytest.mark.asyncio
async def test_live_view_uses_uncheckpointed_events_and_honors_run_authorization(monkeypatch):
    record, path = seed()
    disk_events = len(record["events"])
    record["events"].append({
        "ts": "2026-01-01T00:00:04+00:00", "type": "agent_started",
        "data": {"agent_id": "a2", "label": "read:c", "phase": "Read", "call_index": 2},
    })
    accessed = []

    def result(run_id):
        accessed.append(run_id)
        return record

    async def allow_list(request, operation):
        return None

    async def allow_run(request, run_id):
        return web.json_response({}, status=403) if run_id == "private" else None

    monkeypatch.setattr(native.workflow_api, "_private_memory_refusal", allow_list)
    monkeypatch.setattr(native.workflow_api, "_run_scope_refusal", allow_run)
    service = SimpleNamespace(list_runs=lambda: [{"run_id": "wf_one"}, {"run_id": "private"}], result=result)
    request = Request()
    request.app = {"state": SimpleNamespace(workflow_service=service)}
    live = await native.live_records(request)
    assert accessed == ["wf_one"]
    assert len(live[0]["events"]) == disk_events + 1
    assert len(json.loads(path.read_text())["events"]) == disk_events
    card = native.discover_runs(reader.render_result, live)[0]
    assert len(card["agents"]) == 3


@pytest.mark.asyncio
async def test_live_view_preserves_partial_outputs_and_excludes_temporary_runs(monkeypatch):
    record, _ = seed("failed")
    record["partial_results"] = record.pop("agent_results")
    temporary = {**record, "run_id": "temporary", "memory_mode": "temporary"}

    async def inventory(request):
        return web.json_response({"runs": [{"run_id": "wf_one"}, {"run_id": "temporary"}]})

    monkeypatch.setattr(native.workflow_api, "api_workflow_runs", inventory)
    service = SimpleNamespace(result=lambda run_id: temporary if run_id == "temporary" else record)
    request = Request()
    request.app = {"state": SimpleNamespace(workflow_service=service)}
    live = await native.live_records(request)
    assert [r["run_id"] for r in live] == ["wf_one"]
    assert native.find_agent_output("kirocrew:wf_one", "a0", reader.render_result, live) == "Read complete"


@pytest.mark.asyncio
@pytest.mark.parametrize("status,exception", [(403, web.HTTPForbidden), (503, web.HTTPServiceUnavailable)])
async def test_live_access_failure_is_visible_and_never_uses_disk(monkeypatch, status, exception):
    seed()

    async def refused(request):
        return web.json_response({}, status=status)

    monkeypatch.setattr(native.workflow_api, "api_workflow_runs", refused)
    with pytest.raises(exception):
        await native.live_records(Request())
