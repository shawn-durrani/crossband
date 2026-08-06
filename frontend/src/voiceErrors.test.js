import test from 'node:test'
import assert from 'node:assert/strict'
import { playbackFailureMessage } from './voiceErrors.js'

test('autoplay block tells the user the unlock gesture', () => {
  const m = playbackFailureMessage({ name: 'NotAllowedError' })
  assert.match(m, /blocked/i)
  assert.match(m, /tap/i)
})

test('decode failure suggests a reload', () => {
  assert.match(playbackFailureMessage({ name: 'NotSupportedError' }), /reload/i)
})

test('unknown failures name the error and point at routing', () => {
  const m = playbackFailureMessage({ name: 'AbortError' })
  assert.match(m, /AbortError/)
  assert.match(m, /silent switch|volume|Bluetooth/i)
  assert.match(playbackFailureMessage(undefined), /Voice playback failed/)
})
