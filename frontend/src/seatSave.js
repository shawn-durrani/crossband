// #161: whether a seat save carries voice_id at all - the pure rule.
//
// The Models editor is seeded from a roster fetch, and a voice start can
// auto-assign voices AFTER that fetch: blindly re-sending the editor's
// (now stale) blank cleared the assignment on every unrelated save, and
// the next voice start re-rolled a different voice from the pool. A save
// therefore carries voice_id ONLY when the user actually changed the
// selector from what the editor was seeded with. An untouched control
// sends nothing, whatever the editor thinks it shows; a changed one sends
// its value, where '' is an explicit return to auto-assign.
export function voiceIdPatch({ isNew, voiceId, seedVoiceId }) {
  const v = voiceId || ''
  if (isNew) return { voice_id: v }
  if (v === (seedVoiceId || '')) return {}
  return { voice_id: v }
}
