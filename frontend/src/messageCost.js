// Cost honesty inside the chat itself.
//
// The Spend page has long kept three kinds of dollar apart: `metered`
// (pay-as-you-go API billing, i.e. real cash), `subscription_equiv` (a Claude
// Code guest turn that ran on a subscription OAuth login: an API-list-price
// ESTIMATE the plan already covers, never incremental spend) and `unknown` (a
// turn that recorded no billing mode at all). The chat surface didn't: it
// summed every `usage_json.cost` into one `~$X`, so subscription turns that
// cost nothing extra read as money being charged. Picture a busy chat where
// those estimates pile up into a three-figure `~$X` while the actual invoice
// stays near zero, and you have a number meant to inform that alarms instead.
//
// This module is the chat surface's half of that rule. It mirrors
// backend/accounting.py's CASH axis for ONE MESSAGE at a time, which is what
// the per-message chip needs and what no backend endpoint offers.
//
// It deliberately does NOT roll a chat up. It used to, by summing usage_json
// off message rows, and that could only ever see messages: voice spend
// arrived separately and utility spend not at all, because those calls never
// go through db.insert_message. The header and the export picker printed
// different dollars for the same chat through this very formatter. The chat
// total now comes from the server, off the same scan the picker reads.
//
// Pure so the honesty rules are unit-tested rather than argued about in JSX.


// The summoned Claude Code guest (backend/guest.py SLUG; the same literal
// speakers.js badges). Its turns are the ONLY ones whose billing mode varies at
// runtime: a resident model turn is always key-billed, because the
// Messages/Responses API cannot be served by a subscription.
export const GUEST_SLUG = 'claude-code'

// usage_json.auth → cash bucket. Mirrors accounting._AUTH_CATEGORY.
const AUTH_CATEGORY = { api_key: 'metered', subscription: 'subscription_equiv' }

// usage_json.auth → provenance, for rows written before per-turn provenance
// capture landed. Mirrors accounting._AUTH_PROVENANCE.
const AUTH_PROVENANCE = {
  api_key: 'provider_reported',
  subscription: 'subscription_equivalent',
}

// backend/provenance.py PROVENANCE_STATES - an unrecognised value is treated as
// no record at all rather than trusted.
const PROVENANCE_STATES = new Set([
  'provider_reported', 'rate_card_estimate', 'self_hosted_zero_marginal',
  'subscription_equivalent', 'unknown',
])

// Same palette as the Spend page, so one kind of dollar looks the same
// everywhere. Colour is never the only signal - every figure below is also
// named in words.
export const COST_TONE = {
  billed: '',
  covered: 'text-amber-500',
  unverified: 'text-ink-dim',
  declared: 'text-ink-dim',
}

export function money(n) {
  if (!n) return '$0.00'
  return `$${n.toFixed(n < 0.01 ? 4 : n < 0.1 ? 3 : 2)}`
}

function parseUsage(usageJson) {
  if (!usageJson) return null
  if (typeof usageJson === 'object') return usageJson
  try {
    const u = JSON.parse(usageJson)
    return u && typeof u === 'object' ? u : null
  } catch {
    return null
  }
}

// One message's usage, sorted onto both cost axes. Returns null when the
// message carries no usage record (a user turn, an unparseable blob).
export function classifyUsage(usageJson, speaker) {
  const u = parseUsage(usageJson)
  if (!u) return null
  const guest = speaker === GUEST_SLUG
  const auth = String(u.auth || '').trim().toLowerCase()
  // A provenance RECORDED at turn time is authoritative and immutable:
  // a later rate-card edit must never rewrite a turn's history. Only a row
  // without one falls back to what its auth mode implies.
  const source = u.cost_provenance?.source
  const recorded = PROVENANCE_STATES.has(source) ? source : null
  const input = (u.input || 0) + (u.cache_read || 0) + (u.cache_creation || 0)
  const output = u.output || 0
  return {
    input,
    output,
    tokens: input + output,
    cost: u.cost == null ? 0 : u.cost,
    // A turn can exist without a tracked cost. That is a gap, not a $0 - the
    // two are never rendered the same way.
    hasCost: u.cost != null,
    // CASH axis: is this incremental money? A resident turn is metered whatever
    // else is missing. Only a guest turn can be covered, and a guest turn that
    // recorded no auth mode is `unknown` rather than quietly counted as spend.
    category: guest ? (AUTH_CATEGORY[auth] || 'unknown') : 'metered',
    // HOW-KNOWN axis. The backend resolves a missing value against the live
    // price table; the browser has no price table, so an unrecorded resident
    // row reads as `unknown` HERE while still being metered cash. Only the cash
    // axis decides what money is claimed, so the surfaces still agree on that.
    provenance: recorded || (guest ? (AUTH_PROVENANCE[auth] || 'unknown') : 'unknown'),
  }
}

