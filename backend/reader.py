#!/usr/bin/env python3
"""Read Claude Code dynamic-workflow runs from their on-disk artifacts.

Claude Code writes a workflow run's state next to the session that started it,
and its subagents to a sibling tree:

    ~/.claude/projects/<slug>/<session>/workflows/<run_id>.json
    ~/.claude/projects/<slug>/<session>/subagents/workflows/<run_id>/agent-*.meta.json

Nothing in Kiro Crew reads either. The ACP projection that would have surfaced
these (``async_task_spawned`` and friends) receives no events: verified against a
live three-agent run on 2026-09-11, whose transcript contained zero of them. The
files are the only source that actually carries the work, so this reads them.
"""

from __future__ import annotations

import argparse
import html
import json
import re
import sys
import time
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from typing import Any

DEFAULT_PROJECTS_ROOT = Path.home() / ".claude" / "projects"


def _load_json(path: Path) -> dict[str, Any] | None:
    """Parse *path*, or None when it is unreadable or not an object.

    A run being written while we read it is the normal case, not an error, so a
    partial file is skipped rather than raised — the next poll sees it whole.
    """
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    return payload if isinstance(payload, dict) else None


def _project_path(slug: str) -> str:
    """The checkout a project slug encodes.

    Claude Code flattens a path by replacing every separator with ``-``, which is
    lossy: a dash inside a directory's own name is indistinguishable from a
    separator. Splitting on every dash rendered ``my-app`` as
    ``my/app`` -- a path that does not exist, shown as though it did.

    So the filesystem decides, longest match first: at each position the longest
    run of remaining tokens that names a real directory wins, which is what keeps
    a multi-dash name like ``dashboard-chat-1917`` whole. A slug whose checkout is
    gone falls back to the naive split for the rest, which reads as a guess
    rather than a claim.
    """
    if not slug.startswith("-"):
        return slug
    tokens = slug.lstrip("-").split("-")
    resolved = Path("/")
    index = 0
    while index < len(tokens):
        for stop in range(len(tokens), index, -1):
            candidate = resolved / "-".join(tokens[index:stop])
            if candidate.is_dir():
                resolved, index = candidate, stop
                break
        else:
            return str(resolved / "/".join(tokens[index:]))
    return str(resolved)


#: How much of an agent transcript to read for its latest step. They run to
#: megabytes and only the tail is current, so the whole file is never loaded.
TRANSCRIPT_TAIL_BYTES = 262144


#: The tool a workflow agent answers THROUGH when its task declares a schema.
#: Its input is the agent's return value, not a step it took along the way.
STRUCTURED_OUTPUT_TOOL = "StructuredOutput"


def _transcript_tail(transcript: Path) -> str:
    """The readable tail of an agent transcript, or ``""``.

    They run to megabytes and only the end is current, so the whole file is
    never loaded. A seek lands mid-line unless the file is short; that first
    partial line simply fails to parse and is skipped with every other one.
    """
    try:
        with transcript.open("rb") as handle:
            handle.seek(0, 2)
            handle.seek(max(0, handle.tell() - TRANSCRIPT_TAIL_BYTES))
            return handle.read().decode("utf-8", errors="replace")
    except OSError:
        return ""


def _assistant_parts(raw: str):
    """Every assistant content part in *raw*, newest first."""
    for line in reversed(raw.splitlines()):
        try:
            entry = json.loads(line)
        except (json.JSONDecodeError, ValueError):
            continue
        if not isinstance(entry, dict) or entry.get("type") != "assistant":
            continue
        message = entry.get("message")
        content = message.get("content") if isinstance(message, dict) else None
        for part in reversed(content or []):
            if isinstance(part, dict):
                yield part


def _last_step(transcript: Path) -> tuple[str, str, bool]:
    """The agent's newest narration, tool call, and whether it has an output.

    Narration is what makes a run readable -- "Retrying the push" says more than
    any status word -- and the tool line says what it is doing right now. Thinking
    blocks are skipped (not shown to a reader anywhere else) and so are tool
    results, which are the environment answering rather than a step the agent took.

    Whether an output EXISTS is decided in this same pass, because the tail is
    already read and parsed here; rendering it is left to the route that serves
    it, so a poll over fourteen agents never formats fourteen reports nobody
    opened.
    """
    raw = _transcript_tail(transcript)
    if not raw:
        return "", "", False

    step = tool = ""
    has_output = False
    for part in _assistant_parts(raw):
        if step and tool and has_output:
            break
        kind = part.get("type")
        if kind == "text":
            if not step:
                step = " ".join(str(part.get("text") or "").split())[:160]
            has_output = has_output or bool(str(part.get("text") or "").strip())
        elif kind == "tool_use":
            name = str(part.get("name") or "")
            if not tool:
                detail = ""
                params = part.get("input")
                if isinstance(params, dict):
                    detail = str(params.get("description") or "")
                tool = f"{name}: {detail}" if name and detail else name
            if name == STRUCTURED_OUTPUT_TOOL and part.get("input"):
                has_output = True
    return step, tool, has_output


def agent_output(transcript: Path) -> str:
    """What the agent answered with, rendered like the run's own result.

    Its schema'd answer wins over its prose: an agent whose task declared a
    schema replies THROUGH ``StructuredOutput``, and the sentence it happens to
    type beside that call ("Census complete. Compiling.") is narration, not the
    finding. When there is no schema the last thing it wrote IS the answer.
    """
    raw = _transcript_tail(transcript)
    if not raw:
        return ""
    texts: list[str] = []
    for part in _assistant_parts(raw):
        if (
            part.get("type") == "tool_use"
            and str(part.get("name") or "") == STRUCTURED_OUTPUT_TOOL
            and part.get("input")
        ):
            return render_result(part["input"])
        if part.get("type") == "text":
            texts.append(str(part.get("text") or ""))
    # Newest first from the scan, so the last message it wrote comes first.
    for text in texts:
        if text.strip():
            return text.strip()
    return ""


