// Presentation logic for the Integrations console.
//
// The console is a steady-state, capability-specific surface built ON TOP of the
// unified, read-only status API (GET /api/integrations). This module is the pure
// translation layer between that raw status list and what the console renders:
// grouped sections, one plain-English health readout per entry, provenance
// labels, and the lifecycle/onboarding rules for LLM seats.
//
// Kept React-free and side-effect-free so the meaning is unit-tested directly
// (the same discipline as lifecycle.js / modelReadout.js). The component renders
// whatever this returns; the SAFETY INVARIANTS (a trial seat is never routinely
// selectable; secrets never appear) are decided here where they can be proven.

// Sections come from the SERVER (backend/capabilities.KINDS) so a new kind needs
// no change here; that hand-written list was the last hardcoded site, and the
// reason the console read as a catalogue. SECTIONS remains only as the fallback
// for a server that hasn't sent them yet.
export const SECTIONS = [
  { kind: 'llm', title: 'AI models', blurb: 'The providers that host the models in your room, and the model seats using each one.' },
  { kind: 'code', title: 'Coding guest', blurb: 'A coding agent that can be called into a chat to work on your code. It takes a turn like a participant.' },
  { kind: 'audio', title: 'Voice', blurb: 'Spoken conversations — each model talks in its own voice, and you can talk back.' },
  { kind: 'search', title: 'Web research', blurb: 'Lets the models look things up instead of guessing from memory.' },
  { kind: 'toolset', title: 'Repository tools', blurb: 'What the models may do with your GitHub repositories — read issues and pull requests, and write back with their name attached.' },
  { kind: 'memory', title: 'Shared memory', blurb: 'A companion service that gives every chat a cheat-sheet about you. Optional.' },
  { kind: 'mcp', title: 'MCP servers', blurb: 'External tool servers. Status only — add or change them in config.local.json.' },
  { kind: 'channel', title: 'Incoming events', blurb: 'Ways things reach a chat without anyone asking — your own tools posting in.' },
]

// Apply the any-of rule to a flat requirement list: satisfied when every
// non-optional, ungrouped requirement is met AND every any-of GROUP has at least
// one satisfied member. Mirrors backend integrations.requirements_met, so the
// backend stays the source of truth; this only shapes the collapsed row's tone.
export function requirementsMet(requires) {
  const groups = {}
  for (const r of requires || []) {
    if (r.any_of != null) (groups[r.any_of] ||= []).push(!!r.satisfied)
    else if (!r.optional && !r.satisfied) return false
  }
  return Object.values(groups).every((members) => members.some(Boolean))
}

const WEB_SEARCH_GROUP = 'web_search'

// Collapse the {Tavily, Brave} search group into ONE "Web search" row. The
// backend keeps Tavily and Brave as separate, individually-addressable entries
// (so each key stays fixable and the setup flow is untouched); this is a pure
// presentation merge. Reddit is NOT part of the search group and keeps its own
// row, so Web research renders as TWO rows, not one and not three.
function collapseWebSearch(searchEntries) {
  const group = searchEntries.filter((e) =>
    (e.requires || []).some((r) => r.any_of === WEB_SEARCH_GROUP))
  const rest = searchEntries.filter((e) => !group.includes(e))
  if (group.length < 2) return searchEntries  // nothing to collapse (e.g. a test stub)

  const requires = group.flatMap((e) => e.requires || [])
  const configured = requirementsMet(requires)
  // Any-of health: the BEST state across the engines wins — one healthy engine
  // means search works, regardless of a second unconfigured one.
  const rank = { healthy: 3, unknown: 2, unhealthy: 1, unconfigured: 0 }
  const best = group.reduce((a, e) => (rank[e.health] > rank[a.health] ? e : a))
  const engines = group.map((e) => e.display_name.replace(/ Web Search$/i, '')).join(' & ')

  const merged = {
    id: 'web-search',
    kind: 'search',
    display_name: 'Web search',
    chat_toggle: 'web_enabled',
    requires,
    seats: [],
    configured,
    available: configured,
    health: configured ? best.health : 'unconfigured',
    detail: configured
      ? `Shared web search is on — any one key is enough (${engines}). Models look things up instead of guessing.`
      : `Add a ${engines} key to turn on shared web search. Even without one, models can still fetch specific pages, YouTube transcripts and Reddit threads.`,
  }
  return [merged, ...rest]
}

