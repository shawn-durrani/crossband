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

// Plain-English explainer for the chips, shared by every surface that shows
// them so the honesty note is worded once. No jargon: what happened, and how
// much to trust it.
export const CHIP_EXPLAINER =
  'Labelled by a second listen that tells voices apart. Best effort: '
  + 'labels can be wrong, and voices are not named yet.'
