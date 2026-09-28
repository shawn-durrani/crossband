// The line a voice seat opens with while the round's search of the saved
// chats finishes (membro#136), end to end through the real voice
// controller, fed the round's SSE events and speaking through a fake
// /api/voice/tts socket that records every frame.
// The round sends the line as the seat's first delta, flagged. The pins:
// the line reaches TTS with a flush straight away, before any of the
// model's words, so it plays while the reply waits; the model's reply
// follows on the same speech and ends with the usual flush; the pass gate
// still keeps [pass] out of TTS after the line; and a reply without the
// line reaches TTS exactly as before, with no flush until it ends.
// Run: node --test frontend/src/voiceSpokenLine.test.js
import assert from 'node:assert/strict'
import { readFileSync } from 'node:fs'
import { afterEach, beforeEach, mock, test } from 'node:test'
import VoiceController from './voice.js'
import { clear } from './voiceDebug.js'

const START = 1_800_000_000_000
const SILENT = 'data:audio/wav'
const SLUG = 'claude'

// The round's word for the flag and the lines (backend/history_prefetch.py),
// pinned through the contract fixture.
const fixture = JSON.parse(readFileSync(
  new URL('../../tests/fixtures/backend_contract.json', import.meta.url), 'utf8'))
const FLAG = fixture.history_line.speak_now_flag
const LINE = `${fixture.history_line.lines[0]} `

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


const chunked = (text, size = 7) => text.match(new RegExp(`[\\s\\S]{1,${size}}`, 'g'))

const REPLY = 'You told me about the gift card for your sister last spring. '
  + 'It was a hundred dollars, and the scarf was about forty.'

async function round(deltas, { line = false, end = 'saved' } = {}) {
  ctrl.onEvent({ type: 'user_saved', message: { id: 1 } })
  ctrl.onEvent({ type: 'speaker_start', speaker: SLUG })
  const beforeReply = []
  if (line) {
    ctrl.onEvent({ type: 'delta', speaker: SLUG, text: LINE, [FLAG]: true })
    await settle(1)
    // The socket's opening frame names the voice; what follows is speech.
    beforeReply.push(...tts().flatMap((w) => w.sent).filter((m) => !m.voice_id))
  }
  for (const text of deltas) {
    ctrl.onEvent({ type: 'delta', speaker: SLUG, text })
    await settle(1)
  }
  const said = (line ? LINE : '') + deltas.join('')
  if (end === 'passed') ctrl.onEvent({ type: 'passed', speaker: SLUG })
  else ctrl.onEvent({ type: 'speaker_end', speaker: SLUG, message: { speaker: SLUG, content: said } })
  ctrl.onEvent({ type: 'done' })
  ctrl.onRoundDone()
  await settle()
  return beforeReply
}

test('the line reaches TTS with a flush before the reply starts', async () => {
  const beforeReply = await round(chunked(REPLY), { line: true })
  assert.deepEqual(beforeReply, [{ text: LINE }, { flush: true }],
                   'the line and a flush, and nothing held back')
  const [ws] = tts()
  assert.equal(tts().length, 1, 'one speech for the line and the reply')
  assert.equal(ws.sent[0].voice_id, 'v-claude')
  assert.equal(ws.text(), LINE + REPLY)
  assert.deepEqual(ws.sent.at(-1), { flush: true, done: true })
  assert.equal(audible().length, 1, 'the line and the reply played as one')
  assert.ok(speakers.includes(SLUG))
})

test('the pass gate still keeps [pass] out of TTS after the line', async () => {
  await round(['[', 'pa', 'ss', ']'], { line: true, end: 'passed' })
  const [ws] = tts()
  assert.equal(ws.text(), LINE, 'only the line was sent')
  assert.doesNotMatch(ws.text(), /\[/)
  assert.equal(ws.closed, true, 'the speech is closed when the seat passes')
  assert.equal(ctrl.playing, 0)
})

test('a reply without the line reaches TTS as it always has', async () => {
  await round(chunked(REPLY))
  const [ws] = tts()
  assert.equal(ws.text(), REPLY)
  const flushes = ws.sent.filter((m) => m.flush)
  assert.deepEqual(flushes, [{ flush: true, done: true }], 'no flush until it ends')
  assert.equal(audible().length, 1)
})

test('a flagged line after a barge-in is never spoken', async () => {
  ctrl.onEvent({ type: 'user_saved', message: { id: 1 } })
  ctrl.onEvent({ type: 'speaker_start', speaker: SLUG })
  ctrl.interrupt()
  ctrl.onEvent({ type: 'delta', speaker: SLUG, text: LINE, [FLAG]: true })
  await settle()
  assert.equal(tts().length, 0, 'no speech opened')
  assert.deepEqual(audible(), [])
})