def _agent_sidecars(session_dir: Path, run_id: str) -> tuple[list[dict], float]:
    """Every agent of *run_id*, newest activity first, plus the newest mtime.

    One record per sidecar FILE rather than per description: a resume re-runs the
    same task, so two agents legitimately share one description and keying on it
    collapsed six real agents into three. The file stem is the identity.

    Liveness comes from the agent's own transcript rather than the sidecar, which
    is written once at spawn and never again -- so the sidecar cannot distinguish
    an agent still working from one cut off an hour ago.
    """
    sidecar_dir = session_dir / "subagents" / "workflows" / run_id
    records: list[dict] = []
    newest = 0.0
    for path in sorted(sidecar_dir.glob("agent-*.meta.json")):
        payload = _load_json(path)
        if payload is None:
            continue
        agent_id = path.name[: -len(".meta.json")]
        transcript = sidecar_dir / f"{agent_id}.jsonl"
        try:
            last_active = transcript.stat().st_mtime
        except OSError:
            last_active = path.stat().st_mtime
        step, tool, has_output = _last_step(transcript)
        records.append(
            {
                "agent_id": agent_id,
                "last_active_at": last_active,
                "last_step": step,
                "last_tool": tool,
                "has_output": has_output,
                **payload,
            }
        )
        newest = max(newest, last_active, path.stat().st_mtime)
    records.sort(key=lambda r: r["last_active_at"], reverse=True)
    return records, newest


def returned_agents(sidecar_dir: Path) -> set[str]:
    """The agents that RETURNED, read from the journal's own result events.

    Liveness is an idle-time reading, so it can only ever say "not writing any
    more" -- which turns every agent of a finished run into the same grey
    circle, whether it delivered its finding or died on a quota limit. Whether
    an agent returned is a fact the journal records: a ``result`` carries the
    key of the ``started`` it answers.
    """
    key_to_agent: dict[str, str] = {}
    returned: set[str] = set()
    for entry in _journal_events(sidecar_dir):
        key = str(entry.get("key") or "")
        if not key:
            continue
        if entry.get("type") == "started":
            key_to_agent[key] = str(entry.get("agentId") or "")
        elif entry.get("type") == "result" and key in key_to_agent:
            returned.add(key_to_agent[key])
    return returned


def _agents(
    run: dict[str, Any],
    sidecars: list[dict[str, Any]],
    returned: set[str] | None = None,
) -> list[dict[str, Any]]:
    """The run's agents: one row per sidecar, plus any label with no sidecar yet.

    ``description`` on a sidecar holds the same string the run's progress feed
    uses as a label, which is what lets an announced-but-unspawned agent still be
    listed -- omitting it would under-report a wave that is mid-spawn.
    """
    agents = [
        {
            "agent_id": str(record.get("agent_id") or ""),
            "label": str(record.get("description") or ""),
            "phase": str(record.get("workflowPhase") or ""),
            "model": str(record.get("model") or ""),
            "spawn_depth": record.get("spawnDepth"),
            "shape": str(record.get("requestShape") or ""),
            "last_active_at": record.get("last_active_at"),
            "last_step": str(record.get("last_step") or ""),
            "last_tool": str(record.get("last_tool") or ""),
            "has_output": bool(record.get("has_output")),
            "returned": str(record.get("agent_id") or "").removeprefix("agent-")
            in (returned or set()),
        }
        for record in sidecars
        if str(record.get("description") or "")
    ]
    spawned = {agent["label"] for agent in agents}
    for event in run.get("workflowProgress") or []:
        if not isinstance(event, dict) or event.get("type") != "workflow_agent":
            continue
        label = str(event.get("label") or "")
        if not label or label in spawned:
            continue
        spawned.add(label)
        agents.append(
            {
                "agent_id": "",
                "label": label,
                "phase": "",
                "model": "",
                "spawn_depth": None,
                "shape": "",
                "last_active_at": None,
                "last_step": "",
                "last_tool": "",
                "has_output": False,
                "returned": False,
            }
        )
    return agents


#: Longest first-line preview carried in the list payload.
_PREVIEW_CHARS = 160

#: Field names that name a record, in the order they are tried as its title.
_TITLE_FIELDS = (
    "title", "name", "option", "label", "key", "id", "claim", "finding",
    "question", "subject", "candidate", "item", "step", "file",
)

#: Widest list of records drawn as a table. Past this a phone scrolls sideways
#: through columns nobody can hold in view at once, and a titled list reads better.
_TABLE_MAX_COLUMNS = 6

#: Markdown syntax at the start of a line: a heading marker or a list bullet.
_MD_SYNTAX = re.compile(r"^(?:#{1,6}\s+|[-*]\s+)")


def _scalar(value: Any) -> str:
    """A leaf as a reader would say it.

    "yes", "no" and "none" rather than the file's ``true``, ``false`` and
    ``null``: this pane is for reading what a run found, and the JSON spellings
    read as code sitting in the middle of an answer.
    """
    if value is None:
        return "none"
    if value is True:
        return "yes"
    if value is False:
        return "no"
    return str(value)


def _label(key: Any) -> str:
    """A field name as a reader would say it: ``fatal_flaw`` is "Fatal flaw".

    A word already in capitals keeps them, so ``PR_number`` reads "PR number".
    A lowercase ``pr_url`` reads "Pr url": nothing in the key says it was an
    acronym, and guessing from a list would be wrong as often as right.
    """
    spaced = re.sub(r"(?<=[a-z0-9])(?=[A-Z])", " ", str(key))
    words = [
        w.lower() if w[:1].isupper() and not w.isupper() else w
        for w in re.split(r"[_\-\s]+", spaced)
        if w
    ]
    text = " ".join(words)
    return text[:1].upper() + text[1:] if text else str(key)


def _record_title(record: dict[str, Any]) -> tuple[str, str | None]:
    """The field that names *record*, and its value, when one does."""
    for field in _TITLE_FIELDS:
        value = record.get(field)
        if isinstance(value, bool) or not isinstance(value, (str, int)):
            continue
        if str(value).strip() and not _is_prose(value):
            return str(value).strip(), field
    return "", None


def _table(items: Any) -> str | None:
    """*items* as a markdown table, when it is shaped like one.

    A ranking or a score sheet -- several records sharing the same few short
    fields -- is a table, and drawn as nested bullets it reads as a lookup
    exercise: each row broken into four lines under a bare index. Anything with
    prose or nesting inside it stays a list, because a table cell cannot hold a
    paragraph.
    """
    if not isinstance(items, list) or len(items) < 2:
        return None
    if not all(isinstance(item, dict) and item for item in items):
        return None
    columns = list(items[0].keys())
    if len(columns) > _TABLE_MAX_COLUMNS:
        return None
    for item in items:
        if set(item.keys()) != set(columns):
            return None
        if any(isinstance(v, (dict, list)) or _is_prose(v) for v in item.values()):
            return None

    def cell(value: Any) -> str:
        return _scalar(value).replace("|", "\\|")

    rows = [
        "| " + " | ".join(_label(c) for c in columns) + " |",
        "|" + "---|" * len(columns),
    ]
    rows += ["| " + " | ".join(cell(item.get(c)) for c in columns) + " |" for item in items]
    return "\n".join(rows)


