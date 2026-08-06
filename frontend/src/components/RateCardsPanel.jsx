// Rate-card editing on the Models page.
//
// Before this, the app could tell you a model was unpriced, block its seat's
// promotion on exactly that, and offer no remedy but hand-editing
// config.local.json on the server. This is that remedy.
//
// All meaning lives in ../rateCards.js (pure, unit-tested); this file is
// markup and wiring only.
import { useEffect, useState } from 'react'
import { AlertTriangle, Check, ChevronDown, ChevronRight, Trash2 } from 'lucide-react'

import { api } from '../api.js'
import {
  cardSummary, draftFrom, draftToBody, sortRows, validateDraft,
  ESTIMATE, SELF_HOSTED,
} from '../rateCards.js'

const TONE = {
  override: 'text-link border-link/40',
  builtin: 'text-ink-mid border-edge2',
  unpriced: 'text-amber-400 border-amber-400/40',
}

function OriginBadge({ origin }) {
  return (
    <span
      className={`text-[11px] leading-none px-1.5 py-1 rounded border ${TONE[origin.tone]}`}
      title={origin.title}
    >
      {origin.label}
    </span>
  )
}

// One labelled field. Errors are tied to the input with aria-describedby and
// aria-invalid so a screen reader hears WHY a save was refused, not just that
// it was.
function Field({ id, label, hint, value, onChange, error, ...rest }) {
  const describedBy = [hint ? `${id}-hint` : null, error ? `${id}-err` : null]
    .filter(Boolean).join(' ') || undefined
  return (
    <label className="block" htmlFor={id}>
      <span className="text-xs text-ink-mid">{label}</span>
      {hint && <span id={`${id}-hint`} className="block text-[11px] text-ink-faint">{hint}</span>}
      <input
        id={id}
        className={`mt-1 w-full bg-app border rounded-lg px-2.5 py-1.5 text-sm focus:outline-none
          ${error ? 'border-red-400 focus:border-red-400' : 'border-edge2 focus:border-edge3'}`}
        value={value}
        onChange={(e) => onChange(e.target.value)}
        aria-invalid={error ? 'true' : undefined}
        aria-describedby={describedBy}
        {...rest}
      />
      {error && (
        <span id={`${id}-err`} role="alert" className="mt-1 block text-[11px] text-red-400">
          {error}
        </span>
      )}
    </label>
  )
}

