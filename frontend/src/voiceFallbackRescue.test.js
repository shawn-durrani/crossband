// #304: a turn in flight when realtime transcription fails, end to end
// through the real voice controller. A spoken turn is committed on the
// realtime socket, the relay reports an error before the transcript comes
// back, and the test watches what reaches /send and the network. The bug
// this pins: the switch to batch transcription reset the commit ledger and
// cleared the salvage timer, so that turn was dropped unsent, the screen
// went back to Listening, and someone had to speak again.
// Pins: the batch path transcribes the turn and it's sent once; a late
// realtime final can't send it twice, whichever lands first; a failure with
// nothing in flight and a normal turn behave as before; an empty last piece
// of a long turn still sends the parts already buffered; and the rescue's
// diagnostics hold ids, never words.
// Run: node --test frontend/src/voiceFallbackRescue.test.js
import assert from 'node:assert/strict'
import { afterEach, beforeEach, test } from 'node:test'
import VoiceController from './voice.js'
import { HANDOFF_STALL_MS } from './handoffWatch.js'
import { clear, snapshot } from './voiceDebug.js'

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
  close() { this.readyState = 3 }
}
globalThis.WebSocket = FakeWebSocket
globalThis.Audio = class { setAttribute() {} }
globalThis.location = { protocol: 'http:', host: 'localhost:8902' }

// The batch recorder that runs beside the realtime stream the whole time.
function fakeRecorder() {
  return {
    state: 'recording',
    mimeType: 'audio/webm',
    stop() { this.state = 'inactive'; queueMicrotask(() => this.onstop?.()) },
  }
}

// What realtime heard, and what the batch transcriber hears from the
// recorder's copy. Words no diagnostic has any reason to contain.
const SAID = 'Sam left the ladder by the shed at Fairhaven'
const BATCH = 'Sam left the ladder by the shed'
const WORDS = ['ladder', 'shed', 'Fairhaven']

let posts
let sttReplies   // queued /stt answers, oldest first
let sttGate      // when set, /stt waits on it (to race a late final)
let controllers
const realDebug = console.debug
const realWarn = console.warn

beforeEach(() => {
  console.debug = () => {}
  console.warn = () => {}
  clear()
  posts = []
  sttReplies = []
  sttGate = null
  controllers = []
  globalThis.fetch = async (url, opts) => {
    posts.push({ url, body: opts && typeof opts.body === 'string' ? opts.body : '' })
    if (/\/api\/chats\/\d+\/stt$/.test(url)) {
      if (sttGate) await sttGate
      const r = sttReplies.shift() || { status: 200, body: { text: BATCH } }
      return { ok: r.status < 400, status: r.status, json: async () => r.body }
    }
    return { ok: true, status: 200, json: async () => ({ ok: true }) }
  }
})

afterEach(() => {
  for (const c of controllers) c.stop()
  console.debug = realDebug
  console.warn = realWarn
  delete globalThis.fetch
})

