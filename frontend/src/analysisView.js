// Pure meaning for the Analysis page (#407): run-state badges, the running
// chip's line, what holds a Run button back, the question asked before a
// paid run, and the history's order and labels. The backend's catalogue and
// run records are the contract (backend/analysis.py); nothing here fetches
// or touches the DOM, and AnalysisPage.jsx is markup and wiring only.

// Every state a run record can be in, mirrored from backend/analysis.py
// RUN_STATES through tests/fixtures/backend_contract.json.
export const RUN_STATES = ['running', 'done', 'failed', 'stopped', 'timed out', 'interrupted']

const BADGES = {
  running: { label: 'Running', tone: 'live' },
  done: { label: 'Done', tone: 'ok' },
  failed: { label: 'Failed', tone: 'bad' },
  stopped: { label: 'Stopped', tone: 'muted' },
  'timed out': { label: 'Ran out of time', tone: 'bad' },
  interrupted: { label: 'Cut off', tone: 'muted' },
}

// A run's state as a badge. An unknown state says so rather than passing
// for a finished run.
export function stateBadge(run) {
  return BADGES[run?.state] || { label: run?.state ? `Unknown (${run.state})` : 'Unknown', tone: 'muted' }
}

export function elapsedLabel(seconds) {
  if (seconds == null || !Number.isFinite(seconds) || seconds < 0) return ''
  if (seconds < 60) return 'under a minute'
  const m = Math.floor(seconds / 60)
  return `${m} min`
}

// The running chip's one line: what's going and for how long.
export function chipText(run, nowSecs) {
  if (!run || run.state !== 'running') return ''
  const verb = run.practice ? 'Practice run going' : 'Running'
  const took = elapsedLabel(nowSecs - (run.created_at_unix ?? nowSecs))
  return took ? `${verb}, ${took} so far` : verb
}

// The status ping under the chip: the server's own word that the run is
// alive, and how long ago it came. Quiet for the first check-in window.
export function pingText(run, nowSecs) {
  if (!run || run.state !== 'running' || !run.status_label || run.status_at == null) return ''
  const ago = Math.max(0, Math.round(nowSecs - run.status_at))
  return `${run.status_label}, checked ${ago < 5 ? 'just now' : `${ago} s ago`}.`
}

// Why a measurement's Run button is held back, or '' when it can go.
export function runHold(measurement, starting = false) {
  if (!measurement) return 'loading'
  if (measurement.running) return 'Running now.'
  if (measurement.blocked) return measurement.blocked
  if (starting) return 'Starting…'
  return ''
}

// The question asked before a run that can spend money or read your data.
// A practice run is free and made up, so it asks nothing.
export function confirmText(measurement, practice = false) {
  if (practice || !measurement) return ''
  return `Run the ${measurement.title.toLowerCase()} now?\n\n` +
    `What it costs: ${measurement.costs}\n\nWhat it touches: ${measurement.touches}`
}

export function newestFirst(runs) {
  return [...(runs || [])].sort((a, b) => (b.created_at_unix || 0) - (a.created_at_unix || 0))
}

// The newest settled run of one measurement, for its card's last result.
export function latestFor(runs, measurementId) {
  return newestFirst(runs).find((r) => r.measurement === measurementId && r.state !== 'running') || null
}

export function spentLabel(usd) {
  if (usd == null || !Number.isFinite(usd)) return ''
  if (usd < 0.01) return 'under a cent'
  return `$${usd.toFixed(2)}`
}

const MONTHS = ['Jan', 'Feb', 'Mar', 'Apr', 'May', 'Jun', 'Jul', 'Aug', 'Sep', 'Oct', 'Nov', 'Dec']

// When a run started, relative for the last day and a date after that.
export function whenLabel(unix, nowSecs) {
  if (unix == null) return ''
  const ago = nowSecs - unix
  if (ago < 60) return 'just now'
  if (ago < 3600) return `${Math.floor(ago / 60)} min ago`
  if (ago < 86400) return `${Math.floor(ago / 3600)} h ago`
  const d = new Date(unix * 1000)
  return `${d.getDate()} ${MONTHS[d.getMonth()]}`
}

// One line for a run in the history: what it was and how it ended.
export function historyTitle(run) {
  const badge = stateBadge(run)
  const kind = run?.practice ? `${run.title} (practice)` : (run?.title || run?.measurement || 'Run')
  return `${kind} · ${badge.label}`
}

// The line under it: the headline when there is one, else why it stopped.
export function historyDetail(run) {
  if (!run) return ''
  if (run.summary?.headline) return run.summary.headline
  if (run.error) return run.error.split('\n').filter(Boolean).slice(-1)[0] || ''
  return ''
}

export function reportJsonUrl(runId) {
  return `/api/analysis/runs/${encodeURIComponent(runId)}/report.json`
}

// Where a measurement's README lives, for "how to read the report".
export function readmeUrl(measurement) {
  return measurement?.readme
    ? `https://github.com/shawn-durrani/crossband/blob/main/${measurement.readme}`
    : ''
}

// Should the page keep asking while it's open? Only while something runs.
export function shouldPoll(view) {
  if (!view) return false
  return (view.measurements || []).some((m) => m.running) ||
    (view.runs || []).some((r) => r.state === 'running')
}

// The sidebar's dot beside Analysis: on while any measurement runs.
export function measuring(ids) {
  return Array.isArray(ids) && ids.length > 0
}
