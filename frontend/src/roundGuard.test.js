// Tests for the wedged-round escape hatch.
// Run: node --test frontend/src/roundGuard.test.js
import { test } from 'node:test'
import assert from 'node:assert/strict'
import { ROUND_EVENT_IDLE_MS, shouldForceRoundDone } from './roundGuard.js'

test('no round, no force - whatever the idle time says', () => {
  assert.equal(shouldForceRoundDone({ roundActive: false, playing: 0, idleMs: 1e9 }), false)
})

test('audio playing is proof of life on its own', () => {
  // The bug class this pins: a guard keyed only on time would cut off a
  // long reply mid-speech. Playback always holds the gate.
  assert.equal(shouldForceRoundDone({ roundActive: true, playing: 1, idleMs: 1e9 }), false)
})

test('a quiet round survives up to the bound, then force-clears', () => {
  const base = { roundActive: true, playing: 0 }
  assert.equal(shouldForceRoundDone({ ...base, idleMs: ROUND_EVENT_IDLE_MS - 1 }), false)
  assert.equal(shouldForceRoundDone({ ...base, idleMs: ROUND_EVENT_IDLE_MS }), false)
  assert.equal(shouldForceRoundDone({ ...base, idleMs: ROUND_EVENT_IDLE_MS + 1 }), true)
})

test('garbage idle values never force anything', () => {
  for (const idleMs of [NaN, undefined, null, 'soon', Infinity * 0]) {
    assert.equal(shouldForceRoundDone({ roundActive: true, playing: 0, idleMs }), false,
                 String(idleMs))
  }
})

test('the bound clears long tool-using gaps only if they go silent', () => {
  // Tool rounds emit work_status/tool_activity; each event resets the
  // caller's idle clock, so a live tool round never reaches the bound.
  // This case documents the contract: the guard sees only idleMs, so
  // keeping the clock fresh on EVERY event type is the caller's job.
  assert.equal(shouldForceRoundDone({ roundActive: true, playing: 0,
                                      idleMs: ROUND_EVENT_IDLE_MS * 10 }), true)
})
