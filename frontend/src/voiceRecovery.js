// What a voice session must repair when it comes back to the foreground.
//
// THE BUG this closes: everything the microphone side depends on hangs off ONE
// AudioContext — the analyser the VAD reads, the ScriptProcessor that streams
// PCM to realtime STT, and playback. Browsers suspend that context when the tab
// is backgrounded (mobile Safari especially, and a locked screen counts), and
// nothing in the app ever resumed it. A suspended context produces silence
// forever: the analyser reads flat, `onaudioprocess` stops firing, so no speech
// is detected, captured, or transcribed — while the UI still looks perfectly
// alive. Only a reload built a new context, which is exactly what users hit.
//
// The same root cause fired WITHOUT backgrounding: `_audioUnlocked` latched true
// on the first session and was never reset, so a second `start()` skipped
// priming and left its fresh context suspended — which is why toggling voice
// off and on again didn't recover dictation either.
//
// A backgrounded tab also loses its websockets. A realtime STT socket that
// closes on its own leaves its ScriptProcessor connected and feeding nothing:
// the mic works, the context runs, and transcripts simply never arrive. So
// recovery is two independent questions, and this module answers both. Pure, so
// the decision is unit-tested rather than reasoned about through a browser.

// `ctxState` is AudioContext.state: 'running' | 'suspended' | 'closed'.
// A CLOSED context is unrecoverable by design — stop() closes it, and resume()
// on a closed context rejects. Recovering that is a restart, not a resume, so we
// never claim it here.
export function recoveryPlan({
  active, visible, ctxState, sttRealtime, sttOpen, sttClosing,
} = {}) {
  const idle = { resumeContext: false, reopenStt: false }
  // Not in a call, or not on screen yet: nothing to repair. Repairing while
  // hidden is worse than waiting — a resume the browser immediately re-suspends
  // burns the one gesture-free chance some browsers grant.
  if (!active || !visible) return idle
  return {
    resumeContext: ctxState === 'suspended',
    // Deliberate teardown (stop, a mode switch, the batch fallback) sets
    // sttClosing — reopening there would fight the user's own instruction.
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
export function realtimeCommitAction({ framesSent, speechMs, minSpeechMs } = {}) {
  if (speechMs >= minSpeechMs && framesSent === 0) return 'salvage-rebuild'
  return 'commit'
}