// Group the flat /api/integrations list into the console's ordered sections.
// A section with no entries is dropped (e.g. no MCP servers configured), so the
// console never shows an empty, confusing heading. The Web-research section's
// Tavily/Brave rows are collapsed into one "Web search" row.
export function groupIntegrations(entries, sections) {
  const list = entries || []
  return (sections && sections.length ? sections : SECTIONS)
    .map((s) => {
      let secEntries = list.filter((e) => e.kind === s.kind)
      if (s.kind === 'search') secEntries = collapseWebSearch(secEntries)
      return { ...s, entries: secEntries }
    })
    .filter((s) => s.entries.length > 0)
}

// One glanceable health readout per entry: a stable label, a tone the component
// maps to colour, and whether this state is something the owner can act on.
// `tone` is one of ok | warn | err | muted. Memory absence is deliberately calm
// (it is optional, not broken); a pending MCP handshake reads as "connecting",
// not an alarm.
export function healthPresentation(entry) {
  const kind = entry?.kind
  switch (entry?.health) {
    case 'healthy':
      return { key: 'healthy', label: 'Healthy', tone: 'ok', actionable: false }
    case 'unhealthy':
      return { key: 'unhealthy', label: 'Needs attention', tone: 'err', actionable: true }
    case 'disabled':
      return { key: 'disabled', label: 'Off', tone: 'muted', actionable: false }
    case 'unconfigured':
      if (kind === 'memory') {
        return { key: 'unconfigured', label: 'Not detected', tone: 'muted', actionable: false }
      }
      return { key: 'unconfigured', label: 'Not set up', tone: 'warn', actionable: true }
    case 'unknown':
    default:
      if (kind === 'mcp') {
        return { key: 'unknown', label: 'Connecting…', tone: 'muted', actionable: false }
      }
      return { key: 'unknown', label: 'Not checked yet', tone: 'muted', actionable: false }
  }
}

// Whether an entry's issue can be fixed by entering/validating a credential —
// i.e. whether to offer the "Set up / fix key" shortcut into the secure setup
// flow. LLM/audio/search creds live in the wizard; memory has no key and MCP is
// read-only here, so neither offers a credential fix.
export function offersCredentialFix(entry) {
  if (!entry) return false
  if (entry.kind === 'memory' || entry.kind === 'mcp') return false
  const h = entry.health
  return h === 'unconfigured' || h === 'unhealthy'
}

// Human-readable label for a cost-provenance source (mirrors backend
// provenance.PROVENANCE_LABELS so the console explains, in plain English, HOW a
// seat's cost is known — the thing that gates onboarding).
const PROVENANCE_LABELS = {
  provider_reported: 'Provider-reported (billed)',
  rate_card_estimate: 'Rate-card estimate',
  self_hosted_zero_marginal: 'Self-hosted — no metered marginal cost',
  subscription_equivalent: 'Subscription-equivalent (informational)',
  unknown: 'Unknown — no provenance recorded',
}

export function provenanceLabel(source) {
  return PROVENANCE_LABELS[source] || PROVENANCE_LABELS.unknown
}

// A seat with a KNOWN cost provenance may be onboarded; an `unknown` one may
// not (the onboarding-lifecycle gate). This is the client-side mirror of that
// rule, used only to enable/disable the promote control; the backend PATCH
// remains the source of truth and re-checks on click.
export function seatOnboardable(seat) {
  return !!seat && seat.cost_provenance?.source !== 'unknown' && !!seat.cost_provenance?.source
}

// SAFETY INVARIANT: a trial seat is NEVER routinely/default selectable from
// the console. Only a fully onboarded seat that the backend has marked
// eligible for auto-selection counts as routinely selectable. A trial seat
// stays manual-invoke-only regardless of anything the console shows.
export function routinelySelectable(seat) {
  return !!seat && seat.lifecycle === 'onboarded' && seat.eligible_for_auto_selection === true
}

// The lifecycle badge for a seat (Trial vs Onboarded), matching the participants
// UI. Trial is the loud, actionable state; onboarded is calm/normal.
export function seatBadge(seat) {
  if (seat?.lifecycle === 'onboarded') {
    return {
      lifecycle: 'onboarded',
      label: 'Onboarded',
      tone: 'onboarded',
      title: 'A normal seat: it joins every round automatically.',
    }
  }
  return {
    lifecycle: 'trial',
    label: 'Trial',
    tone: 'trial',
    title: 'Manual-invoke only: this seat will NOT join a normal round on its '
      + 'own. Address it by @mention or by name. Promote it to Onboarded once '
      + 'its cost is known.',
  }
}

