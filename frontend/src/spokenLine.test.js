// The flag that marks the app's own spoken line (membro#136): only a
// delta with the flag set to true and some words in it is spoken at once.
// Everything else goes the usual way, through the pass gate.
// Run: node --test frontend/src/spokenLine.test.js
import { test } from 'node:test'
import assert from 'node:assert/strict'
import { SPEAK_NOW_FLAG, speaksNow } from './spokenLine.js'

const line = (extra) => ({ type: 'delta', speaker: 'claude',
                           text: 'Let me look back through our past chats. ', ...extra })

test('a flagged delta with words in it is spoken at once', () => {
  assert.equal(speaksNow(line({ [SPEAK_NOW_FLAG]: true })), true)
})

test('an ordinary delta takes the usual path', () => {
  assert.equal(speaksNow(line()), false)
  assert.equal(speaksNow(line({ [SPEAK_NOW_FLAG]: false })), false)
  assert.equal(speaksNow(line({ [SPEAK_NOW_FLAG]: 'yes' })), false)
})

test('the flag on anything but a delta, or on no words, does nothing', () => {
  assert.equal(speaksNow({ type: 'work_status', [SPEAK_NOW_FLAG]: true, text: 'hi' }), false)
  assert.equal(speaksNow(line({ [SPEAK_NOW_FLAG]: true, text: '  ' })), false)
  assert.equal(speaksNow(line({ [SPEAK_NOW_FLAG]: true, text: undefined })), false)
  assert.equal(speaksNow(null), false)
})
