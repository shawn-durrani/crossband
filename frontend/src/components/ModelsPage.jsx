import { useEffect, useRef, useState } from 'react'
import { Check, AlertTriangle, RefreshCw, Download, Play, ExternalLink, Menu, Users } from 'lucide-react'
import { api } from '../api'
import { PRESETS, matchPreset } from '../presets'
import { modelReadoutLines } from '../modelReadout'
import { lifecycleBadge, promoteState, addSeatNotice } from '../lifecycle'
import RateCardsPanel from './RateCardsPanel'

const EMPTY = {
  name: '', provider: 'anthropic', model: '', base_url: '', api_key_env: '',
  system_prompt: '', color: '#a78bfa', enabled: true, reasoning_effort: '',
}

// Does this model support a reasoning-effort setting? Mirrors the per-provider
// gating in backend/providers.py (REASONING_CHOICES / _anthropic_unsupported_model)
// so the UI can grey it out when it won't apply (the runtime also drops it
// gracefully as a backstop). The policy is authoritative: Default sends no
// override at all, a fixed level sends only that level, and Adaptive
// (Anthropic-only) is the SOLE way to get provider-decided variable-duration
// thinking. Voice mode never silently downgrades any of this.
function effortSupport(provider, model) {
  const m = (model || '').toLowerCase()
  if (provider === 'anthropic') {
    if (/haiku|claude-3|sonnet-4-5|sonnet-4\.5|sonnet-4-0/.test(m))
      return { ok: false, note: `Not supported on this Claude model (Haiku / Sonnet 4.5) - it would be ignored.` }
    return { ok: true, note: `Default sends no override. Low/Medium/High/Max set output_config.effort ("Max" needs Opus 4.6+, otherwise uses High) - a bounded, non-deliberating request. Adaptive lets Claude decide how long to think per reply, which can add several seconds - including in voice - before the first word.` }
  }
  if (/^gpt-5|^o1|^o3|^o4/.test(m))
    return { ok: true, note: `OpenAI: Default sends no override; Low/Medium/High set reasoning.effort ("Max" maps to High). Applies in tool-free chats; automatically skipped when 🌐 research tools are on (provider limitation). No Adaptive option - that's an Anthropic-only concept.` }
  return { ok: false, note: `Only OpenAI reasoning models (gpt-5.x, o-series) support reasoning effort - it would be ignored otherwise.` }
}

// The <select> options for this provider - Default + the fixed levels always;
// Adaptive appended ONLY for Anthropic (backend/providers.py's
// ANTHROPIC_REASONING_CHOICES vs OPENAI_REASONING_CHOICES - a future
// OpenAI-compatible provider added the same way, provider="openai" plus a
// custom base_url, gets the same OpenAI set here automatically since it's
// keyed on `provider`, not on model or base_url).
function reasoningOptions(provider) {
  const fixed = [
    { value: '', label: 'Default (no effort/thinking override - provider’s plain default)' },
    { value: 'low', label: 'Low' },
    { value: 'medium', label: 'Medium' },
    { value: 'high', label: 'High' },
    { value: 'max', label: 'Max' },
  ]
  if (provider === 'anthropic') {
    fixed.push({ value: 'adaptive',
                label: 'Adaptive (Claude decides thinking duration - can add seconds, incl. in voice)' })
  }
  return fixed
}

// Glanceable "is this what's actually running?" readout for one seat.
// Silent when there's nothing worth flagging: no readout yet, or the live
// model, the last reply's stamp, and the config seed all agree. Speaks up only
// on drift: the model that produced the last reply, or a stale config seed.
//
// Layout: each line is icon + one *flowing* prose span, not a flex row of
// separate label/value chips. On a phone width that keeps "config seed was X,
// live now differs" wrapping as one muted sentence instead of fragmenting so the
// stale seed value lands alone on its own line and reads as the headline number.
// `break-all` on the mono id stops a long model name from overflowing/overlapping.
// Which lines show, in what order, and their tone live in modelReadoutLines().
const READOUT_TONE = {
  attention: 'text-amber-500',
  confirm: 'text-ink-faint',
  muted: 'text-ink-faint',
}

