// Pure helpers for the Claude Code guest job status chip, kept React-free so
// the merge/dedupe and the collapsed-chip labelling are unit-testable without
// a DOM (same discipline as eventStream.js / runningState.js). App.jsx seeds
// from GET /api/chats/{id}/guest_jobs, then merges live `guest_job` events off
// the one global events stream: the same channel messages ride, so voice and
// text/mobile show the identical status.

// Merge a guest_job event (or a fetched row) into the current list: de-duped by
// id, newest-updated last. A guest run is UPDATED in place across its lifecycle
// (running → completed/failed/cancelled), so last-write-wins by updated_at keeps
// a late-arriving stale event from clobbering a fresher state.
export function mergeGuestJob(current, ev) {
  if (!ev || ev.id == null) return current
  const byId = new Map(current.map((j) => [j.id, j]))
  const prev = byId.get(ev.id)
  if (prev && (prev.updated_at || 0) > (ev.updated_at || 0)) return current
  byId.set(ev.id, { ...prev, ...ev })
  return [...byId.values()].sort((a, b) => a.id - b.id)
}

// The one job whose status the chip should surface: a running job if there is
// one (there's only ever one at a time, since a second concurrent run is
// blocked), otherwise the most recent finished job so a just-completed/failed
// run still reports for a moment. Returns null when there's nothing to show.
export function currentJob(jobs) {
  if (!jobs || !jobs.length) return null
  const running = jobs.filter((j) => j.status === 'running')
  if (running.length) return running[running.length - 1]
  return jobs[jobs.length - 1]
}

// Is this job still worth showing a chip for? Running always; a finished job
// only until the caller decides to dismiss it (App keeps completed/failed
// visible briefly, then the narrator's hand-back message carries the result).
export function isActive(job) {
  return !!job && job.status === 'running'
}

// The collapsed, single-line chip label - minimal footprint, no reasoning shown
// (that's expand-on-demand). One genuine line on mobile.
//
// status_label/status_at are the fully-ephemeral periodic check-in: never a
// chat message, just the job row's latest ping (backend/guestjobs.py _ping),
// so a reconnecting client shows it from GET .../guest_jobs exactly like any
// other live field, no history replay involved.
export function chipLabel(job) {
  if (!job) return ''
  const verb = job.mode === 'implement' ? 'building' : 'working'
  switch (job.status) {
    case 'running': {
      const steps = job.step_count ? ` · ${job.step_count} steps` : ''
      const ping = job.status_label ? ` · ${job.status_label}` : ''
      return `Claude Code ${verb}…${steps}${ping}`
    }
    case 'completed':
      return job.kind === 'blocker'
        ? 'Claude Code needs input'
        : 'Claude Code finished - handing back'
    case 'failed':
      return 'Claude Code hit an error'
    case 'cancelled':
      return 'Claude Code run stopped'
    default:
      return 'Claude Code'
  }
}

// A stable status token for styling/aria (running | blocker | done | failed |
// cancelled). Folds the blocker completion path into its own token so the chip
// can flag "needs you" distinctly from a routine finish.
export function chipTone(job) {
  if (!job) return ''
  if (job.status === 'completed') return job.kind === 'blocker' ? 'blocker' : 'done'
  return job.status // running | failed | cancelled
}

// How long a finished job's status stays visible before the strip clears: its
// result arrives as a normal message, so the strip only bridges the gap
// between "finished" and "the room has told you about it".
export const LINGER_SECS = 45

// Should the status strip exist at all right now? Running always shows; a
// finished job shows only while fresh; anything older is history whose result
// is already a message. One rule, shared by the strip itself AND the layout
// wrapper around it (the wrapper once reserved padding for jobs the strip
// would refuse to render, leaving a phantom 16px spacer above the composer).
export function hasVisibleJob(jobs, nowSecs) {
  const job = currentJob(jobs)
  if (!job) return false
  if (job.status === 'running') return true
  return (nowSecs - (job.updated_at || 0)) < LINGER_SECS
}
