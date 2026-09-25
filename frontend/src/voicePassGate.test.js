// The voice's pass gate (#460), end to end through the real voice
// controller: started with its own start(), fed the round's SSE events
// through onEvent, and speaking through a fake /api/voice/tts socket that
// records every frame and answers a finished reply with audio.
// A seat that has nothing to add replies [pass], and the app hides it. On
// 25 September seats also wrote a remark first ("nothing to add [pass]"),
// and the voice spoke it, token and all. The gate holds a reply's text
// while it could still end as a pass, and never sends the token to TTS.
// Pins: a normal reply reaches TTS no later than it did without the gate;
// a bare [pass] opens no speech; a quiet remark before [pass] sends TTS
// nothing and, if its speech had opened, closes it unflushed and unplayed;
// a real sentence before [pass] is spoken without the token; and a
// barge-in partway through the token leaves nothing queued to speak.
// Run: node --test frontend/src/voicePassGate.test.js
import assert from 'node:assert/strict'
import { readFileSync } from 'node:fs'
import { afterEach, beforeEach, mock, test } from 'node:test'
import VoiceController from './voice.js'
import { QUIET_MAX_CHARS } from './passView.js'
import { clear } from './voiceDebug.js'

const START = 1_800_000_000_000
const SILENT = 'data:audio/wav'
const SLUG = 'claude'

// The ElevenLabs first chunk (backend/voice.py tts_init_message, pinned
// through the contract fixture): TTS makes no audio until it holds this
// many characters or is flushed, so it is when "today" could first speak.
const fixture = JSON.parse(readFileSync(
  new URL('../../tests/fixtures/backend_contract.json', import.meta.url), 'utf8'))
const FIRST_CHUNK = fixture.pass.tts_first_chunk_chars

// ---- the browser, as far as voice.js reaches into it ----

globalThis.requestAnimationFrame = () => {}

// /api/voice/stt is the microphone's socket and does nothing here.
// /api/voice/tts is a seat's speech: it opens on the next microtask,
// records what it is sent, and answers a finished reply (done) with one
// audio chunk and the final marker, the way the relay does.
class FakeWebSocket {
  static OPEN = 1
  static all = []
  constructor(url) {
    this.url = url
    this.readyState = FakeWebSocket.OPEN
    this.sent = []
    this.closed = false
    FakeWebSocket.all.push(this)
    if (this.isTts) queueMicrotask(() => this.onopen?.())
  }
  get isTts() { return this.url.endsWith('/api/voice/tts') }
  send(s) {
    const m = JSON.parse(s)
    if (m.audio) return
    this.sent.push(m)
    if (this.isTts && m.done) {
      queueMicrotask(() => this.onmessage?.({ data: JSON.stringify({ audio: 'SUQz', final: true }) }))
    }
  }
  close() { this.readyState = 3; this.closed = true }
  // What TTS was asked to say, and whether it was told to speak it.
  text() { return this.sent.filter((m) => typeof m.text === 'string').map((m) => m.text).join('') }
  flushed() { return this.sent.some((m) => m.flush) }
}
globalThis.WebSocket = FakeWebSocket

// The shared audio element. play() with a source other than the silent
// unlock clip is audible playback of a reply.
let plays
globalThis.Audio = class {
  setAttribute() {}
  pause() {}
  play() { plays.push(this.src); return Promise.resolve() }
}
globalThis.location = { protocol: 'http:', host: 'localhost:8902' }
globalThis.window = { addEventListener() {}, removeEventListener() {} }
globalThis.document = { hidden: false, addEventListener() {}, removeEventListener() {} }
Object.defineProperty(globalThis, 'navigator', {
  configurable: true,
  value: {
    userAgent: 'node',
    mediaDevices: {
      async getUserMedia() {
        const track = { enabled: true, stop() {} }
        return { getTracks: () => [track], getAudioTracks: () => [track] }
      },
    },
  },
})
globalThis.AudioContext = class {
  constructor() { this.state = 'running'; this.sampleRate = 48000; this.destination = {} }
  createMediaStreamSource() { return { connect() {} } }
  createAnalyser() {
    return { fftSize: 2048, frequencyBinCount: 1024,
             getFloatTimeDomainData(b) { b.fill(0) }, getByteFrequencyData(b) { b.fill(0) } }
  }
  createScriptProcessor() { return { connect() {}, disconnect() {} } }
  resume() { return Promise.resolve() }
  close() { this.state = 'closed'; return Promise.resolve() }
}
globalThis.MediaRecorder = class {
  static isTypeSupported() { return true }
  constructor() { this.state = 'inactive'; this.mimeType = 'audio/webm' }
  start() { this.state = 'recording' }
  stop() { this.state = 'inactive' }
}