def _is_prose(value: Any) -> bool:
    """A string long enough to be a report rather than a field."""
    return isinstance(value, str) and ("\n" in value or len(value) > 80)


def _markdown_lines(value: Any, depth: int = 0) -> list[str]:
    """*value* as a nested markdown list a person can scan.

    Two spaces per level, which is the content column of a ``- `` item, so a
    parser reads every level as list nesting and never reaches the four that
    means code. Field names become bold labels, and a record is headed by the
    field that names it -- "A-full-storage-merge" -- instead of a bare ``[0]``
    that says only where it sat in an array.
    """
    pad = "  " * depth
    lines: list[str] = []

    def prose(lead: str, text: str) -> None:
        # First line beside its label; the rest continue the same item, which
        # keeps a paragraph inside the field it belongs to.
        body = str(text).strip().splitlines()
        lines.append(f"{pad}- {lead}{body[0]}" if body else f"{pad}- {lead}".rstrip())
        for line in body[1:]:
            lines.append(f"{pad}  {line}" if line.strip() else "")

    if isinstance(value, dict):
        for key, item in value.items():
            label = _label(key)
            if isinstance(item, (dict, list)) and item:
                lines.append(f"{pad}- **{label}:**")
                lines.extend(_markdown_lines(item, depth + 1))
            elif isinstance(item, (dict, list)):
                lines.append(f"{pad}- **{label}:** none")
            elif _is_prose(item):
                prose(f"**{label}:** ", item)
            else:
                lines.append(f"{pad}- **{label}:** {_scalar(item)}")
        return lines
    if isinstance(value, list):
        for index, item in enumerate(value, start=1):
            if isinstance(item, dict) and item:
                title, used = _record_title(item)
                lines.append(f"{pad}- **{title or f'Item {index}'}**")
                rest = {k: v for k, v in item.items() if k != used}
                lines.extend(_markdown_lines(rest, depth + 1))
            elif isinstance(item, list) and item:
                lines.append(f"{pad}- **Item {index}**")
                lines.extend(_markdown_lines(item, depth + 1))
            elif _is_prose(item):
                prose("", item)
            else:
                lines.append(f"{pad}- {_scalar(item)}")
        return lines
    if _is_prose(value):
        prose("", value)
    else:
        lines.append(f"{pad}- {_scalar(value)}")
    return lines


def render_result(result: Any) -> str:
    """The workflow's return value as MARKDOWN a person can read.

    Markdown because the page renders it with the dashboard's own renderer, and
    because that is the form the content already arrives in: the prose sections
    of a result are written as markdown by the agent that produced them.

    Shape decides the form per section, which is what keeps the markdown
    well-formed:

    * a scalar becomes one ``- key: value`` list item, so a result that is all
      counters reads as a list instead of six headings with a digit under each;
    * prose keeps its key as a heading and is passed through untouched;
    * a structure becomes a NESTED LIST. Fencing it was the obvious way to
      protect its indentation, and it was wrong: a list of findings came out as
      a code card, with a language tab, line numbers and an edit pencil, for
      content that is prose in a record. Inside a list, indentation is measured
      from the parent item rather than from the margin, so two spaces per level
      nests correctly and never reaches the four that means code.
    """
    if result is None:
        return ""
    if isinstance(result, str):
        text = result
    elif isinstance(result, dict):
        blocks: list[str] = []
        scalars: list[str] = []
        for key, value in result.items():
            structured = isinstance(value, (dict, list)) and bool(value)
            label = _label(key)
            if not structured and not _is_prose(value):
                empty = isinstance(value, (dict, list))
                scalars.append(f"- **{label}:** {'none' if empty else _scalar(value)}")
                continue
            if scalars:
                blocks.append("\n".join(scalars))
                scalars = []
            if structured:
                body = _table(value) or "\n".join(_markdown_lines(value)).rstrip()
                blocks.append(f"## {label}\n\n{body}")
            else:
                blocks.append(f"## {label}\n\n{value.strip()}")
        if scalars:
            blocks.append("\n".join(scalars))
        text = "\n\n".join(blocks)
    else:
        text = _table(result) or "\n".join(_markdown_lines(result)).rstrip()
    # Whole, never cut. A 40,000-character cap used to sit here, and it cut 19
    # of the 42 results on this machine -- the largest runs are the ones that
    # matter, and one run showed 21% of its
    # 188,320 characters. It protected nothing: the list only announces a
    # result, so the text travels once, when a reader opens it, into a pane
    # that already scrolls. Rendering uncapped costs the poll nothing either
    # (328 ms against 331; that time is file reads, not formatting).
    return text.strip()


def _result_preview(text: str) -> str:
    """The first line with content, for the collapsed row.

    The section headings this module writes are skipped: a preview reading
    "synthesis" tells a reader the shape of the answer and nothing about the
    answer, which is the opposite of what a collapsed row is for.
    """
    fallback = ""
    for line in text.splitlines():
        stripped = line.strip()
        if not stripped:
            continue
        fallback = fallback or stripped[:_PREVIEW_CHARS]
        plain = stripped.replace("**", "")
        # Structure rather than content: a section heading, a fence, a table
        # row, or a label that only introduces the block beneath it.
        if (
            line.startswith("## ")
            or stripped.startswith("```")
            or stripped.startswith("|")
            or plain.endswith(":")
        ):
            continue
        # Only markdown syntax is dropped -- a heading marker, a list bullet,
        # bold. A bare `lstrip("#")` ate the hash off "#2228 — feat(media): ..."
        # and reported the PR as 2228.
        return _MD_SYNTAX.sub("", plain)[:_PREVIEW_CHARS]
    return fallback


def find_result(run_id: str, projects_root: Path | None = None) -> str | None:
    """The rendered result of *run_id*, or None when there is no run file.

    Looked up by id rather than carried in the list payload: the largest
    result on this machine is 240,849 characters, so shipping every run's on a
    15-second poll would send megabytes nobody is reading.
    """
    root = DEFAULT_PROJECTS_ROOT if projects_root is None else projects_root
    for run_file in root.glob("*/*/workflows/*.json"):
        run = _load_json(run_file)
        if run is None:
            continue
        if str(run.get("runId") or run_file.stem) != run_id:
            continue
        return render_result(run.get("result"))
    return None


