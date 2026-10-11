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
//
// Before any cut, a sound that might be someone talking pauses the reply
// until it's known to hold words (below).

// ---- a reply's audio ----

// Audio arriving for a reply after it was cut off is dropped, whichever
// player it would have gone to.
export function keepsAudio(player) {
  return !player.stopped
}

// The held player (a browser that can't stream the reply, or a phone
// whose streaming source failed) plays a reply as one clip once its
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
//
// A clatter, a pan or a fan can sound enough like speech to pass that
// test. On 11 October two replies in a row were cut by sounds with no
// words in them, and the rest of each was never heard. So with live
// transcription on, the sound first pauses the reply while the
// transcriber listens to it. A word in it cuts the reply off. A sound
// that stops, or runs on for HOLD_MAX_MS, without a word is let go, and
// the reply plays on from where it paused. Without live transcription
// there are no words to wait for, so the sound cuts at once.

// A held sound this long without a voiced frame has stopped.
export const HOLD_QUIET_MS = 600
// A held sound this long without a word isn't someone talking.
export const HOLD_MAX_MS = 4000

export function newBargeIn() {
  return { armed: true }
}

// A cut from anywhere, a barge-in or the stop button.
export function afterCut() {
  return { armed: false }
}

// Something new to cut: a reply starts to play, or a round starts. A
// sound still being checked for words keeps its hold.
export function rearm(state) {
  return state?.held ? state : { armed: true }
}

// Whether a live transcript holds a word: a letter or a digit, outside
// any bracketed tag a transcriber puts in for a sound, like "(sizzling)".
export function hasWords(text) {
  const plain = String(text || '').replace(/\([^)]*\)|\[[^\]]*\]/g, ' ')
  return /[\p{L}\p{N}]/u.test(plain)
}

// One frame of the listening loop while the mic is shut for a reply or a
// round.
//   confirmed: half a second of steady speech
//   voiced:    this frame on its own reads as speech
//   speaking:  an utterance is already open
//   live:      live transcription is on, so there are words to wait for
//   wordsAt:   when the live transcript last showed a word, 0 for never
//   now:       this frame's time
// What to do:
//   open:    start capturing the utterance, when one isn't open already
//   hold:    pause the reply while the sound is checked for words
//   cut:     cut the reply or round off, once per barge-in
//   release: the held sound had no words: end its capture and play on
export function bargeInFrame(state, { confirmed, voiced = false, speaking, live = false,
                                      wordsAt = 0, now = 0 }) {
  const none = { cut: false, open: false, hold: false, release: false }
  const held = state.held
  if (held) {
    if (wordsAt >= held.since) return { ...none, state: afterCut(), cut: true }
    const lastVoiced = voiced ? now : held.lastVoiced
    if (now - lastVoiced >= HOLD_QUIET_MS || now - held.since >= HOLD_MAX_MS) {
      return { ...none, state: { armed: false, letGo: true }, release: true }
    }
    return { ...none, state: { armed: false, held: { since: held.since, lastVoiced } } }
  }
  const heard = !!confirmed
  // A sound that was let go may run on. It's listened for again once it
  // stops, or when there's something new to cut.
  if (state.letGo) return { ...none, state: heard ? state : newBargeIn() }
  if (heard && state.armed && live) {
    return { ...none, state: { armed: false, held: { since: now, lastVoiced: now } },
             hold: true, open: !speaking }
  }
  const cut = heard && state.armed
  return { ...none, state: cut ? afterCut() : state, cut, open: heard && !speaking }
}
