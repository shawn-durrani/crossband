// The one mic setting (#505), in a pure module per the house rule so the
// choice is unit-tested rather than buried in the session class.
//
// Every session asks the mic for the same thing, in solo and in room mode,
// and changing mode never touches a live mic. A clip saved in one mode then
// sounds like a clip saved in another, so a voice learnt in solo still
// matches in a room.
//
// Echo cancellation stays on: it takes the AIs' own playback out of the
// mic. Noise suppression and auto gain stay off. Both are tuned for one
// voice: suppression can treat a quieter second person as noise, and auto
// gain pumps for whoever is loudest. Both also change how a voice sounds
// from one moment to the next, which is what the voice checks compare.

const CONSTRAINTS = Object.freeze({
  echoCancellation: true,
  noiseSuppression: false,
  autoGainControl: false,
})

// The getUserMedia audio constraints, a fresh copy for each call.
export function captureConstraints() {
  return { ...CONSTRAINTS }
}
