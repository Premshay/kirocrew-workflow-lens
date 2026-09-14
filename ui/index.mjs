import { useState, useEffect, useCallback } from 'react'
import { jsx as _jsx, jsxs as _jsxs, Fragment as _Fragment } from 'react/jsx-runtime'
import { useChatLauncher } from '@kirocrew/app-sdk'
// The dashboard's own renderer, so an output reads the way the same report
// would read in chat -- headings, tables, fenced code, file-path chips. A host
// that predates the export leaves the binding undefined rather than throwing,
// so the pane checks before it renders and falls back to plain text.
// The specifier is `@kirocrew/app-sdk/ui`, NOT `@kirocrew/ui`: that is the name
// the dashboard's import map binds to the vendor stub, and an unmapped bare
// specifier does not degrade -- it fails the whole module graph, so the page
// would not load at all.
import { MarkdownRenderer } from '@kirocrew/app-sdk/ui'

const ACCENT = 'var(--accent, #7c3aed)'
const POLL_MS = 15000

const STATE_STYLE = {
  live: { bg: 'var(--ok-subtle, #d1fae5)', fg: 'var(--ok, #047857)', label: 'live' },
  stale: { bg: 'var(--bg-hover, #f3f4f6)', fg: 'var(--muted, #6b7280)', label: 'idle' },
  pending: { bg: 'var(--warn-subtle, #fef3c7)', fg: 'var(--warn, #b45309)', label: 'pending' },
}

