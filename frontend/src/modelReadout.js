// Pure presentation logic for the per-seat model-status readout.
//
// Kept framework-free so the "confirmation vs attention vs drift" hierarchy is
// unit-testable without a DOM, and so the wrapping fix can't silently regress
// the meaning. The component (ModelsPage.jsx) maps each returned line's `tone`
// to colour/weight and `icon` to a lucide glyph; the *ordering* and *tone* here
// are the contract, the styling is downstream.
//
// Why this matters: the live/configured model (shown on the row above) is what
// drives the next turn and must read as dominant. These lines are secondary
// context. The trap: on a narrow screen the muted config-*seed* value must
// never out-shout the live model, or a landed switch misreads as "no".
//
// Tone contract (drives visual weight, brightest → dimmest):
//   'attention' — a change is set but the last reply predates it (needs the eye).
//   'confirm'   — the last reply already ran on the live model (reassurance, muted).
//   'muted'     — background context (stale config seed); never dominant.
export function modelReadoutLines(status) {
  if (!status) return []
  const { last_used, seed, seed_drift, pending } = status
  const lines = []
  // Confirmation / pending state comes first: it answers "did my switch take?".
  if (pending) {
    lines.push({
      key: 'pending', tone: 'attention', icon: 'alert',
      label: 'last reply used', model: last_used, tail: '— change applies next turn',
    })
  } else if (last_used) {
    lines.push({
      key: 'confirmed', tone: 'confirm', icon: 'check',
      label: 'last reply confirmed', model: last_used, tail: '',
    })
  }
  // Config-seed drift is deliberately last and muted: it's provenance, not news.
  if (seed_drift) {
    lines.push({
      key: 'seed', tone: 'muted', icon: null,
      label: 'config seed was', model: seed, tail: '— live now differs',
    })
  }
  return lines
}
