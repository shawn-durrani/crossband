// Only one transcription wins per committed utterance (#85, #104) - pure,
// node --test'd, no DOM.
//
// The race this closes: a commit's realtime final raced a flat timer; on
// timeout the batch fallback sent the text and a per-INSTANCE boolean said
// "drop the late final". The next turn's commit reset that boolean, and the
// previous turn's late realtime final then passed the check and sent the
// same utterance twice. Hard-capped monologue commits (the largest audio
// the client produces) made that race routine.
//
// The ledger replaces the boolean with per-commit accounting, keyed by the
// turn id the relay now stamps onto each final (paired server-side, where
// commit and final meet on one socket). A final whose commit was already
// salvaged - or that names no commit we know - is dropped. A final with no
// id (an older server, mid-deploy) falls back to FIFO order, the old
// assumption held in one place.

export function newLedger() {
  return { pending: [], consumed: new Set() }
}

// A commit was sent for `turnId`. `dispatch` records what the winning
// transcript should DO when it lands: 'send' (a normal turn end) or
// 'buffer' (a capped continuation segment, #104 - text accumulates and the
// round waits for the real end of the turn). `speechMs`, when given, is how
// much speech the commit carried, so a rescue (#304) can meter what it
// transcribes.
export function onCommit(ledger, turnId, dispatch = 'send', speechMs = null) {
  const c = { turnId, dispatch }
  if (Number.isFinite(speechMs)) c.speechMs = speechMs
  ledger.pending.push(c)
  // A commit whose final never arrives (socket died mid-turn) must not pin
  // memory forever; the salvage timer handles its text.
  while (ledger.pending.length > 8) {
    const dropped = ledger.pending.shift()
    ledger.consumed.add(dropped.turnId)
  }
}

// The salvage timer fired for `turnId`: the batch path takes over this
// exact commit. Returns its dispatch, or null when the realtime final
// already won (the timer lost the race - do nothing).
export function onSalvage(ledger, turnId) {
  if (ledger.consumed.has(turnId)) return null
  const i = ledger.pending.findIndex((c) => c.turnId === turnId)
  if (i === -1) return null
  const [c] = ledger.pending.splice(i, 1)
  ledger.consumed.add(turnId)
  return c.dispatch
}

// A realtime final arrived, stamped with its commit's turn id. Exact match:
// consumed or unknown ids drop (the doubled-turn case, and strays). A final
// with no id falls back to oldest-unconsumed order.
export function onFinal(ledger, turnId = null) {
  if (turnId != null && turnId !== '') {
    if (ledger.consumed.has(turnId)) return null
    const i = ledger.pending.findIndex((c) => c.turnId === turnId)
    if (i === -1) return null
    const [c] = ledger.pending.splice(i, 1)
    ledger.consumed.add(turnId)
    return c
  }
  while (ledger.pending.length) {
    const c = ledger.pending.shift()
    if (ledger.consumed.has(c.turnId)) continue
    ledger.consumed.add(c.turnId)
    return c
  }
  return null
}

// #304: realtime transcription failed while commits were still waiting on
// their transcripts. The socket reset that follows would forget them, and
// the turn they belong to was dropped unsent. Take them all, in commit
// order, and mark each consumed, so no late final can send one again: the
// batch path owns them now.
export function takeInFlight(ledger) {
  const out = ledger.pending.filter((c) => !ledger.consumed.has(c.turnId))
  ledger.pending.length = 0
  for (const c of out) ledger.consumed.add(c.turnId)
  return out
}

// What the batch path should do with the commits takeInFlight returned.
// The batch recorder holds one recording, so the answer is one salvage or
// none, as { turnId, dispatch, speechMs } or null:
//  - nothing in flight: nothing to rescue.
//  - a finished turn in flight ('send'): salvage it now and send it, under
//    the latest finished turn's id. Capped segments before it share the
//    recording and go with it.
//  - only capped segments ('buffer') while the person is still talking:
//    nothing now. The recorder keeps running under the same turn, and the
//    batch path transcribes it when the turn ends.
//  - only capped segments during a pause: salvage them into the buffer,
//    which is what their realtime transcripts would have done.
export function rescuePlan(inFlight, { speaking = false } = {}) {
  if (!Array.isArray(inFlight) || !inFlight.length) return null
  const speechMs = inFlight.reduce((n, c) => n + (Number(c.speechMs) || 0), 0)
  const sends = inFlight.filter((c) => c.dispatch === 'send')
  if (sends.length) {
    return { turnId: sends[sends.length - 1].turnId, dispatch: 'send', speechMs }
  }
  if (speaking) return null
  return { turnId: inFlight[inFlight.length - 1].turnId, dispatch: 'buffer', speechMs }
}

// Session teardown or STT reconnect: nothing in flight survives the socket.
export function resetLedger(ledger) {
  ledger.pending.length = 0
  ledger.consumed.clear()
}
