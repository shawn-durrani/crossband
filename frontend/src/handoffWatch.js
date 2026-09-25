// Stall detection for a voice turn's hand-off (#304) - pure, node --test'd,
// no DOM.
//
// A hand-off runs from the moment the app decides you've finished speaking
// to the moment the server confirms your words as a message: /send's
// user_saved event, which carries the same turn id the commit did. On a
// healthy turn that takes a few seconds. A turn still waiting
// HANDOFF_STALL_MS later is the #304 stall: the screen has gone back to
// Listening and the models never heard the turn.
//
// Why 30 s. The slowest healthy hand-off the code allows is about 20 s: the
// realtime transcriber gets up to 15 s of patience on the longest segment
// the VAD commits (a 20 s cap, turnPolicy.sttCommitTimeoutMs), the batch
// fallback then takes a few seconds, and /send answers user_saved before
// any model runs. 30 s clears that with margin, and it still fires well
// inside the 90 s round-stream idle timeout (api.js SSE_IDLE_TIMEOUT_MS),
// so the evidence is saved while the stall is live - before that timeout's
// error path tears down the state it would explain.
//
// The watch only observes. Nothing here changes when or whether a turn is
// sent. Content-free by construction: it holds turn ids, stage words and
// timestamps, and never sees transcript text.

export const HANDOFF_STALL_MS = 30000

// Where a watched turn got to. The stage rides the stall report, so a
// saved bundle says which step the hand-off stopped at.
export const STAGE_TRANSCRIBING = 'transcribing' // audio committed, no transcript yet
export const STAGE_EMPTY = 'empty'               // the transcript came back empty
export const STAGE_FAILED = 'failed'             // the transcriber refused the audio
export const STAGE_HELD = 'held'                 // waiting for the network to return
export const STAGE_SENT = 'sent'                 // words went to /send, not confirmed yet

// A session where nothing ever confirms must not grow the watch forever.
export const MAX_WATCHED = 16

export function newHandoffWatch() {
  return { turns: [] }
}

// The app decided a turn ended and began handing it off. A repeated or
// missing id is ignored.
export function handoffBegan(watch, turnId, now) {
  if (!turnId || watch.turns.some((t) => t.turnId === turnId)) return
  watch.turns.push({ turnId, since: Number(now), stage: STAGE_TRANSCRIBING,
                     reported: false })
  while (watch.turns.length > MAX_WATCHED) watch.turns.shift()
}

// The hand-off moved on a step (see the STAGE_ words above).
export function handoffStage(watch, turnId, stage) {
  const t = watch.turns.find((x) => x.turnId === turnId)
  if (t) t.stage = stage
}

// The server confirmed the turn as a message: the hand-off finished. Returns
// the entry it closed (so a caller can note a confirmation that came after
// a stall report), or null for an id the watch never held.
export function handoffConfirmed(watch, turnId) {
  if (!turnId) return null
  const i = watch.turns.findIndex((t) => t.turnId === turnId)
  if (i === -1) return null
  return watch.turns.splice(i, 1)[0]
}

// The session ended: a hand-off the owner ended is not a stall.
export function resetHandoffWatch(watch) {
  watch.turns.length = 0
}

// The turns overdue at `now` that have not been reported yet. Each one is
// marked as reported, so one stall reports once however often this asks.
// A reported turn stays watched until it is confirmed or the session ends,
// so a late confirmation can still be noted.
export function takeStalledHandoffs(watch, now) {
  const out = []
  for (const t of watch.turns) {
    if (t.reported) continue
    const waited = Number(now) - t.since
    if (!Number.isFinite(waited) || waited <= HANDOFF_STALL_MS) continue
    t.reported = true
    out.push({ turnId: t.turnId, stage: t.stage, waitedMs: Math.round(waited) })
  }
  return out
}
