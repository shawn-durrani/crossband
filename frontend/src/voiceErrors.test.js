// Playback failure messages are user-readable and name the recovery.
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
  // #21: AbortError is the routine cut-off after an interruption - it must
  // not print a device checklist, and never the mobile silent-switch copy.
  const m = playbackFailureMessage({ name: 'AbortError' })
  assert.match(m, /cut off mid-reply/i)
  assert.ok(!/silent switch/i.test(m))
  // the generic failure names the platform's own hardware, not the phone's
  assert.match(playbackFailureMessage({ name: 'ZError' }, { mobile: true }), /silent switch/i)
  assert.ok(!/silent switch/i.test(playbackFailureMessage({ name: 'ZError' })))
  assert.match(playbackFailureMessage(undefined), /Voice playback failed/)
})
