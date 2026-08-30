// #304 evidence capture: the client-side diagnostics ring. Pins: entries
// are capped in number and size, error capture records text+stack, the
// global handlers install once and never throw, and the dump posts the
// ring with the chat id (and survives a dead server).
import assert from 'node:assert/strict'
import { beforeEach, test } from 'node:test'
import {
  clear, dump, installGlobalCapture, record, recordError,
  resetInstalledForTest, snapshot,
} from './voiceDebug.js'

beforeEach(() => {
  clear()
  resetInstalledForTest()
})

test('record keeps timestamped, serialised, size-capped entries', () => {
  record('state', { from: 'listening', to: 'working' })
  record('empty')
  record('big', { blob: 'x'.repeat(5000) })
  const s = snapshot()
  assert.equal(s.length, 3)
  assert.equal(typeof s[0].t, 'number')
  assert.equal(s[0].tag, 'state')
  assert.deepEqual(JSON.parse(s[0].data), { from: 'listening', to: 'working' })
  assert.equal(s[1].data, null)
  assert.ok(s[2].data.length <= 300)
})

test('the ring is bounded to the newest entries', () => {
  for (let i = 0; i < 450; i += 1) record(`e${i}`)
  const s = snapshot()
  assert.equal(s.length, 400)
  assert.equal(s[s.length - 1].tag, 'e449')
  assert.equal(s[0].tag, 'e50')
})

test('recordError keeps message and stack, capped', () => {
  recordError('round', 'model unavailable', 'stack\nline'.repeat(400))
  const [e] = snapshot()
  assert.equal(e.tag, 'error:round')
  const parsed = JSON.parse(e.data)
  assert.equal(parsed.message, 'model unavailable')
  assert.ok(parsed.stack.length <= 1500)
})

test('installGlobalCapture records window errors and rejections, once', () => {
  const listeners = {}
  const fakeWindow = {
    addEventListener: (kind, fn) => { listeners[kind] = fn },
  }
  installGlobalCapture(fakeWindow)
  installGlobalCapture(fakeWindow) // idempotent - handlers do not stack
  listeners.error({ message: 'boom', error: { stack: 'at boom' } })
  listeners.unhandledrejection({ reason: { message: 'rej', stack: 'at rej' } })
  listeners.unhandledrejection({ reason: 'plain string' })
  const s = snapshot()
  assert.equal(s.length, 3)
  assert.equal(s[0].tag, 'error:window')
  assert.deepEqual(JSON.parse(s[0].data),
    { message: 'boom', stack: 'at boom' })
  assert.equal(s[1].tag, 'error:unhandledrejection')
  assert.equal(JSON.parse(s[2].data).message, 'plain string')
})

test('dump posts the ring with the chat id and returns the server answer', async () => {
  record('state', { to: 'working' })
  const calls = []
  globalThis.fetch = async (url, opts) => {
    calls.push({ url, body: JSON.parse(opts.body) })
    return { ok: true, json: async () => ({ ok: true, file: 'f.json', entries: 1 }) }
  }
  const r = await dump(7)
  assert.deepEqual(r, { ok: true, file: 'f.json', entries: 1 })
  assert.equal(calls[0].url, '/api/voice/debug-dump')
  assert.equal(calls[0].body.chat_id, 7)
  assert.equal(calls[0].body.entries.length, 1)
  delete globalThis.fetch
})

test('dump never throws when the server is unreachable', async () => {
  globalThis.fetch = async () => { throw new TypeError('network down') }
  assert.deepEqual(await dump(1), { ok: false })
  globalThis.fetch = async () => ({ ok: false })
  assert.deepEqual(await dump(1), { ok: false })
  delete globalThis.fetch
})
