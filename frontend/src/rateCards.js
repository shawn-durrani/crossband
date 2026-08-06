// Rate-card display + edit logic for the Models page.
//
// Why this surface exists: pricing fails CLOSED, and a seat's promotion is
// gated on known cost provenance. Both are right, but together they could tell
// an owner a model was unpriced, block its seat on exactly that, and leave no
// remedy except hand-editing config.local.json on the server.
//
// The rule this module encodes, and the reason it is worth unit-testing on its
// own: a rate may only be saved WITH a source and an as-of date. The pricing
// bugs behind that rule were each an untranscribed figure standing in for one
// nobody had, and a form is a far faster way to invent a number than a text
// editor ever was. Making pricing easy must not make guessing easy.
//
// Pure (no React), like lifecycle.js and modelReadout.js — the component
// renders whatever this returns.

// A card's origin, in plain English. Never conflate a shipped default with a
// figure the owner typed: they carry different trust.
export function originLabel(origin) {
  if (origin === 'override') {
    return { label: 'Your rate', tone: 'override',
             title: 'A rate you entered locally. It overrides any built-in card '
                  + 'for this model and lives in config.local.json.' }
  }
  if (origin === 'builtin') {
    return { label: 'Built-in', tone: 'builtin',
             title: 'A published list price shipped with Crossband. It is an '
                  + 'estimate, not a billed amount.' }
  }
  return { label: 'Unpriced', tone: 'unpriced',
           title: 'No rate is known for this model, so its turns record no cost '
                + 'and its seat cannot be promoted. Add a rate to fix that.' }
}

// One row's summary line. Returns null for `card` when unpriced, so the
// component can render the call to action rather than an empty table cell.
export function cardSummary(row) {
  const origin = originLabel(row?.origin)
  if (!row?.priced || !row?.card) {
    return { origin, priced: false, rate: null, provenance: null,
             action: 'Add rate card' }
  }
  const c = row.card
  return {
    origin,
    priced: true,
    rate: `$${fmt(c.input)} in / $${fmt(c.output)} out per 1M tokens`,
    // Cache terms are where the real money hides, so show them rather than
    // burying a 1.25x write premium the owner never agreed to.
    cache: c.cache
      ? `cache read ${c.cache.read_mult}x · write ${c.cache.write_mult}x`
      : 'cache terms: provider default',
    provenance: c.as_of ? `${c.source || 'source not recorded'} · as of ${c.as_of}`
                        : (c.source || 'source not recorded'),
    action: row.origin === 'override' ? 'Edit' : 'Override',
  }
}

function fmt(n) {
  if (n === 0) return '0'
  if (n === null || n === undefined || Number.isNaN(n)) return '?'
  return String(Number(n))
}

// Validate a draft BEFORE it is sent, so the refusal is explained in the form
// rather than surfacing as a 400. The backend re-checks all of this — this is
// the courteous half, not the enforcing half.
// Mirrors backend/ratecard.py's rules so a refusal is explained in the form
// rather than arriving as a 400. The backend re-checks all of it and is the
// enforcing half; this is the courteous half.
export const ESTIMATE = 'rate_card_estimate'
export const SELF_HOSTED = 'self_hosted_zero_marginal'
const MAX_RATE = 100000
const URL_RE = /^https?:\/\/\S+$/i
const ISO_RE = /^\d{4}-\d{2}-\d{2}$/

export function validateDraft(draft) {
  const errors = {}
  // A self-hosted declaration carries no rates at all — its $0 IS the claim —
  // so only the source note is required.
  if (draft?.provenance === SELF_HOSTED) {
    if (!String(draft?.source || '').trim()) {
      errors.source = 'Add a short note saying what this is (e.g. "local (Ollama, '
        + 'self-hosted)") so the declared $0 is auditable later.'
    }
    return { ok: Object.keys(errors).length === 0, errors }
  }
  const num = (v) => (v === '' || v === null || v === undefined ? NaN : Number(v))
  const checkRate = (v, field, label) => {
    const n = num(v)
    if (Number.isNaN(n)) errors[field] = `Enter an ${label} rate in $ per 1M tokens.`
    else if (n < 0) errors[field] = 'A rate cannot be negative.'
    else if (n > MAX_RATE) errors[field] = 'That looks too large — check the units ($ per 1M tokens).'
  }
  checkRate(draft?.input, 'input', 'input')
  checkRate(draft?.output, 'output', 'output')
  const src = String(draft?.source || '').trim()
  if (!src) {
    errors.source = 'Say where this figure came from. An unsourced rate is exactly '
      + 'what fail-closed pricing exists to prevent.'
  } else if (!URL_RE.test(src)) {
    errors.source = 'Use an http(s) link to the provider’s rate card, so the '
      + 'estimate can actually be checked later.'
  }
  const asOf = String(draft?.as_of || '').trim()
  if (!asOf) errors.as_of = 'Enter the date this rate was published or checked.'
  else if (!ISO_RE.test(asOf) || Number.isNaN(Date.parse(asOf))) {
    errors.as_of = 'Use a real date in YYYY-MM-DD form — a date nothing can parse '
      + 'is the same as no date.'
  }
  return { ok: Object.keys(errors).length === 0, errors }
}

// Draft -> request body. Cache terms are omitted entirely when not supplied, so
// the card inherits the provider default exactly as a built-in one does —
// one rule, not two.
export function draftToBody(draft) {
  const provenance = draft.provenance === SELF_HOSTED ? SELF_HOSTED : ESTIMATE
  const aliases = String(draft.aliases || '').split(',').map((s) => s.trim()).filter(Boolean)
  if (provenance === SELF_HOSTED) {
    // No rates, no as_of: the declared $0 IS the claim, and the backend stamps
    // today's date. Sending empty strings would be refused as malformed.
    const body = { provenance, source: String(draft.source).trim() }
    if (aliases.length) body.aliases = aliases
    return body
  }
  const body = {
    provenance,
    input: Number(draft.input),
    output: Number(draft.output),
    source: String(draft.source).trim(),
    as_of: String(draft.as_of).trim(),
  }
  const r = draft.read_mult, w = draft.write_mult
  const has = (v) => v !== '' && v !== null && v !== undefined && !Number.isNaN(Number(v))
  if (has(r) && has(w)) body.cache = { read_mult: Number(r), write_mult: Number(w) }
  if (aliases.length) body.aliases = aliases
  return body
}

// Seed the edit form from an existing row (or blank for an unpriced model).
export function draftFrom(row) {
  const c = row?.card
  return {
    model: row?.model || '',
    provenance: c?.provenance === SELF_HOSTED ? SELF_HOSTED : ESTIMATE,
    input: c ? String(c.input) : '',
    output: c ? String(c.output) : '',
    source: c?.source || '',
    as_of: c?.as_of || '',
    read_mult: c?.cache ? String(c.cache.read_mult) : '',
    write_mult: c?.cache ? String(c.cache.write_mult) : '',
    aliases: (c?.aliases || []).join(', '),
  }
}

// Unpriced models first — they are the reason someone opened this page, and
// the only state that actively blocks a seat. Otherwise alphabetical.
export function sortRows(rows) {
  return [...(rows || [])].sort((a, b) => {
    if (a.priced !== b.priced) return a.priced ? 1 : -1
    return String(a.model).localeCompare(String(b.model))
  })
}