// The page carries its own stylesheet rather than inline styles on every node,
// for three reasons the inline version could not reach: the run card's interior
// splits into a rail and an activity column at a CONTAINER width (a card in a
// side panel is not a card on a 27" monitor, and the viewport cannot tell them
// apart), hover and focus-visible states exist, and the type scale is stated
// once instead of per element.
//
// Sizes follow the dashboard's own scale -- 24px/700 page title, 13px body,
// 12px meta, 11px pills, radius var(--radius-lg) on cards -- because this page
// reading a step smaller than every KiroCrew page around it was the complaint
// that started this revision. `--muted-strong` is NOT used for secondary text:
// in the dark themes it resolves DARKER than `--muted` (#52525b on #181b22),
// so the label it was meant to strengthen came out fainter.
const CSS = `
.wfr{width:100%;box-sizing:border-box;padding:20px 24px 32px;color:var(--text);font-size:13px;line-height:1.5}
.wfr-head{display:flex;justify-content:space-between;align-items:center;gap:12px 16px;flex-wrap:wrap;margin-bottom:18px}
.wfr-head-side{display:flex;align-items:center;gap:10px;min-width:0;flex-wrap:wrap}
.wfr-title{margin:0;font-size:24px;font-weight:700;letter-spacing:-.01em;color:var(--text-strong,var(--text))}
.wfr-stamp{font-size:12px;color:var(--muted);font-variant-numeric:tabular-nums}
.wfr-pill{display:inline-flex;align-items:center;padding:3px 10px;border-radius:9999px;font-size:11px;font-weight:600;white-space:nowrap}
.wfr-btn{background:transparent;border:1px solid var(--border);color:${ACCENT};padding:6px 15px;border-radius:9999px;font:inherit;font-size:12px;font-weight:500;cursor:pointer;white-space:nowrap;transition:background .12s,border-color .12s}
.wfr-btn:hover:not(:disabled){background:var(--bg-hover);border-color:var(--border-hover,var(--border))}
.wfr-btn:disabled{color:var(--muted);cursor:default}
.wfr-btn:focus-visible{outline:2px solid ${ACCENT};outline-offset:2px}
.wfr-btn[aria-pressed="true"]{background:var(--accent-subtle,#e8d5f5);border-color:${ACCENT}}
.wfr-btn-solid{background:${ACCENT};color:var(--accent-fg,#fff);border-color:transparent}
.wfr-btn-solid:hover:not(:disabled){background:var(--accent-hover,${ACCENT});border-color:transparent}
.wfr-card{background:var(--card,var(--bg));border:1px solid var(--border);border-radius:var(--radius-lg,12px);padding:18px 20px;margin-bottom:14px;container-type:inline-size}
.wfr-card-head{display:flex;align-items:center;gap:10px;flex-wrap:wrap;margin-bottom:10px}
.wfr-run-name{font-size:15px;font-weight:600;color:${ACCENT};word-break:break-word}
.wfr-spacer{margin-left:auto}
.wfr-summary{font-size:13px;line-height:1.55;margin-bottom:12px;max-width:78ch}
.wfr-warn{font-size:12px;color:var(--danger,#b91c1c);margin-bottom:10px}
/* minmax(0,1fr), not 1fr: a bare 1fr column cannot shrink below its widest
   content, so one long code chip in an output pane widened the whole column past
   a phone's edge and clipped every line in it, the agent's step included. */
.wfr-split{display:grid;grid-template-columns:minmax(0,1fr);gap:16px 32px;align-items:start}
.wfr-split>*{min-width:0}
@container (min-width:820px){.wfr-split{grid-template-columns:minmax(288px,330px) minmax(0,1fr)}}
.wfr-facts{display:grid;grid-template-columns:auto 1fr;gap:4px 12px;font-size:12px;margin:0 0 14px}
.wfr-facts dt{color:var(--muted)}
.wfr-facts dd{margin:0;color:var(--text);font-variant-numeric:tabular-nums}
.wfr-path{font-family:var(--mono,ui-monospace,monospace);font-size:11px;word-break:break-all;line-height:1.4}
.wfr-meter{display:flex;align-items:center;gap:10px;margin:0 0 12px}
.wfr-meter-track{display:flex;gap:2px;flex:1;min-width:0}
.wfr-meter-seg{height:5px;flex:1;border-radius:3px}
.wfr-meter-count{font-size:12px;color:var(--muted);font-variant-numeric:tabular-nums;white-space:nowrap}
.wfr-shape{display:flex;gap:8px;flex-wrap:wrap;align-items:center;margin:14px 0 0;font-size:12px}
.wfr-chip{display:inline-flex;align-items:center;padding:3px 10px;border-radius:9999px;border:0;font:inherit;font-size:11px;font-weight:600;white-space:nowrap;cursor:pointer;text-decoration:underline dotted;text-underline-offset:3px;text-decoration-thickness:1px}
.wfr-chip:hover{text-decoration-style:solid}
.wfr-chip:focus-visible{outline:2px solid ${ACCENT};outline-offset:2px}
.wfr-chip[aria-expanded="true"]{box-shadow:inset 0 0 0 1px currentColor;text-decoration-style:solid}
/* A pill sized for reading is about 22px tall, which is a poor target for a
   finger. Coarse pointers get a taller one; the row still wraps, so the rail
   absorbs it. Sized by INPUT rather than by viewport -- a tablet in landscape
   is a wide screen that is still touched. */
@media (pointer:coarse){.wfr-chip{min-height:34px;padding:8px 14px;font-size:12px}
.wfr-shape{gap:10px}}
.wfr-chip-note{margin:10px 0 0;padding:10px 12px;border-radius:var(--radius-md,8px);background:var(--bg);border:1px solid var(--border);font-size:12px;line-height:1.55;color:var(--text);max-width:62ch}
.wfr-chip-note b{color:var(--text-strong,var(--text))}
.wfr-shape-label{color:var(--muted)}
.wfr-evidence{margin:7px 0 0;padding:0 0 0 15px;font-size:12px;color:var(--muted);line-height:1.55}
.wfr-declared{margin:16px 0 0;padding-top:12px;border-top:1px dashed var(--border)}
.wfr-plan{display:grid;grid-template-columns:auto 1fr;gap:4px 12px;margin:7px 0 0;font-size:12px;line-height:1.5}
.wfr-plan dt{color:var(--text);font-weight:600;white-space:nowrap}
.wfr-plan dd{margin:0;color:var(--muted)}
.wfr-agent{padding:10px 0;border-bottom:1px solid var(--border)}
.wfr-agent:first-child{padding-top:0}
.wfr-agent:last-child{border-bottom:0;padding-bottom:2px}
.wfr-agent-head{display:flex;align-items:center;gap:9px;flex-wrap:wrap}
.wfr-agent-name{font-size:13px;font-weight:600;color:var(--text-strong,var(--text))}
.wfr-agent-attempt{font-weight:400;color:var(--muted)}
.wfr-agent-idle{margin-left:auto;font-size:12px;color:var(--muted);font-variant-numeric:tabular-nums}
.wfr-step{font-size:13px;color:var(--text);margin-top:4px;line-height:1.5;max-width:80ch;overflow-wrap:anywhere}
.wfr-tool{font-size:12px;margin-top:3px;font-family:var(--mono,ui-monospace,monospace);color:var(--text);word-break:break-word}
.wfr-tool span{color:var(--muted);font-family:inherit}
.wfr-bar{height:4px;max-width:260px;background:var(--border);border-radius:2px;margin-top:7px;overflow:hidden}
.wfr-bar i{display:block;height:100%;border-radius:2px}
.wfr-more{font-size:12px;color:var(--muted);padding-top:10px}
.wfr-result{margin-top:16px;border-top:1px solid var(--border);padding-top:13px}
.wfr-result-head{display:flex;align-items:center;gap:10px;flex-wrap:wrap}
.wfr-result-title{font-size:13px;font-weight:600;color:var(--text-strong,var(--text))}
.wfr-result-meta{font-size:12px;color:var(--muted);font-variant-numeric:tabular-nums}
.wfr-result-preview{font-size:13px;color:var(--muted);margin-top:6px;white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
.wfr-result-pane{margin:11px 0 0;max-height:min(52vh,460px);overflow:auto;overscroll-behavior:contain;touch-action:pan-y;user-select:text;background:var(--bg);border:1px solid var(--border);border-radius:var(--radius-md,8px);padding:12px 14px;font-family:var(--mono,ui-monospace,monospace);font-size:12px;line-height:1.6;white-space:pre-wrap;word-break:break-word;color:var(--text)}
.wfr-md-pane{margin:11px 0 0;max-height:min(52vh,460px);overflow:auto;overscroll-behavior:contain;touch-action:pan-y;user-select:text;background:var(--bg);border:1px solid var(--border);border-radius:var(--radius-md,8px);padding:12px 16px;font-size:13px;line-height:1.6;color:var(--text)}
.wfr-md-pane{overflow-wrap:anywhere}
/* What cannot wrap scrolls inside the pane instead of widening the page. */
.wfr-md-pane pre,.wfr-md-pane table{display:block;max-width:100%;overflow-x:auto}
.wfr-md-pane>*:first-child{margin-top:0}
.wfr-md-pane>*:last-child{margin-bottom:0}
.wfr-linkbtn{background:none;border:0;padding:0;margin:0;font:inherit;font-size:12px;color:${ACCENT};cursor:pointer;white-space:nowrap}
.wfr-linkbtn:hover{text-decoration:underline}
.wfr-linkbtn:focus-visible{outline:2px solid ${ACCENT};outline-offset:2px;border-radius:3px}
.wfr-agent-output{margin-top:2px}
.wfr-note{font-size:13px;color:var(--muted);padding:24px 0}
.wfr-error{background:var(--danger-subtle,#fee2e2);color:var(--danger,#b91c1c);padding:10px 14px;border-radius:var(--radius-md,8px);font-size:13px;margin-bottom:14px}
.wfr-shapes{display:grid;grid-template-columns:repeat(auto-fit,minmax(260px,1fr));gap:12px;margin-top:14px}
.wfr-shape-card{border:1px solid var(--border);border-radius:var(--radius-md,8px);padding:13px 14px;display:flex;flex-direction:column}
.wfr-shape-actions{display:flex;align-items:center;gap:12px;margin-top:auto;flex-wrap:wrap}
.wfr-task{display:block;margin-top:14px}
.wfr-task span{display:block;font-size:12px;color:var(--muted);margin-bottom:5px}
.wfr-task textarea{box-sizing:border-box;width:100%;background:var(--bg);border:1px solid var(--border);border-radius:var(--radius-md,8px);color:var(--text);padding:9px 11px;font:inherit;font-size:13px;line-height:1.5;resize:vertical}
.wfr-task textarea:focus-visible{outline:2px solid ${ACCENT};outline-offset:1px}
.wfr-shape-name{font-size:13px;font-weight:600;color:var(--text-strong,var(--text))}
.wfr-shape-blurb{font-size:12px;color:var(--muted);margin:5px 0 11px;line-height:1.5}
.wfr-flow{overflow-x:auto}
.wfr-flow svg{display:block;overflow:visible}
@keyframes wfpulse{0%,100%{opacity:1}50%{opacity:.35}}
.wfr-live-dot{animation:wfpulse 1.8s ease-in-out infinite}
@media (prefers-reduced-motion:reduce){.wfr-live-dot{animation:none}}
`

// A run whose agents have all stopped speaks in ONE tense. Three vocabularies on
// one card -- "killed" on the run, "idle" on every agent, and a forward arrow on
// the command -- left a reader unable to tell "paused between steps" from
// "stopped for good" (design critique, 2026-09-11).
const TERMINAL_STATUSES = ['killed', 'failed', 'error', 'aborted']