let ctrl
let speakers
const realDebug = console.debug
const realWarn = console.warn

beforeEach(async () => {
  console.debug = () => {}
  console.warn = () => {}
  mock.timers.enable({ apis: ['setTimeout', 'setInterval', 'Date'], now: START })
  clear()
  FakeWebSocket.all = []
  plays = []
  speakers = []
  globalThis.fetch = async () => ({ ok: true, status: 200, json: async () => ({ ok: true }) })
  ctrl = new VoiceController({
    getChatId: () => 7,
    getParticipants: () => [{ slug: SLUG, name: 'Claude', voice_id: 'v-claude' }],
    sendText: () => {},
    onState: () => {},
    onError: () => {},
    onPartial: () => {},
    onSpeaker: (slug) => speakers.push(slug),
  })
  await ctrl.start()
  plays = []  // the start gesture's silent unlock clip is not a reply
})

afterEach(() => {
  ctrl.stop()
  mock.timers.reset()
  console.debug = realDebug
  console.warn = realWarn
  delete globalThis.fetch
})

// Let sockets open, audio arrive and the play chain move: microtasks,
// then the player's 100 ms waits on the fake clock.
async function settle(rounds = 6) {
  for (let i = 0; i < rounds; i++) {
    for (let j = 0; j < 4; j++) await new Promise((r) => setImmediate(r))
    mock.timers.tick(100)
  }
}

const tts = () => FakeWebSocket.all.filter((w) => w.isTts)
const ttsText = () => tts().map((w) => w.text()).join('')
const audible = () => plays.filter((src) => src && !String(src).startsWith(SILENT))

// One seat's reply, as the round stream delivers it.
async function reply(deltas, end) {
  ctrl.onEvent({ type: 'user_saved', message: { id: 1 } })
  ctrl.onEvent({ type: 'speaker_start', speaker: SLUG })
  for (const text of deltas) {
    ctrl.onEvent({ type: 'delta', speaker: SLUG, text })
    await settle(1)
  }
  if (end === 'passed') ctrl.onEvent({ type: 'passed', speaker: SLUG })
  if (end === 'saved') {
    ctrl.onEvent({ type: 'speaker_end', speaker: SLUG,
                   message: { speaker: SLUG, content: deltas.join('') } })
  }
  if (end) {
    ctrl.onEvent({ type: 'done' })
    ctrl.onRoundDone()
  }
  await settle()
}

// A reply broken into small deltas, the way a model streams.
const chunked = (text, size = 7) => text.match(new RegExp(`[\\s\\S]{1,${size}}`, 'g'))

const LONG = 'Sand the bench top with 120 grit first, then 180, and wipe the dust '
  + 'off with a damp cloth before the first coat of hard wax oil goes on. '
  + 'Give it a day between coats.'

// ---------- (1) a normal reply reaches TTS no later than today ----------

// The same reply through the same controller with the pass gate taken
// out: today's path, which hands TTS every delta as it arrives.
function withoutTheGate() {
  const set = ctrl._passGates.set.bind(ctrl._passGates)
  ctrl._passGates.set = (slug, gate) => set(slug, null)
}

