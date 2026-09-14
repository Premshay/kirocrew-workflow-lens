"""Reader for Claude Code dynamic-workflow artifacts."""

from __future__ import annotations

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "backend"))

import reader as cwr


def _project(root: Path, slug: str, session: str) -> Path:
    path = root / slug / session / "workflows"
    path.mkdir(parents=True)
    return path


def _run_payload(**overrides) -> dict:
    payload = {
        "runId": "wf_abc123-def",
        "workflowName": "implement-tasks",
        "status": "killed",
        "summary": "Implement the remaining tasks",
        "agentCount": 3,
        "totalTokens": 769188,
        "totalToolCalls": 386,
        "durationMs": 573545,
        "startTime": 1789069852930,
        "phases": [
            {"title": "Implement", "detail": "one worktree per task"},
            {"title": "Review", "detail": "three lenses per PR"},
        ],
        "workflowProgress": [
            {"type": "workflow_phase", "index": 1, "title": "Implement"},
            {"type": "workflow_phase", "index": 2, "title": "Review"},
            {"type": "workflow_agent", "index": 1, "label": "implement:task5"},
            {"type": "workflow_agent", "index": 2, "label": "implement:task6"},
        ],
    }
    payload.update(overrides)
    return payload


def test_reader_joins_agent_sidecars_onto_the_run(tmp_path) -> None:
    workflows = _project(tmp_path, "-home-user-repo", "sess-1")
    (workflows / "wf_abc123-def.json").write_text(json.dumps(_run_payload()), encoding="utf-8")
    sidecars = workflows.parent / "subagents" / "workflows" / "wf_abc123-def"
    sidecars.mkdir(parents=True)
    (sidecars / "agent-aaa.meta.json").write_text(
        json.dumps(
            {
                "agentType": "workflow-subagent",
                "description": "implement:task5",
                "workflowPhase": "Implement",
                "spawnDepth": 1,
                "requestShape": "foreground",
                "model": "opus",
            }
        ),
        encoding="utf-8",
    )

    runs = cwr.discover_runs(tmp_path)

    assert len(runs) == 1
    run = runs[0]
    assert run["run_id"] == "wf_abc123-def"
    assert run["name"] == "implement-tasks"
    assert run["status"] == "killed"
    assert run["session_id"] == "sess-1"
    assert run["project"] == "/home/user/repo"
    assert [p["title"] for p in run["phases"]] == ["Implement", "Review"]
    # The sidecar's `description` is the join key onto the progress label -- an
    # agent the run announced but that has no sidecar yet must still appear.
    by_label = {a["label"]: a for a in run["agents"]}
    assert by_label["implement:task5"]["phase"] == "Implement"
    assert by_label["implement:task5"]["model"] == "opus"
    assert by_label["implement:task6"]["phase"] == ""
    assert run["tokens"] == 769188
    assert run["tool_calls"] == 386


def test_reader_survives_a_run_with_no_sidecar_directory(tmp_path) -> None:
    workflows = _project(tmp_path, "-home-user-repo", "sess-1")
    (workflows / "wf_abc123-def.json").write_text(json.dumps(_run_payload()), encoding="utf-8")

    run = cwr.discover_runs(tmp_path)[0]

    assert [a["label"] for a in run["agents"]] == ["implement:task5", "implement:task6"]
    assert all(a["model"] == "" for a in run["agents"])


def test_reader_skips_a_malformed_run_instead_of_raising(tmp_path) -> None:
    workflows = _project(tmp_path, "-home-user-repo", "sess-1")
    (workflows / "wf_bad.json").write_text("{not json", encoding="utf-8")
    (workflows / "wf_ok.json").write_text(
        json.dumps(_run_payload(runId="wf_ok")), encoding="utf-8"
    )

    runs = cwr.discover_runs(tmp_path)

    assert [r["run_id"] for r in runs] == ["wf_ok"]


def test_runs_are_newest_first(tmp_path) -> None:
    workflows = _project(tmp_path, "-home-user-repo", "sess-1")
    older = workflows / "wf_older.json"
    newer = workflows / "wf_newer.json"
    older.write_text(json.dumps(_run_payload(runId="wf_older")), encoding="utf-8")
    newer.write_text(json.dumps(_run_payload(runId="wf_newer")), encoding="utf-8")
    import os

    os.utime(older, (1_700_000_000, 1_700_000_000))
    os.utime(newer, (1_800_000_000, 1_800_000_000))

    assert [r["run_id"] for r in cwr.discover_runs(tmp_path)] == ["wf_newer", "wf_older"]


def test_live_status_is_derived_from_the_sidecars_not_the_run_file(tmp_path) -> None:
    """A resumed run spawns agents without rewriting the run file.

    The 2026-09-11 case: a run file said ``killed`` at 23:39 while sidecars for
    three fresh agents landed at 23:58. Reporting the run file's own word would
    have called a live workflow dead.
    """
    workflows = _project(tmp_path, "-home-user-repo", "sess-1")
    run_file = workflows / "wf_abc123-def.json"
    run_file.write_text(json.dumps(_run_payload(status="killed")), encoding="utf-8")
    sidecars = workflows.parent / "subagents" / "workflows" / "wf_abc123-def"
    sidecars.mkdir(parents=True)
    sidecar = sidecars / "agent-aaa.meta.json"
    sidecar.write_text(json.dumps({"description": "implement:task5"}), encoding="utf-8")
    import os

    os.utime(run_file, (1_700_000_000, 1_700_000_000))
    os.utime(sidecar, (1_700_000_900, 1_700_000_900))

    run = cwr.discover_runs(tmp_path)[0]

    assert run["status"] == "killed"
    assert run["resumed_after_status"] is True