function runIsOver(run) {
  return TERMINAL_STATUSES.includes(run.status) && !run.live_agents
}

// Liveness is an idle-time reading, so on its own it can only say "not writing
// any more" -- which painted every agent of a finished run the same grey,
// whether it delivered its finding or died on a quota limit. Whether an agent
// RETURNED is a fact the journal records, and it outranks idleness once the
// agent has stopped writing.
function agentMark(agent, over) {
  if (agent.state === 'live') {
    const live = STATE_STYLE.live
    return { word: 'live', bg: live.bg, fg: live.fg, fill: live.fg, pulse: true }
  }
  if (agent.returned) {
    return {
      word: 'returned',
      bg: 'var(--accent-subtle, #e8d5f5)', fg: ACCENT, fill: ACCENT, pulse: false,
    }
  }
  const idle = STATE_STYLE.stale
  return {
    word: over ? 'no result' : 'idle',
    bg: idle.bg, fg: idle.fg, fill: 'var(--bg)', pulse: false,
  }
}

// Two agents legitimately share a label when a resume re-runs the same task, and
// nothing on the row told them apart. Oldest activity is attempt #1.
function withAttempts(agents) {
  const counts = {}
  for (const a of agents) counts[a.label] = (counts[a.label] || 0) + 1
  const order = [...agents]
    .filter(a => counts[a.label] > 1)
    .sort((x, y) => (x.last_active_at || 0) - (y.last_active_at || 0))
  const attempt = new Map()
  const seen = {}
  for (const a of order) {
    seen[a.label] = (seen[a.label] || 0) + 1
    attempt.set(a, seen[a.label])
  }
  return agents.map(a => (attempt.has(a) ? { ...a, attempt: attempt.get(a) } : a))
}

// `title` is a fourth ARGUMENT, not a style override: the earlier signature
// spread it into the style object, so both tooltips on this page -- the one
// explaining a stale run file and the one explaining a composition pattern --
// were silently dropped by the DOM.
function pill(text, bg, fg, title) {
  return _jsx('span', {
    className: 'wfr-pill',
    style: { background: bg, color: fg },
    title: title || undefined,
    children: text,
  })
}

// A shape name is jargon until something says what it means, and the `title`
// attribute says it only to a mouse -- on a phone the explanation did not
// exist. So the chip is a real button that discloses its note in place: no
// hover, no floating layer to collide with the card's own scrolling, and the
// note lands in the flow where a narrow rail still has room for it.
function explainChip({ key, label, open, onToggle, bg, fg, id }) {
  return _jsx('button', {
    type: 'button',
    className: 'wfr-chip',
    onClick: onToggle,
    'aria-expanded': open,
    'aria-controls': open ? id : undefined,
    style: { background: bg, color: fg },
    children: label,
  }, key)
}

function noteBlock(label, note, id) {
  return _jsxs('div', {
    className: 'wfr-chip-note', id, role: 'note',
    children: [_jsx('b', { children: label }), `: ${note}`],
  })
}

function ghostButton(label, onClick, busy, pressed) {
  return _jsx('button', {
    onClick, disabled: busy, className: 'wfr-btn',
    'aria-pressed': pressed === undefined ? undefined : !!pressed,
    children: label,
  })
}

// ── Visual layer ────────────────────────────────────────────────────────────
// Structural diagram, not a chart: the job is "how was this run put together and
// who is moving", so the forms are a flow and two magnitude meters. Status colors
// carry state and ALWAYS ride with their word -- never color alone. Text keeps
// text tokens; only the marks are colored.

const DOT_R = 6.5
const ROW_H = 40
const DOT_GAP = 22
const LANE_X = 104

// The subject an agent worked on, which is what makes a verifier findable from
// the agent it verifies: workflow labels are written role:subject, so
// "verify:task5" and "implement:task5" share a subject and differ in role.
function stemOf(label) {
  const i = String(label || '').indexOf(':')
  return (i === -1 ? String(label || '') : String(label).slice(i + 1)).trim()
}

// Edges are drawn ONLY where the artifacts evidence a relationship. A run whose
// phases share no subjects gets no edges rather than a decorative mesh -- the
// diagram would otherwise assert a structure nobody recorded.
function phaseEdges(prev, next) {
  const edges = []
  if (!prev.length || !next.length) return edges
  if (next.length === 1 && prev.length > 1) {
    // Everything converges on the one agent that follows them: a merge.
    prev.forEach((_, i) => edges.push([i, 0]))
    return edges
  }
  next.forEach((n, j) => {
    const stem = stemOf(n.label)
    prev.forEach((pv, i) => {
      if (stem && stemOf(pv.label) === stem) edges.push([i, j])
    })
  })
  if (!edges.length && prev.length === 1) {
    // One agent ahead of a wave: it routed to all of them.
    next.forEach((_, j) => edges.push([0, j]))
  }
  return edges
}

// A phase wider than this wraps onto another row of dots.
//
// The diagram is drawn to an intrinsic size and then scaled to the rail, so a
// single row of 17 agents was 570px of artwork inside a 280px column -- and
// uniform scaling shrank the phase TITLES along with the dots, to about six
// pixels. Wrapping holds the intrinsic width at five dots whatever the run
// does, so the drawing never has to be shrunk to fit and every run's diagram
// is the same size as every other run's.
const DOTS_PER_ROW = 5
const DOT_ROW_H = 22
//: Room to the right of the widest dot row for the "N agents" count.
const TAIL_X = 74

