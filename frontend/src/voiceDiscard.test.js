import assert from 'node:assert/strict'
import test from 'node:test'

import { canDiscard, discardWarnings, eraseLink } from './voiceDiscard.js'

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

test('the erase link names membro on the same host with the encoded ref', () => {
  const url = eraseLink(
    { source_app: 'multi-model-chat', conversation: '12', message: '3456' },
    'my-mac.my-tailnet.ts.net')
  assert.equal(url,
    'http://my-mac.my-tailnet.ts.net:8901/#erase=multi-model-chat/12/3456')
  // segments are encoded, never trusted raw
  assert.ok(eraseLink({ source_app: 'a/b', conversation: '1', message: '2' },
    'h').includes('#erase=a%2Fb/1/2'))
})

test('no ref or an older server yields no link, never a broken one', () => {
  assert.equal(eraseLink(null, 'h'), null)
  assert.equal(eraseLink({ source_app: 'x', conversation: '1' }, 'h'), null)
  assert.equal(eraseLink({ source_app: 'x', conversation: '1', message: '2' }, ''), null)
})