// Should the "Promote to Onboarded" control show for this seat, and can it be
// used right now? Only trial seats can be promoted. The button is enabled when
// the seat has a known cost provenance; otherwise it shows disabled WITH the
// reason, so a blocked promotion is explained rather than a silent no-op. This
// is the console's equivalent of lifecycle.promoteState, adapted to the richer
// seat shape returned by /api/integrations.
export function seatPromoteState(seat) {
  if (!seat || seat.lifecycle === 'onboarded') return { show: false }
  if (seatOnboardable(seat)) {
    return {
      show: true,
      enabled: true,
      label: 'Promote to Onboarded',
      reason: `Cost is known (${provenanceLabel(seat.cost_provenance.source)}) — safe to onboard.`,
    }
  }
  return {
    show: true,
    enabled: false,
    label: 'Promote to Onboarded',
    reason: 'No cost provenance yet — add a priced rate-card entry or an explicit '
      + 'self-hosted declaration for this model first. Until then it stays a '
      + 'manual trial (still reachable by @mention or by name).',
  }
}

// What an in-place onboarding prompt needs for one entry: which setup service
// to POST to, and the credential fields to ask for. Derived from the entry's
// own requirements, so a capability added to the backend table gets an
// onboarding form with no change here.
//
// Returns null when there is nothing to ask for — a satisfied capability, or one
// whose readiness isn't a credential at all (memory is a service you run; MCP is
// a file you edit).
export function credentialPrompt(entry) {
  if (!entry || entry.kind === 'memory' || entry.kind === 'mcp') return null
  if (entry.health !== 'unconfigured' && entry.health !== 'unhealthy') return null
  const req = (entry.requires || []).find(
    (r) => r.type === 'credential' && r.setup_service && !r.satisfied)
  if (!req) return null
  const env = req.env || []
  if (!env.length) return null
  return {
    service: req.setup_service,
    // Reddit-style pairs ask for both halves; everything else is one field.
    fields: env.map((name, i) => ({
      name,
      role: i === 0 ? 'key' : 'secret',
      label: name.replace(/_/g, ' ').toLowerCase(),
    })),
    label: entry.health === 'unhealthy' ? 'Replace key' : 'Add key',
  }
}

// "Show me how" content for capabilities configured in config.local.json: the
// coding guest, MCP servers, and the ingest token used to say "add it to
// config.local.json" and stop, which is accurate and a dead end for exactly
// the adopter this product courts. A local app should teach in place: each
// entry gets the exact copy-paste snippet plus one sentence of where it goes.
// Kind-keyed so a new config-file capability inherits the pattern by adding
// one entry here. Deeper reference: docs/CONFIG.md (kept complete by a test).
const CONFIG_HOW_TO = {
  code: {
    title: 'How to set up (or add another repo)',
    intro: 'Create or edit config.local.json next to config.json in the repo '
      + 'root (it is gitignored — your paths never leave this machine), then '
      + 'restart Crossband:',
    snippet: `{
  "code_repos": {
    "myapp": "/Users/you/dev/myapp"
  }
}`,
    outro: 'Each entry is short-name → local path. The full reference, '
      + 'including implement mode and GitHub repos, is docs/CONFIG.md.',
  },
  mcp: {
    title: 'How to add an MCP server',
    intro: 'MCP servers live in config.local.json (gitignored — the public '
      + 'repo never learns what your servers are). Add one and restart:',
    snippet: `{
  "mcp_servers": {
    "my-tool": {
      "command": "/Users/you/dev/my-tool/.venv/bin/python",
      "args": ["/Users/you/dev/my-tool/mcp_server.py"]
    }
  }
}`,
    outro: 'Name → command + args (stdio). Full reference: docs/CONFIG.md.',
  },
  channel: {
    title: 'How your own tools post in',
    intro: 'Anything on this machine can POST to /api/ingest — no key needed '
      + 'from localhost. From beyond loopback, set a token in '
      + 'config.local.json and send it as a Bearer header:',
    snippet: `{
  "ingest_token": "choose-a-long-random-string"
}`,
    outro: 'Payloads are rendered and badged by source; Crossband assigns them '
      + 'no meaning. Full reference: docs/CONFIG.md.',
  },
}

// The how-to for one integration entry, or null when its setup isn't
// config-file-based (those entries have real in-app affordances instead).
export function configHowTo(entry) {
  if (!entry) return null
  return CONFIG_HOW_TO[entry.kind] || null
}
