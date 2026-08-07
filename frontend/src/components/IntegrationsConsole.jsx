import { useEffect, useState } from 'react'
import McpServersPanel from './McpServersPanel.jsx'
import {
  X, RefreshCw, Check, AlertTriangle, KeyRound, SlidersHorizontal, Plug,
  ExternalLink, Loader2, Menu, ChevronRight, ChevronDown,
} from 'lucide-react'
import { api } from '../api'
import {
  groupIntegrations, healthPresentation, credentialPrompt, provenanceLabel,
  seatBadge, seatPromoteState, configHowTo
} from '../integrationsView'

// Integrations console: the steady-state, first-class surface for "what is
// configured and safe to use". It reads the ONE unified status API
// (GET /api/integrations), never a second status source, and groups it
// into capability-specific sections: AI models + their seats, voice, web
// research, memory, and MCP (read-only). It never shows a stored key, adds no
// MCP editing, and cannot make a trial model routinely selectable. Fixing a
// credential hands off to the existing secure setup flow; adding/discovering a
// model hands off to the participants editor. This is a legible operator
// console, not the old dense diagnostic modal.

const TONE = {
  ok: 'text-ok border-ok/40 bg-ok/10',
  warn: 'text-warn border-warn/40 bg-warn/10',
  err: 'text-err border-err/40 bg-err/10',
  muted: 'text-ink-faint border-edge2',
}

function HealthChip({ entry }) {
  const p = healthPresentation(entry)
  const Icon = p.tone === 'ok' ? Check : p.tone === 'err' ? AlertTriangle : null
  return (
    <span
      className={`shrink-0 inline-flex items-center gap-1 rounded-full border px-2 py-0.5 text-[11px] font-medium ${TONE[p.tone]}`}
      title={entry.detail}
    >
      {Icon && <Icon size={11} />}
      {p.label}
    </span>
  )
}

function LifecycleBadge({ badge }) {
  const cls = badge.tone === 'trial'
    ? 'border-amber-500/40 text-amber-600 dark:text-amber-500'
    : 'border-edge2 text-ink-faint'
  return (
    <span
      className={`inline-flex items-center rounded-full border px-1.5 py-0.5 text-[10px] font-medium uppercase tracking-wide ${cls}`}
      title={badge.title}
    >
      {badge.label}
    </span>
  )
}

// One LLM model seat: which model it runs, its lifecycle badge, its cost
// provenance in plain English, and - for a trial seat - the promotion/recovery
// path with its gate reason. No "make default" control exists here: a trial seat
// can never be made routine from this console (only promoted once cost is known).
function Seat({ seat, onPromote, onEdit, promoting }) {
  const badge = seatBadge(seat)
  const promo = seatPromoteState(seat)
  return (
    <div className="border border-edge rounded-lg px-3 py-2.5 space-y-1.5">
      <div className="flex items-center flex-wrap gap-x-2 gap-y-1">
        <span className="text-sm font-medium">{seat.name}</span>
        {!seat.enabled && <span className="text-xs text-ink-dim">(disabled)</span>}
        <LifecycleBadge badge={badge} />
        <button
          className="ml-auto text-xs text-ink-mid hover:text-ink inline-flex items-center gap-1"
          onClick={onEdit}
          title="Edit this seat in the participants editor"
        >
          <SlidersHorizontal size={12} /> Edit
        </button>
      </div>
      <div className="text-xs text-ink-dim break-all">
        <span className="font-mono font-medium text-ink-mid">{seat.model}</span>
        {seat.base_url ? ` · ${seat.base_url}` : ''}
      </div>
      <div className="text-xs text-ink-faint">
        Cost: {provenanceLabel(seat.cost_provenance?.source)}
      </div>
      {badge.tone === 'trial' && (
        <div className="text-xs text-amber-600/90 dark:text-amber-500 leading-relaxed">
          Manual-invoke only - won&apos;t join a normal round on its own; @mention
          or address it by name to include it.
          {promo.show && (
            <div className="mt-1 flex items-start gap-2">
              <button
                type="button"
                className="shrink-0 border border-edge2 rounded-md px-2 py-1 text-xs text-ink-mid hover:border-edge3 disabled:opacity-40 disabled:cursor-not-allowed"
                disabled={!promo.enabled || promoting}
                title={promo.reason}
                onClick={onPromote}
              >
                {promoting ? 'promoting…' : promo.label}
              </button>
              <span className="text-ink-faint">{promo.reason}</span>
            </div>
          )}
        </div>
      )}
    </div>
  )
}


