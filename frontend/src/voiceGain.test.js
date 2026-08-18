// Tests for relative voice volume (#163).
// Run: node --test frontend/src/voiceGain.test.js
import { test } from 'node:test'
import assert from 'node:assert/strict'
import { GAIN_MAX, GAIN_MIN, effectiveVolume } from './voiceGain.js'

test('all gains at 1 is byte-for-byte the old behaviour', () => {
  assert.equal(effectiveVolume(1, [1, 1, 1]), 1)
  // turning a loud voice DOWN still works exactly as before
  assert.equal(effectiveVolume(0.6, [1, 0.6, 1]), 0.6)
})

test('boosting one voice ducks the others, never exceeds full volume', () => {
  const gains = [2, 1, 1] // Yu boosted to 2x
  assert.equal(effectiveVolume(2, gains), 1)     // the boosted voice: full
  assert.equal(effectiveVolume(1, gains), 0.5)   // everyone else: halved
})

test('gains clamp to the documented range', () => {
  assert.equal(effectiveVolume(99, [99, 1]), 1)
  assert.equal(effectiveVolume(1, [99, 1]), 1 / GAIN_MAX)
  // a voice pushed below the floor by an extreme spread stays audible
  assert.ok(effectiveVolume(GAIN_MIN, [GAIN_MAX, GAIN_MIN]) >= 0.05)
})

test('garbage gains read as 1 rather than silencing anyone', () => {
  assert.equal(effectiveVolume(undefined, [null, 'x', 1]), 1)
  assert.equal(effectiveVolume(NaN, []), 1)
})