def test_project_path_keeps_a_dash_that_belongs_to_a_directory_name(tmp_path) -> None:
    """A slug cannot say which dash was a separator, so ask the filesystem.

    ``my-app`` rendered as ``my/app`` — a path that does not exist,
    displayed as if it did.
    """
    real = tmp_path / "dev" / "repos" / "acme" / "my-app"
    real.mkdir(parents=True)
    slug = "-" + str(real).lstrip("/").replace("/", "-")

    assert cwr._project_path(slug) == str(real)


def test_project_path_falls_back_to_the_naive_split_when_nothing_exists() -> None:
    assert cwr._project_path("-home-user-repo") == "/home/user/repo"


def test_agents_without_a_label_are_not_listed(tmp_path) -> None:
    workflows = _project(tmp_path, "-home-user-repo", "sess-1")
    payload = _run_payload(
        workflowProgress=[
            {"type": "workflow_agent", "index": 1, "label": "implement:task5"},
            {"type": "workflow_agent", "index": 2, "label": ""},
            {"type": "workflow_agent", "index": 3},
        ]
    )
    (workflows / "wf_abc123-def.json").write_text(json.dumps(payload), encoding="utf-8")

    run = cwr.discover_runs(tmp_path)[0]

    assert [a["label"] for a in run["agents"]] == ["implement:task5"]


def test_a_respawned_label_is_listed_once_per_agent(tmp_path) -> None:
    """A resume re-runs the same task, so two agents share one description.

    Keying the join on the description collapsed six real agents into three.
    """
    workflows = _project(tmp_path, "-home-user-repo", "sess-1")
    (workflows / "wf_abc123-def.json").write_text(
        json.dumps(
            _run_payload(
                workflowProgress=[
                    {"type": "workflow_agent", "index": 1, "label": "implement:task5"}
                ]
            )
        ),
        encoding="utf-8",
    )
    sidecars = workflows.parent / "subagents" / "workflows" / "wf_abc123-def"
    sidecars.mkdir(parents=True)
    for name, model in (("agent-aaa", "opus"), ("agent-bbb", "sonnet")):
        (sidecars / f"{name}.meta.json").write_text(
            json.dumps({"description": "implement:task5", "model": model}),
            encoding="utf-8",
        )

    run = cwr.discover_runs(tmp_path)[0]

    assert len(run["agents"]) == 2
    assert {a["model"] for a in run["agents"]} == {"opus", "sonnet"}
    assert {a["agent_id"] for a in run["agents"]} == {"agent-aaa", "agent-bbb"}


def test_agent_activity_comes_from_its_transcript(tmp_path) -> None:
    workflows = _project(tmp_path, "-home-user-repo", "sess-1")
    (workflows / "wf_abc123-def.json").write_text(
        json.dumps(_run_payload(workflowProgress=[])), encoding="utf-8"
    )
    sidecars = workflows.parent / "subagents" / "workflows" / "wf_abc123-def"
    sidecars.mkdir(parents=True)
    (sidecars / "agent-aaa.meta.json").write_text(
        json.dumps({"description": "implement:task5"}), encoding="utf-8"
    )
    transcript = sidecars / "agent-aaa.jsonl"
    transcript.write_text('{"x": 1}\n', encoding="utf-8")
    import os

    os.utime(transcript, (1_800_000_000, 1_800_000_000))

    run = cwr.discover_runs(tmp_path)[0]

    assert run["agents"][0]["last_active_at"] == 1_800_000_000


def _entry(kind: str, parts: list[dict]) -> str:
    return json.dumps({"type": kind, "message": {"content": parts}})


def test_last_step_comes_from_the_newest_assistant_narration(tmp_path) -> None:
    workflows = _project(tmp_path, "-home-user-repo", "sess-1")
    (workflows / "wf_abc123-def.json").write_text(
        json.dumps(_run_payload(workflowProgress=[])), encoding="utf-8"
    )
    sidecars = workflows.parent / "subagents" / "workflows" / "wf_abc123-def"
    sidecars.mkdir(parents=True)
    (sidecars / "agent-aaa.meta.json").write_text(
        json.dumps({"description": "implement:task5"}), encoding="utf-8"
    )
    (sidecars / "agent-aaa.jsonl").write_text(
        "\n".join(
            [
                _entry("assistant", [{"type": "text", "text": "An older thought"}]),
                _entry("assistant", [{"type": "thinking", "thinking": "ignored"}]),
                _entry("assistant", [{"type": "text", "text": "Retrying the push"}]),
                _entry(
                    "assistant",
                    [
                        {
                            "type": "tool_use",
                            "name": "Bash",
                            "input": {"description": "Push the branch"},
                        }
                    ],
                ),
                _entry("user", [{"type": "tool_result", "content": "ok"}]),
            ]
        )
        + "\n",
        encoding="utf-8",
    )

    agent = cwr.discover_runs(tmp_path)[0]["agents"][0]

    # Narration is what a reader wants; thinking is not shown and a tool_result
    # is not a step the agent took.
    assert agent["last_step"] == "Retrying the push"
    assert agent["last_tool"] == "Bash: Push the branch"


