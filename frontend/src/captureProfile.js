// Capture profiles (#28 phase 4): what the microphone is asked for, decided
// in a pure module per the house rule so the choice is unit-tested rather
// than buried in the session class.
//
// The experiment, from the crosstalk design on the issue: the browser's
// noiseSuppression and autoGainControl are tuned for ONE voice, and may
// actively muffle a second speaker - suppression treats the quieter person
// as noise, gain control pumps for whoever is loudest. So a room-mode
// session captures with both OFF, while echoCancellation stays ON in both
// profiles (it removes the app's own TTS playback from the mic, which
// matters MORE with two people talking across it, and it does not target
// the second human).
//
// Solo sessions are byte-identical to what they always requested - the
// experiment changes room mode only. Each session reports its profile name
// to the relay, which logs it content-free, so field tests can compare
// label rates between profiles.

export const SOLO_PROFILE = 'solo-tuned'
export const ROOM_PROFILE = 'room-open'

// The name a session reports for its capture profile - the only two values
// the relay will log.
export function captureProfileName(roomMode) {
  return roomMode ? ROOM_PROFILE : SOLO_PROFILE
}

// The getUserMedia audio constraints (also re-applied to a live track when
// room mode flips mid-session).
export function captureConstraints(roomMode) {
  return roomMode
    ? { echoCancellation: true, noiseSuppression: false, autoGainControl: false }
    : { echoCancellation: true, noiseSuppression: true, autoGainControl: true }
}
