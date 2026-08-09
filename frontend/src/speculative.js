// Speculative identity at silence-start (#28 PR-B): the firing rule, in a
// pure module per the house rule - voice.js only acts on what this returns.
//
// The idea: an utterance's audio is complete the moment the silence gap
// BEGINS - roughly two seconds before the commit frame at default settings.
// Firing a content-free hint frame ({"speculative": true}, no audio) at
// silence-start lets the server run the LOCAL-ONLY identity check on the
// buffered utterance while the silence window is still counting down, so
// the verdict is cached before the commit and the name attaches with the
// transcript instead of racing it. The hint changes NOTHING upstream - the
// relay treats it exactly like the room_mode control frame and forwards no
// byte of it to ElevenLabs.
//
// Firing rule: once per silence gap, after a short confirmation
// (SPECULATIVE_SILENCE_MS) so ordinary inter-word pauses do not fire.
// Resumed speech re-arms the rule - the NEXT silence gap fires a fresh
// hint, and the server keeps only the latest, so a hint computed on a
// half-finished sentence is superseded rather than trusted. The server
// also re-checks staleness itself (the audio past the hint must be
// near-silence), so this client rule is an optimisation, never a truth
// source.

export const SPECULATIVE_SILENCE_MS = 400

// One VAD tick's decision. `fired` is the previous state; returns
// {fired, fire} where `fire` says "send the hint frame NOW".
// - not in an utterance, or realtime streaming is off: reset, never fire
//   (there is no buffered utterance for the server to check);
// - a voiced frame: reset - speech resumed, the previous hint is stale and
//   the next silence gap should fire a fresh one;
// - silence at least SPECULATIVE_SILENCE_MS deep, not yet fired: fire once;
// - otherwise: hold state.
export function speculativeStep(fired, { inUtterance, streaming, voiced, silenceMs }) {
  if (!inUtterance || !streaming) return { fired: false, fire: false }
  if (voiced) return { fired: false, fire: false }
  if (!fired && Number(silenceMs) >= SPECULATIVE_SILENCE_MS) {
    return { fired: true, fire: true }
  }
  return { fired: !!fired, fire: false }
}
