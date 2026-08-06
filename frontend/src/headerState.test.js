// Tests for the header title/badge: it must track the active chat_id, so the
// header can never describe a different chat than the sidebar selection/body.
// Run: node --test frontend/src/headerState.test.js
import { test } from 'node:test'
import assert from 'node:assert/strict'
import { resolveHeaderChat, resolveHeaderTitle } from './headerState.js'

const chats = [
  { id: 1, title: 'Sourdough starter notes' },
  { id: 2, title: 'Weekend hiking plans' },
]

test('uses the freshly loaded activeChat when it matches the active id', () => {
  const activeChat = { id: 2, title: 'Weekend hiking plans', voice_mode: true }
  assert.equal(resolveHeaderChat(activeChat, 2, chats), activeChat)
  assert.equal(resolveHeaderTitle(activeChat, 2, chats), 'Weekend hiking plans')
})

test('lagging activeChat never leaks a stale title — sidebar copy wins by id', () => {
  // The reported bug: user switched to chat 1, but activeChat is still chat 2
  // (its async getChat has not landed yet). The header must show chat 1.
  const staleActiveChat = { id: 2, title: 'Weekend hiking plans' }
  assert.equal(resolveHeaderTitle(staleActiveChat, 1, chats), 'Sourdough starter notes')
  assert.equal(resolveHeaderChat(staleActiveChat, 1, chats).id, 1)
})

test('out-of-order switch: id is the single source of truth', () => {
  // activeChatId settled on 1; a late getChat(2) response set activeChat to 2.
  // Title still follows the active id, not the last response to arrive.
  const activeChat = { id: 2, title: 'Weekend hiking plans' }
  assert.equal(resolveHeaderTitle(activeChat, 1, chats), 'Sourdough starter notes')
})

test('no active chat means no header chat', () => {
  assert.equal(resolveHeaderChat(null, null, chats), null)
  assert.equal(resolveHeaderTitle(null, null, chats), '')
})

test('brief loading gap (id set, not yet in the sidebar list) is neutral', () => {
  assert.equal(resolveHeaderChat(null, 99, chats), null)
  assert.equal(resolveHeaderTitle(null, 99, chats), '')
})

test('membership is strict — no numeric/string coercion', () => {
  const activeChat = { id: 2, title: 'Weekend hiking plans' }
  // string "2" must not match numeric id 2
  assert.equal(resolveHeaderChat(activeChat, '2', chats), null)
})

test('safe on a missing chats list', () => {
  assert.equal(resolveHeaderTitle(null, 1, undefined), '')
  assert.equal(resolveHeaderChat({ id: 1, title: 'x' }, 1, undefined).title, 'x')
})
