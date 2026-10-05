// Pure helpers for the background-work strip (#604): long work an outside
// MCP server runs, such as a design app building a cabinet, watched by the
// backend (backend/mcpjobs.py) while the chat carries on. React-free, so the
// merge and the one-line labels are testable without a DOM, the same
// discipline as guestJobs.js. App.jsx seeds from GET /api/chats/{id}/mcp_jobs
// and merges live `mcp_job` events off the one global events stream.

import { LINGER_SECS } from './guestJobs.js'

// Merge an mcp_job event (or a fetched row) into the list: one entry per
// watch id, newest update wins, oldest watch first. A late, older event
// never clobbers a fresher one.
export function mergeMcpJob(current, ev) {
  if (!ev || ev.id == null) return current
  const byId = new Map(current.map((j) => [j.id, j]))
  const prev = byId.get(ev.id)
  if (prev && (prev.updated_at || 0) > (ev.updated_at || 0)) return current
  byId.set(ev.id, { ...prev, ...ev })
  return [...byId.values()].sort((a, b) => a.id - b.id)
}

// The watches worth a line right now. A live watch always shows. A finished
// one shows briefly, until its result has arrived as a message, and so does
// a watch the app lost touch with. One stopped by the app shutting down, or
// one that went idle with nothing to report, never shows.
export function visibleMcpJobs(jobs, nowSecs) {
  return (jobs || []).filter((j) => {
    if (j.watching) return true
    if (j.ended !== 'done' && j.ended !== 'lost') return false
    return (nowSecs - (j.updated_at || 0)) < LINGER_SECS
  })
}

// How long the work has run, as of now: the server's count at the last
// update, moved on by the time since, while it's running.
export function mcpElapsed(job, nowSecs) {
  const base = Number(job?.elapsed_s) || 0
  if (!job?.watching || job.state !== 'running') return base
  return base + Math.max(0, nowSecs - (job.updated_at || nowSecs))
}

export function formatElapsed(secs) {
  const s = Math.max(0, Math.floor(Number(secs) || 0))
  if (s < 60) return `${s} s`
  const m = Math.floor(s / 60)
  if (m < 90) return `${m} min`
  return `${Math.floor(m / 60)} h ${m % 60} min`
}

const WAITING = {
  question: 'is asking a question',
  preview: 'has a preview waiting',
  plan: 'has a plan waiting for a yes',
  part: 'is waiting on a part',
}

// The one line for a watch, such as
// "Dovetail: adding the drawer runners · step 48 · 5 min".
export function mcpChipLabel(job, nowSecs) {
  if (!job) return ''
  const title = job.title || job.server || 'Background work'
  if (!job.watching) {
    if (job.ended === 'done') {
      if (job.outcome === 'failed') return `${title} stopped with an error, handing back`
      if (job.outcome === 'stopped') return `${title} was stopped, handing back`
      return `${title} finished, handing back`
    }
    if (job.ended === 'lost') return `${title}: lost touch, the work may still be going`
    return title
  }
  if (job.state === 'waiting') {
    return `${title} ${WAITING[job.waiting_for] || 'is waiting on you'}`
  }
  const head = job.stage ? `${title}: ${job.stage}` : `${title} working…`
  const bits = [head]
  if (job.steps) bits.push(`step ${job.steps}`)
  if (job.parts) bits.push(`${job.parts} part${job.parts === 1 ? '' : 's'}`)
  bits.push(formatElapsed(mcpElapsed(job, nowSecs)))
  return bits.join(' · ')
}

// The status token the strip styles by: running, blocker (waiting on the
// room), done, or failed for work that ended in an error or a watch the app
// lost touch with.
export function mcpChipTone(job) {
  if (!job) return ''
  if (!job.watching) {
    return job.ended === 'done' && job.outcome !== 'failed' ? 'done' : 'failed'
  }
  return job.state === 'waiting' ? 'blocker' : 'running'
}