// Onboard a capability WHERE YOU FOUND IT. Previously this handed you to the
// setup wizard, which closed the console, and one Esc then dismissed both,
// landing you back in chat. The key is POSTed to the same validated endpoint the
// wizard uses: it is checked live against the capability's own probe before it is
// saved, never echoed back, and never logged.
function CredentialForm({ prompt, onSaved }) {
  const [open, setOpen] = useState(false)
  const [vals, setVals] = useState({})
  const [busy, setBusy] = useState(false)
  const [err, setErr] = useState(null)

  async function save() {
    setBusy(true); setErr(null)
    try {
      await api.setupKey(prompt.service, vals.key || '', vals.secret || null)
      setOpen(false); setVals({}); onSaved()
    } catch (e) {
      setErr(e.message)
    } finally {
      setBusy(false)
    }
  }

  if (!open) {
    return (
      <button
        className="shrink-0 inline-flex items-center gap-1.5 border border-edge2 rounded-lg px-2.5 py-1.5 text-xs text-ink-mid hover:text-ink hover:border-edge3"
        onClick={() => setOpen(true)}
        title="Paste the key here - it is validated live, saved to your .env on this machine, and never shown back to you."
      >
        <KeyRound size={13} /> {prompt.label}
      </button>
    )
  }
  return (
    <div className="w-full mt-2 space-y-2">
      {prompt.fields.map((f) => (
        <label key={f.name} className="block">
          <span className="text-[11px] text-ink-faint font-mono">{f.name}</span>
          <input
            type="password"
            autoComplete="off"
            className="mt-0.5 w-full bg-app border border-edge2 rounded-lg px-2.5 py-1.5 text-xs focus:outline-none focus:border-edge3"
            placeholder={`paste your ${f.label}`}
            value={vals[f.role] || ''}
            onChange={(e) => setVals({ ...vals, [f.role]: e.target.value })}
          />
        </label>
      ))}
      {err && <p className="text-xs text-amber-500">{err}</p>}
      <div className="flex items-center gap-2">
        <button
          className="bg-btn text-btn-ink rounded-lg px-3 py-1.5 text-xs font-semibold disabled:opacity-40"
          disabled={busy || !(vals.key || '').trim()}
          onClick={save}
        >
          {busy ? 'Checking…' : 'Check and save'}
        </button>
        <button className="text-xs text-ink-faint hover:text-ink-mid"
          onClick={() => { setOpen(false); setErr(null) }}>Cancel</button>
      </div>
      <p className="text-[11px] text-ink-faint leading-relaxed">
        Checked against the provider before saving, then written to <span className="font-mono">.env</span> on
        this machine. It never leaves your machine and is never shown back to you.
      </p>
    </div>
  )
}

// "Show me how" for config-file capabilities: the exact copy-paste snippet,
// in place, because a local app should teach where you are, not link away.
// Collapsed by default; the content is pure data from integrationsView.js.
function ConfigHowTo({ entry }) {
  const [open, setOpen] = useState(false)
  const [copied, setCopied] = useState(false)
  const how = configHowTo(entry)
  if (!how) return null
  return (
    <div className="text-xs">
      <button
        className="inline-flex items-center gap-1 text-link hover:opacity-80"
        aria-expanded={open}
        onClick={() => setOpen((v) => !v)}
      >
        {open ? <ChevronDown size={12} /> : <ChevronRight size={12} />} Show me how
      </button>
      {open && (
        <div className="mt-2 space-y-2 text-ink-mid leading-relaxed">
          <p>{how.intro}</p>
          <div className="relative">
            <pre className="bg-app border border-edge2 rounded-lg p-2.5 overflow-x-auto text-[11px] leading-relaxed text-ink"><code>{how.snippet}</code></pre>
            <button
              className="absolute top-1.5 right-1.5 border border-edge2 rounded px-1.5 py-0.5 text-[10px] text-ink-dim hover:text-ink bg-panel"
              onClick={() => { navigator.clipboard.writeText(how.snippet); setCopied(true); setTimeout(() => setCopied(false), 1500) }}
            >
              {copied ? 'copied ✓' : 'copy'}
            </button>
          </div>
          <p className="text-ink-dim">{how.outro}</p>
        </div>
      )}
    </div>
  )
}