def test_last_step_is_empty_when_the_transcript_has_none(tmp_path) -> None:
    workflows = _project(tmp_path, "-home-user-repo", "sess-1")
    (workflows / "wf_abc123-def.json").write_text(
        json.dumps(_run_payload(workflowProgress=[])), encoding="utf-8"
    )
    sidecars = workflows.parent / "subagents" / "workflows" / "wf_abc123-def"
    sidecars.mkdir(parents=True)
    (sidecars / "agent-aaa.meta.json").write_text(
        json.dumps({"description": "implement:task5"}), encoding="utf-8"
    )
    (sidecars / "agent-aaa.jsonl").write_text("not json\n", encoding="utf-8")

    agent = cwr.discover_runs(tmp_path)[0]["agents"][0]

    assert agent["last_step"] == ""
    assert agent["last_tool"] == ""


def _agent(label: str, phase: str) -> dict:
    return {
        "agent_id": label.replace(":", "-"),
        "label": label,
        "phase": phase,
        "model": "opus",
        "spawn_depth": 1,
        "shape": "foreground",
        "last_active_at": 1_800_000_000,
        "last_step": "",
        "last_tool": "",
    }


def test_a_phase_of_siblings_reads_as_fan_out() -> None:
    run = {
        "phases": [{"title": "Implement", "detail": ""}],
        "agents": [
            _agent("implement:task5", "Implement"),
            _agent("implement:task6", "Implement"),
            _agent("implement:task7", "Implement"),
        ],
    }

    shape = cwr.infer_shape(run)

    assert shape["phases"][0]["pattern"] == "fan-out"
    assert shape["phases"][0]["count"] == 3
    assert "fan-out" in shape["patterns"]


def test_one_agent_after_many_reads_as_a_synthesize_barrier() -> None:
    run = {
        "phases": [{"title": "Read", "detail": ""}, {"title": "Critique", "detail": ""}],
        "agents": [
            # Three, not two: a pair is not a fan-out, so a merge after it is a
            # merge and nothing stronger should be claimed about the run.
            _agent("read:a", "Read"),
            _agent("read:b", "Read"),
            _agent("read:c", "Read"),
            _agent("critic:completeness", "Critique"),
        ],
    }

    shape = cwr.infer_shape(run)

    assert shape["phases"][1]["pattern"] == "synthesize"
    assert "fan-out-and-synthesize" in shape["patterns"]


def test_one_verifier_per_finding_reads_as_adversarial_verification() -> None:
    run = {
        "phases": [{"title": "Review", "detail": ""}, {"title": "Verify", "detail": ""}],
        "agents": [
            _agent("review:pr1", "Review"),
            _agent("review:pr2", "Review"),
            _agent("verify:pr1", "Verify"),
            _agent("verify:pr2", "Verify"),
        ],
    }

    shape = cwr.infer_shape(run)

    assert "adversarial-verification" in shape["patterns"]


def test_an_unmatched_shape_claims_no_pattern() -> None:
    run = {
        "phases": [{"title": "Solo", "detail": ""}],
        "agents": [_agent("solo:one", "Solo")],
    }

    shape = cwr.infer_shape(run)

    # Claiming a pattern from one agent would be a guess dressed as a finding.
    assert shape["patterns"] == []
    assert shape["phases"][0]["pattern"] == "single"


def test_mermaid_draws_a_column_per_phase_and_escapes_labels() -> None:
    run = {
        "run_id": "wf_x",
        "name": 'the "big" run',
        "phases": [{"title": "Implement", "detail": ""}],
        "agents": [_agent('implement:task"5"', "Implement")],
    }

    diagram = cwr.mermaid(run, now=1_800_000_000)

    assert diagram.startswith("flowchart LR")
    assert "subgraph" in diagram
    assert '"' not in diagram.split("\n", 1)[1].replace('["', "").replace('"]', "")


def test_mermaid_separates_the_label_from_the_idle_clock() -> None:
    run = {
        "run_id": "wf_x",
        "name": "run",
        "phases": [{"title": "Implement", "detail": ""}],
        "agents": [_agent("implement:task7", "Implement")],
    }

    diagram = cwr.mermaid(run, now=1_800_000_000 + 600)

    assert "implement:task7 · 10.0m" in diagram


def test_one_agent_ahead_of_a_fan_out_reads_as_classify_and_act() -> None:
    run = {
        "phases": [{"title": "Route", "detail": ""}, {"title": "Act", "detail": ""}],
        "agents": [
            _agent("classify:incoming", "Route"),
            _agent("act:a", "Act"),
            _agent("act:b", "Act"),
            _agent("act:c", "Act"),
        ],
    }

    assert "classify-and-act" in cwr.infer_shape(run)["patterns"]


