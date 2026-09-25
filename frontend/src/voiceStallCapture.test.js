// #304: the automatic diagnostics save, end to end through the real voice
// controller. A spoken turn runs through voice.js's own commit, final and
// send paths with fake browser parts, and the test watches what reaches the
// network. Pins: a turn the server never saves triggers exactly one save;
// a normal turn triggers none; repeated stalls are rate-limited; and
// nothing that was said reaches the saved bundle or the stall beacon.
// Run: node --test frontend/src/voiceStallCapture.test.js
import assert from 'node:assert/strict'
import { afterEach, beforeEach, test } from 'node:test'
import VoiceController from './voice.js'
import { HANDOFF_STALL_MS } from './handoffWatch.js'
import { autoDump, clear, resetAutoSavesForTest } from './voiceDebug.js'

// The browser parts voice.js reaches on these paths, and no more.
class FakeWebSocket {
  static OPEN = 1
  static last = null
  constructor(url) {
    this.url = url
    this.readyState = FakeWebSocket.OPEN
    this.sent = []
    FakeWebSocket.last = this
  }
  send(s) { this.sent.push(s) }
  close() {}
}
globalThis.WebSocket = FakeWebSocket
globalThis.Audio = class { setAttribute() {} }
globalThis.location = { protocol: 'http:', host: 'localhost:8902' }

// What was said, in words no diagnostic has any reason to contain.
const SAID = 'Mateo hid the spare key under the blue flowerpot'
const SAID_WORDS = ['Mateo', 'spare', 'flowerpot']

let posts
let saves
let controllers
const realDebug = console.debug

beforeEach(() => {
  console.debug = () => {}
  clear()
  resetAutoSavesForTest()
  posts = []
  saves = []
  controllers = []
  globalThis.fetch = async (url, opts) => {
    const body = opts && typeof opts.body === 'string' ? opts.body : ''
    posts.push({ url, body })
    const answer = url === '/api/voice/debug-dump'
      ? { ok: true, file: `f${posts.length}.json`, where: 'data/voice_debug/f.json' }
      : { ok: true }
    return { ok: true, json: async () => answer }
  }
})

afterEach(() => {
  for (const c of controllers) c.stop()
  console.debug = realDebug
  delete globalThis.fetch
})

// A live session with an open realtime transcription socket. onStall does
// what App.jsx does with it: ask for an automatic save.
function liveSession() {
  const sent = []
  const ctrl = new VoiceController({
    getChatId: () => 7,
    getParticipants: () => [],
    sendText: (text, turnId) => sent.push({ text, turnId }),
    onState: () => {},
    onError: () => {},
    onPartial: () => {},
    onStall: (kind) => { saves.push(autoDump(7, kind)) },
  })
  ctrl.active = true
  ctrl.audioCtx = {
    state: 'running', sampleRate: 48000, destination: {},
    createScriptProcessor: () => ({ connect() {}, disconnect() {} }),
    close: async () => {},
  }
  ctrl.micSource = { connect() {} }
  ctrl._openSttStream()
  const ws = FakeWebSocket.last
  ws.onopen()
  controllers.push(ctrl)
  return { ctrl, ws, sent }
}

// One spoken turn: about two seconds of speech, then the pause that ends
// it. Returns the turn id the commit carried.
async function speak(ctrl, ws) {
  const now = Date.now()
  ctrl.speechStart = now - 4100
  ctrl.lastVoice = now - 2100
  ctrl._utterFrames = 40
  await ctrl._finalizeUtterance('gap')
  const commits = ws.sent.map((s) => JSON.parse(s)).filter((m) => m.commit)
  return commits[commits.length - 1].turn_id
}

// The relay hears it: a live partial, then the final for that commit.
function transcribe(ws, turnId, text = SAID) {
  ws.onmessage({ data: JSON.stringify({ partial: text }) })
  ws.onmessage({ data: JSON.stringify({ final: text, turn_id: turnId }) })
}

const dumps = () => posts.filter((p) => p.url === '/api/voice/debug-dump')
const beacons = () => posts.filter((p) => p.url === '/api/voice/stall')

