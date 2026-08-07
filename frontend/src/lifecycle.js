// Onboarding-lifecycle display logic for the Participants UI.
//
// Two facts, one plain-English readout per seat, so an owner can SEE where a
// seat sits and act on it without reading the backend:
//   • lifecycle  - 'trial' (added for manual evaluation; sits out unaddressed
//     rounds - reachable only by @mention/name) or 'onboarded' (a normal seat).
//   • provenance - how the seat's cost is known; a seat can only be onboarded
//     once that is known (the PATCH …/lifecycle gate). A trial seat with unknown
//     provenance therefore shows WHY promotion is blocked instead of failing.
//
// Kept as a pure module (no React) so the meaning is unit-tested directly, the
// same way modelReadout.js is. The component renders whatever this returns.

// Badge for one seat. `status` is a /api/models/status entry (may be undefined
// before the readout loads); `fallbackLifecycle` is the value from the
// participants row (/api/state), so the badge still shows pre-fetch.
// Returns null only when we truly know nothing yet.
export function lifecycleBadge(status, fallbackLifecycle) {
  const lifecycle = status?.lifecycle || fallbackLifecycle
  if (!lifecycle) return null
  if (lifecycle === 'onboarded') {
    return {
      lifecycle,
      label: 'Onboarded',
      tone: 'onboarded',
      title: 'A normal seat: joins every round and, once cost-aware auto-'
        + 'selection ships, is eligible to be picked automatically.',
    }
  }
  // Anything not explicitly onboarded is treated as a trial (conservative
  // default) - and trial is a REAL behavioural gate, so say what it means.
  return {
    lifecycle: 'trial',
    label: 'Trial',
    tone: 'trial',
    title: 'Manual-invoke only: this seat will NOT join a normal round on its '
      + 'own. Address it by @mention or by name to include it. Promote it to '
      + 'Onboarded once its cost is known.',
  }
}

// Should the "Promote to Onboarded" control show, and can it be used right now?
// A promote button appears only for trial seats. It is enabled when the seat
// has a known cost provenance (status.onboardable); otherwise it is shown
// disabled with the reason - the recovery path for an existing priced seat that
// was left on trial, and an honest block for an unsourced one.
export function promoteState(status, fallbackLifecycle) {
  const lifecycle = status?.lifecycle || fallbackLifecycle
  if (lifecycle === 'onboarded') return { show: false }
  // Trial seat. If we haven't loaded the readout yet we don't know provenance;
  // show the button but let the backend gate be the source of truth on click.
  const known = status ? !!status.onboardable : true
  if (known) {
    return {
      show: true,
      enabled: true,
      label: 'Promote to Onboarded',
      reason: status?.cost_provenance_label
        ? `Cost is known (${status.cost_provenance_label}) - safe to onboard.`
        : 'Promote this seat to a normal, always-on participant.',
    }
  }
  return {
    show: true,
    enabled: false,
    label: 'Promote to Onboarded',
    reason: 'No cost provenance yet - add a priced rate-card entry or an '
      + 'explicit self-hosted declaration for this model first. Until then it '
      + 'stays a manual trial.',
  }
}

// Hosts that mean "this machine" - mirrors _LOOPBACK_HOSTS in backend/config.py.
const LOOPBACK = new Set(['localhost', '127.0.0.1', '::1', '[::1]', '0.0.0.0'])

// Is this draft seat served from this machine, keylessly? Mirrors
// config.is_self_hosted_endpoint. The backend remains the authority; this only
// decides which explainer to show BEFORE the seat exists, when there is no
// server-side answer to ask for.
export function isLocalEndpoint(draft) {
  const url = (draft?.base_url || '').trim()
  if (!url || (draft?.api_key_env || '').trim()) return false
  let host = ''
  try {
    host = new URL(url.includes('://') ? url : `http://${url}`).hostname.toLowerCase()
  } catch {
    return false
  }
  return LOOPBACK.has(host) || host.startsWith('127.')
}

// What to tell someone at the moment they ADD a seat.
//
// The trial gate is real behaviour, not a label: a new seat sits out every
// unaddressed round until it is promoted. Leaving a user to discover that from
// a badge after the fact is how the documented zero-key path came to dead-end
// silently - so the add form says it up front, and says which of the two
// promotion stories this particular seat is in.
//
// Returns null when editing an existing seat (the row's own badge already
// covers it) - the notice is only for a seat that doesn't exist yet.
export function addSeatNotice(draft) {
  if (!draft || draft.id) return null
  const local = isLocalEndpoint(draft)
  return {
    local,
    title: 'This will be added as a Trial seat',
    body: 'It answers when you @mention it or address it by name, and sits out '
      + 'rounds where you haven\'t. Promote it to Onboarded - on its row in this '
      + 'list - to have it join every round.',
    // The second sentence is the half that used to be missing: whether promotion
    // is available at all, answered before the user commits to adding.
    promotion: local
      ? 'Served from this machine with no API key, so its cost is known - a '
        + 'declared $0 with nothing metered - and you can promote it straight away.'
      : 'Promotion needs a known cost for this model, so the app never reports a '
        + 'cost it can\'t source. Built-in models already have one; for anything '
        + 'else, add a rate-card entry (or run it locally, which needs none).',
  }
}
