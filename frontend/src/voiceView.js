// The voice call screen's state rules, pure so they're testable
// without a microphone. The component renders what these return; the rules
// themselves never touch the pipeline.
//
// The one honesty principle behind all three functions: **the screen must
// describe what the mic and speakers are actually doing** - a muted session
// must never claim "Listening…", and an interrupt hint must never show when
// speaking over the models can't work.

// What the orb should look like. `muted` wins over listening - a dimmed idle
// orb is the truth when the mic track is emitting silence - but a SPEAKING
// model keeps the speaking visual: who's talking still matters while muted.
export function orbStateFor(voiceState, muted) {
  if (voiceState === 'speaking') return 'speaking'
  if (muted) return 'idle'
  if (voiceState === 'transcribing') return 'transcribing'
  return 'listening'
}

// The status line, most-important-thing-first:
// held messages (you were heard, nothing is lost) > muted (the mic is off and
// you should know) > who's speaking > thinking > listening.
export function statusFor({ held = 0, muted = false, voiceState, speakerName = null }) {
  if (held > 0) return `Holding ${held} message${held > 1 ? 's' : ''} - reconnecting…`
  if (muted && voiceState !== 'speaking') return 'Muted - models keep talking, you aren’t heard'
  if (voiceState === 'speaking') return `${speakerName || 'Speaking'}…`
  if (voiceState === 'transcribing') return 'Thinking…'
  return 'Listening…'
}

// Whether to show "talk any time to interrupt" under the orb. Only while a
// model is speaking AND the mic could actually interrupt it - muted means a
// voice barge-in is impossible, so advertising it would be a lie.
export function showInterruptHint(voiceState, muted) {
  return voiceState === 'speaking' && !muted
}

// Normalize a partial transcript for display: null when there is nothing
// worth showing (empty/whitespace - the realtime stream emits plenty of
// those), trimmed otherwise. The view renders null as "no partial caption".
export function displayPartial(text) {
  const t = (text || '').trim()
  return t.length ? t : null
}

// #98: hold TTS while a streaming reply could still be a bare [pass] - a
// pass is never spoken. The moment the text diverges from the token (or
// carries content beyond it) the hold lifts and buffered text speaks.
export function couldBePass(accumulated) {
  const t = (accumulated || '').trimStart().toLowerCase()
  if (t.length <= 6) return '[pass]'.startsWith(t)
  return t.startsWith('[pass]') && t.slice(6).trim() === ''
}