def test_siblings_competing_on_one_subject_read_as_a_tournament() -> None:
    run = {
        "phases": [{"title": "Explore", "detail": ""}],
        "agents": [
            _agent("attempt:naming", "Explore"),
            _agent("attempt:naming", "Explore"),
            _agent("attempt:naming", "Explore"),
        ],
    }

    shape = cwr.infer_shape(run)

    # Same subject, several agents: they competed rather than divided the work.
    assert "tournament" in shape["patterns"]
    assert "fan-out" not in shape["patterns"]


def test_a_fan_out_over_distinct_subjects_is_not_a_tournament() -> None:
    run = {
        "phases": [{"title": "Implement", "detail": ""}],
        "agents": [
            _agent("implement:task5", "Implement"),
            _agent("implement:task6", "Implement"),
            _agent("implement:task7", "Implement"),
        ],
    }

    shape = cwr.infer_shape(run)

    assert "fan-out" in shape["patterns"]
    assert "tournament" not in shape["patterns"]


def test_a_merge_late_in_a_run_still_reads_as_fan_out_and_synthesize() -> None:
    """The barrier need not sit immediately after the widest phase.

    A real run (a four-phase run, 2026-09-11) fanned out 4 then 10, narrowed to 2,
    and ended with a single critic. Requiring the merge to follow the fan-out
    directly reported only "fan-out" and missed the composition entirely.
    """
    run = {
        "phases": [
            {"title": "Reconcile", "detail": ""},
            {"title": "Verify", "detail": ""},
            {"title": "Census", "detail": ""},
            {"title": "Critique", "detail": ""},
        ],
        "agents": (
            [_agent(f"reconcile:{i}", "Reconcile") for i in range(4)]
            + [_agent(f"brief:{i}", "Verify") for i in range(10)]
            + [_agent(f"census:{i}", "Census") for i in range(2)]
            + [_agent("critic:completeness", "Critique")]
        ),
    }

    patterns = cwr.infer_shape(run)["patterns"]

    assert "fan-out" in patterns
    assert "fan-out-and-synthesize" in patterns


def test_a_running_workflow_is_found_before_its_run_file_exists(tmp_path) -> None:
    """Claude Code writes the run file at the END; a live run has only a journal.

    Discovering by run file alone made the reader blind to exactly the case it
    exists for (observed live 2026-09-11: agents writing, no
    workflows/*.json on disk).
    """
    session = tmp_path / "-home-user-repo" / "sess-1"
    (session / "workflows").mkdir(parents=True)
    sidecars = session / "subagents" / "workflows" / "wf_live"
    sidecars.mkdir(parents=True)
    (sidecars / "journal.jsonl").write_text(
        "\n".join(
            [
                json.dumps({"type": "launched"}),
                json.dumps(
                    {
                        "type": "started",
                        "agentId": "a1",
                        "label": "implement:task8a",
                        "phase": "Implement",
                    }
                ),
                json.dumps(
                    {
                        "type": "started",
                        "agentId": "a2",
                        "label": "review:task8a",
                        "phase": "Review",
                    }
                ),
            ]
        )
        + "\n",
        encoding="utf-8",
    )
    (sidecars / "agent-a1.meta.json").write_text(
        json.dumps({"description": "implement:task8a", "workflowPhase": "Implement"}),
        encoding="utf-8",
    )

    runs = cwr.discover_runs(tmp_path)

    assert [r["run_id"] for r in runs] == ["wf_live"]
    run = runs[0]
    assert run["status"] == "running"
    assert [p["title"] for p in run["phases"]] == ["Implement", "Review"]
    assert {a["label"] for a in run["agents"]} == {"implement:task8a", "review:task8a"}
    assert run["session_id"] == "sess-1"


def test_a_finished_run_is_not_duplicated_by_its_journal(tmp_path) -> None:
    session = tmp_path / "-home-user-repo" / "sess-1"
    workflows = session / "workflows"
    workflows.mkdir(parents=True)
    (workflows / "wf_done.json").write_text(
        json.dumps(_run_payload(runId="wf_done", status="completed")), encoding="utf-8"
    )
    sidecars = session / "subagents" / "workflows" / "wf_done"
    sidecars.mkdir(parents=True)
    (sidecars / "journal.jsonl").write_text(
        json.dumps({"type": "launched"}) + "\n", encoding="utf-8"
    )

    runs = cwr.discover_runs(tmp_path)

    assert [r["run_id"] for r in runs] == ["wf_done"]
    assert runs[0]["status"] == "completed"


