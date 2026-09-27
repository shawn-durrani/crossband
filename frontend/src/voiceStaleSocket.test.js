// #540: a live transcription socket the app has let go of has no say
// once a newer one has taken over, end to end through the real voice
// controller. Its late words were already ignored, but its close still
// cleared the current socket. Turning live transcription off and on again
// quickly let the old socket's close land after the new one opened: the
// new socket was orphaned while still open on the server, its audio was
// unhooked, and a third socket opened. Pins: the old socket's close
// leaves the new one in place and working, opens nothing, and a stale
// "ended from another window" close doesn't end the session.
// Run: node --test frontend/src/voiceStaleSocket.test.js
import assert from 'node:assert/strict'
import { afterEach, beforeEach, mock, test } from 'node:test'
import VoiceController from './voice.js'

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

const SAID = 'Mateo stacked the offcuts'
let controllers
let processors
const realDebug = console.debug
const realWarn = console.warn

beforeEach(() => {
  console.debug = () => {}
  console.warn = () => {}
  mock.timers.enable({ apis: ['setTimeout'] })
  FakeWebSocket.opened = []
  controllers = []
  processors = []
  globalThis.fetch = async () => ({ ok: true, status: 200, json: async () => ({ ok: true }) })
})

afterEach(() => {
  for (const c of controllers) c.stop()
  mock.timers.reset()
  console.debug = realDebug
  console.warn = realWarn
  delete globalThis.fetch
})

function liveSession() {
  const sent = []
  const errors = []
  const ctrl = new VoiceController({
    getChatId: () => 7,
    getParticipants: () => [],
    sendText: (text, turnId) => sent.push({ text, turnId }),
    onState: () => {},
    onError: (m) => errors.push(m),
    onPartial: () => {},
  })
  ctrl.active = true
  ctrl.state = 'listening'
  ctrl.recorder = { state: 'recording', mimeType: 'audio/webm', stop() { this.state = 'inactive' } }
  ctrl.recChunks = [new Blob(['opus frames'])]
  ctrl.audioCtx = {
    state: 'running', sampleRate: 48000, destination: {},
    createScriptProcessor: () => {
      const p = { live: true, connect() {}, disconnect() { this.live = false } }
      processors.push(p)
      return p
    },
    close: async () => {},
  }
  ctrl.micSource = { connect() {} }
  ctrl._openSttStream()
  last().onopen()
  controllers.push(ctrl)
  return { ctrl, sent, errors }
}

const last = () => FakeWebSocket.opened[FakeWebSocket.opened.length - 1]

async function settle() {
  for (let i = 0; i < 6; i++) await new Promise((r) => setImmediate(r))
}

// Live transcription off and on again before the old socket's close
// arrives. The browser reports a close only after the closing handshake,
// so the app's own "we closed it" flag has already reset by then.
function swapSockets(ctrl) {
  const old = ctrl.sttWs
  ctrl.setSttRealtime(false)
  ctrl.setSttRealtime(true)
  mock.timers.tick(0)
  const fresh = last()
  fresh.onopen()
  assert.notEqual(fresh, old)
  return { old, fresh }
}

// One turn streamed to the current socket and ended by the pause.
async function speak(ctrl) {
  const now = Date.now()
  ctrl.speechStart = now - 4100
  ctrl._sttStartStreaming()
  ctrl._utterFrames = 40
  ctrl.lastVoice = now - 2100
  const ws = ctrl.sttWs
  const before = ws.sent.length
  await ctrl._finalizeUtterance('gap')
  return ws.sent.slice(before).find((m) => m.commit)?.turn_id
}

test("an old socket's late close leaves the new one working", async () => {
  const { ctrl, sent } = liveSession()
  const { old, fresh } = swapSockets(ctrl)
  old.onclose({ code: 1000, reason: '' })
  assert.equal(ctrl.sttWs, fresh, 'the new socket is still the current one')
  assert.equal(processors[processors.length - 1].live, true, 'its audio is still hooked up')
  mock.timers.tick(5000)
  assert.equal(FakeWebSocket.opened.length, 2, 'no third socket')
  const turnId = await speak(ctrl)
  assert.ok(turnId, 'the turn committed on the new socket')
  fresh.onmessage({ data: JSON.stringify({ final: SAID, turn_id: turnId }) })
  await settle()
  assert.deepEqual(sent, [{ text: SAID, turnId }])
})

test("an old socket's 'ended from another window' close doesn't end the session", () => {
  const { ctrl, errors } = liveSession()
  const { old, fresh } = swapSockets(ctrl)
  old.onclose({ code: 4001, reason: 'capture killed' })
  assert.equal(ctrl.active, true)
  assert.equal(ctrl.sttWs, fresh)
  assert.deepEqual(errors, [])
})

test("the current socket's own drop still reopens", () => {
  const { ctrl } = liveSession()
  const ws = ctrl.sttWs
  ws.onclose({ code: 1006, reason: '' })
  assert.equal(ctrl.sttWs, null)
  mock.timers.tick(500)
  assert.equal(FakeWebSocket.opened.length, 2)
  assert.equal(ctrl.sttWs, last())
})
