// #304 evidence capture: the client-side diagnostics ring. Pins: entries
// are capped in number and size, error capture records text+stack, the
// global handlers install once and never throw, and the dump posts the
// ring with the chat id (and survives a dead server). The automatic save
// is rate-limited: one per gap, a cap per page, failures never count, and
// a save already in flight is never doubled.
import assert from 'node:assert/strict'
import { beforeEach, test } from 'node:test'
import {
  AUTO_SAVE_MAX_PER_PAGE, AUTO_SAVE_MIN_GAP_MS, autoDump, autoSaveDecision,
  autoSaveNotice, clear, dump, installGlobalCapture, record, recordError,
  resetAutoSavesForTest, resetInstalledForTest, snapshot,
} from './voiceDebug.js'

beforeEach(() => {
  clear()
  resetInstalledForTest()
  resetAutoSavesForTest()
  delete globalThis.fetch
})

// A fake server for the dump route: records each posted body and answers
// with the next file name.
function fakeServer({ ok = true } = {}) {
  const posts = []
  globalThis.fetch = async (url, opts) => {
    const body = JSON.parse(opts.body)
    posts.push({ url, body })
    if (!ok) return { ok: false }
    return { ok: true,
             json: async () => ({ ok: true, file: `f${posts.length}.json`,
                                  where: `data/voice_debug/f${posts.length}.json`,
                                  entries: body.entries.length }) }
  }
  return posts
}

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

test('the button posts as manual, a stall posts its own kind', async () => {
  const posts = fakeServer()
  await dump(3)
  await autoDump(3, 'handoff_stalled', 0)
  assert.equal(posts[0].body.trigger, 'manual')
  assert.equal(posts[1].body.trigger, 'handoff_stalled')
})

test('a stall triggers one automatic save, and the ring says so', async () => {
  const posts = fakeServer()
  record('state', { to: 'listening' })
  const r = await autoDump(7, 'handoff_stalled', 1000)
  assert.equal(r.ok, true)
  assert.equal(posts.length, 1)
  assert.equal(posts[0].url, '/api/voice/debug-dump')
  assert.equal(posts[0].body.chat_id, 7)
  // The saved ring carries its own trigger, so the file explains itself.
  assert.ok(posts[0].body.entries.some((e) => e.tag === 'diag:autoSave'))
})

test('repeated stalls are rate-limited to one save per gap', async () => {
  const posts = fakeServer()
  const t0 = 5000
  assert.equal((await autoDump(1, 'round_guard_forced', t0)).ok, true)
  // A flapping connection: three more stalls inside the gap save nothing.
  for (const dt of [1000, 60000, AUTO_SAVE_MIN_GAP_MS - 1]) {
    const r = await autoDump(1, 'handoff_stalled', t0 + dt)
    assert.deepEqual(r, { ok: false, skipped: 'too_soon' })
  }
  assert.equal(posts.length, 1)
  // Each skip is still on record for a later manual save.
  const skipped = snapshot().filter((e) => e.tag === 'diag:autoSkipped')
  assert.equal(skipped.length, 3)
  // Past the gap, a new stall saves again.
  assert.equal((await autoDump(1, 'handoff_stalled', t0 + AUTO_SAVE_MIN_GAP_MS)).ok, true)
  assert.equal(posts.length, 2)
})

test('a page load saves at most its cap, however long it runs', async () => {
  const posts = fakeServer()
  let now = 0
  for (let i = 0; i < AUTO_SAVE_MAX_PER_PAGE + 3; i += 1) {
    await autoDump(1, 'handoff_stalled', now)
    now += AUTO_SAVE_MIN_GAP_MS
  }
  assert.equal(posts.length, AUTO_SAVE_MAX_PER_PAGE)
  assert.deepEqual(await autoDump(1, 'handoff_stalled', now),
                   { ok: false, skipped: 'page_limit' })
})

test('a failed save writes nothing, so it does not use up the limit', async () => {
  fakeServer({ ok: false })
  assert.equal((await autoDump(1, 'handoff_stalled', 0)).ok, false)
  const posts = fakeServer()
  assert.equal((await autoDump(1, 'handoff_stalled', 10)).ok, true)
  assert.equal(posts.length, 1)
  assert.ok(snapshot().some((e) => e.tag === 'diag:autoFailed'))
})

test('two stalls in the same moment post once', async () => {
  const posts = fakeServer()
  const [a, b] = await Promise.all([
    autoDump(1, 'gated_speech_stranded', 0),
    autoDump(1, 'round_guard_forced', 0),
  ])
  assert.equal(a.ok, true)
  assert.deepEqual(b, { ok: false, skipped: 'in_flight' })
  assert.equal(posts.length, 1)
})

test('the decision rule on its own', () => {
  assert.deepEqual(autoSaveDecision([], 0), { allowed: true, reason: null })
  assert.equal(autoSaveDecision([0], AUTO_SAVE_MIN_GAP_MS - 1).reason, 'too_soon')
  assert.equal(autoSaveDecision([0], AUTO_SAVE_MIN_GAP_MS).allowed, true)
  const full = Array.from({ length: AUTO_SAVE_MAX_PER_PAGE }, (_, i) => i)
  assert.equal(autoSaveDecision(full, 1e12).reason, 'page_limit')
})

test('the notice says it was saved, that no speech is in it, and where', () => {
  const line = autoSaveNotice('data/voice_debug/voice_debug_x.json')
  assert.match(line, /saved diagnostics/)
  assert.match(line, /no speech/)
  assert.ok(line.endsWith('data/voice_debug/voice_debug_x.json'))
})