function IntegrationCard({ entry, onFixKey, onAddModel, onPromoteSeat, onEditSeat, promotingSlug }) {
  const isLlm = entry.kind === 'llm'
  const prompt = credentialPrompt(entry)
  return (
    <div className="border border-edge2 rounded-xl bg-panel p-4 space-y-3">
      <div className="flex items-start gap-2">
        <div className="flex-1 min-w-0">
          <div className="flex items-center gap-2 flex-wrap">
            <h4 className="font-medium text-sm">{entry.display_name}</h4>
            <HealthChip entry={entry} />
          </div>
          <p className="text-xs text-ink-mid mt-1 leading-relaxed">{entry.detail}</p>
        </div>
        {prompt && <CredentialForm prompt={prompt} onSaved={onFixKey} />}
        {entry.kind === 'memory' && entry.url && entry.available && (
          <a
            href={entry.url}
            target="_blank"
            rel="noreferrer"
            className="shrink-0 inline-flex items-center gap-1.5 border border-edge2 rounded-lg px-2.5 py-1.5 text-xs text-ink-mid hover:text-ink hover:border-edge3"
            title="Open the memory service to review, correct, or erase what it keeps"
          >
            Open memory <ExternalLink size={12} />
          </a>
        )}
      </div>

      {/* MCP is read-only here: list its tools, and say where it is configured. */}
      {entry.kind === 'mcp' && (
        <div className="text-xs text-ink-faint space-y-1">
          {entry.tools?.length > 0 && (
            <div className="flex flex-wrap gap-1">
              {entry.tools.map((t) => (
                <span key={t} className="font-mono bg-app border border-edge rounded px-1.5 py-0.5">{t}</span>
              ))}
            </div>
          )}
          <p>Read-only here - add or change MCP servers in <span className="font-mono">config.local.json</span>.</p>
        </div>
      )}

      {/* LLM provider: its dependent model seats + the safe add/discover flow. */}
      {isLlm && (
        <div className="space-y-2">
          {entry.seats.length === 0 ? (
            <p className="text-xs text-ink-faint">
              No model seats use this provider yet.
            </p>
          ) : (
            entry.seats.map((s) => (
              <Seat
                key={s.slug}
                seat={s}
                promoting={promotingSlug === s.slug}
                onPromote={() => onPromoteSeat(s)}
                onEdit={() => onEditSeat(s)}
              />
            ))
          )}
          <button
            className="inline-flex items-center gap-1.5 text-xs text-link hover:opacity-80"
            onClick={onAddModel}
          >
            <SlidersHorizontal size={12} /> Add or discover models
          </button>
        </div>
      )}
      <ConfigHowTo entry={entry} />
    </div>
  )
}

