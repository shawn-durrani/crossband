// What a voice session must repair when it comes back to the foreground.
//
// THE BUG this closes: everything the microphone side depends on hangs off ONE
// AudioContext - the analyser the VAD reads, the ScriptProcessor that streams
// PCM to realtime STT, and playback. Browsers suspend that context when the tab
// is backgrounded (mobile Safari especially, and a locked screen counts), and
// nothing in the app ever resumed it. A suspended context produces silence
// forever: the analyser reads flat, `onaudioprocess` stops firing, so no speech
// is detected, captured, or transcribed - while the UI still looks perfectly
// alive. Only a reload built a new context, which is exactly what users hit.
//
// The same root cause fired WITHOUT backgrounding: `_audioUnlocked` latched true
// on the first session and was never reset, so a second `start()` skipped
// priming and left its fresh context suspended - which is why toggling voice
// off and on again didn't recover dictation either.
//
// A backgrounded tab also loses its websockets. A realtime STT socket that
// closes on its own leaves its ScriptProcessor connected and feeding nothing:
// the mic works, the context runs, and transcripts simply never arrive. So
// recovery is two independent questions, and this module answers both. Pure, so
// the decision is unit-tested rather than reasoned about through a browser.

// `ctxState` is AudioContext.state: 'running' | 'suspended' | 'closed'.
// A CLOSED context is unrecoverable by design - stop() closes it, and resume()
// on a closed context rejects. Recovering that is a restart, not a resume, so we
// never claim it here.
export function recoveryPlan({
  active, visible, ctxState, sttRealtime, sttOpen, sttClosing,
} = {}) {
  const idle = { resumeContext: false, reopenStt: false }
  // Not in a call, or not on screen yet: nothing to repair. Repairing while
  // hidden is worse than waiting - a resume the browser immediately re-suspends
  // burns the one gesture-free chance some browsers grant.
  if (!active || !visible) return idle
  return {
    resumeContext: ctxState === 'suspended',
    // Deliberate teardown (stop, a mode switch, the batch fallback) sets
    // sttClosing - reopening there would fight the user's own instruction.
    reopenStt: !!sttRealtime && !sttOpen && !sttClosing,
  }
}

// Should a socket that just closed be treated as a dropped connection worth
// reopening, or as a close we asked for? Errors are handled elsewhere (they
// fall back to the batch recorder, which is the honest response to a service
// that is failing rather than a tab that went to sleep).
export function shouldReopenAfterClose({ active, sttRealtime, sttClosing } = {}) {
  return !!active && !!sttRealtime && !sttClosing
}

// How to end a realtime utterance, given whether the capture graph actually
// produced audio. The VAD (analyser-driven) can outlive the ScriptProcessor
// feed (iOS kills the processor across playback route changes while the level
// meter keeps reading), so "the turn opened" does not prove audio flowed.
// Committing a zero-frame utterance transcribes nothing and silently loses the
// question; the parallel batch recorder still holds the audio, so salvage from
// it and rebuild the capture graph. A short zero-frame blip commits exactly as
// today (the drop-commit guard swallows it); only a real utterance with a dead
// feed diverges.
//
// #470: `socketChanged` says the realtime socket this utterance began on
// isn't the one open now (it closed and was reopened while the person was
// talking, or it wasn't open yet when they started). Realtime then holds
// only the end of what they said, so a real utterance is salvaged from the
// recorder instead, which holds all of it.
export function realtimeCommitAction({ framesSent, speechMs, minSpeechMs,
                                       socketChanged = false } = {}) {
  if (speechMs >= minSpeechMs && framesSent === 0) return 'salvage-rebuild'
  if (speechMs >= minSpeechMs && socketChanged) return 'salvage'
  return 'commit'
}

// #537: when to try live transcription again after it failed. A network
// drop, or a hiccup at ElevenLabs, passes, and a session that stayed on
// the slower backup path for good paid for it on every later turn (26
// turns on 27 September, until a reload). The waits grow each time it
// fails again, so a service that really is down is asked rarely. A
// failure no wait can mend (the key, the account's quota or terms, or a
// request ElevenLabs refuses outright) isn't retried: `kind` is the
// error's type as the relay passes it on. Null means don't retry.
export const REALTIME_RETRY_MS = [10000, 30000, 60000, 120000, 300000]
const REALTIME_FOR_GOOD = new Set(['auth_error', 'quota_exceeded', 'unaccepted_terms',
                                   'invalid_request'])

export function realtimeRetryMs(kind, attempt) {
  if (REALTIME_FOR_GOOD.has(kind)) return null
  const n = Math.max(0, Math.floor(Number(attempt) || 0))
  return REALTIME_RETRY_MS[Math.min(n, REALTIME_RETRY_MS.length - 1)]
}
