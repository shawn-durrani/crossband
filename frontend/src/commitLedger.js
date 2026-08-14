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
// round waits for the real end of the turn).
export function onCommit(ledger, turnId, dispatch = 'send') {
  ledger.pending.push({ turnId, dispatch })
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

// Session teardown or STT reconnect: nothing in flight survives the socket.
export function resetLedger(ledger) {
  ledger.pending.length = 0
  ledger.consumed.clear()
}