def find_agent_output(
    run_id: str, agent_id: str, projects_root: Path | None = None
) -> str | None:
    """One agent's own output, or None when that agent has no transcript.

    Keyed on the sidecar stem, which is the agent's identity here: a resumed run
    spawns a second agent with the same label, and a label would serve the wrong
    attempt's answer.
    """
    root = DEFAULT_PROJECTS_ROOT if projects_root is None else projects_root
    if "/" in agent_id or "\\" in agent_id or agent_id.startswith("."):
        return None
    for sidecar_dir in root.glob("*/*/subagents/workflows/*"):
        if sidecar_dir.name != run_id or not sidecar_dir.is_dir():
            continue
        transcript = sidecar_dir / f"{agent_id}.jsonl"
        if not transcript.is_file():
            return None
        return agent_output(transcript)
    return None


def read_run(run_file: Path) -> dict[str, Any] | None:
    """One normalized run, or None when the file cannot be read."""
    run = _load_json(run_file)
    if run is None:
        return None
    session_dir = run_file.parent.parent
    run_id = str(run.get("runId") or run_file.stem)
    sidecars, newest_sidecar = _agent_sidecars(session_dir, run_id)
    run_mtime = run_file.stat().st_mtime
    status = str(run.get("status") or "")
    result_text = render_result(run.get("result"))
    phases = [
        {"title": str(p.get("title") or ""), "detail": str(p.get("detail") or "")}
        for p in (run.get("phases") or [])
        if isinstance(p, dict)
    ]
    return {
        "run_id": run_id,
        "name": str(run.get("workflowName") or ""),
        "status": status,
        "summary": str(run.get("summary") or ""),
        "project": _project_path(session_dir.parent.name),
        "session_id": session_dir.name,
        "phases": phases,
        "agents": _agents(
            run, sidecars, returned_agents(session_dir / "subagents" / "workflows" / run_id)
        ),
        "tokens": run.get("totalTokens"),
        "tool_calls": run.get("totalToolCalls"),
        "duration_ms": run.get("durationMs"),
        "updated_at": run_mtime,
        # A resume spawns agents without rewriting the run file, so a terminal
        # status there can describe work that is still going. Reported alongside
        # the status rather than overriding it: which one is true depends on
        # whether the session is still alive, which this reader cannot see.
        "resumed_after_status": bool(
            status in _TERMINAL_STATUSES and newest_sidecar > run_mtime
        ),
        # Ordering evidence, which phase membership cannot carry. Paired in the
        # run's OWN declared order, not the order the journal happens to show.
        "ordering": read_ordering(
            session_dir / "subagents" / "workflows" / run_id,
            [p["title"] for p in phases],
        ),
        # Announced, not shipped: the page asks for the text only when a reader
        # opens it. `result_chars` is the rendered length, which is what the
        # collapsed row can honestly promise.
        "has_result": bool(result_text),
        "result_preview": _result_preview(result_text),
        "result_chars": len(result_text),
    }


#: Statuses a run file uses for work it believes has stopped.
_TERMINAL_STATUSES = frozenset({"killed", "completed", "failed", "error", "aborted"})


def _journal_events(sidecar_dir: Path) -> list[dict[str, Any]]:
    """The run's own journal, one JSON object per line."""
    events: list[dict[str, Any]] = []
    try:
        raw = (sidecar_dir / "journal.jsonl").read_text(encoding="utf-8")
    except OSError:
        return events
    for line in raw.splitlines():
        try:
            entry = json.loads(line)
        except (json.JSONDecodeError, ValueError):
            continue
        if isinstance(entry, dict):
            events.append(entry)
    return events


def detect_pipelines(
    sidecar_dir: Path, declared: list[str] | None = None
) -> list[dict[str, str]]:
    """Phase pairs where the later phase began before the earlier one finished.

    Phase MEMBERSHIP cannot tell a pipeline from a barrier: ``pipeline`` and
    ``parallel``-then-``parallel`` put exactly the same agents in exactly the
    same phases. The difference is ordering, and the journal records it --
    every ``started`` carries its phase and key, every ``result`` carries the
    key of the agent it belongs to, and the file is append-ordered, so a
    result can be attributed to a phase without any timestamp (there are none).

    The test is therefore: did any agent of B start before the LAST agent of A
    returned? Observed on a generate-and-verify run,
    where Premises ran to a barrier -- all four results landed before Generate
    began -- while each Verify triple started the moment its own generator
    returned, with seven generators still running.

    Two guards keep this from reporting a shape nobody wrote. Phases are paired
    in the order the RUN declares them (*declared*), not the order they appear
    in the journal -- and the later phase must also have started AFTER the
    earlier one began. Without that second guard, one agent that started early
    and failed was enough to manufacture a pipeline: in
    a generate-and-filter run, a first ``synthesis`` agent started at
    journal position 29, died on a quota limit, and was restarted at 153, which
    read as "Synthesize starts before Filter finishes" across two phases that
    are not even adjacent.

    A phase whose predecessor has returned nothing yet claims nothing: the
    evidence for a barrier and for a pipeline is the same absence.

    The same silence covers most older runs, and that is the artifacts' limit
    rather than this test's. Of the 15 runs on this machine whose script calls
    ``pipeline``, 14 were written before the journal recorded a phase on its
    ``started`` events -- and their sidecars carry ``workflowPhase: null`` too,
    so those runs have no phase attribution anywhere to order. The one that
    does is detected. Runs written by the current format all carry it.
    """
    return read_ordering(sidecar_dir, declared)["pipelined"]


