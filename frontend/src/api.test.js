// The fetch wrapper's contract (#242): failures REJECT (the distill fix in
// #243 depends on it), a 401 fires the lock-screen hook exactly once, the
// lock probe never recurses into that hook, and path segments are encoded
// so a slash in a model id cannot break routing.
import { test } from 'node:test'
import assert from 'node:assert/strict'
import { api, setUnauthorizedHandler, streamSSE, SSE_IDLE_TIMEOUT_MS } from './api.js'

const okJson = (payload) => ({ ok: true, json: async () => payload })
const errJson = (status, payload) => ({
  ok: false, status, statusText: `HTTP ${status}`,
  json: async () => { if (payload === undefined) throw new SyntaxError('not json'); return payload },
})

function captureFetch(response) {
  const calls = []
  globalThis.fetch = async (url, opts = {}) => {
    calls.push({ url, ...opts })
    return typeof response === 'function' ? response(url, opts) : (response ?? okJson({}))
  }
  return calls
}

test('a failed call rejects with the server detail - never resolves with an error payload', async () => {
  captureFetch(errJson(500, { detail: 'boom' }))
  await assert.rejects(api.state(), { message: 'boom' })
})

test('a non-JSON or detail-less error body falls back to the status text', async () => {
  captureFetch(errJson(500))
  await assert.rejects(api.state(), { message: 'HTTP 500' })
  captureFetch(errJson(500, {}))
  await assert.rejects(api.state(), { message: 'HTTP 500' })
})

test('a 401 fires the lock-screen hook exactly once; other statuses never do', async () => {
  let fired = 0
  setUnauthorizedHandler(() => { fired += 1 })
  captureFetch(errJson(401, { detail: 'session expired' }))
  await assert.rejects(api.state(), { message: 'session expired' })
  assert.equal(fired, 1)
  captureFetch(errJson(403, { detail: 'no' }))
  await assert.rejects(api.state(), { message: 'no' })
  assert.equal(fired, 1)
  setUnauthorizedHandler(null)
  captureFetch(errJson(401, { detail: 'still expired' }))
  await assert.rejects(api.state(), { message: 'still expired' })
})

test('authSession bypasses the 401 hook by design - probing the lock must never recurse', async () => {
  let fired = 0
  setUnauthorizedHandler(() => { fired += 1 })
  captureFetch({ ok: false, status: 401, statusText: 'HTTP 401', json: async () => ({ enrolled: true }) })
  assert.deepEqual(await api.authSession(), { enrolled: true })
  assert.equal(fired, 0)
  setUnauthorizedHandler(null)
})

test('URL, method and body shapes for a representative sample', async () => {
  let calls = captureFetch()
  await api.authLogin('pw')
  assert.equal(calls[0].url, '/api/auth/login')
  assert.equal(calls[0].method, 'POST')
  assert.deepEqual(JSON.parse(calls[0].body), { password: 'pw' })
  assert.equal(calls[0].headers['Content-Type'], 'application/json')

  calls = captureFetch()
  await api.updateChat(7, { title: 'x' })
  assert.equal(calls[0].url, '/api/chats/7')
  assert.equal(calls[0].method, 'PATCH')

  calls = captureFetch()
  await api.deleteProject(3)
  assert.deepEqual([calls[0].url, calls[0].method, calls[0].body],
    ['/api/projects/3', 'DELETE', undefined])

  // The events-stream hydration contract (#243 threadpooled this route).
  calls = captureFetch()
  await api.messagesAfter(7, 42)
  assert.equal(calls[0].url, '/api/chats/7/messages?after=42')

  calls = captureFetch()
  await api.discardTurn(7, 9)
  assert.deepEqual([calls[0].url, calls[0].method],
    ['/api/chats/7/messages/9/discard', 'POST'])

  calls = captureFetch()
  await api.listModels('openai')
  assert.equal(calls[0].url, '/api/models?provider=openai')
  calls = captureFetch()
  await api.listModels('openai', 'KEY', 'http://x')
  assert.equal(calls[0].url,
    '/api/models?provider=openai&api_key_env=KEY&base_url=http%3A%2F%2Fx')
})