function ModelReadout({ status }) {
  const lines = modelReadoutLines(status)
  if (!lines.length) return null
  return (
    <div className="text-xs mt-1 space-y-1">
      {lines.map((ln) => (
        <div key={ln.key} className={`flex items-baseline gap-1 ${READOUT_TONE[ln.tone]}`}>
          {ln.icon === 'alert' && <AlertTriangle size={11} className="shrink-0 translate-y-px" />}
          {ln.icon === 'check' && <Check size={11} className="shrink-0 translate-y-px" />}
          <span className="min-w-0">
            {ln.label} <span className="font-mono break-all">{ln.model}</span>
            {ln.tail ? ` ${ln.tail}` : ''}
          </span>
        </div>
      ))}
    </div>
  )
}

// Small pill showing a seat's onboarding lifecycle. Trial is the loud,
// actionable state (amber); Onboarded is the calm normal state. The badge text
// is the stable anchor; the tooltip carries the plain-English "what & why".
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

// `intent` carries WHY you arrived, so the page lands where the click
// promised: {action:'add'} opens the add form immediately; {action:'edit',
// slug} opens that seat's editor. `backLabel` names where onClose actually
// returns to ("Back to Connections" when that's where you came from): a
// button that says "Back to chat" and means it is navigation; one that says
// it and lies is how you lose your place.
export default function ModelsPage({ participants, settings, voiceEnabled, onChanged, onClose, onOpenMenu, intent = null, backLabel = 'Back to chat' }) {
  const [editing, setEditing] = useState(null) // participant object or EMPTY clone
  const [saving, setSaving] = useState(false)
  const [error, setError] = useState(null)
  const [models, setModels] = useState(null) // null | [] | [{id,label}]
  const [loadingModels, setLoadingModels] = useState(false)
  const [shared, setShared] = useState(settings?.shared_instructions ?? '')
  const [sharedSaved, setSharedSaved] = useState(false)
  const [voices, setVoices] = useState(null)
  const [modelStatus, setModelStatus] = useState(null) // { [slug]: {...} } | null
  const [refreshKey, setRefreshKey] = useState(0) // bump to re-pull the readout
  const [promoting, setPromoting] = useState(null) // participant id mid-promote
  const previewAudio = useRef(null)

  // Land where the click promised: consume the arrival intent once.
  const intentDone = useRef(false)
  useEffect(() => {
    if (intentDone.current || !intent) return
    intentDone.current = true
    if (intent.action === 'add') { setModels(null); setEditing({ ...EMPTY }) }
    else if (intent.action === 'edit' && intent.slug) {
      const seat = participants.find((x) => x.slug === intent.slug)
      if (seat) { setModels(null); setEditing({ ...seat }) }
    }
  }, [intent, participants])

  // Pull the read-only "what's actually running" readout when the roster list
  // is showing, and re-pull after any save. Answers "did the switch land?"
  // without a DB query: live model + the model that stamped the last reply.
  useEffect(() => {
    if (editing) return
    let live = true
    api.modelsStatus()
      .then((r) => { if (live) setModelStatus(Object.fromEntries(r.participants.map((p) => [p.slug, p]))) })
      .catch(() => { if (live) setModelStatus(null) })
    return () => { live = false }
  }, [editing, refreshKey])

  async function fetchVoices() {
    try {
      const r = await api.voiceVoices()
      setVoices(r.voices)
    } catch (e) {
      setError(`Couldn't fetch voices: ${e.message}`)
    }
  }

  function playPreview(voiceId) {
    const v = voices?.find((x) => x.voice_id === voiceId)
    previewAudio.current?.pause()
    if (v?.preview_url) {
      previewAudio.current = new Audio(v.preview_url)
      previewAudio.current.play().catch(() => {})
    }
  }

  async function saveShared() {
    setError(null)
    try {
      await api.updateSettings({ shared_instructions: shared })
      setSharedSaved(true)
      setTimeout(() => setSharedSaved(false), 1500)
      onChanged()
    } catch (e) {
      setError(e.message)
    }
  }

  async function fetchModels() {
    setLoadingModels(true)
    setError(null)
    try {
      const r = await api.listModels(editing.provider, editing.api_key_env, editing.base_url)
      setModels(r.models)
    } catch (e) {
      setModels(null)
      setError(`Couldn't fetch models: ${e.message} - you can still type a model ID manually.`)
    } finally {
      setLoadingModels(false)
    }
  }

  // Applying a preset fills base_url + api_key_env; both stay editable after.
  // "Custom" clears them so the user starts fresh; "OpenAI (default)" clears the
  // base_url (SDK default) but sets the standard key env. Local presets need no key.
  function applyPreset(id) {
    const p = PRESETS.find((x) => x.id === id)
    if (!p) return
    setEditing((e) => ({
      ...e,
      // Presets are all OpenAI-compatible endpoints; picking one sets the API
      // style itself, because a newcomer clicking "Ollama" shouldn't need to
      // know to switch a dropdown first.
      provider: 'openai',
      reasoning_effort: e.reasoning_effort === 'adaptive' ? '' : e.reasoning_effort,
      base_url: p.base_url,
      api_key_env: p.api_key_env,
    }))
  }

  async function save() {
    setSaving(true)
    setError(null)
    try {
      const body = {
        name: editing.name,
        provider: editing.provider,
        model: editing.model,
        base_url: editing.base_url || null,
        api_key_env: editing.api_key_env || null,
        system_prompt: editing.system_prompt ?? '',
        color: editing.color,
        enabled: editing.enabled,
        voice_id: editing.voice_id ?? null,
        voice_gain: editing.voice_gain ?? 1,
        reasoning_effort: editing.reasoning_effort ?? '',
      }
      if (editing.id) await api.updateParticipant(editing.id, body)
      else await api.createParticipant(body)
      setEditing(null)
      onChanged()
    } catch (e) {
      setError(e.message)
    } finally {
      setSaving(false)
    }
  }

  async function remove(p) {
    if (!confirm(`Remove ${p.name}? Their past messages stay in chat history.`)) return
    try {
      await api.deleteParticipant(p.id)
      onChanged()
    } catch (e) {
      setError(e.message)
    }
  }

  // Promote a trial seat to onboarded. The backend re-checks the cost-provenance
  // gate and returns 409 with a plain-English reason if the seat isn't actually
  // onboardable - we surface that verbatim rather than guessing client-side, so
  // the button never silently no-ops.
  async function promote(p) {
    setError(null)
    setPromoting(p.id)
    try {
      await api.updateParticipant(p.id, { lifecycle: 'onboarded' })
      onChanged()
      setRefreshKey((k) => k + 1)
    } catch (e) {
      setError(e.message)
    } finally {
      setPromoting(null)
    }
  }

  const field = 'mt-1 w-full bg-app border border-edge2 rounded-lg px-3 py-2 text-sm focus:outline-none focus:border-edge3'
  const eff = editing ? effortSupport(editing.provider, editing.model) : null
  // Which preset the current base_url/api_key_env corresponds to (openai style only).
  const presetId = editing && editing.provider === 'openai' ? matchPreset(editing) : null
  const preset = presetId ? PRESETS.find((p) => p.id === presetId) : null
  // Trial-seat explainer for a seat being ADDED, and null while editing an
  // existing one. Recomputed as the draft changes, so picking the Ollama preset
  // flips it to "you can promote this straight away" while the user watches.
  const addNotice = editing ? addSeatNotice(editing) : null

  return (
    <div className="flex-1 min-h-0 overflow-y-auto">
      <div className="mx-auto w-full max-w-3xl px-4 sm:px-6 py-6 space-y-4">
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
              <Users size={18} className="text-ink-dim" /> Models
            </h1>
            <p className="text-sm text-ink-mid mt-0.5">
              The models in your room. Each seat has its own name, persona and voice -
              and you choose which model version it runs.
            </p>
          </div>
          <button
            className="shrink-0 border border-edge2 rounded-lg px-3 py-1.5 text-sm text-ink-mid hover:border-edge3"
            onClick={() => { setModels(null); setEditing({ ...EMPTY }) }}
          >
            + Add model
          </button>
        </header>
        {error && <div className="flex items-center gap-1.5 text-sm text-red-400"><AlertTriangle size={14} /> {error}</div>}

        {!editing && (
          <label className="block">
            <span className="text-sm text-ink-mid flex items-baseline">
              Shared instructions
              <span className="text-ink-faint ml-1.5">(every participant, every chat - controls tone & length)</span>
              <button
                className="ml-auto inline-flex items-center gap-1 text-xs text-link hover:opacity-80"
                onClick={saveShared}
              >
                {sharedSaved ? <><Check size={12} /> saved</> : 'Save'}
              </button>
            </span>
            <textarea
              className="mt-1 w-full bg-app border border-edge2 rounded-lg px-3 py-2 text-sm h-28 focus:outline-none focus:border-edge3"
              value={shared}
              onChange={(e) => setShared(e.target.value)}
              placeholder="e.g. Keep replies short and conversational…"
            />
          </label>
        )}

        {/* The remedy for an unpriced model, next to the seats whose promotion
            an unpriced model blocks, collapsed by default so it doesn't crowd
            the roster. */}
        {!editing && <RateCardsPanel />}

        {!editing && (
          <div className="space-y-2">
            {participants.map((p) => {
              const st = modelStatus?.[p.slug]
              const badge = lifecycleBadge(st, p.lifecycle)
              const promo = promoteState(st, p.lifecycle)
              return (
              <div key={p.id} className="flex items-center gap-3 border border-edge rounded-xl px-3 py-2.5">
                <span className="h-3 w-3 rounded-full shrink-0" style={{ backgroundColor: p.color }} />
                <div className="flex-1 min-w-0">
                  <div className="text-sm font-medium flex items-center flex-wrap gap-x-2 gap-y-1">
                    <span>{p.name}</span>
                    {!p.enabled && <span className="text-xs text-ink-dim">(disabled)</span>}
                    {badge && <LifecycleBadge badge={badge} />}
                  </div>
                  {/* The live/configured model drives the next turn, so it is the
                      dominant value on the row: brighter + medium weight, and
                      allowed to wrap rather than truncate so it is never cut off in
                      favour of a muted seed value in the readout below. */}
                  <div className="text-xs text-ink-dim break-all">
                    {p.provider} · <span className="font-mono font-medium text-ink-mid">{p.model}</span>
                    {p.system_prompt ? ' · has persona' : ''}
                  </div>
                  <ModelReadout status={st} />
                  {/* A trial seat is a REAL gate: it sits out unaddressed rounds.
                      Say so here so it never looks like the seat silently vanished,
                      and offer the promotion / recovery path inline. */}
                  {badge?.tone === 'trial' && (
                    <div className="mt-1.5 text-xs text-amber-600/90 dark:text-amber-500 leading-relaxed">
                      Manual-invoke only - won't join a normal round on its own;
                      @mention or address it by name to include it.
                      {promo.show && (
                        <div className="mt-1 flex items-start gap-2">
                          <button
                            type="button"
                            className="shrink-0 border border-edge2 rounded-md px-2 py-1 text-xs text-ink-mid hover:border-edge3 disabled:opacity-40 disabled:cursor-not-allowed"
                            disabled={!promo.enabled || promoting === p.id}
                            title={promo.reason}
                            onClick={() => promote(p)}
                          >
                            {promoting === p.id ? 'promoting…' : promo.label}
                          </button>
                          <span className="text-ink-faint">{promo.reason}</span>
                        </div>
                      )}
                    </div>
                  )}
                </div>
                <button className="text-xs text-ink-mid hover:text-ink" onClick={() => { setModels(null); setEditing({ ...p, enabled: !!p.enabled }) }}>
                  Edit
                </button>
                <button className="text-xs text-ink-faint hover:text-red-400" onClick={() => remove(p)}>
                  Remove
                </button>
              </div>
            )})}
          </div>
        )}

        {editing && (
          <div className="space-y-3">
            {/* One-click starts, up front: these were hidden behind the
                API-style dropdown, so "add Ollama" required already knowing
                Ollama is OpenAI-compatible. Picking one fills style + endpoint
                + key env; everything stays editable below. */}
            {!editing.id && (
              <div className="flex flex-wrap items-center gap-1.5 text-xs">
                <span className="text-ink-dim mr-0.5">Quick start:</span>
                {PRESETS.filter((p) => p.id !== 'custom').map((p) => (
                  <button
                    key={p.id}
                    type="button"
                    title={p.hint}
                    className="rounded-full border border-edge2 px-2.5 py-1 text-ink-mid hover:text-ink hover:border-edge3"
                    onClick={() => applyPreset(p.id)}
                  >
                    {p.label}
                  </button>
                ))}
                <span className="text-ink-faint">- or fill the form for anything else</span>
              </div>
            )}
            <div className="grid grid-cols-2 gap-3">
              <label className="block">
                <span className="text-sm text-ink-mid">Display name</span>
                <input className={field} value={editing.name} autoFocus
                  placeholder="e.g. Gemini"
                  onChange={(e) => setEditing({ ...editing, name: e.target.value })} />
              </label>
              <label className="block">
                <span className="text-sm text-ink-mid">Color</span>
                <input type="color" className="mt-1 h-9 w-full bg-app border border-edge2 rounded-lg cursor-pointer"
                  value={editing.color}
                  onChange={(e) => setEditing({ ...editing, color: e.target.value })} />
              </label>
              <label className="block">
                <span className="text-sm text-ink-mid">API style</span>
                <select className={field} value={editing.provider}
                  onChange={(e) => {
                    setModels(null)
                    const provider = e.target.value
                    // "adaptive" is Anthropic-only, so switching to OpenAI with
                    // it selected would otherwise fail validation silently at
                    // save time; reset to Default instead so the UI stays honest.
                    const reasoning_effort = editing.reasoning_effort === 'adaptive' && provider !== 'anthropic'
                      ? '' : editing.reasoning_effort
                    setEditing({ ...editing, provider, reasoning_effort })
                  }}>
                  <option value="anthropic">Anthropic</option>
                  <option value="openai">OpenAI / OpenAI-compatible</option>
                </select>
              </label>
              <label className="block">
                <span className="text-sm text-ink-mid flex items-center">
                  Model / version
                  <button
                    type="button"
                    className="ml-auto text-xs text-link hover:opacity-80 disabled:opacity-40"
                    disabled={loadingModels}
                    onClick={fetchModels}
                  >
                    {loadingModels ? 'fetching…' : models
                      ? <span className="inline-flex items-center gap-1"><RefreshCw size={12} /> refresh list</span>
                      : <span className="inline-flex items-center gap-1"><Download size={12} /> fetch available models</span>}
                  </button>
                </span>
                {models ? (
                  <select
                    className={field}
                    value={models.some((m) => m.id === editing.model) ? editing.model : ''}
                    onChange={(e) => setEditing({ ...editing, model: e.target.value })}
                  >
                    {!models.some((m) => m.id === editing.model) && (
                      <option value="" disabled>
                        {editing.model ? `current: ${editing.model} (not in list)` : 'choose a model…'}
                      </option>
                    )}
                    {models.map((m) => (
                      <option key={m.id} value={m.id}>{m.label !== m.id ? `${m.label} - ${m.id}` : m.id}</option>
                    ))}
                  </select>
                ) : (
                  <input className={field} value={editing.model}
                    placeholder="e.g. claude-opus-4-8, gpt-5.1"
                    onChange={(e) => setEditing({ ...editing, model: e.target.value })} />
                )}
              </label>
              {editing.provider === 'openai' && (
                <label className="block col-span-2">
                  <span className="text-sm text-ink-mid">
                    Preset{' '}
                    <span className="text-ink-faint">(fills the two fields below - pick a local or hosted endpoint, or Custom)</span>
                  </span>
                  <select
                    className={field}
                    value={presetId || 'custom'}
                    onChange={(e) => applyPreset(e.target.value)}
                  >
                    {PRESETS.map((p) => (
                      <option key={p.id} value={p.id}>{p.label}</option>
                    ))}
                  </select>
                  {preset && (
                    <span className="mt-1 flex items-start gap-2 text-xs text-ink-faint">
                      <span className="flex-1">{preset.hint}</span>
                      {preset.needsKey && preset.getKeyUrl && (
                        <a
                          href={preset.getKeyUrl}
                          target="_blank"
                          rel="noreferrer"
                          className="shrink-0 inline-flex items-center gap-1 text-link hover:underline"
                        >
                          Get a key <ExternalLink size={11} />
                        </a>
                      )}
                    </span>
                  )}
                </label>
              )}
              <label className="block">
                <span className="text-sm text-ink-mid">Base URL <span className="text-ink-faint">(optional - for OpenAI-compatible APIs)</span></span>
                <input className={field} value={editing.base_url || ''}
                  placeholder="e.g. https://generativelanguage.googleapis.com/v1beta/openai/"
                  onChange={(e) => setEditing({ ...editing, base_url: e.target.value })} />
              </label>
              <label className="block">
                <span className="text-sm text-ink-mid">API key env var <span className="text-ink-faint">(name only - key goes in .env)</span></span>
                <input className={field} value={editing.api_key_env || ''}
                  placeholder={editing.provider === 'anthropic' ? 'ANTHROPIC_API_KEY' : 'OPENAI_API_KEY'}
                  onChange={(e) => setEditing({ ...editing, api_key_env: e.target.value })} />
              </label>
            </div>
            <label className="block">
              <span className="text-sm text-ink-mid">
                Reasoning effort{' '}
                <span className="text-ink-faint">(how hard it thinks before replying - higher is deeper but slower and pricier)</span>
              </span>
              <select
                className={`${field} disabled:opacity-50 disabled:cursor-not-allowed`}
                disabled={!eff?.ok}
                value={editing.reasoning_effort || ''}
                onChange={(e) => setEditing({ ...editing, reasoning_effort: e.target.value })}>
                {reasoningOptions(editing.provider).map((o) => (
                  <option key={o.value} value={o.value}>{o.label}</option>
                ))}
              </select>
              <span className={`mt-1 block text-xs ${eff?.ok ? 'text-ink-faint' : 'text-amber-500'}`}>
                {eff?.note}
              </span>
            </label>
            {voiceEnabled && (
              <label className="block">
                <span className="text-sm text-ink-mid flex items-center">
                  Voice (ElevenLabs)
                  {!voices && (
                    <button type="button" className="ml-auto inline-flex items-center gap-1 text-xs text-link hover:opacity-80" onClick={fetchVoices}>
                      <Download size={12} /> fetch voices
                    </button>
                  )}
                </span>
                {voices ? (
                  <div className="flex items-center gap-2">
                    <select
                      className={field}
                      value={editing.voice_id || ''}
                      onChange={(e) => {
                        setEditing({ ...editing, voice_id: e.target.value })
                        playPreview(e.target.value) // hear it as you pick it
                      }}
                    >
                      <option value="">(auto-assign on first voice session)</option>
                      {voices.map((v) => (
                        <option key={v.voice_id} value={v.voice_id}>{v.name}</option>
                      ))}
                    </select>
                    <button
                      type="button"
                      title="Play sample of the selected voice"
                      className="mt-1 shrink-0 border border-edge2 rounded-lg px-2.5 py-2 text-sm text-ink-mid hover:border-edge3 disabled:opacity-40"
                      disabled={!editing.voice_id}
                      onClick={() => playPreview(editing.voice_id)}
                    >
                      <Play size={14} />
                    </button>
                  </div>
                ) : (
                  <input
                    className={field}
                    value={editing.voice_id || ''}
                    placeholder="voice ID - or fetch the list above"
                    onChange={(e) => setEditing({ ...editing, voice_id: e.target.value })}
                  />
                )}
              </label>
            )}
            {voiceEnabled && (
              <label className="block">
                <span className="text-sm text-ink-mid">
                  Voice volume{' '}
                  <span className="text-ink-faint">
                    (turn a loud voice down so everyone speaks at the same level)
                  </span>
                </span>
                <span className="mt-1 flex items-center gap-2 text-xs text-ink-dim">
                  <input
                    type="range" min="0.2" max="1" step="0.05"
                    value={editing.voice_gain ?? 1}
                    onChange={(e) => setEditing({ ...editing, voice_gain: Number(e.target.value) })}
                    className="w-40 accent-sky-500 cursor-pointer"
                  />
                  <span className="tabular-nums">{Math.round((editing.voice_gain ?? 1) * 100)}%</span>
                </span>
              </label>
            )}
            {editing.id && (
              <p className="text-xs text-ink-dim leading-relaxed">
                Switching the model/version is non-destructive: chat history and project
                memory are plain text, so the new model inherits the full transcript and
                this persona. Past replies stay attributed as they were. Only the reply
                style changes going forward - and the first reply after a switch re-reads
                the whole history without the provider's cache (a one-off cost blip).
              </p>
            )}
            <label className="block">
              <span className="text-sm text-ink-mid">System prompt / persona <span className="text-ink-faint">(this model only, every chat)</span></span>
              <textarea className={`${field} h-28`} value={editing.system_prompt || ''}
                placeholder="e.g. You are the skeptic of the group - stress-test ideas and play devil's advocate."
                onChange={(e) => setEditing({ ...editing, system_prompt: e.target.value })} />
            </label>
            <label className="flex items-center gap-2 text-sm text-ink-mid">
              <input type="checkbox" checked={!!editing.enabled}
                onChange={(e) => setEditing({ ...editing, enabled: e.target.checked })} />
              Enabled (available to join chats)
            </label>
            {addNotice && (
              <div className="rounded-lg border border-amber-500/40 bg-amber-500/5 px-3 py-2.5 text-xs">
                <div className="flex items-baseline gap-1.5">
                  <AlertTriangle size={12} className="shrink-0 translate-y-0.5 text-amber-600 dark:text-amber-500" />
                  <span className="font-medium text-amber-700 dark:text-amber-400">{addNotice.title}</span>
                </div>
                <p className="mt-1 text-ink-mid">{addNotice.body}</p>
                <p className="mt-1 text-ink-faint">{addNotice.promotion}</p>
              </div>
            )}
            <div className="flex justify-end gap-2 pt-1">
              <button className="border border-edge2 rounded-lg px-4 py-2 text-sm text-ink-mid hover:border-edge3"
                onClick={() => setEditing(null)}>
                Back
              </button>
              <button
                className="bg-btn text-btn-ink rounded-lg px-4 py-2 text-sm font-semibold hover:bg-btn-hover disabled:opacity-40"
                disabled={saving || !editing.name.trim() || !editing.model.trim()}
                onClick={save}
              >
                {editing.id ? 'Save' : 'Add'}
              </button>
            </div>
          </div>
        )}

        {!editing && (
          <div className="flex justify-end pt-2">
            <button className="border border-edge2 rounded-lg px-4 py-2 text-sm text-ink-mid hover:border-edge3" onClick={onClose}>
              {backLabel}
            </button>
          </div>
        )}
      </div>
    </div>
  )
}