function PhaseFlow({ run }) {
  const phases = run.phase_shape || []
  if (!phases.length) return null
  const byPhase = phases.map(p => (run.agents || []).filter(a => a.phase === p.title))
  // A phase is as tall as the rows of dots it holds; the lane below it starts a
  // constant distance under the LAST of them, so the gap between phases reads
  // the same whether a phase has two agents or twenty.
  const dotRows = byPhase.map(m => Math.max(1, Math.ceil(m.length / DOTS_PER_ROW)))
  const tops = []
  let cursor = 20
  dotRows.forEach(rows => {
    tops.push(cursor)
    cursor += (rows - 1) * DOT_ROW_H + ROW_H
  })
  const height = cursor - ROW_H + 32
  const widest = Math.min(Math.max(1, ...byPhase.map(m => m.length)), DOTS_PER_ROW)
  const width = LANE_X + widest * DOT_GAP + TAIL_X
  const dotX = (j) => LANE_X + 8 + (j % DOTS_PER_ROW) * DOT_GAP
  const dotY = (i, j) => tops[i] + Math.floor(j / DOTS_PER_ROW) * DOT_ROW_H

  const edges = []
  const marks = []
  const labels = []

  phases.forEach((p, i) => {
    const members = byPhase[i]
    const started = members.length > 0
    labels.push(_jsx('text', {
      x: LANE_X - 14, y: tops[i] + 4, textAnchor: 'end',
      style: { fontSize: '12px', fill: started ? 'var(--text)' : 'var(--muted)', fontWeight: started ? 600 : 400 },
      children: p.title,
    }, `t${i}`))

    if (i < phases.length - 1) {
      const pair = phaseEdges(members, byPhase[i + 1])
      const lastRowY = dotY(i, Math.max(0, members.length - 1))
      if (pair.length) {
        // A drawn relationship replaces the plain spine for that gap. Each
        // curve leaves its OWN dot, which may sit on any wrapped row, so the
        // control points are the midpoint of the two it joins rather than a
        // fixed offset from the lane.
        pair.forEach(([from, to], k) => {
          const y1 = dotY(i, from) + DOT_R + 1
          const y2 = dotY(i + 1, to) - DOT_R - 1
          const mid = (y1 + y2) / 2
          edges.push(_jsx('path', {
            d: `M ${dotX(from)} ${y1} C ${dotX(from)} ${mid}, ${dotX(to)} ${mid}, ${dotX(to)} ${y2}`,
            fill: 'none', stroke: 'var(--border)', strokeWidth: 1.5, strokeLinecap: 'round',
          }, `e${i}-${k}`))
        })
      } else {
        // On the dot column, not beside it. Drawn at `LANE_X - 4` the
        // connector sat to the LEFT of every circle it joined, so a phase pair
        // with no evidenced relationship looked like it belonged to a
        // different column than the agents above and below it.
        edges.push(_jsx('line', {
          x1: dotX(0), y1: (started ? lastRowY : tops[i]) + DOT_R + 3,
          x2: dotX(0), y2: dotY(i + 1, 0) - DOT_R - 3,
          stroke: 'var(--border)', strokeWidth: 2, strokeLinecap: 'round',
        }, `c${i}`))
      }
    }

    if (!started) {
      labels.push(_jsx('text', {
        x: LANE_X + 4, y: tops[i] + 4,
        style: { fontSize: '11px', fill: 'var(--muted)' },
        children: runIsOver(run) ? 'never ran' : 'not started',
      }, `n${i}`))
      return
    }

    members.forEach((a, j) => {
      const mark = agentMark(a, runIsOver(run))
      marks.push(_jsxs('g', {
        children: [
          _jsx('title', { children: `${a.label} — ${mark.word}${a.idle ? ', idle ' + a.idle : ''}${a.model ? ', ' + a.model : ''}` }),
          _jsx('circle', {
            cx: dotX(j), cy: dotY(i, j), r: DOT_R,
            fill: mark.fill, stroke: mark.fg, strokeWidth: 2,
            className: mark.pulse ? 'wfr-live-dot' : undefined,
          }),
        ],
      }, `d${i}-${j}`))
    })
    labels.push(_jsx('text', {
      // NOT dotX(): that wraps on the modulo, so a full row of five put the
      // count back at column zero, on top of the first dot.
      x: LANE_X + 8 + Math.min(members.length, DOTS_PER_ROW) * DOT_GAP + 4, y: tops[i] + 4,
      style: { fontSize: '11px', fill: 'var(--muted)' },
      children: `${members.length} ${members.length === 1 ? 'agent' : 'agents'}`,
    }, `k${i}`))
  })

  return _jsx('div', {
    className: 'wfr-flow',
    children: _jsx('svg', {
      viewBox: `0 0 ${width} ${height}`, width: '100%',
      style: { maxWidth: `${width}px` },
      role: 'img', 'aria-label': `phases: ${phases.map(p => `${p.title} ${p.count}`).join(', ')}`,
      children: [...edges, ...marks, ...labels],
    }),
  })
}

function ProgressMeter({ phases }) {
  if (!phases || !phases.length) return null
  const started = phases.filter(p => p.count > 0).length
  return _jsxs('div', {
    className: 'wfr-meter',
    children: [
      _jsx('div', {
        className: 'wfr-meter-track',
        children: phases.map((p, i) => _jsx('div', {
          className: 'wfr-meter-seg',
          style: { background: p.count > 0 ? ACCENT : 'var(--border)' },
        }, i)),
      }),
      _jsxs('span', {
        className: 'wfr-meter-count',
        children: [started, ' / ', phases.length, ' phases'],
      }),
    ],
  })
}

function IdleBar({ agent, maxIdle }) {
  const mins = parseFloat(agent.idle) || 0
  const frac = maxIdle > 0 ? Math.max(0.02, Math.min(1, mins / maxIdle)) : 0
  const mark = agentMark(agent, false)
  return _jsx('div', {
    className: 'wfr-bar',
    children: _jsx('i', {
      style: {
        width: `${frac * 100}%`,
        background: agent.state === 'live' ? mark.fg : 'var(--muted)',
        opacity: agent.state === 'live' ? 1 : 0.45,
      },
    }),
  })
}

function AgentRow({ run, agent, maxIdle, over }) {
  const mark = agentMark(agent, over)
  return _jsxs('div', {
    className: 'wfr-agent',
    children: [
      _jsxs('div', {
        className: 'wfr-agent-head',
        children: [
          pill(mark.word, mark.bg, mark.fg),
          _jsxs('span', {
            className: 'wfr-agent-name',
            children: [
              agent.label || '(unnamed)',
              agent.attempt ? _jsx('span', { className: 'wfr-agent-attempt', children: ` · #${agent.attempt}` }) : null,
            ],
          }),
          agent.model ? pill(agent.model, 'var(--bg)', 'var(--muted)') : null,
          _jsx('span', { className: 'wfr-agent-idle', children: agent.idle || '' }),
        ],
      }),
      agent.last_step ? _jsx('div', { className: 'wfr-step', children: agent.last_step }) : null,
      agent.last_tool ? _jsxs('div', {
        className: 'wfr-tool',
        children: [
          // The agent's own liveness, not the run's: a completed run is not
          // "over" in the stopped sense, and its returned agents read as still
          // running their last tool.
          _jsx('span', { children: agent.state === 'live' ? 'running ' : 'was running ' }),
          agent.last_tool,
        ],
      }) : null,
      _jsx(IdleBar, { agent, maxIdle }),
      agent.has_output && agent.agent_id ? _jsx(AgentOutput, { run, agent }) : null,
    ],
  })
}

