# Workflow Lens

**For Claude Code dynamic workflows.** When a Claude session runs a workflow, its
subagents work out of sight, and the KiroCrew chat shows none of them. This
[KiroCrew](https://github.com/kirodotdev/KiroCrew) app reads what Claude Code writes to
disk and shows the phases a run declared, which agents are live, what shape the run
took, and what it and each of its agents answered with.

![A generate-and-filter run: its phases, agents and the shape it took](assets/screenshots/shape.png)

The runs in these screenshots are invented for illustration.

## Who it is for

**Anyone whose Claude Code sessions run dynamic workflows**, whether started from the
Claude Code CLI or from a KiroCrew session backed by Claude. The app reads the
artifacts Claude Code writes under `~/.claude/projects` on the gateway's machine, so
on a machine with no workflow runs the page is empty.

## What it shows

- **Phases and agents** — a diagram of each phase and its agents, with live agents
  pulsing and stopped agents filled or hollow by whether they returned a result.
- **Declared plan** — what each phase was declared *for*, kept visibly apart from
  everything the run actually did.
- **Shape** — the composition the run evidences, each claim with the evidence line
  that produced it. Tap a chip for what it means.
- **Outputs** — the run's final result and each agent's own answer, rendered as
  markdown and fetched only when opened.
- **Launchers** — start a new workflow in a chosen shape, with the task filled in or
  the prompt copied.

## How a shape is decided

Two readings of the same artifacts, and every claim names what produced it:

- **Membership** — which agents ran in which phase: fan-out, tournament,
  classify-and-act, adversarial verification, the merge, widening.
- **Journal order** — when each agent started relative to the others returning:
  pipeline, barrier, re-attempt, rolling.

A shape the artifacts do not evidence is not claimed. Tree of thought can be started
from a launcher but is never detected, because no run has yet shown one.

The composition names come from the
[claudefa.st guide to Claude Code dynamic workflows](https://claudefa.st/blog/guide/development/ultracode-dynamic-workflows-agent-teams).
`pipeline` is a function of the Workflow script API; barrier, re-attempt, rolling and
widening are this app's own.

## Install

**Before you install:** the app has a Python backend that runs inside the gateway,
and KiroCrew refuses third-party executable code unless you allow it. Set this in
`~/.kiro/crew/config.json`:

```json
{ "agent": { "apps_allow_third_party": true } }
```

Without it, enabling the app fails with `app_execution_denied`.

Install from the App Store, or from a local clone:

```bash
git clone https://github.com/Premshay/kirocrew-workflow-lens.git
kirocrew app install ./kirocrew-workflow-lens
kirocrew app enable workflow-lens
```

The app is read-only: every route answers from disk, it makes no network calls, and
it declares no storage.

## Known limits

- **Backend changes do not load on a file copy.** The gateway caches an app's Python
  for its process lifetime. After updating `backend/`, touch the files and run
  `kirocrew app disable workflow-lens && kirocrew app enable workflow-lens`. UI changes
  need no reload.
- **Older runs report less.** Runs written before Claude Code recorded a phase on each
  journal event carry no phase attribution, so their shape reads as fan-out at most.
- **A run still in progress has no totals.** Claude Code writes token and tool-call
  counts when a run stops, so a live run shows them as not counted yet.

## Tests

The route tests import `aiohttp` and `kiro_crew`, so run them with KiroCrew's
environment:

```bash
/path/to/KiroCrew/.venv/bin/python -m pytest tests/ -q
```

## License

[MIT](LICENSE)