// What one message's cost says, in words.
//
// The rule that matters: a figure that is NOT money carries a visible
// plain-English qualifier - never a colour or an icon alone, and never anything
// less visible than the number it qualifies. `~` means the figure is an
// estimate; a provider-reported amount and a declared $0 are not hedged,
// because neither is a guess.
export function costLabel(entry) {
  if (!entry || !entry.hasCost) return null
  const amount = money(entry.cost)
  if (entry.provenance === 'self_hosted_zero_marginal') {
    return {
      tone: 'declared',
      text: `${amount} self-hosted`,
      title: 'Ran on your own hardware, so there is no metered charge. A '
        + 'declared $0 - not a missing figure.',
    }
  }
  if (entry.category === 'subscription_equiv') {
    return {
      tone: 'covered',
      text: `${amount} subscription - no extra charge`,
      title: 'This turn ran on your Claude subscription. The amount is what the '
        + 'same work would have cost on the metered API; your plan already '
        + 'covers it, so you were not charged for this turn.',
    }
  }
  if (entry.category === 'unknown') {
    return {
      tone: 'unverified',
      text: `~${amount} unverified`,
      title: "This turn didn't record whether it billed to your API key or ran "
        + 'on a subscription, so there is no way to say whether it cost money. '
        + 'Shown apart rather than counted as spend.',
    }
  }
  const exact = entry.provenance === 'provider_reported'
  return {
    tone: 'billed',
    text: `${exact ? '' : '~'}${amount}`,
    title: exact
      ? 'Billed to your metered API key - the cost the provider itself reported '
        + 'for this turn.'
      : 'Billed to your metered API key. Estimated from the local price table '
        + '(prices editable in config.json).',
  }
}

// The chat total as labelled figures, biggest question first. Every dollar
// figure is named, so nobody has to work out which kind it is.
//
// When anything non-billed is present, billed is stated even at zero: "$0.00
// billed" answers "am I being charged for this?" outright, where an absent
// figure leaves the reader to infer it.
export function chatCostFigures(t) {
  if (!t) return []
  const { billed = 0, covered = 0, unknown = 0 } = t
  if (!billed && !covered && !unknown) return []
  const out = [{
    tone: 'billed',
    text: billed > 0 ? `~${money(billed)} billed` : '$0.00 billed',
  }]
  if (covered > 0) out.push({ tone: 'covered', text: `~${money(covered)} subscription` })
  if (unknown > 0) out.push({ tone: 'unverified', text: `~${money(unknown)} unverified` })
  return out
}

// The plain-English line shown WITH a cost total, on screen rather than in a
// tooltip. Null when there is nothing to disambiguate: a total made of only one
// kind of dollar needs no paragraph about subscriptions.
//
// `subject` names what the total covers, so the same sentence serves the chat
// header and the export picker's selection without either one lying about its
// scope.
export function chatCostNote(t, subject = 'this chat') {
  if (!t) return null
  const { billed = 0, covered = 0, unknown = 0 } = t
  const parts = []
  if (covered > 0) {
    parts.push(`${money(covered)} of it ran on your Claude subscription - usage `
      + 'your plan already covers, not money charged.')
  }
  if (unknown > 0) {
    parts.push(`${money(unknown)} didn't record how it was billed, so it isn't `
      + 'counted as spend either way.')
  }
  if (!parts.length) return null
  const lead = billed > 0
    ? `Only the ${money(billed)} billed figure is money you pay. `
    : `Nothing in ${subject} was billed to a metered API key. `
  return lead + parts.join(' ')
}