// One fetch-on-open output pane, used by the run's own result and by each
// agent's. They answer different questions -- what the workflow returned, and
// what one agent inside it answered with -- but the mechanics are identical:
// announce it in the list, fetch the text when a reader asks, render it in a
// bounded pane that scrolls and selects like the transcript does.
function useOutput(url) {
  const [open, setOpen] = useState(false)
  const [text, setText] = useState('')
  const [chars, setChars] = useState(0)
  const [error, setError] = useState('')
  const [busy, setBusy] = useState(false)

  const toggle = useCallback(async () => {
    if (open) { setOpen(false); return }
    setOpen(true)
    if (text || busy) return
    setBusy(true)
    setError('')
    try {
      const resp = await fetch(url)
      if (!resp.ok) setError(`gateway answered ${resp.status}`)
      else {
        const body = await resp.json()
        setText(body.text || '')
        setChars(body.chars || 0)
      }
    } catch (e) {
      setError(String(e && e.message ? e.message : e))
    }
    setBusy(false)
  }, [open, text, busy, url])

  return { open, text, chars, error, busy, toggle }
}

function OutputBody({ text, busy, error, empty }) {
  if (busy) return _jsx('div', { className: 'wfr-result-preview', children: 'Reading…' })
  if (error) return _jsx('div', { className: 'wfr-error', style: { marginTop: '10px', marginBottom: 0 }, children: error })
  if (!text) return _jsx('div', { className: 'wfr-result-preview', children: empty })
  // Markdown because that is the form the content arrives in -- the reports
  // agents write are markdown, and the reader renders their headings, tables
  // and fenced blocks instead of showing the syntax.
  return MarkdownRenderer
    ? _jsx('div', { className: 'wfr-md-pane', tabIndex: 0, children: _jsx(MarkdownRenderer, { content: text }) })
    : _jsx('pre', { className: 'wfr-result-pane', tabIndex: 0, children: text })
}

// What the workflow RETURNED, which is the one thing a reader of a finished run
// actually wants and the page never showed. Read from the run file's own
// `result` field -- the same artifact this app already reads for phases and
// tokens -- so nothing new is written, logged or kept anywhere to support it.
function ResultPane({ run }) {
  const url = `/api/apps/workflow-lens/runs/${encodeURIComponent(run.run_id)}/result`
  const { open, text, error, busy, toggle } = useOutput(url)

  return _jsxs('div', {
    className: 'wfr-result',
    children: [
      _jsxs('div', {
        className: 'wfr-result-head',
        children: [
          _jsx('span', { className: 'wfr-result-title', children: 'Final output' }),
          _jsx('span', {
            className: 'wfr-result-meta',
            children: `${(run.result_chars || 0).toLocaleString()} characters`,
          }),
          _jsx('span', {
            className: 'wfr-spacer',
            children: _jsx('button', {
              onClick: toggle, className: 'wfr-btn', 'aria-expanded': open,
              children: open ? 'Hide output' : 'Read output',
            }),
          }),
        ],
      }),
      !open && run.result_preview ? _jsx('div', { className: 'wfr-result-preview', children: run.result_preview }) : null,
      open ? _jsx(OutputBody, { text, busy, error, empty: 'The run returned nothing.' }) : null,
    ],
  })
}

// An agent's own answer. Worth its own control rather than always-on: on a
// fourteen-agent run every row carrying a report would bury the run, and on a
// KILLED run -- which has no result of its own at all -- these are the only
// thing that survives it.
function AgentOutput({ run, agent }) {
  const url = `/api/apps/workflow-lens/runs/${encodeURIComponent(run.run_id)}/agents/${encodeURIComponent(agent.agent_id)}/output`
  const { open, text, chars, error, busy, toggle } = useOutput(url)

  return _jsxs('div', {
    className: 'wfr-agent-output',
    children: [
      _jsxs('button', {
        onClick: toggle, className: 'wfr-linkbtn', 'aria-expanded': open,
        // Short in the row, specific to a screen reader: every agent carries
        // this control, so six rows of "Read this agent's output" was six
        // repetitions of one phrase -- but "Read output" alone, announced out
        // of context, does not say whose.
        'aria-label': `${open ? 'Hide' : 'Read'} the output of ${agent.label || 'this agent'}`,
        children: [
          open ? 'Hide output' : 'Read output',
          open && chars ? _jsx('span', { style: { color: 'var(--muted)' }, children: ` · ${chars.toLocaleString()} characters` }) : null,
        ],
      }),
      open ? _jsx(OutputBody, { text, busy, error, empty: 'This agent wrote nothing back.' }) : null,
    ],
  })
}