def test_a_returned_report_keeps_its_paragraph_breaks(tmp_path) -> None:
    """JSON-encoding the result would turn every break into a literal \\n."""
    workflows = _project(tmp_path, "-home-user-repo", "sess-1")
    (workflows / "wf_abc123-def.json").write_text(
        json.dumps(
            _run_payload(
                status="completed",
                result={
                    "synthesis": "# Landing report\n\nBoth slices are pushed.\n",
                    "confirmed": [{"title": "a gate reopens", "severity": "P2"}],
                },
            )
        ),
        encoding="utf-8",
    )

    run = cwr.discover_runs(tmp_path)[0]
    text = cwr.find_result("wf_abc123-def", tmp_path)

    assert run["has_result"] is True
    assert run["result_preview"] == "Landing report"
    assert run["result_chars"] == len(text)
    assert "## Synthesis" in text
    assert "Both slices are pushed." in text
    assert "\\n" not in text
    # A structured section stays readable as key/value lines, not as JSON.
    assert "- **a gate reopens**" in text


def test_a_run_that_returned_nothing_announces_no_output(tmp_path) -> None:
    workflows = _project(tmp_path, "-home-user-repo", "sess-1")
    (workflows / "wf_abc123-def.json").write_text(
        json.dumps(_run_payload(result=None)), encoding="utf-8"
    )

    run = cwr.discover_runs(tmp_path)[0]

    assert run["has_result"] is False
    assert run["result_preview"] == ""
    assert run["result_chars"] == 0
    assert cwr.find_result("wf_abc123-def", tmp_path) == ""


def test_a_live_run_read_from_its_journal_announces_no_output(tmp_path) -> None:
    """The result is written with the run file, so a journal run cannot have one."""
    sidecars = tmp_path / "-home-user-repo" / "sess-1" / "subagents" / "workflows" / "wf_live"
    sidecars.mkdir(parents=True)
    (sidecars / "journal.jsonl").write_text(
        json.dumps({"type": "workflow_phase", "index": 1, "title": "Read"}) + "\n",
        encoding="utf-8",
    )

    run = cwr.discover_runs(tmp_path)[0]

    assert run["status"] == "running"
    assert run["has_result"] is False


def test_a_large_result_is_returned_whole() -> None:
    """A 40,000-character cap cut 19 of the 42 results on this machine, and the
    largest runs are the ones that matter. The text travels once, when a reader
    opens it, so there is nothing for a cap to protect."""
    report = "\n\n".join(f"Finding {i}: " + "evidence " * 40 for i in range(2000))

    text = cwr.render_result({"verdict": report})

    assert len(text) > 500_000
    assert text.endswith(report.strip().splitlines()[-1])
    assert "truncated" not in text


def test_an_empty_phase_does_not_hide_the_merge_behind_it() -> None:
    """A phase that never ran cannot stand between two that did.

    A generate-and-filter run fanned out 6 then 8,
    skipped Filter entirely, and merged to one in Synthesize -- and reported a
    plain fan-out, because the phase immediately before the barrier was empty.
    """
    run = {
        "phases": [
            {"title": "Facts", "detail": ""},
            {"title": "Generate", "detail": ""},
            {"title": "Filter", "detail": ""},
            {"title": "Synthesize", "detail": ""},
        ],
        "agents": (
            [_agent(f"fact:{i}", "Facts") for i in range(6)]
            + [_agent(f"gen:{i}", "Generate") for i in range(8)]
            + [_agent("synthesis", "Synthesize")]
        ),
    }

    shape = cwr.infer_shape(run)

    assert "fan-out-and-synthesize" in shape["patterns"]
    assert "Generate runs 8 agents, Synthesize merges to one" in shape["evidence"]
    # The empty phase is still REPORTED, just not read as a relationship.
    assert [p["count"] for p in shape["phases"]] == [6, 8, 0, 1]


def test_a_result_of_counters_reads_as_a_list_not_six_headings() -> None:
    text = cwr.render_result(
        {"generated": 0, "unique": 0, "refuted": [], "synthesis": None}
    )

    assert text == (
        "- **Generated:** 0\n- **Unique:** 0\n- **Refuted:** none\n- **Synthesis:** none"
    )


def test_a_structure_reads_as_a_nested_list_not_a_code_card() -> None:
    """Fencing it rendered a list of findings as a code card, with a language
    tab, line numbers and an edit pencil, for content that is prose."""
    text = cwr.render_result({"confirmed": [{"title": "a gate reopens", "sev": "P2"}]})

    assert text == "## Confirmed\n\n- **a gate reopens**\n  - **Sev:** P2"
    assert "```" not in text


def test_a_paragraph_stays_inside_the_field_it_belongs_to() -> None:
    """Indented to the item's content column, so it continues that item rather
    than becoming a sibling -- and two spaces per level never reaches the four
    that a parser reads as code."""
    text = cwr.render_result(
        {"refuted": [{"flaw": "The leg is load-bearing.\n\nTwo call sites need it."}]}
    )

    assert text.splitlines() == [
        "## Refuted",
        "",
        "- **Item 1**",
        "  - **Flaw:** The leg is load-bearing.",
        "",
        "    Two call sites need it.",
    ]


def test_prose_passes_through_as_markdown() -> None:
    text = cwr.render_result({"summary": "# Landing report\n\nBoth slices shipped."})

    assert text == "## Summary\n\n# Landing report\n\nBoth slices shipped."


