// The one mic setting (#505).
// Run: node --test frontend/src/captureProfile.test.js
//
// What these pin: the mic is asked for echo cancellation on, and noise
// suppression and auto gain off, and nothing a caller does to one answer
// leaks into the next. That it's the same in every mode, through the real
// voice controller, is pinned in voiceMicSetting.test.js.
import { test } from 'node:test'
import assert from 'node:assert/strict'
import { captureConstraints } from './captureProfile.js'

test('echo cancellation on, noise suppression and auto gain off', () => {
  assert.deepEqual(captureConstraints(),
    { echoCancellation: true, noiseSuppression: false, autoGainControl: false })
})

test('whatever a caller passes, the setting is the same', () => {
  for (const mode of [true, false, undefined, null, 'room', 1]) {
    assert.deepEqual(captureConstraints(mode), captureConstraints())
  }
})

test('each call is a fresh copy, so one caller cannot change the next', () => {
  const first = captureConstraints()
  first.noiseSuppression = true
  assert.equal(captureConstraints().noiseSuppression, false)
  assert.notEqual(captureConstraints(), captureConstraints())
})