test('a turn the server never saves triggers exactly one save', async () => {
  const { ctrl, ws, sent } = liveSession()
  const turnId = await speak(ctrl, ws)
  transcribe(ws, turnId)
  assert.deepEqual(sent, [{ text: SAID, turnId }], 'the words went to /send')
  // No user_saved ever arrives for that turn. Just inside the bound it is
  // a slow turn; past it, a stall.
  ctrl._checkHandoffs(Date.now() + HANDOFF_STALL_MS - 1000)
  assert.equal(beacons().length, 0)
  ctrl._checkHandoffs(Date.now() + HANDOFF_STALL_MS + 1)
  ctrl._checkHandoffs(Date.now() + HANDOFF_STALL_MS + 5000) // same stall, asked again
  await Promise.all(saves)
  assert.equal(beacons().length, 1)
  assert.equal(JSON.parse(beacons()[0].body).kind, 'handoff_stalled')
  assert.equal(JSON.parse(beacons()[0].body).stage, 'sent')
  assert.equal(dumps().length, 1)
  const bundle = JSON.parse(dumps()[0].body)
  assert.equal(bundle.trigger, 'handoff_stalled')
  assert.equal(bundle.chat_id, 7)
  // The ring shows the hand-off starting and stalling, by turn id.
  const tags = bundle.entries.map((e) => e.tag)
  assert.ok(tags.includes('handoff:begin'))
  assert.ok(tags.includes('stall:handoff'))
  assert.ok(bundle.entries.some((e) => e.tag === 'stall:handoff'
                                       && e.data.includes(turnId)))
})

test('a normal turn, saved by the server, triggers nothing', async () => {
  const { ctrl, ws } = liveSession()
  const turnId = await speak(ctrl, ws)
  transcribe(ws, turnId)
  ctrl.onEvent({ type: 'user_saved',
                 message: { id: 41, speaker: 'user', content: SAID,
                            voice_turn_id: turnId } })
  ctrl._checkHandoffs(Date.now() + HANDOFF_STALL_MS * 20)
  await Promise.all(saves)
  assert.equal(beacons().length, 0)
  assert.equal(dumps().length, 0)
})

test('an empty transcript is a stall too, and says so', async () => {
  // The words never reach /send at all: the screen goes back to Listening
  // and the person has to speak again. The report names the stage.
  const { ctrl, ws, sent } = liveSession()
  const turnId = await speak(ctrl, ws)
  transcribe(ws, turnId, '')
  assert.equal(sent.length, 0)
  ctrl._checkHandoffs(Date.now() + HANDOFF_STALL_MS + 1)
  await Promise.all(saves)
  assert.equal(JSON.parse(beacons()[0].body).stage, 'empty')
  assert.equal(dumps().length, 1)
})

test('repeated stalls are rate-limited to one save', async () => {
  const { ctrl, ws } = liveSession()
  const first = await speak(ctrl, ws)
  transcribe(ws, first)
  ctrl._checkHandoffs(Date.now() + HANDOFF_STALL_MS + 1)
  await Promise.all(saves)
  const second = await speak(ctrl, ws)
  transcribe(ws, second)
  ctrl._checkHandoffs(Date.now() + HANDOFF_STALL_MS + 1)
  await Promise.all(saves)
  // Each stall still reaches the server log; only the first saves a file.
  assert.equal(beacons().length, 2)
  assert.equal(dumps().length, 1)
})

test('ending the session is not a stall', async () => {
  const { ctrl, ws } = liveSession()
  const turnId = await speak(ctrl, ws)
  transcribe(ws, turnId)
  ctrl.stop()
  ctrl._checkHandoffs(Date.now() + HANDOFF_STALL_MS * 3)
  await Promise.all(saves)
  assert.equal(dumps().length, 0)
})

test('nothing that was said reaches the bundle or the beacon', async () => {
  const { ctrl, ws } = liveSession()
  const turnId = await speak(ctrl, ws)
  transcribe(ws, turnId)
  // A typed message with the same words lands mid-stall and confirms
  // nothing (it carries no voice turn id), but it passes through onEvent.
  ctrl.onEvent({ type: 'user_saved',
                 message: { id: 42, speaker: 'user', content: SAID } })
  ctrl.onEvent({ type: 'speaker_start', speaker: 'claude' })
  ctrl.onEvent({ type: 'error', speaker: 'claude', message: 'upstream timeout' })
  ctrl._checkHandoffs(Date.now() + HANDOFF_STALL_MS + 1)
  await Promise.all(saves)
  assert.equal(dumps().length, 1)
  for (const p of [...dumps(), ...beacons()]) {
    for (const word of [SAID, ...SAID_WORDS]) {
      assert.ok(!p.body.includes(word), `"${word}" reached ${p.url}`)
    }
  }
})
