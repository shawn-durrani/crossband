// Pure-function tests for the voice playback gate (a regression guard).
// Run: node --test frontend/src/voiceGate.test.js
//
// The guardrail these pin: a barge-in's `dropQueue` must never leak past the
// round it happened in. That leak is the exact bug that made TTS go silent once
// rounds became detached (a false barge-in muted every following reply).
import { test } from 'node:test'
import assert from 'node:assert/strict'
import { initGate, gateEvent, gateRoundDone } from './voiceGate.js'

// Simulate the interrupt() side of the controller: a barge-in sets dropQueue.
const bargeIn = (s) => ({ ...s, dropQueue: true })

test('a normal round un-drops on the user turn and voices its speakers', () => {
  let g = initGate()
  g = gateEvent(g, 'user_saved')
  assert.deepEqual(g, { roundActive: true, dropQueue: false })
  g = gateEvent(g, 'speaker_start')
  assert.equal(g.dropQueue, false) // _beginSpeaker will open TTS
})

test('a barge-in only silences the round it happened in — not the next one', () => {
  // Round 1: user turn, then a false barge-in mid-round sets dropQueue.
  let g = gateEvent(initGate(), 'user_saved')
  g = bargeIn(g)
  assert.equal(g.dropQueue, true) // rest of round 1 correctly stays silent

  // THE FIX: the round ending clears the drop. Previously this leaked, because
  // a detached round's abort never routed through a reset, so audio stayed dead.
  g = gateRoundDone()
  assert.deepEqual(g, { roundActive: false, dropQueue: false })

  // Round 2 (next user turn) is audible again.
  g = gateEvent(g, 'user_saved')
  g = gateEvent(g, 'speaker_start')
  assert.equal(g.dropQueue, false)
})

test('regression: without the round-done reset, a stuck drop would mute round 2', () => {
  // Documents the OLD broken behavior so the fix can't silently regress: if the
  // round end did NOT clear dropQueue, the next user turn resets it via
  // user_saved — but a barge-in whose STT produced no send (echo/false trip)
  // leaves NO user_saved, so audio would stay dead. gateRoundDone closes that.
  let g = bargeIn(gateEvent(initGate(), 'user_saved'))
  const withoutReset = gateEvent(g, 'delta') // a non-resetting event
  assert.equal(withoutReset.dropQueue, true) // still muted — the trap
  const withReset = gateRoundDone()
  assert.equal(withReset.dropQueue, false)   // the fix frees it
})

test('"Let them continue": a barge-in in round 1 does not silence rounds 2..N', () => {
  // One detached stream, multiple internal `round` markers; roundActive never
  // drops between them, so the reset rides the round marker.
  let g = gateEvent(initGate(), 'user_saved')
  g = gateEvent(g, 'round')          // round 1 of N
  g = bargeIn(g)                     // user interrupts round 1
  assert.equal(g.dropQueue, true)
  g = gateEvent(g, 'round')          // round 2 marker
  assert.equal(g.dropQueue, false)   // un-dropped for the autonomous continuation
})

test('an autonomous speaker with no active round un-drops (post-continue)', () => {
  // A speaker_start with roundActive already true must NOT clear a drop set this
  // round — only a start that OPENS a fresh autonomous round does.
  let g = bargeIn({ roundActive: true, dropQueue: true })
  const midRound = gateEvent(g, 'speaker_start')
  assert.equal(midRound.dropQueue, true) // same round: still muted
  const freshRound = gateEvent({ roundActive: false, dropQueue: true }, 'speaker_start')
  assert.equal(freshRound.dropQueue, false) // new autonomous round: audible
})

test('gateEvent is pure — it never mutates the input state', () => {
  const g = { roundActive: false, dropQueue: false }
  gateEvent(g, 'user_saved')
  assert.deepEqual(g, { roundActive: false, dropQueue: false })
})

test('unrelated event types leave gating untouched', () => {
  const g = { roundActive: true, dropQueue: true }
  for (const t of ['delta', 'speaker_end', 'error', 'tool_activity', 'guest_job']) {
    assert.deepEqual(gateEvent(g, t), g)
  }
})
