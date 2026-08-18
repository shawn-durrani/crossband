// Relative voice volume (#163).
//
// Different TTS voices render at very different perceived loudness, and an
// HTML audio element's volume CAPS at 1.0 - so a quiet voice could never be
// turned up, only the loud ones down, one by one. The fix that works on
// every device (no WebAudio, which is fragile on iPhone Safari - the main
// device): gains are RELATIVE WEIGHTS. Boosting one voice above 1.0 ducks
// everyone else proportionally, so the balance is right and the device
// volume rocker sets the overall level once, instead of being ridden
// between turns.
//
// Backwards compatible by construction: while every gain is at or below
// 1.0 the roster maximum is 1.0 and each voice plays at exactly its own
// gain - byte-for-byte the old behaviour.

export const GAIN_MIN = 0.2
export const GAIN_MAX = 3.0
const VOLUME_FLOOR = 0.05 // a heavily out-weighed voice stays audible

function clampGain(g) {
  const n = Number(g)
  if (!Number.isFinite(n)) return 1
  return Math.min(GAIN_MAX, Math.max(GAIN_MIN, n))
}

// The element volume for one speaker, given every participant's gain.
// gain / max(all gains), clamped into [VOLUME_FLOOR, 1].
export function effectiveVolume(gain, allGains) {
  const own = clampGain(gain ?? 1)
  const top = Math.max(1e-9, ...(allGains || [])
    .map((g) => clampGain(g ?? 1)), own)
  return Math.min(1, Math.max(VOLUME_FLOOR, own / top))
}