function RunCard({ run, expanded, onToggle }) {
  // One note open at a time, per card: six chips each with their own open
  // panel would push the agent rows off the screen on a phone. The status
  // chip shares the state, so opening one closes the other.
  const [openNote, setOpenNote] = useState('')
  const noteId = `wfr-note-${run.run_id}`
  const statusNoteId = `wfr-status-note-${run.run_id}`
  const declared = (run.phase_shape || []).filter(p => p.detail)
  const terminal = TERMINAL_STATUSES.includes(run.status)
  // A run file is written when the run STOPS, so a resumed run keeps saying
  // "completed" or "killed" while its agents work. Observation beats the file in
  // both directions: an agent writing seconds ago means the run is running,
  // whatever its file still claims.
  const statusWord = run.live_agents ? 'running' : (run.status || 'unknown')
  const statusTitle = run.live_agents && run.status && run.status !== 'running'
    ? `an agent wrote seconds ago, so this run is going. Its file still says "${run.status}" because that file is written when a run STOPS, and a resume does not rewrite it.`
    : ''
  const statusBg = run.live_agents ? 'var(--ok-subtle, #d1fae5)' : terminal ? 'var(--danger-subtle, #fee2e2)' : 'var(--bg-hover, #f3f4f6)'
  const statusFg = run.live_agents ? 'var(--ok, #047857)' : terminal ? 'var(--danger, #b91c1c)' : 'var(--muted, #6b7280)'
  const over = runIsOver(run)
  const agents = withAttempts(run.agents)
  const neverRan = (run.phase_shape || []).filter(p => !p.count).length
  const shown = expanded ? agents : agents.slice(0, 4)
  const maxIdle = Math.max(0, ...agents.map(a => parseFloat(a.idle) || 0))

  // Facts read as a labelled table rather than three run-together lines: in the
  // rail they are what the eye scans down, and a token count wants its own cell
  // to align on. Totals are written with the run file, when a run stops, so a
  // run still going has none yet -- and printing that absence as 0 claimed it
  // had done no work.
  const counted = (value) => (value == null ? 'not counted yet' : Number(value).toLocaleString())
  const facts = [
    ['Agents', over ? `${agents.length}, none still running` : `${agents.length}, ${run.live_agents} live`],
    ['Tokens', counted(run.tokens)],
    ['Tool calls', counted(run.tool_calls)],
  ]

  return _jsxs('div', {
    className: 'wfr-card',
    children: [
      _jsxs('div', {
        className: 'wfr-card-head',
        children: [
          _jsx('span', { className: 'wfr-run-name', children: run.name || run.run_id }),
          // The status carries a note only when the file and the agents
          // disagree, so it is a chip exactly then and a plain pill otherwise:
          // a control that does nothing when pressed is worse than no control.
          statusTitle
            ? explainChip({
                key: 'status', label: statusWord,
                open: openNote === 'status',
                onToggle: () => setOpenNote(v => (v === 'status' ? '' : 'status')),
                bg: statusBg, fg: statusFg, id: statusNoteId,
              })
            : pill(statusWord, statusBg, statusFg),
          run.resumed_after_status && !run.live_agents ? pill('resumed after it stopped', 'var(--warn-subtle, #fef3c7)', 'var(--warn, #b45309)') : null,
          _jsx('span', { className: 'wfr-spacer', children: ghostButton(expanded ? 'Collapse' : `All ${agents.length} agents`, onToggle, false) }),
        ],
      }),
      // Beside its own chip rather than down in the rail: a note that appears
      // far from the control that opened it reads as unrelated.
      openNote === 'status' && statusTitle
        ? noteBlock(statusWord, statusTitle, statusNoteId)
        : null,
      run.summary ? _jsx('div', { className: 'wfr-summary', children: run.summary }) : null,
      over && neverRan ? _jsx('div', {
        className: 'wfr-warn',
        children: `stopped · ${neverRan} of ${run.phase_shape.length} phases never ran`,
      }) : null,
      _jsxs('div', {
        className: 'wfr-split',
        children: [
          // Left: how the run was put together. Right: who is moving in it.
          // The two answer different questions and the wide side belongs to the
          // one that is still changing.
          _jsxs('div', {
            children: [
              _jsxs('dl', {
                className: 'wfr-facts',
                children: [
                  ...facts.flatMap(([k, v], i) => [
                    _jsx('dt', { children: k }, `k${i}`),
                    _jsx('dd', { children: v }, `v${i}`),
                  ]),
                  run.project ? _jsx('dt', { children: 'Project' }, 'kp') : null,
                  run.project ? _jsx('dd', { className: 'wfr-path', children: run.project }, 'vp') : null,
                ],
              }),
              _jsx(ProgressMeter, { phases: run.phase_shape }),
              _jsx(PhaseFlow, { run }),
              // What each phase was FOR, which the reader has always parsed and
              // the page never showed. Every one of the 196 phases on this
              // machine declares it, so it is reliably there.
              //
              // Kept visibly apart from everything above it, and labelled
              // DECLARED, because it is the only thing on this card the run did
              // not do: it is what the script said it would do, and the two can
              // disagree -- a run killed at 2 of 4 phases never reached the
              // intent the other two declare.
              declared.length ? _jsxs('div', {
                className: 'wfr-declared',
                children: [
                  _jsx('div', { className: 'wfr-shape-label', children: 'Declared' }),
                  _jsx('dl', {
                    className: 'wfr-plan',
                    children: declared.flatMap((p, i) => [
                      _jsx('dt', { children: p.title }, `pt${i}`),
                      _jsx('dd', { children: p.detail }, `pd${i}`),
                    ]),
                  }),
                ],
              }) : null,
              // Directly beneath the diagram it names. The composition is a
              // property of how the run was ASSEMBLED, so it belongs with the
              // phases; sitting above the agent rows it read as a header for
              // the activity feed, which it never was.
              run.patterns && run.patterns.length ? _jsxs('div', {
                className: 'wfr-shape',
                children: [
                  _jsx('span', { className: 'wfr-shape-label', children: run.patterns.length > 1 ? 'Composition' : 'Shape' }),
                  ...run.patterns.map(p => explainChip({
                    key: p,
                    label: shapeInfo(p).label || p,
                    open: openNote === p,
                    onToggle: () => setOpenNote(v => (v === p ? '' : p)),
                    bg: OBSERVED[p] ? 'var(--bg)' : 'var(--accent-subtle, #e8d5f5)',
                    fg: OBSERVED[p] ? 'var(--muted)' : ACCENT,
                    id: noteId,
                  })),
                ],
              }) : null,
              openNote && shapeInfo(openNote).note
                ? noteBlock(shapeInfo(openNote).label || openNote, shapeInfo(openNote).note, noteId)
                : null,
              run.evidence && run.evidence.length ? _jsx('ul', {
                className: 'wfr-evidence',
                children: run.evidence.map((e, i) => _jsx('li', { children: e }, i)),
              }) : null,
            ],
          }),
          _jsxs('div', {
            children: [
              _jsx('div', { children: shown.map((a, i) => _jsx(AgentRow, { run, agent: a, maxIdle, over }, a.agent_id || i)) }),
              !expanded && agents.length > shown.length ? _jsx('div', {
                className: 'wfr-more',
                children: `+ ${agents.length - shown.length} more`,
              }) : null,
            ],
          }),
        ],
      }),
      run.has_result ? _jsx(ResultPane, { run }) : null,
    ],
  })
}

