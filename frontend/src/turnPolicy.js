// Bounded active-turn endpointing (#60): auto mode normally ends a turn on
// silenceMs of quiet after speech. Sustained background noise - road noise,
// wind, a fan - can keep the VAD reading "voiced" (or keep RMS above the
// calibrated floor) indefinitely, so that silence gap never opens and the
// turn listens forever instead of sending what it already captured. This
// module is the bounded fallback: a maximum active-turn duration, evaluated
// only once the normal silence check has already failed to fire this tick.
// Pure, per the house rule - voice.js only acts on what this returns.
//
// Two tiers, so the fix trades premature truncation against "never sends"
// rather than picking one absolutely:
//  - SOFT_MAX_TURN_MS: once the turn has run this long, take the next
//    natural-feeling opening - an unvoiced frame, even one too brief to
//    satisfy the full silenceMs/CONFIRM_MS gate - instead of waiting out
//    the whole silence window. This is what actually fires in the
//    continuous-noise case: real speech has micro-gaps between words and
//    sentences even when background noise never lets RMS fall below floor
//    for the FULL silenceMs.
//  - HARD_MAX_TURN_MS: past this, finalize unconditionally, mid-word if it
//    must. Bounds the pathological case (noise with no gaps at all, or a
//    genuine monologue) so a turn is never held open indefinitely - the
//    same "must eventually send" guarantee push-to-talk gets for free from
//    an explicit user action.
//
// Deliberately NOT applied in manual (push-to-talk) mode: the user already
// controls when a turn ends there, so an unconditional cap would fight
// their own gesture rather than substitute for a silence timeout that
// isn't running.
export const SOFT_MAX_TURN_MS = 12000
export const HARD_MAX_TURN_MS = 20000

// #104: the caps above bound a SEGMENT, no longer the logical turn. A capped
// segment commits its audio and the turn continues - text buffers until a
// real silence gap ends the turn, so a monologue stays one message. This
// bound is the #60 guarantee's new home: even a zero-gap noise wall must
// eventually send, so past this TOTAL duration the turn ends uncondition-
// ally, buffered segments and all.
export const MAX_TURN_TOTAL_MS = 60000

// #104's other half: the realtime STT gets a flat 5s to finalize a commit
// before the batch fallback takes over - but a capped segment commits the
// largest audio the client ever produces, and healthy finalization of ~20s
// of speech can exceed 5s. Scale the patience to the audio: floor 5s, plus
// three-quarters of the speech length, capped at 20s (past that the batch
// path is genuinely the better bet).
export function sttCommitTimeoutMs(speechMs) {
  const ms = Number(speechMs)
  if (!Number.isFinite(ms) || ms <= 0) return 5000
  return Math.min(20000, Math.max(5000, Math.round(ms * 0.75)))
}

// `turnMs`: elapsed ms since this turn's speech began. `voiced`: this tick's
// frame classification. Returns true when the turn should finalize now even
// though the ordinary silenceMs-since-lastVoice check didn't fire.
export function shouldForceEndpoint({ turnMs, voiced }) {
  const ms = Number(turnMs)
  if (!Number.isFinite(ms)) return false
  if (ms >= HARD_MAX_TURN_MS) return true
  if (ms >= SOFT_MAX_TURN_MS && !voiced) return true
  return false
}
