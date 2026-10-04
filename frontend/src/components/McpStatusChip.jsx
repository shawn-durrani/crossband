import { Plug } from 'lucide-react'
import { visibleMcpJobs, mcpChipLabel, mcpChipTone } from '../mcpJobs'

// The background-work strip (#604): one quiet line per outside MCP server
// whose long work the app is watching, such as
// "Dovetail: adding the drawer runners · step 48 · 5 min". It sits with the
// Claude Code guest strip between the thread and the composer, and looks
// like it. The work's questions and its result arrive as messages, so the
// strip only says where it's got to. The rules live in mcpJobs.js.
const TONE = {
  running: { dot: 'bg-amber-400 animate-pulse', text: 'text-ink-mid' },
  blocker: { dot: 'bg-amber-400', text: 'text-amber-500' },
  done: { dot: 'bg-emerald-400', text: 'text-ink-mid' },
  failed: { dot: 'bg-edge3', text: 'text-ink-dim' },
}

export default function McpStatusChip({ jobs }) {
  const now = Date.now() / 1000
  const shown = visibleMcpJobs(jobs, now)
  if (!shown.length) return null
  return (
    <>
      {shown.map((job) => {
        const tone = TONE[mcpChipTone(job)] || TONE.running
        return (
          <div
            key={job.id}
            className="mx-auto w-full max-w-[768px] text-xs bg-panel2 border border-edge2 rounded-lg"
            role="status"
            title="Runs in the background and won't hold up the chat. Its questions and result come back as messages."
          >
            <div className="flex items-center gap-2 px-3 py-1.5">
              <Plug size={13} className="text-ink-dim shrink-0" aria-hidden="true" />
              <span className={`inline-flex h-1.5 w-1.5 rounded-full shrink-0 ${tone.dot}`} aria-hidden="true" />
              <span className={`flex-1 min-w-0 truncate ${tone.text}`}>{mcpChipLabel(job, now)}</span>
            </div>
          </div>
        )
      })}
    </>
  )
}