function EditForm({ row, onSaved, onCancel }) {
  const [draft, setDraft] = useState(() => draftFrom(row))
  const [errors, setErrors] = useState({})
  const [saving, setSaving] = useState(false)
  const [failed, setFailed] = useState(null)
  const set = (k) => (v) => setDraft((d) => ({ ...d, [k]: v }))
  const id = (k) => `rate-${row.model}-${k}`.replace(/[^a-zA-Z0-9-]/g, '_')

  async function save() {
    const v = validateDraft(draft)
    setErrors(v.errors)
    if (!v.ok) return
    setSaving(true); setFailed(null)
    try {
      await api.saveRateCard(row.model, draftToBody(draft))
      onSaved()
    } catch (e) {
      setFailed(e.message || 'Could not save that rate.')
    } finally {
      setSaving(false)
    }
  }

  const selfHosted = draft.provenance === SELF_HOSTED
  return (
    <div className="mt-2.5 border-t border-edge pt-2.5 space-y-2.5">
      {/* Only these two may be attested. provider_reported and
          subscription_equivalent are DERIVED per turn from what a provider
          actually returned, so offering them would let an estimate be
          relabelled as billed spend. */}
      <fieldset>
        <legend className="text-xs text-ink-mid">What kind of price is this?</legend>
        {[[ESTIMATE, 'A published list price', 'Dated and sourced. An estimate — not your actual bill.'],
          [SELF_HOSTED, 'Local / self-hosted — $0', 'A declared zero marginal cost, which is a real figure rather than a gap.']]
          .map(([val, label, hint]) => (
            <label key={val} className="flex items-start gap-2 mt-1.5 cursor-pointer">
              <input type="radio" name={id('prov')} className="mt-1" value={val}
                     checked={draft.provenance === val}
                     onChange={() => set('provenance')(val)} />
              <span className="text-xs">
                <span className="text-ink">{label}</span>
                <span className="block text-[11px] text-ink-faint">{hint}</span>
              </span>
            </label>
          ))}
      </fieldset>
      {!selfHosted && (
        <div className="grid grid-cols-1 sm:grid-cols-2 gap-2.5">
          <Field id={id('in')} label="Input rate" hint="$ per 1M tokens"
                 value={draft.input} onChange={set('input')} error={errors.input}
                 inputMode="decimal" placeholder="2.00" />
          <Field id={id('out')} label="Output rate" hint="$ per 1M tokens"
                 value={draft.output} onChange={set('output')} error={errors.output}
                 inputMode="decimal" placeholder="12.00" />
        </div>
      )}
      <Field id={id('src')}
             label={selfHosted ? 'What is this?' : 'Where this figure came from'}
             hint={selfHosted
               ? 'A short note, so the declared $0 is auditable later.'
               : 'An http(s) link to the provider’s rate card. Required — a rate with no checkable source is refused.'}
             value={draft.source} onChange={set('source')} error={errors.source}
             placeholder={selfHosted ? 'local (Ollama, self-hosted)' : 'https://…/pricing'} />
      {!selfHosted && (
        <Field id={id('asof')} label="Published or checked on"
               hint="YYYY-MM-DD, so a stale rate is visible as stale rather than trusted forever."
               value={draft.as_of} onChange={set('as_of')} error={errors.as_of}
               placeholder="2026-07-31" />
      )}
      {!selfHosted && (
      <details className="text-xs text-ink-mid">
        <summary className="cursor-pointer select-none">Cache terms and aliases (optional)</summary>
        <p className="mt-1.5 text-[11px] text-ink-faint">
          Providers charge differently to <em>store</em> a conversation in their cache than to
          re-read it — that difference, not the headline rate, is where most of a long chat&apos;s
          cost lives. Leave blank to use the provider default. Aliases are other model ids you
          are attesting share this exact price.
        </p>
        <div className="mt-2 grid grid-cols-1 sm:grid-cols-2 gap-2.5">
          <Field id={id('rd')} label="Cache read multiplier" hint="e.g. 0.1 = 10% of input"
                 value={draft.read_mult} onChange={set('read_mult')} inputMode="decimal" />
          <Field id={id('wr')} label="Cache write multiplier" hint="e.g. 1.25 = 125% of input"
                 value={draft.write_mult} onChange={set('write_mult')} inputMode="decimal" />
        </div>
        <div className="mt-2.5">
          <Field id={id('al')} label="Aliases" hint="Comma-separated model ids priced the same"
                 value={draft.aliases} onChange={set('aliases')} />
        </div>
      </details>
      )}
      {failed && (
        <div role="alert" className="flex items-start gap-1.5 text-xs text-red-400">
          <AlertTriangle size={13} className="mt-0.5 shrink-0" /> {failed}
        </div>
      )}
      <div className="flex items-center gap-2">
        <button
          className="border border-edge2 rounded-lg px-3 py-1.5 text-sm hover:border-edge3 disabled:opacity-50"
          onClick={save}
          disabled={saving}
        >
          {saving ? 'Saving…' : 'Save rate'}
        </button>
        <button className="text-sm text-ink-mid hover:text-ink" onClick={onCancel}>Cancel</button>
      </div>
    </div>
  )
}

