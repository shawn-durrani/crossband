// The discard affordance (#106/#111). Only the owner's settled voice turns
// qualify. The confirm copy states exactly what a discard cannot undo, and the
// erase link names membro at the origin it reports (contract 1.4), or on the
// same host with membro's fixed port for an older membro, or honestly
// nothing on an older server.
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

test('the erase link uses the origin membro reports when it gives one', () => {
  const ref = { source_app: 'some-app', conversation: '12', message: '3456' }
  assert.equal(eraseLink(ref, 'my-mac.my-tailnet.ts.net', 'https://my-mac.my-tailnet.ts.net:8443'),
    'https://my-mac.my-tailnet.ts.net:8443/#erase=some-app/12/3456')
  // a trailing slash on the origin never doubles up
  assert.equal(eraseLink(ref, 'ignored', 'http://127.0.0.1:8901/'),
    'http://127.0.0.1:8901/#erase=some-app/12/3456')
  // the reported origin wins even when the hostname is empty
  assert.equal(eraseLink(ref, '', 'http://127.0.0.1:8901'),
    'http://127.0.0.1:8901/#erase=some-app/12/3456')
})

test('an older membro falls back to the same host on its fixed port', () => {
  const url = eraseLink(
    { source_app: 'some-app', conversation: '12', message: '3456' },
    'my-mac.my-tailnet.ts.net')
  assert.equal(url,
    'http://my-mac.my-tailnet.ts.net:8901/#erase=some-app/12/3456')
  assert.equal(eraseLink(
    { source_app: 'some-app', conversation: '12', message: '3456' },
    'my-mac.my-tailnet.ts.net', ''), url)
  assert.equal(eraseLink(
    { source_app: 'some-app', conversation: '12', message: '3456' },
    'my-mac.my-tailnet.ts.net', undefined), url)
  // segments are encoded, never trusted raw, on both paths
  assert.ok(eraseLink({ source_app: 'a/b', conversation: '1', message: '2' },
    'h').includes('#erase=a%2Fb/1/2'))
  assert.ok(eraseLink({ source_app: 'a/b', conversation: '1', message: '2' },
    'h', 'https://o').includes('#erase=a%2Fb/1/2'))
})

test('no ref or an older server yields no link, never a broken one', () => {
  assert.equal(eraseLink(null, 'h'), null)
  assert.equal(eraseLink(null, 'h', 'https://o'), null)
  assert.equal(eraseLink({ source_app: 'x', conversation: '1' }, 'h'), null)
  assert.equal(eraseLink({ source_app: 'x', conversation: '1', message: '2' }, ''), null)
})
