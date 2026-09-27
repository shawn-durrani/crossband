// Cutting a reply off: what happens to its audio, and when talking over
// the AIs cuts them off. Pure, per the house rule: voice.js and
// streamPlayer.js act only on what these return.
//
// A reply is cut off three ways: someone talks over it (a barge-in), the
// stop button, or voice being switched off. On 27 September an iPhone
// missed all three. Its browser has no MediaSource, so its player holds a
// reply's audio until the whole speech stream has ended, and a cut had no
// way to reach a player still holding. The reply counted as playing, so
// the mic stayed shut and the owner's words went nowhere, while the
// barge-in fired again on every frame (about 230 times, each one another
// abort). Switching voice off closed the speech socket, which ended the
// stream, and the held reply played about 38 seconds late.

// ---- a reply's audio ----

// Audio arriving for a reply after it was cut off is dropped, whichever
// player it would have gone to.
export function keepsAudio(player) {
  return !player.stopped
}

// The held player (no MediaSource) plays a reply as one clip once its
// speech stream has ended, and never once the reply is cut off. A cut
// ends the wait at once instead of waiting for the stream.
export function heldReplyPlays(player) {
  return !player.stopped && !!player.ended && (player.chunks?.length || 0) > 0
}

// Whether a queued reply takes the shared audio element when its turn in
// the play chain comes. A cut reply never does, even after the round that
// was cut has ended and its drop flag has cleared (voiceGate.js
// gateRoundDone), which can happen before the chain reaches it.
export function takesTurn(player, { dropQueue }) {
  return !dropQueue && !player.stopped && !player.abandoned
}

// ---- talking over the AIs ----
//
// While a reply plays or a round runs, the mic listens only for someone
// talking over it, and half a second of steady speech is a barge-in. That
// speech carries on after the cut, so every frame after it still reads as
// a barge-in. A cut is spent once made, and only something new to cut
// arms it again: a reply starting to play, or a new round starting.
// The stop button spends it too.

export function newBargeIn() {
  return { armed: true }
}

// A cut from anywhere, a barge-in or the stop button.
export function afterCut() {
  return { armed: false }
}

// Something new to cut: a reply starts to play, or a round starts.
export function rearm() {
  return { armed: true }
}

// One frame of the listening loop while the mic is shut for a reply or a
// round. `confirmed`: half a second of steady speech. `speaking`: an
// utterance is already open.
//   open: start capturing the utterance, when one isn't open already
//   cut:  cut the reply or round off, once per barge-in
export function bargeInFrame(state, { confirmed, speaking }) {
  const heard = !!confirmed
  const cut = heard && state.armed
  return {
    state: cut ? afterCut() : state,
    cut,
    open: heard && !speaking,
  }
}