def test_an_agents_own_fenced_block_survives_whole() -> None:
    """Nothing is cut, so a fence the agent wrote is never left open."""
    text = cwr.render_result("here is the patch\n\n```python\n" + "x = 1\n" * 5000 + "```")

    assert text.count("```") == 2
    assert text.endswith("```")


def _transcript(path, *entries) -> None:
    path.write_text(
        "\n".join(
            json.dumps({"type": "assistant", "message": {"content": list(parts)}})
            for parts in entries
        )
        + "\n",
        encoding="utf-8",
    )


def test_an_agents_schemad_answer_wins_over_the_sentence_beside_it(tmp_path) -> None:
    """The prose next to a StructuredOutput call is narration, not the finding."""
    transcript = tmp_path / "agent-a.jsonl"
    _transcript(
        transcript,
        [{"type": "text", "text": "Census complete. Compiling."},
         {"type": "tool_use", "name": "StructuredOutput", "input": {"verdict": "LIVE"}}],
    )

    assert cwr.agent_output(transcript) == "- **Verdict:** LIVE"
    assert cwr._last_step(transcript)[2] is True


def test_an_agent_without_a_schema_answers_with_its_last_message(tmp_path) -> None:
    transcript = tmp_path / "agent-b.jsonl"
    _transcript(
        transcript,
        [{"type": "text", "text": "Starting the sweep."}],
        [{"type": "text", "text": "Done. No findings."}],
    )

    assert cwr.agent_output(transcript) == "Done. No findings."


def test_an_agent_that_has_written_nothing_announces_no_output(tmp_path) -> None:
    transcript = tmp_path / "agent-c.jsonl"
    _transcript(transcript, [{"type": "tool_use", "name": "Bash", "input": {}}])

    assert cwr.agent_output(transcript) == ""
    assert cwr._last_step(transcript)[2] is False


def test_an_agent_id_cannot_escape_the_run_directory(tmp_path) -> None:
    sidecars = tmp_path / "-home-user-repo" / "sess-1" / "subagents" / "workflows" / "wf_one"
    sidecars.mkdir(parents=True)
    (sidecars / "journal.jsonl").write_text("{}\n", encoding="utf-8")

    assert cwr.find_agent_output("wf_one", "../../../etc/passwd", tmp_path) is None


def _journal(path, *events) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        "\n".join(json.dumps(e) for e in events) + "\n", encoding="utf-8"
    )


def _started(key: str, phase: str) -> dict:
    return {"type": "started", "key": key, "agentId": key, "phase": phase}


def _result(key: str) -> dict:
    return {"type": "result", "key": key}


def test_a_stage_that_starts_before_the_one_above_it_finishes_is_a_pipeline(tmp_path) -> None:
    """Observed on a generate-and-verify run: each Verify triple started
    the moment its own generator returned, with others still running."""
    journal = tmp_path / "journal.jsonl"
    _journal(
        journal,
        _started("g1", "Generate"),
        _started("g2", "Generate"),
        _result("g1"),
        _started("v1", "Verify"),
        _result("g2"),
        _started("v2", "Verify"),
    )

    assert cwr.detect_pipelines(tmp_path, ["Generate", "Verify"]) == [
        {"from": "Generate", "to": "Verify"}
    ]


def test_a_barrier_claims_nothing(tmp_path) -> None:
    journal = tmp_path / "journal.jsonl"
    _journal(
        journal,
        _started("p1", "Premises"),
        _started("p2", "Premises"),
        _result("p1"),
        _result("p2"),
        _started("g1", "Generate"),
    )

    assert cwr.detect_pipelines(tmp_path, ["Premises", "Generate"]) == []


def test_an_early_failed_agent_does_not_manufacture_a_pipeline(tmp_path) -> None:
    """A first `synthesis` agent started early, died on a quota limit and was
    restarted. Pairing phases by journal order read that as a pipeline across
    two phases that are not even adjacent."""
    journal = tmp_path / "journal.jsonl"
    _journal(
        journal,
        _started("g1", "Generate"),
        _started("s1", "Synthesize"),  # started early, then failed
        _result("g1"),
        _started("f1", "Filter"),
        _result("f1"),
        _started("s2", "Synthesize"),
        _result("s2"),
    )

    assert cwr.detect_pipelines(tmp_path, ["Generate", "Filter", "Synthesize"]) == []


def test_a_journal_without_phases_claims_nothing(tmp_path) -> None:
    """Older runs record no phase on `started` and none in their sidecars."""
    journal = tmp_path / "journal.jsonl"
    _journal(
        journal,
        {"type": "started", "key": "a", "agentId": "a"},
        {"type": "result", "key": "a"},
        {"type": "started", "key": "b", "agentId": "b"},
    )

    assert cwr.detect_pipelines(tmp_path, ["Find", "Verify"]) == []


def test_the_pipeline_claim_is_additive_and_names_its_phases() -> None:
    run = {
        "phases": [{"title": "Generate", "detail": ""}, {"title": "Verify", "detail": ""}],
        "agents": (
            # Distinct subjects on each side: equal counts that SHARE subjects
            # are adversarial verification, which is a different claim.
            [_agent(f"gen:{i}", "Generate") for i in range(3)]
            + [_agent(f"verify:lens{i}", "Verify") for i in range(3)]
        ),
        "ordering": {"pipelined": [{"from": "Generate", "to": "Verify"}]},
    }

    shape = cwr.infer_shape(run)

    # The membership claim survives: the run is a fan-out AND a pipeline.
    assert shape["patterns"] == ["fan-out", "pipeline"]
    assert "Verify starts before Generate finishes" in shape["evidence"]


