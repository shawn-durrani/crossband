import { useRef, useState } from 'react'
import { Upload, X } from 'lucide-react'
import { streamSSE } from '../api'

// Import a Claude.ai / ChatGPT data export: chats land locally, and Membro is
// seeded with the full history (verbatim record + optional fact mining) so
// long-term recall works from day one. Progress streams over the same SSE
// plumbing the round loop uses.
export default function ImportModal({ onClose }) {
  const fileRef = useRef(null)
  const [mine, setMine] = useState(true)
  const [busy, setBusy] = useState(false)
  const [progress, setProgress] = useState(null) // {phase, done, total}
  const [result, setResult] = useState(null)
  const [note, setNote] = useState(null)

  async function run() {
    const file = fileRef.current?.files?.[0]
    if (!file || busy) return
    setBusy(true); setNote(null); setResult(null)
    const fd = new FormData()
    fd.append('file', file)
    fd.append('mine', String(mine))
    try {
      await streamSSE('/api/import', fd, (ev) => {
        if (ev.type === 'progress') setProgress(ev)
        else if (ev.type === 'error' || ev.type === 'note') setNote(ev.message)
        else if (ev.type === 'done') { setResult(ev); setProgress(null) }
      })
    } catch (e) {
      setNote(e.message)
    } finally {
      setBusy(false)
    }
  }

  return (
    <div className="fixed inset-0 bg-black/60 flex items-center justify-center z-50 p-6" onClick={onClose}>
      <div
        className="bg-panel border border-edge2 rounded-2xl w-full max-w-xl p-6 space-y-4"
        onClick={(e) => e.stopPropagation()} role="dialog" aria-modal="true"
      >
        <div className="flex items-center justify-between">
          <h2 className="text-lg font-semibold flex items-center gap-2"><Upload size={18} /> Import from Claude or ChatGPT</h2>
          <button onClick={onClose} aria-label="Close import"><X size={18} /></button>
        </div>
        <p className="text-sm text-ink-mid leading-relaxed">
          Upload your data export (Claude: Settings → Privacy → Export data · ChatGPT:
          Settings → Data controls → Export). Your conversations become chats here, and
          your memory is seeded with the full history — so the AIs can recall things you
          discussed months ago. Re-importing a newer export only adds what&apos;s new.
        </p>
        <input ref={fileRef} type="file" accept=".zip,.json" className="block w-full text-sm"
               disabled={busy} aria-label="Export file (.zip or conversations.json)" />
        <label className="flex items-start gap-2 text-xs text-ink-dim leading-relaxed">
          <input type="checkbox" className="mt-0.5" checked={mine}
                 onChange={(e) => setMine(e.target.checked)} disabled={busy} />
          <span><b className="text-ink-mid">Mine facts into memory</b> (recommended): a small
            model reads each imported conversation once to extract durable facts. A large
            history takes a while and uses real API credit — you can watch it work on
            Membro&apos;s admin page.</span>
        </label>
        {progress && (
          <div className="text-sm text-ink-mid" aria-live="polite">
            {progress.phase}{progress.total ? ` — ${progress.done}/${progress.total}` : '…'}
            {progress.total > 0 && (
              <div className="mt-1 h-1.5 bg-edge2 rounded-full overflow-hidden">
                <div className="h-full bg-btn transition-all"
                     style={{ width: `${(100 * progress.done) / progress.total}%` }} />
              </div>
            )}
          </div>
        )}
        {note && <div className="text-sm text-amber-400">{note}</div>}
        {result && (
          <div className="text-sm space-y-1" aria-live="polite">
            <div className="font-semibold">Done ({result.format}).</div>
            <div>{result.chats} new chats · {result.updated_chats} updated · {result.messages} messages</div>
            <div>{result.ingested} messages remembered · {result.mined_chats} chats mined · {result.memory_entries} exported memories saved</div>
            {result.skipped_existing > 0 && (
              <div className="text-ink-dim">{result.skipped_existing} unchanged (skipped)</div>
            )}
          </div>
        )}
        <div className="flex justify-end gap-2">
          <button className="border border-edge2 rounded-lg px-4 py-2 text-sm text-ink-mid hover:border-edge3"
                  onClick={onClose}>{result ? 'Close' : 'Cancel'}</button>
          <button className="bg-btn text-btn-ink rounded-lg px-4 py-2 text-sm font-semibold disabled:opacity-50"
                  onClick={run} disabled={busy}>
            {busy ? 'Importing…' : 'Import'}
          </button>
        </div>
      </div>
    </div>
  )
}