def read_ordering(
    sidecar_dir: Path, declared: list[str] | None = None
) -> dict[str, list[dict[str, Any]]]:
    """Everything the journal's ORDER says that its membership cannot.

    One pass, four readings:

    ``pipelined``
        A stage that began before the stage above it finished.
    ``barriers``
        A wave that a later stage waited on in full -- the opposite arrangement,
        and the one with a wall-clock cost worth seeing.
    ``reattempted``
        A subject started more times than it returned, which is a retry rather
        than breadth. It is what separates a retried agent from a TOURNAMENT:
        several agents on one subject that all return are competing; several
        where only the last returns are one agent being tried again.
    ``rolling``
        A phase whose own agents started only as its earlier ones returned --
        a wave shaped by a concurrency cap rather than launched at once.
    """
    phase_of_key: dict[str, str] = {}
    label_of_key: dict[str, str] = {}
    order: list[str] = []
    first_start: dict[str, int] = {}
    last_result: dict[str, int] = {}
    first_result: dict[str, int] = {}
    starts: dict[str, int] = {}
    results: dict[str, int] = {}
    phase_of_label: dict[str, str] = {}
    staggered: dict[str, int] = {}
    seen_label: set[str] = set()

    for index, entry in enumerate(_journal_events(sidecar_dir)):
        kind = entry.get("type")
        key = str(entry.get("key") or "")
        if kind == "started":
            phase = str(entry.get("phase") or "")
            label = str(entry.get("label") or "")
            if not phase:
                continue
            if key:
                phase_of_key[key] = phase
                label_of_key[key] = label
            if label:
                starts[label] = starts.get(label, 0) + 1
                phase_of_label.setdefault(label, phase)
                # Only a subject's FIRST start can be a rolling start; a retry
                # naturally follows a result and would fake the whole claim.
                if label not in seen_label:
                    seen_label.add(label)
                    if phase in first_result:
                        staggered[phase] = staggered.get(phase, 0) + 1
            if phase not in first_start:
                first_start[phase] = index
                order.append(phase)
        elif kind == "result":
            phase = phase_of_key.get(key)
            label = label_of_key.get(key)
            if phase:
                last_result[phase] = index
                first_result.setdefault(phase, index)
            if label:
                results[label] = results.get(label, 0) + 1

    # The run's own order when it has one; a live run has only the journal.
    sequence = [t for t in (declared or []) if t in first_start] or order

    pipelined: list[dict[str, Any]] = []
    barriers: list[dict[str, Any]] = []
    for earlier, later in zip(sequence, sequence[1:]):
        if earlier not in last_result:
            continue
        if first_start[earlier] < first_start[later] < last_result[earlier]:
            pipelined.append({"from": earlier, "to": later})
        elif first_start[later] > last_result[earlier]:
            width = sum(1 for lb, ph in phase_of_label.items() if ph == earlier)
            if width >= FAN_OUT_MINIMUM:
                barriers.append({"from": earlier, "to": later, "width": width})

    reattempted: list[dict[str, Any]] = []
    for phase in sequence:
        subjects = [lb for lb, ph in phase_of_label.items() if ph == phase]
        retried = [lb for lb in subjects if starts[lb] > results.get(lb, 0)]
        attempts = sum(starts[lb] for lb in subjects)
        if retried and attempts > len(subjects):
            reattempted.append(
                {"phase": phase, "subjects": len(subjects), "attempts": attempts}
            )

    rolling: list[dict[str, Any]] = []
    for phase, count in staggered.items():
        total = sum(1 for lb, ph in phase_of_label.items() if ph == phase)
        if count >= 2 and total >= FAN_OUT_MINIMUM:
            rolling.append({"phase": phase, "staggered": count, "total": total})

    return {
        "pipelined": pipelined,
        "barriers": barriers,
        "reattempted": reattempted,
        "rolling": rolling,
    }


def _run_from_journal(session_dir: Path, run_id: str) -> dict[str, Any] | None:
    """Reconstruct a run that has no run file yet.

    Claude Code writes ``workflows/<run_id>.json`` when the run REACHES A
    TERMINAL POINT. While it is running there is only the sidecar tree, so
    discovering by run file alone was blind to exactly the case this reader
    exists for -- verified live on 2026-09-11 against a workflow whose agents
    were writing while no run file existed at all.

    The journal carries what the run file would: each ``started`` event names an
    agent, its label and its phase, and their order is the phase order.
    """
    sidecar_dir = session_dir / "subagents" / "workflows" / run_id
    events = _journal_events(sidecar_dir)
    if not events:
        return None
    phases: list[str] = []
    announced: list[dict[str, Any]] = []
    for event in events:
        if event.get("type") != "started":
            continue
        phase = str(event.get("phase") or "")
        label = str(event.get("label") or "")
        if phase and phase not in phases:
            phases.append(phase)
        if label:
            announced.append({"label": label, "phase": phase})
    sidecars, newest = _agent_sidecars(session_dir, run_id)
    agents = _agents({"workflowProgress": []}, sidecars, returned_agents(sidecar_dir))
    spawned = {a["label"] for a in agents}
    for item in announced:
        if item["label"] in spawned:
            continue
        spawned.add(item["label"])
        agents.append(
            {
                "agent_id": "",
                "label": item["label"],
                "phase": item["phase"],
                "model": "",
                "spawn_depth": None,
                "shape": "",
                "last_active_at": None,
                "last_step": "",
                "last_tool": "",
                "has_output": False,
                "returned": False,
            }
        )
    return {
        "run_id": run_id,
        # The run file holds the name; a live run has no run file, so the id is
        # the only handle there is. The page falls back to it rather than
        # inventing one.
        "name": "",
        "status": "running",
        "summary": "",
        "project": _project_path(session_dir.parent.name),
        "session_id": session_dir.name,
        "phases": [{"title": title, "detail": ""} for title in phases],
        "agents": agents,
        "tokens": None,
        "tool_calls": None,
        "duration_ms": None,
        "updated_at": max(newest, (sidecar_dir / "journal.jsonl").stat().st_mtime),
        "resumed_after_status": False,
        "ordering": read_ordering(sidecar_dir, phases),
        # A run reconstructed from its journal has not returned anything yet --
        # the result is written with the run file, when the run stops.
        "has_result": False,
        "result_preview": "",
        "result_chars": 0,
    }


def discover_runs(projects_root: Path | None = None) -> list[dict[str, Any]]:
    """Every readable run under *projects_root*, newest first.

    The default is resolved HERE rather than in the signature: a default
    argument binds at import, so a caller that relocates the projects root --
    a test, or a gateway whose data home has moved -- cannot reach it.
    """
    root = DEFAULT_PROJECTS_ROOT if projects_root is None else projects_root
    runs: list[dict[str, Any]] = []
    seen: set[str] = set()
    for run_file in root.glob("*/*/workflows/*.json"):
        run = read_run(run_file)
        if run is not None:
            runs.append(run)
            seen.add(run["run_id"])
    # A run whose file has not been written yet is reconstructed from its
    # journal. Second, so a finished run's own file always wins.
    for sidecar_dir in root.glob("*/*/subagents/workflows/*"):
        if not sidecar_dir.is_dir() or sidecar_dir.name in seen:
            continue
        run = _run_from_journal(sidecar_dir.parent.parent.parent, sidecar_dir.name)
        if run is not None:
            runs.append(run)
            seen.add(run["run_id"])
    runs.sort(key=lambda r: r["updated_at"], reverse=True)
    return runs


