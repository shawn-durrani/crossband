// Room-mode voice label chips (#28 phase 1): ALL the decision logic for the
// "Voice N" chips on labelled turns, in a pure module per the house rule -
// which chips a message shows, and how ordinals derive from raw cluster data.
// The JSX in Message.jsx only renders what chipsForMessage returns.
//
// The persisted shape (messages.voice_labels, written by the backend's
// parallel diarization pass a second or two after the turn): a JSON object
// {"clusters": ["speaker_0", ...], "labels": ["Voice 1", ...]}. Labels are
// the session-assigned ordinals; clusters are the raw per-request ids kept
// for phase 2 (naming, anchors). Phase 1 labels are best-effort per
// utterance - batch diarization clusters are not stable across requests -
// and an absent/empty/malformed field simply means an unlabelled turn.

const MAX_CHIPS = 8        // a turn with more clusters than this is noise
const MAX_LABEL_CHARS = 24 // persisted labels are short; cap display anyway

// Ordinal assignment from cluster data: first-seen order, "Voice 1" upward.
// `map` carries assignments already made (cluster id -> label) so ordinals
// stay stable across calls; a fresh object comes back, the input untouched.
export function assignVoiceOrdinals(clusters, map = {}) {
  const next = { ...map }
  const labels = []
  for (const c of clusters || []) {
    if (typeof c !== 'string' || !c) continue
    if (!next[c]) next[c] = `Voice ${Object.keys(next).length + 1}`
    labels.push(next[c])
  }
  return { labels, map: next }
}

// The one rule Message.jsx consults: which chips does this message show?
// - user turns only (labels describe who SPOKE the utterance)
// - persisted labels win; de-duplicated, order kept, bounded
// - defensively, a row carrying clusters but no labels (a future or partial
//   writer) derives per-message ordinals - better than silently showing
//   nothing when the data says two voices were present
// - anything absent or malformed => no chips, never a crash
export function chipsForMessage(msg) {
  if (!msg || msg.speaker !== 'user' || !msg.voice_labels) return []
  let data
  try {
    data = JSON.parse(msg.voice_labels)
  } catch {
    return []
  }
  if (!data || typeof data !== 'object' || Array.isArray(data)) return []
  let labels = Array.isArray(data.labels) ? data.labels : []
  if (!labels.length && Array.isArray(data.clusters) && data.clusters.length > 1) {
    labels = assignVoiceOrdinals(data.clusters).labels
  }
  const out = []
  for (const l of labels) {
    if (typeof l !== 'string') continue
    const t = l.trim().slice(0, MAX_LABEL_CHARS)
    if (t && !out.includes(t)) out.push(t)
    if (out.length >= MAX_CHIPS) break
  }
  return out
}

// The name a chip SHOWS (#28: naming is law). Voice labels are written with
// a person's identity name - the stable key corrections and roster rows
// match on - but the owner's preferred spelling must win everywhere a name
// renders. `people` is the remembered-voices list ({name, preferred_name,
// merged_names}); the label resolves through identity and merged-away names
// alike, and an unknown label (an ordinal, a person since forgotten) shows
// unchanged. This is the fix for the phase-3 punt where chips kept showing
// the introduced spelling after a rename.
export function displayLabel(label, people) {
  if (typeof label !== 'string' || !label) return label
  const key = label.trim().toLowerCase()
  for (const p of people || []) {
    if (!p || typeof p !== 'object') continue
    const names = [p.name, ...(Array.isArray(p.merged_names) ? p.merged_names : [])]
    if (names.some((n) => typeof n === 'string' && n.trim().toLowerCase() === key)) {
      const preferred = typeof p.preferred_name === 'string' && p.preferred_name.trim()
      return preferred ? p.preferred_name.trim().slice(0, MAX_LABEL_CHARS) : label
    }
  }
  return label
}

// Phase 2 (#28): the payload may carry names, per-label uncertainty and a
// corrected marker - {"labels": ["Alex"], "uncertain": ["Sam"],
// "corrected": true}. chipData is chipsForMessage plus that: each chip says
// whether it is a confident voice match or still uncertain, and whether a
// human correction set it. `label` stays the persisted identity name (what
// a correction sends back); `display` is what renders, resolved through
// the remembered people's preferred names. Same defensive posture - junk
// renders nothing.
//
// #28 PR-C (the owner's identity is shown, not hidden): a payload the
// backend marked {"owner": true} - a confident on-device match of the
// owner's own voice - sets `owner: true` on its confident chip, so the UI
// can render the quiet voice-confirmed reassurance on solo/room-off turns.
// The key is only ever ADDED (never owner: false), so payloads from before
// the marker existed produce exactly the chips they always did.
export function chipData(msg, people = []) {
  if (!msg || msg.speaker !== 'user' || !msg.voice_labels) return []
  let data
  try {
    data = JSON.parse(msg.voice_labels)
  } catch {
    return []
  }
  if (!data || typeof data !== 'object' || Array.isArray(data)) return []
  const uncertain = new Set(
    (Array.isArray(data.uncertain) ? data.uncertain : [])
      .filter((u) => typeof u === 'string')
      .map((u) => u.trim().slice(0, MAX_LABEL_CHARS)))  // match display form
  return chipsForMessage(msg).map((label) => {
    const chip = {
      label,
      display: displayLabel(label, people),
      uncertain: uncertain.has(label),
      corrected: data.corrected === true,
    }
    if (data.owner === true && !chip.uncertain) chip.owner = true
    // #28 cold start: the backend placed this turn by ELIMINATION (one
    // person in the room, their voice not learned yet) and marked the
    // payload {"learning": true}. The name still rides `uncertain`, so a
    // consumer that has never heard of this key keeps the old cautious
    // rendering; the key only ever gets ADDED, never learning: false.
    if (data.learning === true) chip.learning = true
    return chip
  })
}

