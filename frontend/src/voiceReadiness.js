// Voice readiness on the Voices page (#482 stage 2).
//
// The backend's calibrated scorer tests each person's stored voice in the
// background and /api/voice/people carries the result per person, plus the
// test's state and rules under `readiness`. This module turns one person's
// result into the plain line shown beside their name. Pure, so the words
// are pinned by voiceReadiness.test.js; the component is markup only.

export const READINESS_DEFAULTS = { min_pieces: 20, share: 0.95, piece_seconds: 2 }

// What "Ready" means, for the hover title.
export function readinessExplainer(summary) {
  const rules = { ...READINESS_DEFAULTS, ...(summary || {}) }
  return `Ready means ${rules.piece_seconds}-second pieces of their speech were `
    + `named as them at least ${Math.round(rules.share * 100)} times in 100, `
    + `and never as anyone else, over at least ${rules.min_pieces} pieces. `
    + 'Each piece is tested with that day\'s recordings left out, so it says '
    + 'how well the voice will be named on another day.'
}

function seconds(value) {
  const n = Number(value)
  if (!Number.isFinite(n) || n <= 0) return ''
  return `${n < 10 ? n.toFixed(1) : Math.round(n)}s of speech`
}

// One person's line: { text, detail, ready, title }, or null when there's
// nothing to show (the test is switched off, or the answer hasn't loaded).
export function readinessLine(readiness, summary) {
  const state = summary && summary.state
  if (!state || state === 'off') return null
  const rules = { ...READINESS_DEFAULTS, ...summary }
  const title = readinessExplainer(rules)
  if (!readiness) {
    if (state === 'waiting' || state === 'building') {
      return { text: 'Checking whether this voice is ready', detail: '',
               ready: false, title }
    }
    if (state === 'unavailable' || state === 'failed') {
      return { text: 'Readiness can\'t be checked right now', detail: '',
               ready: false, title }
    }
    // Known, but no speech stored yet.
    return { text: `Needs more speech: 0 of ${rules.min_pieces} pieces`,
             detail: '', ready: false, title }
  }
  const pieces = Number(readiness.pieces) || 0
  const detail = seconds(readiness.speech_seconds)
  let text
  if (readiness.ready) {
    text = 'Ready'
  } else if (readiness.reason === 'too_few_pieces' || pieces < rules.min_pieces) {
    text = `Needs more speech: ${pieces} of ${rules.min_pieces} pieces`
  } else if (readiness.reason === 'one_day') {
    text = 'Needs speech from another day'
  } else if (readiness.reason === 'no_calibration') {
    text = 'Not checked yet: needs a second person\'s voice to compare with'
  } else if (readiness.reason === 'named_as_other') {
    const n = Number(readiness.named_as_other) || 0
    text = `Needs more speech: ${n} of ${pieces} pieces sounded like someone else`
  } else {
    const right = Math.floor((Number(readiness.share_right) || 0) * 100)
    text = `Needs more speech: ${right}% of pieces named right, `
      + `${Math.round(rules.share * 100)}% needed`
  }
  return { text, detail, ready: Boolean(readiness.ready), title }
}