#: Idle time after which an agent is reported stale rather than live.
#:
#: Fifteen minutes, not five: an agent blocked on one long command writes nothing
#: while it waits, and at five minutes three agents sitting on "wait for the
#: regression sweep to finish" and "retry the push up to four times" were all
#: called dead while plainly working. The signal this must catch is the other
#: one -- the agents a gateway restart cut off on 2026-09-11 held the same mtime
#: for an hour while the run file still described them as the current wave.
STALE_AFTER_SECONDS = 900


def _agent_state(agent: dict[str, Any], now: float) -> tuple[str, str]:
    """The agent's liveness word and how long it has been silent."""
    last = agent.get("last_active_at")
    if not last:
        return "pending", ""
    idle = max(0.0, now - float(last))
    word = "live" if idle < STALE_AFTER_SECONDS else "stale"
    return word, f"{idle / 60:.1f}m"


#: Agents in a phase before it is described as a fan-out rather than a step.
#: Two is a pair, not a wave; three is the smallest count where "the work was
#: split" is a better description than "it took a couple of tries".
FAN_OUT_MINIMUM = 3


def _label_stem(label: str) -> str:
    """The part of an agent label that names its subject, not its role.

    Workflow labels are written ``role:subject`` (``implement:task5``,
    ``verify:task5``), which is what makes a verifier findable from the agent it
    verifies -- the role differs and the subject does not.
    """
    _, _, subject = label.partition(":")
    return (subject or label).strip()


def infer_shape(run: dict[str, Any]) -> dict[str, Any]:
    """Which composition patterns this run's own agents evidence.

    Read from the shape of the phases rather than from the script: a run records
    who it spawned and in which phase, and that is enough to recognise the common
    compositions -- a phase of siblings is a fan-out, one agent after many is the
    barrier that merges them, and a phase with one verifier per subject of an
    earlier phase is adversarial verification.

    Conservative by construction. Every claim names the phases that produced it,
    and a shape matching nothing claims nothing: a guess presented as a finding is
    worse here than silence, because the whole point is to describe a run the
    reader cannot see.
    """
    phases: list[dict[str, Any]] = []
    previous_count = 0
    for phase in run.get("phases") or []:
        title = str(phase.get("title") or "")
        members = [a for a in run.get("agents") or [] if a.get("phase") == title]
        if len(members) >= FAN_OUT_MINIMUM:
            pattern = "fan-out"
        elif len(members) == 1:
            # One agent after several is the barrier that merges them, which is a
            # different thing from a phase that simply has one agent in it.
            pattern = "synthesize" if previous_count > 1 else "single"
        elif members:
            pattern = "pair"
        else:
            pattern = "empty"
        previous_count = len(members)
        stems = [_label_stem(str(a.get("label") or "")) for a in members]
        phases.append(
            {
                "title": title,
                "detail": str(phase.get("detail") or ""),
                "count": len(members),
                # Agents answer "how many ran"; SUBJECTS answer "how many
                # distinct pieces of work", and a retry moves only the first.
                # Every relational rule below reads subjects, because a merge
                # that was retried is still a merge -- testing `count == 1`
                # hid the funnel on a generate-and-filter run, whose
                # single `synthesis` died on a quota limit and was restarted.
                "subjects": len(set(stems)),
                "pattern": pattern,
                "stems": stems,
            }
        )

    patterns: list[str] = []
    evidence: list[str] = []
    # Relationships are read between the phases that RAN. A phase that never ran
    # has no agents to relate to anything, and standing between two phases that
    # did, it hid the relationship they have with each other: generate-and-filter
    # run fanned out 6 then 8, skipped Filter, and
    # merged to one -- and was reported as a plain fan-out because the phase
    # immediately before the barrier was the empty one.
    active = [p for p in phases if p["count"]]
    ordering = run.get("ordering") or {}
    retried_phases = {r["phase"] for r in ordering.get("reattempted") or []}
    for index, phase in enumerate(active):
        distinct = phase["subjects"]
        if (
            phase["pattern"] == "fan-out"
            and distinct == 1
            and phase["title"] not in retried_phases
        ):
            # Several agents, one subject: they competed on it rather than
            # dividing it. Calling that a fan-out would describe the opposite
            # strategy, so the two are exclusive by construction.
            #
            # Unless they were RETRIES. Agents that all return are competing;
            # agents where only the last returns are one agent tried again, and
            # the journal is what tells the two apart.
            if "tournament" not in patterns:
                patterns.append("tournament")
                evidence.append(
                    f"{phase['count']} agents on one subject in {phase['title']}"
                )
        elif phase["pattern"] == "fan-out":
            if "fan-out" not in patterns:
                patterns.append("fan-out")
                evidence.append(f"{phase['count']} sibling agents in {phase['title']}")
        following = active[index + 1] if index + 1 < len(active) else None
        # A wave whose every subject EXTENDS the single subject before it is
        # working on that same thing -- implement:task8a then
        # review:task8a:security, :spec, :convergence. Reading subjects rather
        # than agents made this reachable for the first time, and without the
        # guard it claimed the run classified and routed when it reviewed.
        one_subject = next(iter(set(phase["stems"])), "")
        elaborates = following is not None and all(
            stem.startswith(one_subject) for stem in following["stems"]
        )
        if (
            following is not None
            and phase["subjects"] == 1
            and following["count"] >= FAN_OUT_MINIMUM
            and following["subjects"] > 1
            and not elaborates
        ):
            # One agent deciding, then a wave acting on what it decided. The
            # single agent has to come FIRST -- one after a wave is the barrier
            # that merges it, which is the opposite arrangement.
            if "classify-and-act" not in patterns:
                patterns.append("classify-and-act")
                evidence.append(
                    f"one agent in {phase['title']} ahead of {following['count']} in {following['title']}"
                )
        previous = active[index - 1] if index else None
        if not previous:
            continue
        # A composition describes the RUN, so the barrier does not have to sit
        # immediately after the widest phase: a run that fans out, narrows, and
        # ends with one agent is still fan-out-and-synthesize. Requiring
        # adjacency reported plain "fan-out" for a four-phase run that plainly
        # merged at the end (a four-phase run, 2026-09-11).
        fanned_out_earlier = any(
            p["subjects"] >= FAN_OUT_MINIMUM for p in active[:index]
        )
        if phase["subjects"] == 1 and previous["subjects"] > 1 and fanned_out_earlier:
            if "fan-out-and-synthesize" not in patterns:
                patterns.append("fan-out-and-synthesize")
                widest = max(active[:index], key=lambda p: p["count"])
                evidence.append(
                    f"{widest['title']} runs {widest['count']} agents, {phase['title']} merges to one"
                )
        shared = set(phase["stems"]) & set(previous["stems"])
        if phase["subjects"] and phase["subjects"] == previous["subjects"] and shared:
            if "adversarial-verification" not in patterns:
                patterns.append("adversarial-verification")
                evidence.append(
                    f"{phase['title']} runs one agent per {previous['title']} subject"
                )
    # Each stage producing more work than the one before it -- the arc a
    # generate-and-filter run traces. Distinct from fan-out, which says one
    # phase had siblings, and from the merge, which says where it collapsed:
    # this is the multiplication between them, and on
    # a generate-and-filter run it is the whole story (6, 9, 52).
    widening: list[dict[str, Any]] = []
    chain: list[dict[str, Any]] = []
    for phase in active:
        chain = chain + [phase] if chain and phase["subjects"] > chain[-1]["subjects"] else [phase]
        if len(chain) > len(widening):
            widening = list(chain)
    if len(widening) >= 3:
        patterns.append("widening")
        evidence.append(
            "each stage wider than the last: "
            + ", ".join(f"{p['title']} {p['subjects']}" for p in widening)
        )

    titles = [p["title"] for p in phases]
    if len(titles) != len(set(titles)):
        patterns.append("loop-until-done")
        evidence.append("a phase title repeats")

    # The claims below do NOT come from phase membership. They are read from
    # journal ORDER, which is the only place these differences are recorded,
    # and they are additive to the membership ones: a run that fans out and
    # then pipelines is both, and says both.
    for pair in ordering.get("pipelined") or []:
        if "pipeline" not in patterns:
            patterns.append("pipeline")
        evidence.append(f"{pair['to']} starts before {pair['from']} finishes")

    barriers = ordering.get("barriers") or []
    if barriers:
        # One line, for the widest wave that was waited on. A barrier at every
        # seam is the common case, and listing them all would bury the seam
        # that actually cost something.
        widest = max(barriers, key=lambda b: b["width"])
        patterns.append("barrier")
        evidence.append(
            f"{widest['to']} waits for all {widest['width']} of {widest['from']}"
        )

    for retry in ordering.get("reattempted") or []:
        if "re-attempt" not in patterns:
            patterns.append("re-attempt")
        evidence.append(
            f"{retry['phase']} ran {retry['subjects']} "
            f"{'subject' if retry['subjects'] == 1 else 'subjects'} "
            f"over {retry['attempts']} attempts"
        )

    for wave in ordering.get("rolling") or []:
        if "rolling" not in patterns:
            patterns.append("rolling")
        evidence.append(
            f"{wave['phase']} started {wave['staggered']} of its {wave['total']} "
            "only as earlier ones returned"
        )
    return {"phases": phases, "patterns": patterns, "evidence": evidence}


