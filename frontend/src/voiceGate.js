// Pure state machine for voice playback gating.
//
// Two booleans decide whether an incoming reply is spoken:
//   roundActive - a round is generating/speaking (drives barge-in listening and
//                 the "un-drop on a fresh autonomous round" reset below).
//   dropQueue   - the user cut THIS round off (a barge-in / the ◼ button):
//                 _beginSpeaker refuses to open TTS while it's true, so the
//                 round's remaining queued speech stays silent.
//
// THE BUG this closes: before rounds became detached, a barge-in CANCELLED the
// round, so the abort and the silence ended together. Detached rounds changed
// that: the abort stops the client's tail but the server round can keep
// generating, and its later messages arrive over the GLOBAL message stream,
// which never re-voices them. Meanwhile `dropQueue` was only ever cleared by
// the next `user_saved` / `round` marker / `speaker_start`-with-no-round-active.
// So a false barge-in could leave `dropQueue` stuck true and silence every
// following reply ("text arrives, no audio") until a brand-new user turn
// happened to reset it.
//
// The invariant that keeps audio alive: a `dropQueue` set during one round must
// NEVER survive into the next. Every fresh round clears it - and, new here, so
// does a round ENDING (gateRoundDone): once a round is over, the barge-in that
// silenced it is over too.

export function initGate() {
  return { roundActive: false, dropQueue: false }
}

// Advance the gate for one incoming SSE event type. Pure: returns a new state,
// never mutates. Event types other than the ones below leave gating untouched
// (delta / speaker_end / error / tool_activity / guest_job / work_status all
// pass through).
export function gateEvent(state, type) {
  const s = { roundActive: state.roundActive, dropQueue: state.dropQueue }
  if (type === 'user_saved' || type === 'round') {
    // A new user turn, or a fresh "Let them continue" round marker: a new round
    // begins - un-drop so an earlier interrupt can't silence it.
    s.roundActive = true
    s.dropQueue = false
  } else if (type === 'speaker_start') {
    // A speaker starting while NO round is active means an autonomous round began
    // without a user message ("Let them continue") - un-drop it. Inside an
    // already-active round we must NOT clear dropQueue, or a barge-in in this
    // round would be undone the instant its next speaker starts.
    if (!s.roundActive) s.dropQueue = false
    s.roundActive = true
  }
  return s
}

// The round is over (its SSE tail ended or was aborted). Clear the active flag
// AND any lingering barge-in drop: a false barge-in in the round just finished
// must never leak forward and mute the next one. This is the leak that
// detached rounds exposed.
export function gateRoundDone() {
  return { roundActive: false, dropQueue: false }
}