// The composition names come from the guide to Claude Code dynamic workflows
// at https://claudefa.st/blog/guide/development/ultracode-dynamic-workflows-agent-teams
// -- fan-out-and-synthesize, adversarial verification, tournament (n-way),
// loop until done, classifier / model routing. `classify-and-act` is this
// app's rendering of the last, which names the routing but not the deciding
// that precedes it.
//
// `pipeline` is firmer still: a FUNCTION of the Workflow script API, so a run
// evidencing it called it by that name. `barrier`, `re-attempt`, `rolling`
// and `widening` are this app's own, each defined by the evidence line it
// prints beside itself.
//
// A run evidences these; the launchers below start one. Each blurb says what
// the shape BUYS, because "fan-out" names the mechanism and not the reason.
const PATTERNS = {
  'fan-out': {
    label: 'Fan-out',
    note: 'Several agents worked side by side on different pieces, each in its own context, so none could contaminate another.',
    blurb: 'Split the work and give each piece its own clean context, so they cannot contaminate each other.',
    prompt: 'Use a dynamic workflow to split this into independent pieces, run one agent per piece in its own context, and report each result separately: ',
  },
  // Not read from phase membership like the others, but from journal order --
  // and additive to them, because a run that fans out and then pipelines is
  // both. The blurb says what the shape BUYS: the second stage's wall-clock.
  pipeline: {
    label: 'Pipeline',
    note: 'Each item moved to the next stage as soon as it was ready, instead of every item waiting for the slowest one.',
    blurb: 'Each item moves to the next stage the moment it is ready, instead of every item waiting for the slowest one.',
    prompt: 'Use a dynamic workflow that pipelines the stages, so each item starts the next stage as soon as its own previous stage finishes rather than waiting at a barrier: ',
  },
  'fan-out-and-synthesize': {
    label: 'Fan-out and synthesize',
    note: 'The run fanned out, then one agent waited for all of them and merged their results into a single answer.',
    blurb: 'Fan out, then one agent waits for all of them and merges the structured results into a single answer.',
    prompt: 'Use a dynamic workflow to fan out over the pieces of this, then add a final agent that waits for all of them and merges their findings into one report: ',
  },
  'adversarial-verification': {
    label: 'Adversarial verification',
    note: 'Each finding was checked by a separate agent trying to refute it, so nothing was graded by the agent that produced it.',
    blurb: 'Every finding gets its own verifier, so the agent that produced it is never the one grading it.',
    prompt: 'Use a dynamic workflow where each finding is checked by a separate agent that tries to refute it against the evidence, and only confirmed findings survive: ',
  },
  'classify-and-act': {
    label: 'Classify and act',
    note: 'One agent decided what kind of thing this was, then the work was routed to agents suited to that kind.',
    blurb: 'One agent decides what kind of thing this is, then routes to agents suited to that kind.',
    prompt: 'Use a dynamic workflow that first classifies each item, then routes it to an agent chosen for that class: ',
  },
  tournament: {
    label: 'Tournament',
    note: 'Several agents attempted the same thing differently and a judge compared them, which is more reliable than scoring one alone.',
    blurb: 'Several agents attempt the same thing differently and a judge compares them pairwise, which is more reliable than scoring one in isolation.',
    prompt: 'Use a dynamic workflow where several agents each attempt this with a different approach, then a judging agent compares them pairwise and picks a winner: ',
  },
  // Launch-only: startable, never claimed. Tree-of-thought is the one shape
  // from the guide's longer list worth offering, because none of the others
  // here expresses branch-and-prune -- and no run on this machine evidences
  // it, so a detector would have nothing to learn from.
  //
  // Four were dropped after being added. Map-reduce IS fan-out plus a merge,
  // and two buttons that write the same workflow make a worse menu.
  // RAG-pipeline is about what agents DO rather than how they are arranged,
  // so it is not a shape at all. Reflection is loop-until-done with a
  // reviewer in it. Self-critique is a line in a prompt -- a careful
  // generate script already carries one -- and it does not need a workflow.
  'tree-of-thought': {
    label: 'Tree of thought',
    launchOnly: true,
    blurb: 'Branch each promising line of reasoning into its own agents and prune the ones that fail, instead of committing to the first path.',
    prompt: 'Use a dynamic workflow that explores this as a tree: branch each promising line of reasoning into its own agents, prune the branches that fail against the evidence, and report the surviving path with what killed the others: ',
  },
  'loop-until-done': {
    label: 'Loop until done',
    note: 'The run kept starting passes until a stop condition held, rather than a number of rounds fixed up front.',
    blurb: 'Keep spawning passes until a stop condition holds, rather than guessing a fixed number up front.',
    prompt: 'Use a dynamic workflow that keeps running passes until no new findings appear, instead of a fixed number of rounds: ',
  },
}

// How the run BEHAVED, as opposed to a composition you could ask for. These
// carry a label and a blurb but deliberately no prompt: "Rolling" is something
// a concurrency cap did to a run, not a shape to request. They render in a
// neutral pill for the same reason -- a composition is what the run IS, an
// observation is how it went.
const OBSERVED = {
  barrier: {
    label: 'Barrier',
    note: 'A stage waited for every agent of the wave before it, so the slowest one decided when the next stage could start.',
  },
  're-attempt': {
    label: 'Re-attempt',
    note: 'A piece of work was started again after an earlier try returned nothing, so there were more agents than distinct work.',
  },
  widening: {
    label: 'Widening',
    note: 'Each stage produced more work than the one before it, so the run broadened as it went instead of narrowing toward an answer.',
  },
  rolling: {
    label: 'Rolling',
    note: 'The wave started in slots as earlier agents returned, which is a concurrency cap rather than a smaller fan-out.',
  },
}

function shapeInfo(key) {
  return PATTERNS[key] || OBSERVED[key] || {}
}

function PatternLaunchers() {
  const [open, setOpen] = useState(false)
  const [task, setTask] = useState('')
  // `{ key, ok }` rather than a key: a failed copy has to be SAID. It used to
  // reset to the idle label, which left the button reading exactly as it did
  // before the click -- so the only sign the clipboard still held something
  // else was pasting it.
  const [copied, setCopied] = useState(null)
  // The SDK's own launcher rather than the `__mc_chat_launch` global this used
  // to set by hand: same effect, but a documented seam instead of an internal
  // one. It prefills and NAVIGATES -- setting the global alone left the user
  // on this page with their prompt waiting in a composer they were not
  // looking at.
  const { openChat } = useChatLauncher()
  const keys = Object.keys(PATTERNS)

  useEffect(() => {
    if (!copied) return
    const t = setTimeout(() => setCopied(null), copied.ok ? 1600 : 4000)
    return () => clearTimeout(t)
  }, [copied])

  // Every prompt ends mid-sentence, waiting for the subject. Typed here it
  // arrives whole; left empty the composer opens with the trailing colon and
  // the user finishes it there, which is what happened before this field
  // existed.
  const fullPrompt = (key) => PATTERNS[key].prompt + task.trim()

  const copyLabel = (key, label, spoken) => {
    const state = copied?.key === key ? (copied.ok ? 'done' : 'failed') : 'idle'
    if (spoken) {
      return {
        idle: `Copy the ${label} prompt`,
        done: `Copied the ${label} prompt`,
        failed: `Could not copy the ${label} prompt`,
      }[state]
    }
    return { idle: 'Copy prompt', done: 'Copied', failed: "Couldn't copy" }[state]
  }

  // Fails when the page is not focused, when permission is refused, or where
  // `navigator.clipboard` does not exist at all -- the last throws inside the
  // `try` too, so all three land in the same visible state.
  const copy = async (key) => {
    try {
      await navigator.clipboard.writeText(fullPrompt(key))
      setCopied({ key, ok: true })
    } catch {
      setCopied({ key, ok: false })
    }
  }

  return _jsxs('div', {
    className: 'wfr-card',
    children: [
      // Title and control on one line, explanation beneath. Three flex children
      // competing for a phone's width squeezed the title into "Start a /
      // workflow" over two lines while the blurb took three.
      _jsxs('div', {
        className: 'wfr-card-head',
        style: { marginBottom: '4px' },
        children: [
          _jsx('span', { className: 'wfr-run-name', style: { whiteSpace: 'nowrap' }, children: 'Start a workflow' }),
          _jsx('span', { className: 'wfr-spacer', children: ghostButton(open ? 'Hide shapes' : 'Show shapes', () => setOpen(v => !v), false, open) }),
        ],
      }),
      _jsx('div', {
        style: { fontSize: '13px', color: 'var(--muted)', maxWidth: '78ch' },
        children: 'Pick a shape. It opens the chat composer with the prompt ready, and you edit before sending.',
      }),
      open ? _jsxs('div', {
        children: [
          _jsxs('label', {
            className: 'wfr-task',
            children: [
              _jsx('span', { children: 'What should it work on?' }),
              _jsx('textarea', {
                value: task,
                onChange: e => setTask(e.target.value),
                rows: 2,
                placeholder: 'the task, in your own words — optional, you can finish the prompt in the composer',
              }),
            ],
          }),
          _jsx('div', {
            className: 'wfr-shapes',
            children: keys.map(k => _jsxs('div', {
              className: 'wfr-shape-card',
              children: [
                _jsx('div', { className: 'wfr-shape-name', children: PATTERNS[k].label }),
                _jsx('div', { className: 'wfr-shape-blurb', children: PATTERNS[k].blurb }),
                _jsxs('div', {
                  className: 'wfr-shape-actions',
                  children: [
                    // Named for their shape as well: eight cards make sixteen
                    // buttons, and a list of them read out of context was
                    // eight identical "Use this shape"s.
                    _jsx('button', {
                      onClick: () => openChat({ message: fullPrompt(k) }),
                      className: 'wfr-btn wfr-btn-solid',
                      'aria-label': `Use the ${PATTERNS[k].label} shape`,
                      children: 'Use this shape',
                    }),
                    _jsx('button', {
                      onClick: () => copy(k),
                      className: 'wfr-linkbtn',
                      'aria-label': copyLabel(k, PATTERNS[k].label, true),
                      style: copied?.key === k && !copied.ok ? { color: 'var(--danger, #b91c1c)' } : undefined,
                      children: copyLabel(k, PATTERNS[k].label, false),
                    }),
                  ],
                }),
              ],
            }, k)),
          }),
        ],
      }) : null,
    ],
  })
}

