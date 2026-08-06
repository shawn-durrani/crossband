// Invariant test for the cross-chat write-guard.
// Run: node --test frontend/src/streamGuard.test.js
//
// Proves the core invariant: an event carrying chat A's origin is DROPPED once
// chat B is the active chat. This is the seatbelt for detached rounds — chat A's
// round keeps running server-side and its reader can dispatch late events, but
// none of them may touch chat B's message state.
import { test } from 'node:test'
import assert from 'node:assert/strict'
import { eventBelongsToActiveChat } from './streamGuard.js'

test('late event from chat A is dropped while chat B is active', () => {
  const streamChatId = 'chat-A' // the chat this stream was opened for
  const activeChatId = 'chat-B' // where the user is now
  assert.equal(eventBelongsToActiveChat(streamChatId, activeChatId), false)
})

test('event is applied when its stream matches the active chat', () => {
  assert.equal(eventBelongsToActiveChat('chat-A', 'chat-A'), true)
})

test('event is dropped when there is no active chat', () => {
  assert.equal(eventBelongsToActiveChat('chat-A', null), false)
})

test('a null/unknown stream origin never writes (no active chat)', () => {
  assert.equal(eventBelongsToActiveChat(null, null), false)
  assert.equal(eventBelongsToActiveChat(undefined, 'chat-B'), false)
})

test('numeric chat ids compare by strict equality', () => {
  assert.equal(eventBelongsToActiveChat(7, 7), true)
  assert.equal(eventBelongsToActiveChat(7, 8), false)
  // no loose coercion: a stringified id must not match a numeric active id
  assert.equal(eventBelongsToActiveChat('7', 7), false)
})
