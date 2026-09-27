// #470: when a voice turn takes the backup path, and what it tells the
// server, end to end through the real voice controller. Scribe used to
// close the realtime socket after about 15 seconds of quiet, the browser
// reopened it, and a turn spoken as that happened reached realtime only
// in part: its commit went to the new socket, which had heard just the
// end. Pins: a turn spoken across a socket change is sent once from the
// backup recording, its partial realtime words are never sent, and its
// commit still goes so the end can't run into the next turn; a socket
// replaced between turns changes nothing; and every backup-path call says
// why it was made (batch, reconnecting, reconnect, late, rescue, no_audio),
// a word the server logs, never anything that was said.
// Run: node --test frontend/src/voiceBackupPath.test.js
import assert from 'node:assert/strict'
import { afterEach, beforeEach, mock, test } from 'node:test'
import VoiceController from './voice.js'
import { sttCommitTimeoutMs } from './turnPolicy.js'

class FakeWebSocket {
  static OPEN = 1
  static CONNECTING = 0
  static last = null
  constructor(url) {
    this.url = url
    this.readyState = FakeWebSocket.OPEN
    this.sent = []
    FakeWebSocket.last = this
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

// What realtime heard of a turn, and what the backup copy holds.
const TAIL = 'the shed at Fairhaven'
const BATCH = 'Sam left the ladder by the shed at Fairhaven'

let posts
let controllers
const realDebug = console.debug
const realWarn = console.warn

beforeEach(() => {
  console.debug = () => {}
  console.warn = () => {}
  mock.timers.enable({ apis: ['setTimeout'] })
  posts = []
  controllers = []
  globalThis.fetch = async (url, opts) => {
    const form = opts && opts.body instanceof FormData ? opts.body : null
    posts.push({ url, why: form ? form.get('why') : null })
    if (/\/api\/chats\/\d+\/stt$/.test(url)) {
      return { ok: true, status: 200, json: async () => ({ text: BATCH }) }
    }
    return { ok: true, status: 200, json: async () => ({ ok: true }) }
  }
})

afterEach(() => {
  for (const c of controllers) c.stop()
  mock.timers.reset()
  console.debug = realDebug
  console.warn = realWarn
  delete globalThis.fetch
})

function liveSession({ realtime = true } = {}) {
  const sent = []
  const ctrl = new VoiceController({
    getChatId: () => 7,
    getParticipants: () => [],
    sendText: (text, turnId) => sent.push({ text, turnId }),
    onState: () => {},
    onError: () => {},
    onPartial: () => {},
    onSttFallback: () => {},
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
  if (realtime) {
    ctrl._openSttStream()
    FakeWebSocket.last.onopen()
  } else {
    ctrl.sttRealtime = false
  }
  controllers.push(ctrl)
  return { ctrl, sent }
}

// One spoken turn: its audio starts streaming, `during()` runs while it's
// spoken, and the pause ends it.
async function speak(ctrl, { speechMs = 2000, frames = 40, during = () => {} } = {}) {
  const now = Date.now()
  ctrl.speechStart = now - speechMs - 2100
  ctrl._sttStartStreaming()
  ctrl._utterFrames = frames
  during()
  ctrl.lastVoice = now - 2100
  await ctrl._finalizeUtterance('gap')
}

// Scribe closing an idle socket, and the browser reopening it.
function dropSocket() {
  FakeWebSocket.last.onclose({ code: 1000, reason: '' })
}
function reopenSocket() {
  mock.timers.tick(500)
  FakeWebSocket.last.onopen()
  return FakeWebSocket.last
}

function relay(ws, msg) {
  ws.onmessage({ data: JSON.stringify(msg) })
}

async function settle() {
  for (let i = 0; i < 6; i++) await new Promise((r) => setImmediate(r))
}

const sttPosts = () => posts.filter((p) => /\/api\/chats\/\d+\/stt$/.test(p.url))
const commits = (ws) => ws.sent.filter((m) => m.commit)

test('a turn spoken across a socket change is sent once, from the backup copy', async () => {
  const { ctrl, sent } = liveSession()
  const first = FakeWebSocket.last
  let second = null
  await speak(ctrl, { during: () => { dropSocket(); second = reopenSocket() } })
  assert.notEqual(second, first)
  // The commit still goes to the new socket, so the end it heard is
  // closed off there, and the first socket gets nothing more.
  assert.equal(commits(first).length, 0)
  assert.equal(commits(second).length, 1)
  const turnId = commits(second)[0].turn_id
  await settle()
  assert.deepEqual(sttPosts().map((p) => p.why), ['reconnect'])
  assert.deepEqual(sent, [{ text: BATCH, turnId }])
  // The new socket's words for the end of it never go anywhere.
  relay(second, { final: TAIL, turn_id: turnId })
  await settle()
  assert.deepEqual(sent, [{ text: BATCH, turnId }])
  assert.equal(ctrl.state, 'listening')
})

test('a turn that began while the socket was still connecting is salvaged too', async () => {
  const { ctrl, sent } = liveSession()
  dropSocket()
  mock.timers.tick(500)
  const connecting = FakeWebSocket.last
  connecting.readyState = FakeWebSocket.CONNECTING
  await speak(ctrl, { during: () => { connecting.readyState = FakeWebSocket.OPEN; connecting.onopen() } })
  await settle()
  assert.deepEqual(sttPosts().map((p) => p.why), ['reconnect'])
  assert.equal(sent.length, 1)
  assert.equal(sent[0].text, BATCH)
})

test('a socket replaced between turns changes nothing', async () => {
  const { ctrl, sent } = liveSession()
  dropSocket()
  const ws = reopenSocket()
  await speak(ctrl)
  const turnId = commits(ws)[0].turn_id
  relay(ws, { final: BATCH, turn_id: turnId })
  await settle()
  assert.equal(sttPosts().length, 0, 'realtime had all of it')
  assert.deepEqual(sent, [{ text: BATCH, turnId }])
})

test('a turn on one socket throughout goes by realtime, as ever', async () => {
  const { ctrl, sent } = liveSession()
  const ws = FakeWebSocket.last
  await speak(ctrl)
  const turnId = commits(ws)[0].turn_id
  relay(ws, { final: BATCH, turn_id: turnId })
  await settle()
  assert.equal(sttPosts().length, 0)
  assert.deepEqual(sent, [{ text: BATCH, turnId }])
})

test('each backup-path call says why it was made', async () => {
  // Realtime off.
  let s = liveSession({ realtime: false })
  await speak(s.ctrl)
  await settle()
  // The socket closed and not yet reopened when the turn ends.
  s = liveSession()
  dropSocket()
  await speak(s.ctrl)
  await settle()
  // Realtime never answers in time.
  s = liveSession()
  await speak(s.ctrl)
  mock.timers.tick(sttCommitTimeoutMs(2000) + 1)
  await settle()
  // Realtime fails with the turn waiting on its words.
  s = liveSession()
  await speak(s.ctrl)
  relay(FakeWebSocket.last, { error: 'transcriber unavailable' })
  await settle()
  // No audio reached the socket.
  s = liveSession()
  await speak(s.ctrl, { frames: 0 })
  await settle()
  assert.deepEqual(sttPosts().map((p) => p.why),
                   ['batch', 'reconnecting', 'late', 'rescue', 'no_audio'])
})

test('a held turn keeps its reason when it is sent again', async () => {
  const { ctrl, sent } = liveSession({ realtime: false })
  let offline = true
  const realFetch = globalThis.fetch
  globalThis.fetch = async (url, opts) => {
    if (offline && /\/stt$/.test(url)) throw new TypeError('network down')
    return realFetch(url, opts)
  }
  await speak(ctrl)
  await settle()
  assert.equal(ctrl.heldUtterances.length, 1)
  offline = false
  mock.timers.tick(3000)
  await settle()
  assert.deepEqual(sttPosts().map((p) => p.why), ['batch'])
  assert.equal(sent.length, 1)
})