export default function WorkflowLens() {
  const [runs, setRuns] = useState([])
  const [error, setError] = useState('')
  const [loading, setLoading] = useState(true)
  const [liveOnly, setLiveOnly] = useState(false)
  const [expanded, setExpanded] = useState({})
  const [fetchedAt, setFetchedAt] = useState(null)

  const load = useCallback(async () => {
    try {
      const resp = await fetch('/api/apps/workflow-lens/runs?limit=20')
      if (!resp.ok) { setError(`gateway answered ${resp.status}`); setLoading(false); return }
      const body = await resp.json()
      setRuns(body.runs || [])
      setFetchedAt(new Date())
      setError('')
    } catch (e) {
      setError(String(e && e.message ? e.message : e))
    }
    setLoading(false)
  }, [])

  useEffect(() => {
    load()
    const t = setInterval(load, POLL_MS)
    return () => clearInterval(t)
  }, [load])

  const shown = liveOnly ? runs.filter(r => r.live_agents > 0) : runs
  const liveTotal = runs.reduce((n, r) => n + r.live_agents, 0)

  // `width:100%` is load-bearing, not belt-and-braces. The page is a flex item
  // in the dashboard's column-flex <main>, and a flex item with `auto` cross
  // margins is exempt from the stretch that would otherwise give it the full
  // width -- so `margin:0 auto` alone left the page shrink-to-fit, and it got
  // visibly NARROWER whenever a filter removed the widest card.
  return _jsxs('div', {
    className: 'wfr',
    children: [
      _jsx('style', { children: CSS }),
      _jsxs('div', {
        className: 'wfr-head',
        children: [
          _jsxs('div', {
            className: 'wfr-head-side',
            children: [
              _jsxs('svg', {
                xmlns: 'http://www.w3.org/2000/svg', width: 22, height: 22, viewBox: '0 0 24 24',
                fill: 'none', stroke: ACCENT, strokeWidth: 2, strokeLinecap: 'round', strokeLinejoin: 'round',
                'aria-hidden': 'true',
                // The same glyph as ui/icon.svg, so the sidebar and the page
                // header name the app with one picture.
                children: [
                  _jsx('circle', { cx: 12, cy: 4.5, r: 2.25 }),
                  _jsx('circle', { cx: 4.75, cy: 12, r: 2.25 }),
                  _jsx('circle', { cx: 12, cy: 12, r: 2.25 }),
                  _jsx('circle', { cx: 19.25, cy: 12, r: 2.25 }),
                  _jsx('circle', { cx: 12, cy: 19.5, r: 2.25 }),
                  _jsx('path', { d: 'M12 6.75v3M12 14.25v3' }),
                ],
              }),
              _jsx('h2', { className: 'wfr-title', children: 'Workflow Lens' }),
              // The name alone does not say whose workflows these are.
              _jsx('span', { className: 'wfr-stamp', children: 'for Claude Code dynamic workflows' }),
              pill(liveTotal ? `${liveTotal} agents live` : 'nothing running', liveTotal ? 'var(--ok-subtle, #d1fae5)' : 'var(--bg-hover, #f3f4f6)', liveTotal ? 'var(--ok, #047857)' : 'var(--muted, #6b7280)'),
            ],
          }),
          _jsxs('div', {
            className: 'wfr-head-side',
            children: [
              _jsx('span', { className: 'wfr-stamp', children: fetchedAt ? `updated ${fetchedAt.toLocaleTimeString()}` : '' }),
              ghostButton('Live only', () => setLiveOnly(v => !v), false, liveOnly),
              ghostButton('↻ Refresh', load, loading),
            ],
          }),
        ],
      }),
      error ? _jsx('div', { className: 'wfr-error', children: error }) : null,
      _jsx(PatternLaunchers, {}),
      loading && !runs.length ? _jsx('div', { className: 'wfr-note', children: 'Reading workflow artifacts…' }) : null,
      !loading && !shown.length ? _jsx('div', {
        className: 'wfr-note',
        children: liveOnly ? 'No run has a live agent right now.' : 'No Claude Code workflow runs found yet.',
      }) : null,
      ...shown.map(run => _jsx(RunCard, {
        run,
        expanded: !!expanded[run.run_id],
        onToggle: () => setExpanded(e => ({ ...e, [run.run_id]: !e[run.run_id] })),
      }, run.run_id)),
    ],
  })
}