def test_a_finished_agent_keeps_whether_it_returned(tmp_path) -> None:
    """Idle time can only say "not writing any more", so a finished run painted
    every agent the same grey -- the one that delivered its finding and the one
    that died on a quota limit."""
    session = tmp_path / "-home-user-repo" / "sess-1"
    workflows = session / "workflows"
    workflows.mkdir(parents=True)
    (workflows / "wf_abc123-def.json").write_text(
        json.dumps(_run_payload(status="completed")), encoding="utf-8"
    )
    sidecars = session / "subagents" / "workflows" / "wf_abc123-def"
    sidecars.mkdir(parents=True)
    for agent_id, label in (("aaa", "implement:task5"), ("bbb", "implement:task6")):
        (sidecars / f"agent-{agent_id}.meta.json").write_text(
            json.dumps({"description": label, "workflowPhase": "Implement"}),
            encoding="utf-8",
        )
    # Only the first one answered.
    (sidecars / "journal.jsonl").write_text(
        "\n".join(
            json.dumps(e)
            for e in (
                {"type": "started", "key": "k1", "agentId": "aaa", "phase": "Implement"},
                {"type": "started", "key": "k2", "agentId": "bbb", "phase": "Implement"},
                {"type": "result", "key": "k1"},
            )
        )
        + "\n",
        encoding="utf-8",
    )

    agents = {a["label"]: a for a in cwr.discover_runs(tmp_path)[0]["agents"]}

    assert agents["implement:task5"]["returned"] is True
    assert agents["implement:task6"]["returned"] is False


def test_a_result_for_an_agent_that_never_started_is_ignored(tmp_path) -> None:
    (tmp_path / "journal.jsonl").write_text(
        json.dumps({"type": "result", "key": "orphan"}) + "\n", encoding="utf-8"
    )

    assert cwr.returned_agents(tmp_path) == set()


def _ordering_journal(tmp_path, *events) -> None:
    (tmp_path / "journal.jsonl").write_text(
        "\n".join(json.dumps(e) for e in events) + "\n", encoding="utf-8"
    )


def _start(key: str, phase: str, label: str) -> dict:
    return {"type": "started", "key": key, "agentId": key, "phase": phase, "label": label}


def test_a_merge_that_was_retried_is_still_a_merge() -> None:
    """The funnel on a generate-and-filter run was invisible because
    its single `synthesis` died on a quota limit and was restarted, making the
    phase hold two agents. Subjects are the unit; a retry moves only agents."""
    run = {
        "phases": [{"title": "Filter", "detail": ""}, {"title": "Synthesize", "detail": ""}],
        "agents": (
            [_agent(f"filter:{i}", "Filter") for i in range(4)]
            + [_agent("synthesis", "Synthesize"), _agent("synthesis", "Synthesize")]
        ),
    }

    shape = cwr.infer_shape(run)

    assert "fan-out-and-synthesize" in shape["patterns"]
    assert "Filter runs 4 agents, Synthesize merges to one" in shape["evidence"]


def test_retried_agents_on_one_subject_are_not_a_tournament() -> None:
    """Agents that all return are competing; agents where only the last returns
    are one agent tried again."""
    run = {
        "phases": [{"title": "Build", "detail": ""}],
        "agents": [_agent("build:api", "Build") for _ in range(3)],
        "ordering": {"reattempted": [{"phase": "Build", "subjects": 1, "attempts": 3}]},
    }

    shape = cwr.infer_shape(run)

    assert "tournament" not in shape["patterns"]
    assert "re-attempt" in shape["patterns"]


def test_a_review_wave_is_not_a_classifier_routing(tmp_path) -> None:
    """Every subject of the wave extends the single subject before it, so the
    run reviewed one thing rather than classifying and routing."""
    run = {
        "phases": [{"title": "Implement", "detail": ""}, {"title": "Review", "detail": ""}],
        "agents": (
            [_agent("implement:task8a", "Implement")]
            + [_agent(f"review:task8a:{lens}", "Review") for lens in ("spec", "sec", "perf")]
        ),
    }

    assert "classify-and-act" not in cwr.infer_shape(run)["patterns"]


def test_a_wave_the_next_stage_waited_on_is_a_barrier(tmp_path) -> None:
    _ordering_journal(
        tmp_path,
        _start("p1", "Premises", "premise:1"),
        _start("p2", "Premises", "premise:2"),
        _start("p3", "Premises", "premise:3"),
        {"type": "result", "key": "p1"},
        {"type": "result", "key": "p2"},
        {"type": "result", "key": "p3"},
        _start("g1", "Generate", "gen:a"),
    )

    ordering = cwr.read_ordering(tmp_path, ["Premises", "Generate"])

    assert ordering["barriers"] == [
        {"from": "Premises", "to": "Generate", "width": 3}
    ]
    assert ordering["pipelined"] == []


