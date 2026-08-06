// Tests for voice session recovery: a session must repair itself when the tab
// comes back, instead of looking alive while the microphone side is dead.
// Run: node --test frontend/src/voiceRecovery.test.js
import { test } from 'node:test'
import assert from 'node:assert/strict'
import { realtimeCommitAction, recoveryPlan, shouldReopenAfterClose } from './voiceRecovery.js'

const live = {
  active: true, visible: true, ctxState: 'running',
  sttRealtime: true, sttOpen: true, sttClosing: false,
}

test('a healthy foreground session needs no repair', () => {
  assert.deepEqual(recoveryPlan(live), { resumeContext: false, reopenStt: false })
})

test('THE bug: a suspended context is resumed on return', () => {
  // Backgrounding suspends the context; everything the mic feeds — the VAD
  // analyser, the STT ScriptProcessor, playback — goes silent until it resumes.
  const p = recoveryPlan({ ...live, ctxState: 'suspended' })
  assert.equal(p.resumeContext, true)
})

test('a dropped STT socket is reopened on return', () => {
  const p = recoveryPlan({ ...live, sttOpen: false })
  assert.equal(p.reopenStt, true)
})

test('both can need repair at once — backgrounding takes both', () => {
  const p = recoveryPlan({ ...live, ctxState: 'suspended', sttOpen: false })
  assert.deepEqual(p, { resumeContext: true, reopenStt: true })
})

test('nothing is repaired while the tab is still hidden', () => {
  // Resuming into a hidden tab burns the attempt: the browser re-suspends it,
  // and on some browsers that spends the one gesture-free chance we get.
  const p = recoveryPlan({ ...live, visible: false, ctxState: 'suspended', sttOpen: false })
  assert.deepEqual(p, { resumeContext: false, reopenStt: false })
})

test('nothing is repaired when no call is running', () => {
  const p = recoveryPlan({ ...live, active: false, ctxState: 'suspended', sttOpen: false })
  assert.deepEqual(p, { resumeContext: false, reopenStt: false })
})

test('a CLOSED context is never "resumed" — that needs a restart', () => {
  // stop() closes it; resume() on a closed context rejects. Claiming we can
  // recover it would hide a real restart behind a no-op.
  const p = recoveryPlan({ ...live, ctxState: 'closed' })
  assert.equal(p.resumeContext, false)
})

test('STT is not reopened when realtime mode is off', () => {
  // Batch mode owns its own recorder; there is no stream to restore.
  const p = recoveryPlan({ ...live, sttRealtime: false, sttOpen: false })
  assert.equal(p.reopenStt, false)
})

test('a deliberate teardown is not fought', () => {
  // stop(), a mode switch, or the batch fallback all close the socket on
  // purpose — reopening there would override the user's own instruction.
  const p = recoveryPlan({ ...live, sttOpen: false, sttClosing: true })
  assert.equal(p.reopenStt, false)
})

test('an empty/undefined state is inert, never a crash', () => {
  assert.deepEqual(recoveryPlan(), { resumeContext: false, reopenStt: false })
  assert.deepEqual(recoveryPlan({}), { resumeContext: false, reopenStt: false })
})

// ── the close path ───────────────────────────────────────────────────────────

test('a socket that drops mid-call is reopened', () => {
  assert.equal(shouldReopenAfterClose(
    { active: true, sttRealtime: true, sttClosing: false }), true)
})

test('a socket we closed ourselves stays closed', () => {
  assert.equal(shouldReopenAfterClose(
    { active: true, sttRealtime: true, sttClosing: true }), false)
})

test('no reopening after the call ends, or in batch mode', () => {
  assert.equal(shouldReopenAfterClose(
    { active: false, sttRealtime: true, sttClosing: false }), false)
  assert.equal(shouldReopenAfterClose(
    { active: true, sttRealtime: false, sttClosing: false }), false)
  assert.equal(shouldReopenAfterClose(), false)
})

test('a real utterance with zero frames sent salvages and rebuilds', () => {
  assert.equal(realtimeCommitAction({ framesSent: 0, speechMs: 1800, minSpeechMs: 500 }),
    'salvage-rebuild')
})

test('frames flowed: commit, regardless of length', () => {
  assert.equal(realtimeCommitAction({ framesSent: 12, speechMs: 1800, minSpeechMs: 500 }), 'commit')
  assert.equal(realtimeCommitAction({ framesSent: 3, speechMs: 200, minSpeechMs: 500 }), 'commit')
})

test('a short zero-frame blip commits exactly as today (drop-commit swallows it)', () => {
  assert.equal(realtimeCommitAction({ framesSent: 0, speechMs: 200, minSpeechMs: 500 }), 'commit')
})