def _node_id(prefix: str, index: int) -> str:
    return f"{prefix}{index}"


def _mermaid_text(value: str, limit: int = 40) -> str:
    """A label Mermaid will accept inside a node.

    Quotes and brackets end a node early and the rest of the line becomes syntax,
    so a diagram with a stray quote does not render wrong -- it does not render.
    """
    cleaned = " ".join(str(value).split())[:limit]
    for char in '"[]{}()<>|':
        cleaned = cleaned.replace(char, "")
    return cleaned or "-"


def mermaid(run: dict[str, Any], now: float | None = None) -> str:
    """The run drawn as phases left to right, agents stacked inside each.

    Mermaid because it renders in the chat transcript itself, which after tonight
    is the only surface that reaches a phone: the dashboard is served over HTTPS
    and refuses to embed a loopback page.
    """
    now = time.time() if now is None else now
    shape = infer_shape(run)
    lines = ["flowchart LR"]
    live_nodes: list[str] = []
    previous_anchor = ""
    for p_index, phase in enumerate(shape["phases"]):
        anchor = _node_id("P", p_index)
        lines.append(f'  subgraph {anchor}["{_mermaid_text(phase["title"])}"]')
        members = [
            a for a in run.get("agents") or [] if a.get("phase") == phase["title"]
        ]
        if not members:
            lines.append(f'    {anchor}x[" "]')
        for a_index, agent in enumerate(members):
            node = _node_id(f"A{p_index}_", a_index)
            state, idle = _agent_state(agent, now)
            # Composed BEFORE cleaning, or the separator is stripped and the label
            # runs into the clock: "implement:task712.9m".
            display = f"{agent['label']} · {idle}" if idle else str(agent["label"])
            lines.append(f'    {node}["{_mermaid_text(display, 40)}"]')
            if state == "live":
                live_nodes.append(node)
        lines.append("  end")
        if previous_anchor:
            lines.append(f"  {previous_anchor} --> {anchor}")
        previous_anchor = anchor
    if live_nodes:
        lines.append("  classDef live stroke-width:3px")
        lines.append(f"  class {','.join(live_nodes)} live")
    return "\n".join(lines)