// A live session with an open realtime transcription socket.
function liveSession({ realtime = true } = {}) {
  const sent = []
  const errors = []
  const fallbacks = []
  const ctrl = new VoiceController({
    getChatId: () => 7,
    getParticipants: () => [],
    sendText: (text, turnId) => sent.push({ text, turnId }),
    onState: () => {},
    onError: (m) => errors.push(m),
    onPartial: () => {},
    onSttFallback: (cause) => fallbacks.push(cause),
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
  let ws = null
  if (realtime) {
    ctrl._openSttStream()
    ws = FakeWebSocket.last
    ws.onopen()
  } else {
    ctrl.sttRealtime = false
  }
  controllers.push(ctrl)
  return { ctrl, ws, sent, errors, fallbacks }
}

// One spoken turn: `speechMs` of speech, then the pause that ends it (a
// 'gap'), or a cap that ends only the segment. Returns the commit's turn id
// on the realtime path.
async function speak(ctrl, ws, { cause = 'gap', speechMs = 2000 } = {}) {
  const now = Date.now()
  ctrl.speechStart = now - speechMs - 2100
  ctrl.lastVoice = now - 2100
  ctrl._utterFrames = 40
  await ctrl._finalizeUtterance(cause)
  if (!ws) return null
  const commits = ws.sent.map((s) => JSON.parse(s)).filter((m) => m.commit)
  return commits[commits.length - 1].turn_id
}

function relay(ws, msg) {
  ws.onmessage({ data: JSON.stringify(msg) })
}

async function settle() {
  for (let i = 0; i < 6; i++) await new Promise((r) => setTimeout(r, 0))
}

const sttPosts = () => posts.filter((p) => /\/api\/chats\/\d+\/stt$/.test(p.url))
const beacons = () => posts.filter((p) => p.url === '/api/voice/stall')

test('a relay error with a turn in flight: batch transcribes it and it is sent once', async () => {
  const { ctrl, ws, sent, errors, fallbacks } = liveSession()
  const turnId = await speak(ctrl, ws)
  assert.equal(ctrl.state, 'transcribing')
  relay(ws, { partial: SAID })
  relay(ws, { error: 'transcriber unavailable' })
  // The red error still shows, and the screen stays on Thinking while the
  // batch path works, instead of dropping back to Listening.
  assert.deepEqual(errors, ['Realtime STT: transcriber unavailable'])
  assert.equal(fallbacks.length, 1)
  assert.equal(ctrl.sttRealtime, false)
  assert.equal(ctrl.state, 'transcribing')
  await settle()
  assert.equal(sttPosts().length, 1, 'the recorder copy went to the batch transcriber')
  assert.deepEqual(sent, [{ text: BATCH, turnId }], 'sent once, under its own turn id')
  assert.equal(ctrl.state, 'listening')
  // The server saves it under that id, so the hand-off is confirmed and
  // no stall is reported.
  ctrl.onEvent({ type: 'user_saved', message: { id: 51, voice_turn_id: turnId } })
  ctrl._checkHandoffs(Date.now() + HANDOFF_STALL_MS * 2)
  assert.equal(beacons().length, 0)
})

test('a websocket error with a turn in flight is rescued the same way', async () => {
  const { ctrl, ws, sent } = liveSession()
  const turnId = await speak(ctrl, ws)
  ws.onerror()
  await settle()
  assert.equal(sttPosts().length, 1)
  assert.deepEqual(sent, [{ text: BATCH, turnId }])
})

test('the realtime final arriving after the batch one sends nothing more', async () => {
  const { ctrl, ws, sent } = liveSession()
  const turnId = await speak(ctrl, ws)
  relay(ws, { error: 'transcriber unavailable' })
  await settle()
  assert.equal(sent.length, 1)
  relay(ws, { final: SAID, turn_id: turnId })
  relay(ws, { final: SAID })  // an older relay's final, with no id
  await settle()
  assert.deepEqual(sent, [{ text: BATCH, turnId }])
  assert.equal(sttPosts().length, 1)
  assert.equal(ctrl.state, 'listening')
})

test('a late realtime final racing the batch call changes nothing', async () => {
  const { ctrl, ws, sent } = liveSession()
  const turnId = await speak(ctrl, ws)
  let open
  sttGate = new Promise((r) => { open = r })
  relay(ws, { error: 'transcriber unavailable' })
  await settle()
  // The batch call is still out when the realtime final turns up.
  relay(ws, { final: SAID, turn_id: turnId })
  assert.deepEqual(sent, [])
  assert.equal(ctrl.state, 'transcribing', 'still Thinking: the batch call owns the turn')
  open()
  await settle()
  assert.deepEqual(sent, [{ text: BATCH, turnId }])
})

test('a realtime error with no turn in flight behaves as before', async () => {
  const { ctrl, ws, sent, errors, fallbacks } = liveSession()
  // One normal turn first, transcribed and sent by realtime.
  const turnId = await speak(ctrl, ws)
  relay(ws, { final: SAID, turn_id: turnId })
  assert.deepEqual(sent, [{ text: SAID, turnId }])
  relay(ws, { error: 'socket closed by peer' })
  await settle()
  assert.deepEqual(errors, ['Realtime STT: socket closed by peer'])
  assert.deepEqual(fallbacks, ['relay error: socket closed by peer'])
  assert.equal(ctrl.sttRealtime, false)
  assert.equal(sttPosts().length, 0, 'nothing to rescue, so no batch call')
  assert.deepEqual(sent, [{ text: SAID, turnId }], 'the finished turn is not sent again')
  assert.equal(ctrl.state, 'listening')
  const tags = snapshot().map((e) => e.tag)
  assert.ok(tags.includes('stt:batchFallback'))
  assert.ok(!tags.includes('stt:rescue'))
  // The next turn goes through the batch path, as the fallback always did.
  await speak(ctrl, null)
  await settle()
  assert.equal(sttPosts().length, 1)
  assert.equal(sent.length, 2)
  assert.equal(sent[1].text, BATCH)
})

test('a normal turn is unchanged: realtime sends it and batch never runs', async () => {
  const { ctrl, ws, sent, fallbacks } = liveSession()
  const turnId = await speak(ctrl, ws)
  relay(ws, { partial: SAID })
  relay(ws, { final: SAID, turn_id: turnId })
  await settle()
  assert.deepEqual(sent, [{ text: SAID, turnId }])
  assert.equal(sttPosts().length, 0)
  assert.equal(fallbacks.length, 0)
  assert.equal(ctrl.sttRealtime, true)
  assert.equal(ctrl.state, 'listening')
})

test('a capped segment in flight while still talking waits for the turn to end', async () => {
  // Mid-sentence failure: the recorder is still capturing this same turn,
  // so the batch path transcribes it once, when the turn ends.
  const { ctrl, ws, sent } = liveSession()
  await speak(ctrl, ws, { cause: 'cap', speechMs: 12000 })
  ctrl.speechStart = Date.now() - 1000  // still talking
  relay(ws, { error: 'transcriber unavailable' })
  await settle()
  assert.equal(sttPosts().length, 0)
  assert.deepEqual(sent, [])
  assert.equal(ctrl.state, 'listening')
  await speak(ctrl, null)  // the pause that ends the turn
  await settle()
  assert.equal(sttPosts().length, 1)
  assert.deepEqual(sent.map((s) => s.text), [BATCH])
})

test('an empty last piece still sends the parts already buffered', async () => {
  const { ctrl, ws, sent } = liveSession()
  const seg = await speak(ctrl, ws, { cause: 'cap', speechMs: 12000 })
  relay(ws, { final: 'Alex asked about the ladder', turn_id: seg })
  assert.deepEqual(sent, [], 'a capped segment buffers')
  const end = await speak(ctrl, ws, { speechMs: 600 })
  relay(ws, { final: '', turn_id: end })
  // Before #304's fix the buffered part waited for someone to speak again.
  assert.deepEqual(sent, [{ text: 'Alex asked about the ladder', turnId: end }])
})

test('an empty or refused last piece on the batch path sends what is buffered', async () => {
  for (const last of [{ status: 200, body: { text: '' } },
                      { status: 502, body: { detail: 'invalid audio' } }]) {
    const { ctrl, sent, errors } = liveSession({ realtime: false })
    sttReplies = [{ status: 200, body: { text: 'Dave fixed the gate' } }, last]
    await speak(ctrl, null, { cause: 'cap', speechMs: 12000 })
    assert.deepEqual(sent, [])
    await speak(ctrl, null, { speechMs: 900 })
    assert.deepEqual(sent.map((s) => s.text), ['Dave fixed the gate'])
    assert.equal(errors.length, last.status === 502 ? 1 : 0)
  }
})

test('the rescue is recorded by turn id, never by what was said', async () => {
  const { ctrl, ws } = liveSession()
  const turnId = await speak(ctrl, ws)
  relay(ws, { partial: SAID })
  relay(ws, { error: 'transcriber unavailable' })
  await settle()
  const ring = snapshot()
  const rescue = ring.find((e) => e.tag === 'stt:rescue')
  assert.ok(rescue, 'the ring shows the rescue')
  assert.ok(rescue.data.includes(turnId))
  assert.ok(rescue.data.includes('"dispatch":"send"'))
  const fallback = ring.find((e) => e.tag === 'stt:batchFallback')
  assert.ok(fallback.data.includes('"inFlight":1'))
  const all = JSON.stringify(ring)
  for (const word of [SAID, BATCH, ...WORDS]) {
    assert.ok(!all.includes(word), `"${word}" reached the diagnostics ring`)
  }
})
