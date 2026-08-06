import { useEffect, useMemo, useState } from 'react'
import { Download, Check } from 'lucide-react'
import { headline } from '../spendView'
import { chatCostFigures, chatCostNote, COST_TONE } from '../messageCost'

const fmtTok = (n) => (n >= 1000 ? `${(n / 1000).toFixed(1)}k` : n)
const fmtDay = (ts) => new Date(ts * 1000).toISOString().slice(0, 10)

// The three provenance buckets, named. `/api/usage/chats` returns them apart
// and never as one `cost`, so the picker can't re-collapse the split: a chat of
// subscription-covered guest turns must not look like money spent.
function CostFigures({ row, className = '', lead = '' }) {
  const figures = chatCostFigures(headline(row))
  if (!figures.length) return null
  return (
    <span className={className}>
      {lead}
      {figures.map((f, i) => (
        <span key={f.tone}>
          {i > 0 && ' · '}
          <span className={COST_TONE[f.tone]}>{f.text}</span>
        </span>
      ))}
    </span>
  )
}

export default function ExportModal({ chats, onClose }) {
  const [usage, setUsage] = useState({})
  const [selected, setSelected] = useState(new Set())
  const [format, setFormat] = useState('md')

  useEffect(() => {
    fetch('/api/usage/chats').then((r) => r.json()).then(setUsage).catch(() => {})
  }, [])

  const sorted = useMemo(
    () => [...chats].sort((a, b) => b.updated_at - a.updated_at),
    [chats],
  )

  function toggle(id) {
    setSelected((s) => {
      const next = new Set(s)
      if (next.has(id)) next.delete(id)
      else next.add(id)
      return next
    })
  }

  function selectWhere(pred) {
    setSelected(new Set(sorted.filter(pred).map((c) => c.id)))
  }

  const weekAgo = Date.now() / 1000 - 7 * 86400
  // Selection totals stay bucketed all the way through: summing them into one
  // figure here would bring the collapsed total back in the footer alone.
  const totals = [...selected].reduce(
    (acc, id) => {
      const u = usage[id]
      if (!u) return acc
      acc.tokens += u.tokens || 0
      acc.metered += u.metered || 0
      acc.subscription_equiv += u.subscription_equiv || 0
      acc.unknown += u.unknown || 0
      return acc
    },
    { tokens: 0, metered: 0, subscription_equiv: 0, unknown: 0 },
  )
  const selectionNote = chatCostNote(headline(totals), 'the selected chats')

  function runExport() {
    const ids = [...selected].join(',')
    window.open(`/api/export?ids=${ids}&format=${format}`, '_blank')
  }

  return (
    <div className="fixed inset-0 bg-black/60 flex items-center justify-center z-50 p-6" onClick={onClose}>
      <div
        className="bg-panel border border-edge2 rounded-2xl w-full max-w-3xl p-6 space-y-4 max-h-[88vh] flex flex-col"
        onClick={(e) => e.stopPropagation()}
      >
        <div className="flex items-baseline gap-3 flex-wrap">
          <h2 className="text-lg font-semibold flex items-center gap-2"><Download size={18} /> Export chats</h2>
          <span className="text-xs text-ink-dim">
            Markdown pastes straight into Claude or ChatGPT (projects, messages, files).
          </span>
        </div>
        <div className="flex gap-2 text-xs text-ink-mid">
          <span className="text-ink-faint">Select:</span>
          <button className="text-link hover:opacity-80" onClick={() => selectWhere(() => true)}>all</button>
          <button className="text-link hover:opacity-80" onClick={() => selectWhere((c) => c.updated_at >= weekAgo)}>last 7 days</button>
          <button className="text-link hover:opacity-80" onClick={() => setSelected(new Set())}>none</button>
        </div>

        <div className="flex-1 overflow-y-auto grid grid-cols-2 md:grid-cols-3 gap-2 content-start">
          {sorted.map((c) => {
            const u = usage[c.id]
            const on = selected.has(c.id)
            return (
              <button
                key={c.id}
                onClick={() => toggle(c.id)}
                className={`text-left border rounded-xl px-3 py-2.5 transition-colors ${
                  on ? 'border-link bg-app' : 'border-edge hover:border-edge2'
                }`}
              >
                <div className="text-sm font-medium truncate flex items-center gap-1">{on && <Check size={13} className="text-link shrink-0" />}{c.title}</div>
                <div className="text-[11px] text-ink-dim mt-1">{fmtDay(c.updated_at)}</div>
                <div className="text-[11px] text-ink-faint">
                  {u ? `${u.messages} msg · ${fmtTok(u.tokens)} tok` : '—'}
                </div>
                {u && <CostFigures row={u} className="block text-[11px] text-ink-faint" />}
              </button>
            )
          })}
        </div>

        {/* Why the selection can show two dollar figures, in words — visible,
            not a tooltip, and absent when there is nothing to disambiguate. */}
        {selectionNote && (
          <p className="text-[11px] text-ink-faint leading-snug">{selectionNote}</p>
        )}

        <div className="flex items-center gap-3 pt-1 border-t border-edge">
          <span className="text-xs text-ink-dim flex flex-wrap items-baseline gap-x-1">
            <span>{selected.size} selected</span>
            {selected.size > 0 && <span>· {fmtTok(totals.tokens)} tok</span>}
            {selected.size > 0 && <CostFigures row={totals} lead="· " />}
          </span>
          <select
            className="ml-auto bg-app border border-edge2 rounded-lg px-2 py-1.5 text-sm"
            value={format}
            onChange={(e) => setFormat(e.target.value)}
          >
            <option value="md">Markdown (.md)</option>
            <option value="json">JSON (.json)</option>
          </select>
          <button
            className="border border-edge2 rounded-lg px-4 py-2 text-sm text-ink-mid hover:border-edge3"
            onClick={onClose}
          >
            Close
          </button>
          <button
            className="bg-btn text-btn-ink rounded-lg px-4 py-2 text-sm font-semibold hover:bg-btn-hover disabled:opacity-40"
            disabled={!selected.size}
            onClick={runExport}
          >
            Export
          </button>
        </div>
      </div>
    </div>
  )
}
