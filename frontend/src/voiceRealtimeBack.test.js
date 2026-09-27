// #537: live transcription comes back after it fails, end to end through
// the real voice controller. On 27 September a network drop switched a
// session to the slower backup transcription, and every later turn in it
// went that way until the page was reloaded. Pins: after a connection
// error the backup path takes over at once, live transcription is tried
// again after a wait and the next turn goes live; the banner says it will
// come back and is cleared when it does; a second failure waits longer; a
// failure no wait can mend (the key, the quota) isn't retried; a hidden
// tab waits to be shown before it reopens; and an ended session tries
// nothing.
// Run: node --test frontend/src/voiceRealtimeBack.test.js
import assert from 'node:assert/strict'
import { afterEach, beforeEach, mock, test } from 'node:test'
import VoiceController from './voice.js'
import { REALTIME_RETRY_MS } from './voiceRecovery.js'

class FakeWebSocket {
  static OPEN = 1
  static opened = []
  constructor(url) {
    this.url = url
    this.readyState = FakeWebSocket.OPEN
    this.sent = []
    FakeWebSocket.opened.push(this)
  }
  send(s) { this.sent.push(JSON.parse(s)) }
  close() { this.readyState = 3 }
}
globalThis.WebSocket = FakeWebSocket
globalThis.Audio = class { setAttribute() {} }
globalThis.location = { protocol: 'http:', host: 'localhost:8902' }

function fakeRecorder() {
  return {
    state: 'recording',
    mimeType: 'audio/webm',
    stop() { this.state = 'inactive'; queueMicrotask(() => this.onstop?.()) },
  }
}

const SAID = 'Dave fixed the gate'
let posts
let controllers
const realDebug = console.debug
const realWarn = console.warn

beforeEach(() => {
  console.debug = () => {}
  console.warn = () => {}
  mock.timers.enable({ apis: ['setTimeout'] })
  FakeWebSocket.opened = []
  posts = []
  controllers = []
  globalThis.fetch = async (url) => {
    posts.push(url)
    if (/\/stt$/.test(url)) return { ok: true, status: 200, json: async () => ({ text: SAID }) }
    return { ok: true, status: 200, json: async () => ({ ok: true }) }
  }
})

afterEach(() => {
  for (const c of controllers) c.stop()
  mock.timers.reset()
  console.debug = realDebug
  console.warn = realWarn
  delete globalThis.fetch
  delete globalThis.document
})

function liveSession() {
  const sent = []
  const banners = []
  let back = 0
  const ctrl = new VoiceController({
    getChatId: () => 7,
    getParticipants: () => [],
    sendText: (text, turnId) => sent.push({ text, turnId }),
    onState: () => {},
    onError: () => {},
    onPartial: () => {},
    onSttFallback: (cause, retrying) => banners.push({ cause, retrying }),
    onSttBack: () => { back++ },
  })
  ctrl.active = true
  ctrl.state = 'listening'
  ctrl.recorder = fakeRecorder()
  ctrl.recChunks = [new Blob(['opus frames'])]
  ctrl.audioCtx = {
    state: 'running', sampleRate: 48000, destination: {},
    createScriptProcessor: () => ({ connect() {}, disconnect() {} }),
    close: async () => {},
  }
  ctrl.micSource = { connect() {} }
  ctrl._openSttStream()
  last().onopen()
  controllers.push(ctrl)
  return { ctrl, sent, banners, backs: () => back }
}

const last = () => FakeWebSocket.opened[FakeWebSocket.opened.length - 1]
const sttPosts = () => posts.filter((u) => /\/stt$/.test(u))

async function settle() {
  for (let i = 0; i < 6; i++) await new Promise((r) => setImmediate(r))
}

// One turn: its audio streams, and the pause ends it. Returns the commit's
// turn id when it went to a live socket.
async function speak(ctrl) {
  const now = Date.now()
  ctrl.speechStart = now - 4100
  ctrl._sttStartStreaming()
  ctrl._utterFrames = 40
  ctrl.lastVoice = now - 2100
  const ws = ctrl.sttWs
  const before = ws ? ws.sent.length : 0
  await ctrl._finalizeUtterance('gap')
  const commit = ws && ws.sent.slice(before).find((m) => m.commit)
  return commit ? commit.turn_id : null
}

// The connection drops: the browser reports an error, then the close.
function drop(ws) {
  ws.onerror()
  ws.onclose({ code: 1006, reason: '' })
}

test('after a network error live transcription comes back and the next turn goes live', async () => {
  const { ctrl, sent, banners, backs } = liveSession()
  drop(last())
  assert.equal(ctrl.sttRealtime, false)
  assert.deepEqual(banners, [{ cause: 'websocket error', retrying: true }])
  // Meanwhile a turn goes the backup way, as before.
  await speak(ctrl)
  await settle()
  assert.equal(sttPosts().length, 1)
  assert.equal(FakeWebSocket.opened.length, 1, 'no reopen before the wait')
  mock.timers.tick(REALTIME_RETRY_MS[0])
  assert.equal(FakeWebSocket.opened.length, 2, 'live transcription tried again')
  assert.equal(ctrl.sttRealtime, true)
  last().onopen()
  const turnId = await speak(ctrl)
  assert.ok(turnId, 'the next turn committed on the new socket')
  last().onmessage({ data: JSON.stringify({ final: SAID, turn_id: turnId }) })
  await settle()
  assert.equal(sttPosts().length, 1, 'no backup call for it')
  assert.deepEqual(sent.map((m) => m.turnId).slice(-1), [turnId])
  assert.equal(backs(), 1, 'the banner is cleared')
})

test('a second failure waits longer before the next try', async () => {
  const { ctrl } = liveSession()
  drop(last())
  mock.timers.tick(REALTIME_RETRY_MS[0])
  assert.equal(FakeWebSocket.opened.length, 2)
  drop(last())
  mock.timers.tick(REALTIME_RETRY_MS[0])
  assert.equal(FakeWebSocket.opened.length, 2, 'not after the first wait again')
  mock.timers.tick(REALTIME_RETRY_MS[1] - REALTIME_RETRY_MS[0])
  assert.equal(FakeWebSocket.opened.length, 3)
  assert.equal(ctrl.sttRealtime, true)
})

test('a failure no wait can mend is not retried', async () => {
  const { ctrl, banners } = liveSession()
  last().onmessage({ data: JSON.stringify({ error: 'Invalid API key', kind: 'auth_error' }) })
  assert.deepEqual(banners, [{ cause: 'relay error: Invalid API key', retrying: false }])
  mock.timers.tick(REALTIME_RETRY_MS[REALTIME_RETRY_MS.length - 1] * 2)
  assert.equal(FakeWebSocket.opened.length, 1)
  assert.equal(ctrl.sttRealtime, false)
})

test('a hidden tab waits to be shown before it reopens', async () => {
  const { ctrl } = liveSession()
  globalThis.document = { hidden: true }
  drop(last())
  mock.timers.tick(REALTIME_RETRY_MS[0])
  assert.equal(FakeWebSocket.opened.length, 1, 'nothing opened while hidden')
  assert.equal(ctrl.sttRealtime, true)
  globalThis.document.hidden = false
  ctrl._recover()
  assert.equal(FakeWebSocket.opened.length, 2)
})

test('an ended session tries nothing', async () => {
  const { ctrl } = liveSession()
  drop(last())
  ctrl.stop()
  mock.timers.tick(REALTIME_RETRY_MS[0] * 10)
  assert.equal(FakeWebSocket.opened.length, 1)
})