export default function IntegrationsConsole({
  participants, onClose, onManageParticipants, onChanged, onOpenMenu,
}) {
  const [entries, setEntries] = useState(null)
  const [serverSections, setServerSections] = useState(null)
  const [loading, setLoading] = useState(true)
  const [probing, setProbing] = useState(false)
  const [error, setError] = useState(null)
  const [promotingSlug, setPromotingSlug] = useState(null)

  // slug → participant id, so a seat's Promote/Edit can reach the underlying
  // participant row (the status API reports seats by slug, not id).
  const slugToId = Object.fromEntries((participants || []).map((p) => [p.slug, p.id]))

  async function load(probe = false) {
    if (probe) setProbing(true)
    else setLoading(true)
    setError(null)
    try {
      const r = await api.integrations(probe)
      setEntries(r.integrations)
      setServerSections(r.sections || null)
    } catch (e) {
      setError(`Couldn't load integration status: ${e.message}`)
    } finally {
      setLoading(false)
      setProbing(false)
    }
  }

  // Open with the LIVE picture: the cheap snapshot renders instantly (so a slow
  // third-party can never block the page, the original concern when checks were
  // button-only), then the probes fill statuses in behind it.
  // A page whose every card says "Not checked yet" until you find a button is
  // a page that opens unsure of itself. The button stays for re-runs.
  useEffect(() => {
    let live = true
    load(false).then(() => { if (live) return load(true) }).catch(() => {})
    return () => { live = false }
  }, [])

  useEffect(() => {
    const onKey = (e) => { if (e.key === 'Escape') onClose() }
    window.addEventListener('keydown', onKey)
    return () => window.removeEventListener('keydown', onKey)
  }, [onClose])

  async function promoteSeat(seat) {
    const id = slugToId[seat.slug]
    if (!id) { setError('Could not find that seat to promote - try refreshing.'); return }
    setError(null)
    setPromotingSlug(seat.slug)
    try {
      // The backend re-checks the cost-provenance gate and 409s with a plain
      // reason if the seat isn't actually onboardable - surfaced verbatim.
      await api.updateParticipant(id, { lifecycle: 'onboarded' })
      await load(false)
      onChanged?.()
    } catch (e) {
      setError(e.message)
    } finally {
      setPromotingSlug(null)
    }
  }

  const sections = groupIntegrations(entries, serverSections)

  return (
    <div className="flex-1 min-h-0 overflow-y-auto">
      <div className="mx-auto w-full max-w-3xl px-4 sm:px-6 py-6 space-y-6">
        <header className="flex items-start gap-3">
          {onOpenMenu && (
            <button
              className="sm:hidden text-ink-mid hover:text-ink p-1 -ml-1 shrink-0"
              aria-label="Open chats & settings"
              onClick={onOpenMenu}
            >
              <Menu size={20} />
            </button>
          )}
          <div className="flex-1 min-w-0">
            <h1 className="text-lg font-semibold flex items-center gap-2">
              <Plug size={18} className="text-ink-dim" /> Connections
            </h1>
            <p className="text-sm text-ink-mid mt-0.5">
              Everything your room can use - what&apos;s connected, healthy, and safe to
              rely on. Fixing a key opens the secure setup flow; keys are never shown here.
            </p>
          </div>
          <button
            className="shrink-0 inline-flex items-center gap-1.5 border border-edge2 rounded-lg px-3 py-1.5 text-sm text-ink-mid hover:text-ink hover:border-edge3 disabled:opacity-50"
            onClick={() => load(true)}
            disabled={probing || loading}
            title="Run each capability's own live check (a model list, an account check, a search probe). Off by default so opening this page makes no external calls."
          >
            <RefreshCw size={14} className={probing ? 'animate-spin' : ''} />
            {probing ? 'Checking…' : 'Run live checks'}
          </button>
          <button
            className="shrink-0 text-ink-dim hover:text-ink p-1.5"
            aria-label="Close integrations"
            title="Back to chat"
            onClick={onClose}
          >
            <X size={18} />
          </button>
        </header>

        {error && (
          <div className="flex items-center gap-1.5 text-sm text-err">
            <AlertTriangle size={14} /> {error}
          </div>
        )}

        {loading && !entries && (
          <div className="flex items-center gap-2 text-sm text-ink-faint py-10 justify-center">
            <Loader2 size={16} className="animate-spin" /> Loading integration status…
          </div>
        )}

        {sections.map((sec) => (
          <section key={sec.kind} className="space-y-3">
            <div>
              <h2 className="text-[11px] font-semibold uppercase tracking-[0.06em] text-ink-dim">
                {sec.title}
              </h2>
              <p className="text-xs text-ink-faint mt-0.5">{sec.blurb}</p>
            </div>
            <div className="space-y-3">
              {sec.entries.map((entry) => (
                <IntegrationCard
                  key={entry.id}
                  entry={entry}
                  promotingSlug={promotingSlug}
                  onFixKey={() => load(true)}
                  onAddModel={() => onManageParticipants({ action: 'add' })}
                  onEditSeat={(seat) => onManageParticipants({ action: 'edit', slug: seat.slug })}
                  onPromoteSeat={promoteSeat}
                />
              ))}
            </div>
          </section>
        ))}

        <McpServersPanel />
      </div>
    </div>
  )
}
