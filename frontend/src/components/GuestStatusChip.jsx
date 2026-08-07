import { useState } from 'react'
import { ChevronRight, ChevronDown, SquareTerminal, X } from 'lucide-react'
import { currentJob, chipLabel, chipTone, hasVisibleJob } from '../guestJobs'

// The Claude Code guest status strip. Design intent: Claude Code re-enters the
// conversation like a colleague reporting back, NOT as a persistent task panel
// you have to babysit. So this is a single quiet line ("Claude Code working…")
// with NO reasoning or steps shown inline. It expands ON DEMAND only.
//
// Spacing: it sits between the thread and the composer as a strip in the
// thread's own column width, with real air on both sides (the old wrapper
// measured 0px to the context bar and 9px to the composer, at a third width,
// which read as "smooshed"). A floating-pill version was tried and rejected
// live: anchored bottom-center it sat exactly on the "Let them continue"
// controls.
//
// A finished job lingers briefly then clears - its result arrives as a
// message, so the pill doesn't need to hold state. Old jobs from a previous
// session (seeded from the snapshot on chat open) never show.
const TONE = {
  running: { dot: 'bg-amber-400 animate-pulse', text: 'text-ink-mid' },
  blocker: { dot: 'bg-amber-400', text: 'text-amber-500' },
  done: { dot: 'bg-emerald-400', text: 'text-ink-mid' },
  failed: { dot: 'bg-red-400', text: 'text-red-400' },
  cancelled: { dot: 'bg-edge3', text: 'text-ink-dim' },
}

export default function GuestStatusChip({ jobs }) {
  const [expanded, setExpanded] = useState(false)
  const [dismissed, setDismissed] = useState(() => new Set())

  const job = currentJob(jobs)
  if (!job || dismissed.has(job.id)) return null
  const running = job.status === 'running'
  if (!hasVisibleJob(jobs, Date.now() / 1000)) return null // old finish: its result is a message

  const tone = TONE[chipTone(job)] || TONE.running
  const label = chipLabel(job)

  return (
    <div className="mx-auto w-full max-w-[768px] text-xs bg-panel2 border border-edge2 rounded-lg" role="status">
      <div className="flex items-center gap-2 px-3 py-1.5">
        <SquareTerminal size={13} className="text-ink-dim shrink-0" aria-hidden="true" />
        <span className={`inline-flex h-1.5 w-1.5 rounded-full shrink-0 ${tone.dot}`} aria-hidden="true" />
        {/* The status text is the stable anchor; the variable step count is
            appended by chipLabel, never displacing the label. */}
        <span className={`flex-1 min-w-0 truncate ${tone.text}`}>{label}</span>
        <button
          className="inline-flex items-center gap-0.5 text-ink-dim hover:text-ink shrink-0"
          aria-expanded={expanded}
          title={expanded ? 'Hide details' : 'Show what Claude Code is doing'}
          onClick={() => setExpanded((v) => !v)}
        >
          {expanded ? <ChevronDown size={13} /> : <ChevronRight size={13} />}
          <span className="hidden sm:inline">Details</span>
        </button>
        {!running && (
          <button
            className="inline-flex items-center text-ink-dim hover:text-ink shrink-0"
            title="Dismiss"
            aria-label="Dismiss guest status"
            onClick={() => setDismissed((s) => new Set(s).add(job.id))}
          >
            <X size={12} />
          </button>
        )}
      </div>
      {expanded && (
        <div className="px-3 pb-2 pt-0.5 border-t border-edge2 text-ink-dim space-y-1">
          <div><span className="text-ink-mid">Task:</span> {job.task || '—'}</div>
          <div className="flex flex-wrap gap-x-4 gap-y-0.5">
            <span><span className="text-ink-mid">Repo:</span> {job.repo || '—'}</span>
            <span><span className="text-ink-mid">Mode:</span> {job.mode || 'investigate'}</span>
            <span><span className="text-ink-mid">Status:</span> {job.status}</span>
            {job.step_count ? <span><span className="text-ink-mid">Steps:</span> {job.step_count}</span> : null}
          </div>
          <div className="text-ink-dim/80">
            Claude Code runs in the background - it won’t interrupt the chat. Its
            result comes back as a message from Claude or GPT when there’s a natural pause.
          </div>
        </div>
      )}
    </div>
  )
}
