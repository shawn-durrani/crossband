import assert from 'node:assert/strict'
import test from 'node:test'

import { canDiscard, discardWarnings } from './voiceDiscard.js'

test('only your own settled voice turns carry the discard', () => {
  assert.equal(canDiscard({ speaker: 'user', voice_turn_id: 't1' }), true)
  assert.equal(canDiscard({ speaker: 'user', voice_turn_id: '' }), false)   // typed
  assert.equal(canDiscard({ speaker: 'claude', voice_turn_id: 't1' }), false)
  assert.equal(canDiscard({ speaker: 'user', voice_turn_id: 't1',
                            streaming: true }), false)
  assert.equal(canDiscard(null), false)
})

test('the confirm copy states exactly what a discard cannot undo', () => {
  const w = discardWarnings({ hasLaterReplies: true })
  assert.equal(w.length, 3)
  assert.match(w[1], /already read it/)
  assert.match(w[2], /append-only/)
  const w2 = discardWarnings({ hasLaterReplies: false })
  assert.equal(w2.length, 2)
})