def _render(runs: list[dict[str, Any]], now: float | None = None) -> str:
    if not runs:
        return "no Claude workflow runs found"
    now = time.time() if now is None else now
    lines: list[str] = []
    for run in runs:
        flag = "  (agents spawned since)" if run["resumed_after_status"] else ""
        lines.append(
            f"{time.strftime('%Y-%m-%d %H:%M', time.localtime(run['updated_at']))}  "
            f"{run['run_id']}  {run['name']}  [{run['status']}]{flag}"
        )
        live = sum(1 for a in run["agents"] if _agent_state(a, now)[0] == "live")
        lines.append(
            f"    {run['project']}  session {run['session_id'][:8]}  "
            f"agents {len(run['agents'])} ({live} live)  "
            f"tokens={run['tokens']}  tools={run['tool_calls']}"
        )
        if run["phases"]:
            lines.append("    phases: " + " · ".join(p["title"] for p in run["phases"]))
        for agent in run["agents"]:
            word, idle = _agent_state(agent, now)
            detail = " ".join(
                part for part in (agent["phase"], agent["model"]) if part
            )
            lines.append(
                f"      {word:<7} {agent['label']:<28} {idle:>6}  {detail}".rstrip()
            )
            if agent["last_step"]:
                lines.append(f"              {agent['last_step']}")
            if agent["last_tool"]:
                lines.append(f"              -> {agent['last_tool']}")
    return "\n".join(lines)


def _html(runs: list[dict[str, Any]], refresh: int) -> str:
    """The same digest as a page, for the dashboard's Browser panel.

    Deliberately one static document with a meta refresh and no script: the panel
    is where this gets read, and a page that needs JavaScript to show its content
    is a page that can show nothing at all there.
    """
    now = time.time()
    rows: list[str] = []
    for run in runs:
        live = sum(1 for a in run["agents"] if _agent_state(a, now)[0] == "live")
        flag = " · agents spawned since" if run["resumed_after_status"] else ""
        rows.append(
            f"<h2>{_esc(run['name'])} <small>{_esc(run['run_id'])}</small></h2>"
            f"<p class=meta>{_esc(run['status'])}{_esc(flag)} · "
            f"{len(run['agents'])} agents, {live} live · "
            f"{run['tokens']} tokens · {run['tool_calls']} tool calls<br>"
            f"{_esc(run['project'])}</p>"
        )
        if run["phases"]:
            rows.append(
                "<p class=phases>"
                + " &middot; ".join(_esc(p["title"]) for p in run["phases"])
                + "</p>"
            )
        cells = []
        for agent in run["agents"]:
            word, idle = _agent_state(agent, now)
            cells.append(
                f"<tr class={word}><td>{word}</td><td>{_esc(agent['label'])}</td>"
                f"<td class=num>{idle}</td><td>{_esc(agent['phase'])}</td>"
                f"<td>{_esc(agent['model'])}</td></tr>"
                f"<tr class=step><td></td><td colspan=4>{_esc(agent['last_step'])}"
                f"{' &rarr; ' + _esc(agent['last_tool']) if agent['last_tool'] else ''}"
                "</td></tr>"
            )
        rows.append(
            "<table><tr><th>state<th>agent<th>idle<th>phase<th>model</tr>"
            + "".join(cells)
            + "</table>"
        )
    return (
        "<!doctype html><meta charset=utf-8>"
        f"<meta http-equiv=refresh content={refresh}>"
        "<title>Claude workflow runs</title>"
        "<style>"
        "body{font:14px/1.5 ui-sans-serif,system-ui;margin:2rem auto;max-width:60rem;"
        "color:#1a1a1a;background:#fff}"
        "@media(prefers-color-scheme:dark){body{color:#e6e6e6;background:#161616}"
        "table{border-color:#333}td,th{border-color:#333}}"
        "h2{margin:1.6rem 0 .2rem;font-size:1.05rem}"
        "h2 small{font-weight:400;opacity:.55;font-size:.85rem}"
        ".meta,.phases{margin:.2rem 0;opacity:.75}"
        "table{border-collapse:collapse;width:100%;margin:.6rem 0 1.4rem}"
        "td,th{border-bottom:1px solid #ddd;padding:.3rem .5rem;text-align:left}"
        "th{font-weight:600;opacity:.6;font-size:.8rem;text-transform:uppercase}"
        ".num{text-align:right;font-variant-numeric:tabular-nums}"
        "tr.live td:first-child{color:#158a3a;font-weight:600}"
        "tr.stale td:first-child{opacity:.5}"
        "tr.pending td:first-child{color:#8a6d15}"
        "tr.step td{border:0;padding:0 .5rem .4rem;opacity:.7;font-size:.9rem}"
        "</style>"
        f"<p class=meta>refreshes every {refresh}s &middot; "
        f"{time.strftime('%H:%M:%S', time.localtime(now))}</p>"
        + ("".join(rows) or "<p>no Claude workflow runs found</p>")
    )


def _esc(value: object) -> str:
    return html.escape(str(value), quote=True)


def _serve(port: int, projects_root: Path, limit: int, refresh: int) -> int:
    """Serve the digest on loopback only, for the dashboard's Browser panel."""

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self) -> None:  # noqa: N802 -- BaseHTTPRequestHandler's spelling
            body = _html(discover_runs(projects_root)[:limit], refresh).encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *_args: object) -> None:
            return

    server = HTTPServer(("127.0.0.1", port), Handler)
    print(f"serving on http://127.0.0.1:{port} (loopback only; Ctrl-C to stop)")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument(
        "--projects-root", type=Path, default=DEFAULT_PROJECTS_ROOT,
        help="where Claude Code keeps its per-project session directories",
    )
    parser.add_argument("--json", action="store_true", help="emit the joined records")
    parser.add_argument(
        "--mermaid", action="store_true",
        help="draw each run as a Mermaid flowchart instead of a table",
    )
    parser.add_argument(
        "--watch", type=int, metavar="SECONDS",
        help="re-read on this interval until interrupted",
    )
    parser.add_argument("--limit", type=int, default=10, help="most recent N runs")
    parser.add_argument(
        "--serve", type=int, metavar="PORT",
        help="serve the digest as a page on 127.0.0.1 for the Browser panel",
    )
    args = parser.parse_args(argv)

    if args.serve:
        return _serve(args.serve, args.projects_root, args.limit, args.watch or 10)

    while True:
        runs = discover_runs(args.projects_root)[: args.limit]
        if args.json:
            print(json.dumps(runs, indent=2))
        elif args.mermaid:
            for run in runs:
                shape = infer_shape(run)
                print(f"%% {run['name']}  {run['run_id']}")
                if shape['patterns']:
                    print('%% patterns: ' + ', '.join(shape['patterns']))
                    for note in shape['evidence']:
                        print(f'%%   {note}')
                print(mermaid(run))
                print()
        else:
            print(_render(runs))
        if not args.watch:
            return 0
        try:
            time.sleep(max(1, args.watch))
        except KeyboardInterrupt:
            return 0
        print()


if __name__ == "__main__":
    sys.exit(main())