export default function RateCardsPanel() {
  const [rows, setRows] = useState(null)
  const [open, setOpen] = useState(false)
  const [editing, setEditing] = useState(null) // model id
  const [error, setError] = useState(null)

  async function load() {
    try {
      const r = await api.rateCards()
      setRows(sortRows(r.cards))
      setError(null)
    } catch (e) {
      setError(e.message || 'Could not load rate cards.')
    }
  }

  useEffect(() => { if (open && rows === null) load() }, [open])

  const unpriced = (rows || []).filter((r) => !r.priced).length

  return (
    <section className="border border-edge rounded-xl">
      <h3>
        <button
          className="w-full flex items-center gap-2 px-3 py-2.5 text-left"
          onClick={() => setOpen((v) => !v)}
          aria-expanded={open}
        >
          {open ? <ChevronDown size={15} /> : <ChevronRight size={15} />}
          <span className="text-sm font-medium">Model prices</span>
          {unpriced > 0 && (
            <span className="text-[11px] px-1.5 py-1 rounded border border-amber-400/40 text-amber-400">
              {unpriced} unpriced
            </span>
          )}
          <span className="ml-auto text-xs text-ink-faint">
            {open ? 'Hide' : 'Show'}
          </span>
        </button>
      </h3>

      {open && (
        <div className="px-3 pb-3 space-y-2.5">
          <p className="text-xs text-ink-faint">
            A <strong>rate card</strong> is what Crossband charges a model&apos;s tokens at when it
            works out what a chat cost. These are published list prices, not your actual bill.
            If a model has no rate card, Crossband deliberately records{' '}
            <em>no cost at all</em> rather than guessing from a similarly-named model — that is
            why an unpriced model&apos;s seat can&apos;t be promoted. Adding a rate here fixes
            that immediately, without restarting anything.
          </p>

          {error && (
            <div role="alert" className="flex items-center gap-1.5 text-sm text-red-400">
              <AlertTriangle size={14} /> {error}
            </div>
          )}
          {rows === null && !error && <p className="text-sm text-ink-mid">Loading…</p>}

          {(rows || []).map((row) => {
            const s = cardSummary(row)
            return (
              <div key={row.model} className="border border-edge2 rounded-lg px-3 py-2.5">
                <div className="flex items-center gap-2 flex-wrap">
                  <span className="text-sm font-medium break-all">{row.model}</span>
                  <OriginBadge origin={s.origin} />
                  <span className="ml-auto flex items-center gap-2">
                    {row.origin === 'override' && (
                      <button
                        className="text-xs text-ink-mid hover:text-red-400 inline-flex items-center gap-1"
                        onClick={async () => {
                          try { await api.deleteRateCard(row.model); await load() }
                          catch (e) { setError(e.message) }
                        }}
                        aria-label={`Remove your rate for ${row.model}`}
                      >
                        <Trash2 size={12} /> Remove
                      </button>
                    )}
                    <button
                      className="text-xs text-link hover:opacity-80"
                      onClick={() => setEditing(editing === row.model ? null : row.model)}
                      aria-expanded={editing === row.model}
                    >
                      {s.action}
                    </button>
                  </span>
                </div>
                {s.priced ? (
                  <div className="mt-1 text-xs text-ink-mid">
                    <div>{s.rate}</div>
                    <div className="text-ink-faint">{s.cache}</div>
                    <div className="text-ink-faint break-all">{s.provenance}</div>
                  </div>
                ) : (
                  <div className="mt-1 text-xs text-amber-400">
                    No rate known — turns on this model record no cost, and its seat
                    can&apos;t be promoted.
                  </div>
                )}
                {editing === row.model && (
                  <EditForm
                    row={row}
                    onCancel={() => setEditing(null)}
                    onSaved={async () => { setEditing(null); await load() }}
                  />
                )}
              </div>
            )
          })}

          {rows !== null && rows.length === 0 && (
            <p className="text-sm text-ink-mid flex items-center gap-1.5">
              <Check size={14} /> No models to price yet.
            </p>
          )}
        </div>
      )}
    </section>
  )
}