test('path segments are encoded, so a slash in a model id cannot break routing', async () => {
  let calls = captureFetch()
  await api.saveRateCard('org/model:free', {})
  assert.equal(calls[0].url, '/api/pricing/org%2Fmodel%3Afree')
  calls = captureFetch()
  await api.renameVoice('p 1', 'Sam')
  assert.equal(calls[0].url, '/api/voice/people/p%201/name')
  assert.deepEqual(JSON.parse(calls[0].body), { name: 'Sam' })
  // Pure URL builder: no fetch happens at all.
  calls = captureFetch()
  assert.equal(api.voiceClipAudioUrl('p 1', 'a b.wav'),
    '/api/voice/people/p%201/clips/a%20b.wav/audio')
  assert.equal(calls.length, 0)
})

test('optional query parts appear only when given', async () => {
  let calls = captureFetch()
  await api.usageSummary()
  assert.equal(calls[0].url, '/api/usage/summary?window=all')
  calls = captureFetch()
  await api.integrations()
  assert.equal(calls[0].url, '/api/integrations')
  calls = captureFetch()
  await api.integrations(true)
  assert.equal(calls[0].url, '/api/integrations?probe=true')
  calls = captureFetch()
  await api.voiceHealth()
  assert.equal(calls[0].url, '/api/voice/health')
  calls = captureFetch()
  await api.voiceHealth(5)
  assert.equal(calls[0].url, '/api/voice/health?chat_id=5')
})

test('uploads ride FormData with no explicit headers - the browser sets the boundary', async () => {
  const calls = captureFetch()
  await api.uploadAttachment(new File(['x'], 'a.txt'))
  assert.equal(calls[0].url, '/api/attachments')
  assert.ok(calls[0].body instanceof FormData)
  assert.equal(calls[0].headers, undefined)
})

test('ping maps reachability to a boolean and never throws', async () => {
  captureFetch(okJson({}))
  assert.equal(await api.ping(), true)
  globalThis.fetch = async () => { throw new TypeError('network down') }
  assert.equal(await api.ping(), false)
})

test('streamSSE rejects with err.status so a 409 round-conflict can route to a hold', async () => {
  captureFetch(errJson(409, { detail: 'round in flight' }))
  await assert.rejects(streamSSE('/api/x', {}, () => {}), (e) => {
    assert.equal(e.message, 'round in flight')
    assert.equal(e.status, 409)
    return true
  })
})

test('streamSSE parses only data: lines and survives a frame split across reads', async () => {
  const chunks = ['event: x\ndata: {"a"', ':1}\n\n: comment\n\nda', 'ta: {"b":2}\n\n']
  const enc = new TextEncoder()
  const stream = new ReadableStream({
    start(ctrl) { chunks.forEach((c) => ctrl.enqueue(enc.encode(c))); ctrl.close() },
  })
  globalThis.fetch = async () => ({ ok: true, status: 200, body: stream })
  const seen = []
  await streamSSE('/api/x', null, (ev) => seen.push(ev), undefined, 'GET')
  assert.deepEqual(seen, [{ a: 1 }, { b: 2 }])
})

test('GET sends no body and no Content-Type; JSON posts send both', async () => {
  const seen = []
  globalThis.fetch = async (url, opts) => {
    seen.push(opts)
    return { ok: true, status: 200, body: new ReadableStream({ start(c) { c.close() } }) }
  }
  await streamSSE('/api/x', { ignored: true }, () => {}, undefined, 'GET')
  assert.deepEqual([seen[0].body, seen[0].headers], [undefined, undefined])
  await streamSSE('/api/x', { a: 1 }, () => {})
  assert.deepEqual(JSON.parse(seen[1].body), { a: 1 })
  assert.equal(seen[1].headers['Content-Type'], 'application/json')
})

test('the SSE idle timeout sits above the 60s voice round guard', () => {
  assert.equal(SSE_IDLE_TIMEOUT_MS, 90000)
})