def test_a_subject_started_more_often_than_it_returned_is_a_re_attempt(tmp_path) -> None:
    _ordering_journal(
        tmp_path,
        _start("g1", "Generate", "gen:a"),
        _start("g2", "Generate", "gen:b"),
        {"type": "result", "key": "g2"},
        _start("g3", "Generate", "gen:a"),
        {"type": "result", "key": "g3"},
    )

    assert cwr.read_ordering(tmp_path, ["Generate"])["reattempted"] == [
        {"phase": "Generate", "subjects": 2, "attempts": 3}
    ]


def test_a_wave_that_started_in_slots_is_rolling(tmp_path) -> None:
    """A concurrency cap, not a smaller fan-out. Only FIRST starts count: a
    retry naturally follows a result and would fake the whole claim."""
    _ordering_journal(
        tmp_path,
        _start("f1", "Filter", "filter:1"),
        {"type": "result", "key": "f1"},
        _start("f2", "Filter", "filter:2"),
        {"type": "result", "key": "f2"},
        _start("f3", "Filter", "filter:3"),
        {"type": "result", "key": "f3"},
    )

    assert cwr.read_ordering(tmp_path, ["Filter"])["rolling"] == [
        {"phase": "Filter", "staggered": 2, "total": 3}
    ]


def test_each_stage_wider_than_the_last_is_a_widening_run() -> None:
    """The generate-and-filter arc: 6 facts, 9 designs, 52 checks. Distinct
    from fan-out, which says one phase had siblings, and from the merge, which
    says where it collapsed."""
    run = {
        "phases": [
            {"title": "Facts", "detail": ""},
            {"title": "Generate", "detail": ""},
            {"title": "Filter", "detail": ""},
        ],
        "agents": (
            [_agent(f"fact:{i}", "Facts") for i in range(2)]
            + [_agent(f"gen:{i}", "Generate") for i in range(4)]
            + [_agent(f"filter:{i}", "Filter") for i in range(9)]
        ),
    }

    shape = cwr.infer_shape(run)

    assert "widening" in shape["patterns"]
    assert (
        "each stage wider than the last: Facts 2, Generate 4, Filter 9"
        in shape["evidence"]
    )


def test_a_run_that_narrows_claims_no_widening() -> None:
    run = {
        "phases": [
            {"title": "Provenance", "detail": ""},
            {"title": "Feasibility", "detail": ""},
            {"title": "Inventory", "detail": ""},
        ],
        "agents": (
            [_agent(f"prov:{i}", "Provenance") for i in range(3)]
            + [_agent(f"feas:{i}", "Feasibility") for i in range(2)]
            + [_agent("inventory", "Inventory")]
        ),
    }

    assert "widening" not in cwr.infer_shape(run)["patterns"]


def test_two_widening_stages_are_not_yet_a_funnel() -> None:
    """Any run that fans out after a single agent widens once; the claim is
    about an arc, so it needs three stages."""
    run = {
        "phases": [{"title": "Plan", "detail": ""}, {"title": "Build", "detail": ""}],
        "agents": [_agent("plan", "Plan")] + [_agent(f"build:{i}", "Build") for i in range(4)],
    }

    assert "widening" not in cwr.infer_shape(run)["patterns"]


def test_records_sharing_a_few_short_fields_read_as_a_table() -> None:
    """A ranking drawn as bullets broke each row into four lines under a bare
    index; it is a table."""
    text = cwr.render_result({
        "ranking": [
            {"option": "A-full-storage-merge", "score": 1, "survived": 0, "fatal": True},
            {"option": "B-route-convergence", "score": 3, "survived": 2, "fatal": False},
        ]
    })

    assert text == (
        "## Ranking\n\n"
        "| Option | Score | Survived | Fatal |\n"
        "|---|---|---|---|\n"
        "| A-full-storage-merge | 1 | 0 | yes |\n"
        "| B-route-convergence | 3 | 2 | no |"
    )


def test_a_record_holding_prose_stays_a_list() -> None:
    """A table cell cannot hold a paragraph."""
    text = cwr.render_result({
        "refuted": [
            {"key": "a", "flaw": "short"},
            {"key": "b", "flaw": "x" * 120},
        ]
    })

    assert "|" not in text
    assert "- **a**" in text and "- **b**" in text


def test_a_record_is_headed_by_the_field_that_names_it() -> None:
    text = cwr.render_result([{"severity": "P2", "title": "a gate reopens"}])

    assert text.splitlines() == ["- **a gate reopens**", "  - **Severity:** P2"]


def test_a_cell_holding_a_pipe_does_not_split_the_row() -> None:
    text = cwr.render_result({"rows": [{"a": "x|y", "b": 1}, {"a": "z", "b": 2}]})

    assert "| x\\|y | 1 |" in text


def test_field_names_read_as_words_and_keep_their_acronyms() -> None:
    assert cwr._label("fatal_flaw") == "Fatal flaw"
    assert cwr._label("filesChanged") == "Files changed"
    assert cwr._label("PR_number") == "PR number"