// The delta at which speech opened, and the one at which TTS first held
// a first chunk of text.
async function whenSpoken(deltas) {
  ctrl.onEvent({ type: 'user_saved', message: { id: 1 } })
  ctrl.onEvent({ type: 'speaker_start', speaker: SLUG })
  let opened = -1
  for (let i = 0; i < deltas.length; i++) {
    ctrl.onEvent({ type: 'delta', speaker: SLUG, text: deltas[i] })
    await settle(1)
    if (opened < 0 && tts().length) opened = i
    if (ttsText().length >= FIRST_CHUNK) return { opened, firstChunk: i }
  }
  return { opened, firstChunk: -1 }
}

test('a normal reply reaches TTS as soon as TTS could first speak it', async () => {
  const deltas = chunked(LONG)
  assert.ok(LONG.length > FIRST_CHUNK + 20)
  // The first delta at which the reply itself holds a first chunk: no
  // path can hand TTS that much text any sooner.
  let soonest = 0
  for (let n = 0; soonest < deltas.length; soonest++) {
    n += deltas[soonest].length
    if (n >= FIRST_CHUNK) break
  }
  const gated = await whenSpoken(deltas)
  assert.equal(gated.opened, 0, 'speech opened on the first delta')
  assert.equal(tts()[0].sent[0].voice_id, 'v-claude')
  assert.equal(gated.firstChunk, soonest,
               'TTS held a first chunk at the same delta as the reply did')
  ctrl.stop()
  // Today, for comparison, on a fresh controller with the gate taken out.
  FakeWebSocket.all = []
  ctrl = new VoiceController({ getChatId: () => 7,
    getParticipants: () => [{ slug: SLUG, name: 'Claude', voice_id: 'v-claude' }],
    sendText: () => {}, onState: () => {}, onError: () => {} })
  await ctrl.start()
  withoutTheGate()
  assert.deepEqual(await whenSpoken(deltas), gated, 'no later than without the gate')
})

test('a long reply streams on as it arrives once it is past a quiet remark', async () => {
  const deltas = chunked(LONG)
  ctrl.onEvent({ type: 'user_saved', message: { id: 1 } })
  ctrl.onEvent({ type: 'speaker_start', speaker: SLUG })
  let seen = ''
  for (const text of deltas) {
    ctrl.onEvent({ type: 'delta', speaker: SLUG, text })
    await settle(1)
    seen += text
    if (seen.trim().length > QUIET_MAX_CHARS) {
      assert.equal(ttsText(), seen, 'everything so far is with TTS')
    }
  }
})

test('a short reply reaches TTS with its flush, when TTS could first speak it', async () => {
  // Under a first chunk, TTS makes no audio until the flush at the end of
  // the reply, with or without the gate.
  await reply(chunked('Sand it first, then oil it.'), 'saved')
  const [ws] = tts()
  assert.equal(ws.text(), 'Sand it first, then oil it.')
  assert.deepEqual(ws.sent.at(-1), { flush: true, done: true })
  assert.equal(audible().length, 1, 'the reply played')
  assert.ok(speakers.includes(SLUG))
})

// ---------- (2) a bare [pass] ----------

test('a reply of exactly [pass] sends TTS nothing and plays nothing', async () => {
  await reply(['[', 'pa', 'ss', ']'], 'passed')
  assert.equal(tts().length, 0, 'no speech socket opened')
  assert.equal(ttsText(), '')
  assert.deepEqual(audible(), [])
  assert.ok(!speakers.includes(SLUG))
  assert.equal(ctrl.playing, 0)
})

// ---------- (3) a quiet remark before [pass] ----------

test('a quiet remark before [pass] sends TTS nothing and opens no speech', async () => {
  await reply(chunked('Nothing to add from me.  [pass]', 4), 'passed')
  assert.equal(tts().length, 0, 'no speech socket opened')
  assert.deepEqual(audible(), [])
  assert.equal(ctrl.playing, 0)
})

