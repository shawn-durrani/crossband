// Pure-function tests for the mic capture-profile decision (#28 phase 4).
// Run: node --test frontend/src/captureProfile.test.js
//
// What these pin: solo sessions ask the mic for EXACTLY what they always
// did (the experiment must not touch the path that already works); room
// mode drops noiseSuppression and autoGainControl (single-voice tuning that
// can muffle the second speaker) while echoCancellation stays on in both
// profiles; and the reported profile names are the two the relay's
// allowlist will log - drift here and the field comparison logs go dark.
import { test } from 'node:test'
import assert from 'node:assert/strict'
import {
  captureConstraints, captureProfileName, ROOM_PROFILE, SOLO_PROFILE,
} from './captureProfile.js'

test('solo capture is byte-identical to what start() always requested', () => {
  assert.deepEqual(captureConstraints(false),
    { echoCancellation: true, noiseSuppression: true, autoGainControl: true })
})

test('room capture keeps echo cancellation, drops the single-voice tuning', () => {
  assert.deepEqual(captureConstraints(true),
    { echoCancellation: true, noiseSuppression: false, autoGainControl: false })
})

test('profile names are the two the relay allowlist logs', () => {
  assert.equal(captureProfileName(false), SOLO_PROFILE)
  assert.equal(captureProfileName(true), ROOM_PROFILE)
  assert.equal(SOLO_PROFILE, 'solo-tuned')
  assert.equal(ROOM_PROFILE, 'room-open')
})

test('truthiness coerces - undefined and null read as solo', () => {
  assert.equal(captureProfileName(undefined), SOLO_PROFILE)
  assert.deepEqual(captureConstraints(null), captureConstraints(false))
  assert.deepEqual(captureConstraints(1), captureConstraints(true))
})