// What a chip shows after the name, decided here rather than in the JSX so
// the three states can never drift apart across surfaces: the owner's quiet
// tick, the cold-start "learning" tag, or the bare "?" every other
// uncertain label has always carried.
export function chipSuffix(chip) {
  if (!chip) return ''
  if (chip.owner) return ' ✓'
  if (chip.learning) return ' · learning'
  if (chip.uncertain) return '?'
  return ''
}

// Plain-English explainer for the chips, shared by every surface that shows
// them so the honesty note is worded once. No jargon: what happened, and how
// much to trust it.
export const CHIP_EXPLAINER =
  'Labelled by a second listen that tells voices apart. Best effort: '
  + 'labels can be wrong, and voices are not named yet.'

// ---- crosstalk (#28 phase 4) ----
//
// The second listen can tell when two voices shared one utterance. On a
// single microphone the quieter voice's overlapped words are often simply
// gone from the transcript, so the honest render is a plain-English marker -
// and, when the words alternated cleanly enough to split, a best-effort
// per-voice attribution as metadata under the turn. Message text itself is
// never rewritten.

export const CROSSTALK_NOTE = 'Two voices at once - some words may be missing.'

const MAX_SEGMENTS_SHOWN = 8
const MAX_SEGMENT_CHARS = 200

function labelData(msg) {
  if (!msg || msg.speaker !== 'user' || !msg.voice_labels) return null
  let data
  try {
    data = JSON.parse(msg.voice_labels)
  } catch {
    return null
  }
  if (!data || typeof data !== 'object' || Array.isArray(data)) return null
  return data
}

// The plain-English marker for a crosstalk-marked user turn; '' otherwise.
// Same defensive posture as the chips: junk renders nothing.
export function crosstalkNote(msg) {
  const data = labelData(msg)
  return data && data.crosstalk === true ? CROSSTALK_NOTE : ''
}

// Best-effort attributed sub-segments for a crosstalk turn, bounded and
// sanitised for display: [{label, display, text, uncertain}]. Empty when
// the turn is not crosstalk-marked, carries no split, or the data is junk.
// An uncertain segment keeps its uncertainty (the UI shows "label?"), and a
// phase-1 ordinal counts as uncertain by construction. `display` resolves
// the identity label through preferred names (#28: naming is law).
export function crosstalkSegments(msg, people = []) {
  const data = labelData(msg)
  if (!data || data.crosstalk !== true || !Array.isArray(data.segments)) return []
  const out = []
  for (const seg of data.segments) {
    if (!seg || typeof seg !== 'object' || Array.isArray(seg)) continue
    if (typeof seg.label !== 'string' || typeof seg.text !== 'string') continue
    const label = seg.label.trim().slice(0, MAX_LABEL_CHARS)
    const text = seg.text.trim().slice(0, MAX_SEGMENT_CHARS)
    if (!label || !text) continue
    out.push({
      label,
      display: displayLabel(label, people),
      text,
      uncertain: seg.uncertain === true || /^Voice \d+$/.test(label),
    })
    if (out.length >= MAX_SEGMENTS_SHOWN) break
  }
  return out
}

// Per-chip explainers for the named phase: how much to trust this exact
// label, and that tapping it corrects it. Speaks the display name (#28:
// naming is law), falling back to the raw label for older callers.
export function chipTitle(chip) {
  if (!chip) return ''
  const shown = chip.display || chip.label
  if (chip.corrected) return `Corrected by you to ${shown}.`
  if (chip.owner) {
    // #28 PR-C: the owner's voice-confirmed chip is reassurance, not an
    // identification of a guest - say so plainly.
    return `Voice confirmed: this turn matched ${shown}'s own voice, `
      + 'checked on this device.'
  }
  if (chip.learning) {
    // #28 cold start: say WHY the name is there - elimination, not
    // recognition - so trust in it is calibrated rather than assumed.
    return `${shown} was the only person in the room, so this turn is `
      + 'theirs. Their voice is still being learned, and this turn helps. '
      + 'Tap to correct if wrong.'
  }
  if (chip.uncertain) {
    return `Probably ${shown} - their voice is still being learned, so `
      + 'this stays uncertain. Tap to correct if wrong.'
  }
  if (/^Voice \d+$/.test(chip.label)) return CHIP_EXPLAINER
  return `Matched to ${shown}'s remembered voice. Tap to correct if wrong.`
}