test('a quiet remark whose speech had opened is closed unflushed and unplayed', async () => {
  // "timber" is no quiet word, so speech opens; the gate still holds it.
  await reply(chunked('You two carry on with the timber order, passing.  [pass]', 5), 'passed')
  const [ws] = tts()
  assert.ok(ws, 'speech opened for this remark')
  assert.equal(ws.text(), '', 'TTS was sent no words')
  assert.equal(ws.flushed(), false, 'and never told to speak')
  assert.equal(ws.closed, true, 'the socket was closed')
  assert.deepEqual(audible(), [], 'no audio played')
  // The orb tints to a seat while its speech is open, as for any reply
  // waiting on TTS, and clears when the pass closes it.
  assert.equal(speakers.at(-1), null, 'nobody is shown speaking')
  assert.equal(ctrl.playing, 0)
  assert.equal(ctrl.sockets.size, 0)
})

// ---------- (4) a real sentence before [pass] ----------

test('a real sentence before [pass] is spoken without the token', async () => {
  await reply(['Sand it first,', ' then oil it.', '  [pa', 'ss]'], 'saved')
  const [ws] = tts()
  assert.equal(ws.text().trim(), 'Sand it first, then oil it.')
  assert.doesNotMatch(ws.text(), /\[/)
  assert.deepEqual(ws.sent.at(-1), { flush: true, done: true })
  assert.equal(audible().length, 1, 'the sentence played')
})

test('a long reply that ends in [pass] never sends the token', async () => {
  await reply([...chunked(LONG), ' [', 'PA', 'SS', '].'], 'saved')
  const [ws] = tts()
  // The full stop after the token is spoken as nothing.
  const words = (t) => t.replace(/[\s.]+$/, '')
  assert.equal(words(ws.text()), words(LONG))
  assert.doesNotMatch(ws.text(), /\[|pass/i)
  assert.equal(audible().length, 1)
})

// ---------- (5) a barge-in partway through the token ----------

async function bargeInAfter(deltas) {
  ctrl.onEvent({ type: 'user_saved', message: { id: 1 } })
  ctrl.onEvent({ type: 'speaker_start', speaker: SLUG })
  for (const text of deltas) {
    ctrl.onEvent({ type: 'delta', speaker: SLUG, text })
    await settle(1)
  }
  // The owner talks over it: the round is cut off and its stream ends
  // with no passed and no speaker_end.
  ctrl.interrupt()
  ctrl.onRoundDone()
  await settle()
}

const nothingQueued = () => {
  assert.equal(ctrl._pendingSpeaker, null, 'no reply waiting to start')
  assert.equal(ctrl.sockets.size, 0, 'no speech socket left open')
  assert.equal(ctrl.playing, 0, 'nothing in the play chain')
  assert.deepEqual(audible(), [], 'nothing played')
}

test('a barge-in partway through a bare [pass] leaves nothing queued', async () => {
  await bargeInAfter(['[', 'pa'])
  assert.equal(tts().length, 0)
  nothingQueued()
})

test('a barge-in partway through a quiet remark\'s [pass] leaves nothing queued', async () => {
  await bargeInAfter(chunked('Nothing to add. [pa', 4))
  assert.equal(ttsText(), '')
  nothingQueued()
})

test('a barge-in after a quiet remark opened its speech leaves nothing queued', async () => {
  await bargeInAfter(chunked('You two carry on with the timber order, passing.  [pa', 5))
  const [ws] = tts()
  assert.ok(ws, 'speech had opened')
  assert.equal(ws.text(), '')
  assert.equal(ws.flushed(), false)
  assert.equal(ws.closed, true)
  nothingQueued()
})

test('a barge-in partway through a long reply\'s [pass] never sends the token', async () => {
  await bargeInAfter([...chunked(LONG), ' [', 'pa'])
  const [ws] = tts()
  assert.equal(ws.text().trimEnd(), LONG)
  assert.doesNotMatch(ws.text(), /\[/)
  assert.equal(ws.flushed(), false, 'the cut-off reply is never told to speak')
})
