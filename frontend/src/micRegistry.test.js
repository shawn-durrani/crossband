// #134: every live microphone visible from every surface - the pure rules
// behind the banner. The field failure: two capture sessions at once, the
// owner ended the visible one, the orphan kept hearing the room.
import assert from 'node:assert/strict'
import test from 'node:test'

import { captureBanner, foreignCaptures } from './micRegistry.js'

const A = { sid: 'aaa', chat_id: 1, started_at: 1755300000 }
const B = { sid: 'bbb', chat_id: 2, started_at: 1755300000 }

test('own session is never foreign; empty registry says nothing', () => {
  assert.deepEqual(foreignCaptures([A], 'aaa'), [])
  assert.equal(captureBanner([], null, 1), null)
  assert.equal(captureBanner([A], 'aaa', 1), null)
})

test('a mic live in another window is named, singular and plural', () => {
  const one = captureBanner([A], null, 99)
  assert.equal(one.level, 'elsewhere')
  assert.match(one.text, /A microphone is live in another window/)
  const two = captureBanner([A, B], null, 99)
  assert.match(two.text, /2 microphones/)
})

test('a second mic in THIS chat is the loud case (#134 doubled turns)', () => {
  const mic = captureBanner([A, { ...B, chat_id: 1 }], 'bbb', 1)
  assert.equal(mic.level, 'double')
  assert.match(mic.text, /heard twice/)
  // without a session of our own, another window is just 'elsewhere' -
  // nothing is doubled if only one mic is live
  assert.equal(captureBanner([A], null, 1).level, 'elsewhere')
})
