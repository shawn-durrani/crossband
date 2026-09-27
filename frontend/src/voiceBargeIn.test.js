// Cutting a reply off, end to end through the real voice controller: its
// own start(), its listening loop on a scripted microphone and clock, the
// round's events through onEvent, and each seat's speech through a fake
// /api/voice/tts socket the test feeds audio into.
// On 27 September an iPhone talked over a reply and the reply kept going.
// The phone has no MediaSource, so its player holds a reply's audio until
// the speech stream ends, and a cut couldn't reach a player still holding.
// The reply counted as playing, the mic stayed shut, the barge-in fired on
// every frame with an abort each time, and the owner's words were never
// sent. Switching voice off closed the speech socket, and the held reply
// played about 38 seconds late.
// Pins: a barge-in cuts a held reply at once, cuts once however long the
// speech goes on, and its words are sent; audio still arriving for a cut
// reply never plays; nothing plays after voice is switched off; a reply
// queued behind the cut one never plays, even when the round ends before
// the queue drains; a new reply or round can be cut again; a round slow to
// end is cut once too; and with MediaSource a reply still streams as it
// arrives and a cut stops it the same way.
// An iPhone with iOS 17.1 or later streams through ManagedMediaSource,
// and the diagnostics name the path a session took. There a reply is
// heard, and its playback traced, before its speech
// stream ends; a barge-in cuts it once and pauses it; a reply queued
// behind it never plays; and after a cut or voice off, neither late audio
// nor the source asking for more puts anything in.
// Run: node --test frontend/src/voiceBargeIn.test.js
import assert from 'node:assert/strict'
import { afterEach, beforeEach, mock, test } from 'node:test'
import VoiceController from './voice.js'
import { clear, snapshot } from './voiceDebug.js'

const START = 1_800_000_000_000
const FRAME_MS = 20
const SILENT = 'data:audio/wav'
const REPLY = 'Sand the bench top with 120 grit first, then 180, and wipe it down.'
// A number means neither reply could still be a pass, so neither is held
// back by the pass gate (passView.js) and both reach their players.
const SECOND = 'Give it 2 coats of hard wax oil, a day apart.'
const SAID = 'Alex wants the oak for the top instead'
const SAID_AGAIN = 'Sam says the pine is fine'

// ---- the browser, as far as voice.js reaches into it ----

let nextFrame = null
globalThis.requestAnimationFrame = (cb) => { nextFrame = cb }

// The realtime transcription relay answers each commit with the next of
// these, `latencyMs` later.
const relay = {
  latencyMs: 300,
  texts: [],
  queue: [],
  commits: 0,
  heard(ws, turnId) {
    this.queue.push({ ws, turnId, due: Date.now() + this.latencyMs,
                      text: this.texts[this.commits++] || '' })
  },
  deliverDue() {
    const due = this.queue.filter((q) => q.due <= Date.now())
    this.queue = this.queue.filter((q) => q.due > Date.now())
    for (const q of due) q.ws.onmessage?.({ data: JSON.stringify({ final: q.text, turn_id: q.turnId }) })
  },
}

// /api/voice/stt-stream is the microphone's socket; /api/voice/tts is a
// seat's speech. A speech socket opens on the next microtask and records
// what it's sent. The test sends its audio with speak(). Closing a socket
// fires its close event, as a browser does, and for a speech socket that
// is its player's end of stream.
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
    if (m.commit) relay.heard(this, m.turn_id)
  }
  close() {
    if (this.closed) return
    this.closed = true
    this.readyState = 3
    queueMicrotask(() => this.onclose?.({ code: 1000 }))
  }
  // One chunk of speech from the relay, or its last.
  speak({ final = false } = {}) {
    this.onmessage?.({ data: JSON.stringify(final ? { audio: 'SUQz', final: true } : { audio: 'SUQz' }) })
  }
}
globalThis.WebSocket = FakeWebSocket

// MediaSource, for the desktop path only (an iPhone has none). Setting
// the audio element's src to one opens it, as a browser does.
class FakeSourceBuffer {
  constructor() { this.updating = false; this.appended = 0; this.listeners = [] }
  addEventListener(type, fn) { if (type === 'updateend') this.listeners.push(fn) }
  appendBuffer() {
    this.appended++
    this.updating = true
    queueMicrotask(() => { this.updating = false; for (const fn of this.listeners) fn() })
  }
}
class FakeMediaSource {
  static isTypeSupported(type) { return type === 'audio/mpeg' }
  static byUrl = new Map()
  constructor() { this.readyState = 'closed'; this.listeners = []; this.ended = false }
  addEventListener(type, fn) { if (type === 'sourceopen') this.listeners.push(fn) }
  addSourceBuffer() { this.sb = new FakeSourceBuffer(); return this.sb }
  endOfStream() { this.readyState = 'ended'; this.ended = true }
  open() { this.readyState = 'open'; for (const fn of this.listeners) fn() }
}
// ManagedMediaSource, for an iPhone with iOS 17.1 or later. It can ask
// the page to stop and start feeding it.
class Emitter {
  constructor() { this.handlers = {} }
  addEventListener(type, fn) { (this.handlers[type] ||= []).push(fn) }
  emit(type, ev = {}) { for (const fn of this.handlers[type] || []) fn(ev) }
}
class FakeManagedSourceBuffer extends Emitter {
  constructor() { super(); this.updating = false; this.appended = 0 }
  get buffered() { return { length: 0 } }
  appendBuffer() {
    this.appended++
    this.updating = true
    queueMicrotask(() => { this.updating = false; this.emit('updateend') })
  }
}
class FakeManagedMediaSource extends Emitter {
  static isTypeSupported(type) { return type === 'audio/mpeg' }
  static byUrl = new Map()
  constructor() { super(); this.readyState = 'closed'; this.ended = false }
  addSourceBuffer() { this.sb = new FakeManagedSourceBuffer(); return this.sb }
  endOfStream() { this.readyState = 'ended'; this.ended = true }
  open() { this.readyState = 'open'; this.emit('sourceopen') }
  stream(on) { this.emit(on ? 'startstreaming' : 'endstreaming') }
}
const realCreateObjectURL = URL.createObjectURL
let mseUrls = 0

// The shared audio element. A play() whose source isn't the silent unlock
// clip is a reply being heard.
let plays
let pauses
globalThis.Audio = class {
  constructor() { this._src = ''; this.currentTime = 0 }
  get src() { return this._src }
  set src(v) {
    this._src = v
    const ms = FakeMediaSource.byUrl.get(v)
    if (ms) queueMicrotask(() => ms.open())
    // WebKit opens a managed source only once remote playback is off.
    const mms = FakeManagedMediaSource.byUrl.get(v)
    if (mms && this.disableRemotePlayback) queueMicrotask(() => mms.open())
  }
  setAttribute() {}
  pause() { pauses.push(this._src) }
  play() { plays.push(this._src); return Promise.resolve() }
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
// The microphone: `mic.voiced` decides what the analyser reads on each
// frame, and the capture processor is fed a frame of audio each tick.
const mic = { voiced: false, proc: null }
globalThis.AudioContext = class {
  constructor() { this.state = 'running'; this.sampleRate = 48000; this.destination = {} }
  createMediaStreamSource() { return { connect() {} } }
  createAnalyser() {
    return {
      fftSize: 2048,
      frequencyBinCount: 1024,
      getFloatTimeDomainData(buf) { buf.fill(mic.voiced ? 0.1 : 0) },
      // Speech-shaped: energy below 1.2 kHz, none up high.
      getByteFrequencyData(buf) { for (let i = 0; i < buf.length; i++) buf[i] = i < 52 ? 200 : 0 },
    }
  }
  createScriptProcessor() { return (mic.proc = { connect() {}, disconnect() {} }) }
  resume() { return Promise.resolve() }
  close() { this.state = 'closed'; return Promise.resolve() }
}
globalThis.MediaRecorder = class {
  static isTypeSupported() { return true }
  constructor() { this.state = 'inactive'; this.mimeType = 'audio/webm' }
  start() { this.state = 'recording' }
  stop() { this.state = 'inactive'; queueMicrotask(() => this.onstop?.()) }
}

// ---- the app around the controller ----

let ctrl
let app
let sent
let posts
const realDebug = console.debug
const realWarn = console.warn

beforeEach(async () => {
  console.debug = () => {}
  console.warn = () => {}
  mock.timers.enable({ apis: ['setTimeout', 'setInterval', 'Date'], now: START })
  clear()
  FakeWebSocket.all = []
  FakeMediaSource.byUrl = new Map()
  FakeManagedMediaSource.byUrl = new Map()
  delete globalThis.MediaSource
  delete globalThis.ManagedMediaSource
  relay.latencyMs = 300
  relay.texts = [SAID, SAID_AGAIN]
  relay.queue = []
  relay.commits = 0
  mic.voiced = false
  plays = []
  pauses = []
  sent = []
  posts = []
  nextFrame = null
  // The app's stop: every cut aborts the round (one POST each), and the
  // first ends the round's stream, whose end the app reports `endsInMs`
  // later, as the stream's own finally does.
  app = { aborts: 0, streamOpen: false, endsInMs: 8 }
  globalThis.fetch = async (url) => {
    posts.push(url)
    return { ok: true, status: 200, json: async () => ({ ok: true }) }
  }
  ctrl = new VoiceController({
    getChatId: () => 7,
    getParticipants: () => [{ slug: 'claude', name: 'Claude', voice_id: 'v-claude' },
                            { slug: 'gpt', name: 'GPT', voice_id: 'v-gpt' }],
    sendText: (text, turnId) => sent.push({ text, turnId }),
    onState: () => {},
    onError: () => {},
    onPartial: () => {},
    onInterruptRound: () => {
      app.aborts++
      if (app.streamOpen) {
        app.streamOpen = false
        setTimeout(() => ctrl.onRoundDone(), app.endsInMs)
      }
    },
  })
  await ctrl.start()
  plays = [] // the start gesture's silent unlock clip is not a reply
})

afterEach(() => {
  ctrl.stop()
  mock.timers.reset()
  delete globalThis.MediaSource
  delete globalThis.ManagedMediaSource
  URL.createObjectURL = realCreateObjectURL
  console.debug = realDebug
  console.warn = realWarn
  delete globalThis.fetch
})

// Microtasks only: sockets open, chains move, promises settle. No time
// passes.
async function flush() {
  for (let i = 0; i < 6; i++) await new Promise((r) => setImmediate(r))
}

const elapsed = () => Date.now() - START

// One animation frame of the listening loop: the clock moves, the mic
// reads, audio flows to the relay, and the relay answers what's due.
async function frame(voicedAt) {
  mock.timers.tick(FRAME_MS)
  mic.voiced = voicedAt(elapsed())
  mic.proc?.onaudioprocess?.({ inputBuffer: { getChannelData: () => new Float32Array(960) } })
  const f = nextFrame
  nextFrame = null
  f?.()
  relay.deliverDue()
  await flush()
}

async function runUntil(untilMs, voicedAt) {
  while (elapsed() < untilMs) await frame(voicedAt)
}

const talking = (from, to) => (t) => t >= from && t < to
const silence = () => false

const tts = () => FakeWebSocket.all.filter((w) => w.isTts)
const audible = () => plays.filter((src) => src && !String(src).startsWith(SILENT))
const tagged = (tag) => snapshot().filter((e) => e.tag === tag)
const aborts = () => app.aborts

// A round the app is streaming: the owner's turn is saved and a seat
// starts to reply. Its speech socket opens and the relay sends some audio,
// but the reply isn't finished, so its stream hasn't ended.
async function replyUnderway(slug = 'claude', text = REPLY) {
  app.streamOpen = true
  ctrl.onEvent({ type: 'user_saved', message: { id: 1 } })
  return nextSpeaker(slug, text)
}

async function nextSpeaker(slug, text) {
  ctrl.onEvent({ type: 'speaker_start', speaker: slug })
  ctrl.onEvent({ type: 'delta', speaker: slug, text })
  await flush()
  const ws = tts().at(-1)
  ws.speak()
  ws.speak()
  await flush()
  return ws
}

// ---------- the iPhone: no MediaSource ----------

test('a barge-in cuts a held reply at once, cuts once, and its words are sent', async () => {
  const ws = await replyUnderway()
  assert.equal(ctrl.playing, 1, 'the reply is playing, held for its whole stream')
  assert.deepEqual(audible(), [], 'the phone plays nothing before the stream ends')
  // The owner talks over it for three seconds, then stops.
  const talk = talking(elapsed() + 100, elapsed() + 3100)
  await runUntil(elapsed() + 800, talk)
  assert.equal(tagged('vad:bargeIn').length, 1, 'the barge-in fired')
  assert.equal(ctrl.playing, 0, 'the cut reply stopped at once')
  assert.equal(ws.closed, true, 'its speech socket is closed')
  const branches = tagged('vad:branch').map((e) => JSON.parse(e.data).gated)
  assert.equal(branches.at(-1), false, 'the mic opened for the rest of the turn')
  // The rest of the speech is the same barge-in: it cuts nothing more.
  await runUntil(elapsed() + 8000, talk)
  assert.equal(tagged('vad:bargeIn').length, 1, 'one barge-in, however long the speech')
  assert.equal(aborts(), 1, 'one abort')
  assert.deepEqual(sent.map((m) => m.text), [SAID], 'the words were sent, once')
  assert.deepEqual(audible(), [], 'the cut reply never played')
  // The relay's last audio for the cut reply was already on its way.
  ws.speak()
  ws.speak({ final: true })
  ws.onclose?.({ code: 1000 })
  await runUntil(elapsed() + 10000, silence)
  assert.deepEqual(audible(), [], 'audio arriving after the cut is dropped')
  ctrl.stop()
  await flush()
  assert.deepEqual(audible(), [], 'and ending voice plays nothing')
})

test('switching voice off with a reply held never plays it later', async () => {
  const ws = await replyUnderway()
  ctrl.stop()
  await flush()
  assert.equal(ws.closed, true)
  assert.equal(ctrl.playing, 0)
  // The relay's in-flight audio and the minutes after.
  ws.speak()
  ws.speak({ final: true })
  mock.timers.tick(40000)
  await flush()
  assert.deepEqual(audible(), [], 'nothing plays after the session ended')
})

test('the stop button cuts a held reply at once and drops its late audio', async () => {
  const ws = await replyUnderway()
  ctrl.interrupt()
  await flush()
  assert.equal(ctrl.playing, 0, 'no time passed and the reply is gone')
  assert.equal(ctrl.sockets.size, 0)
  assert.equal(ws.closed, true)
  ws.speak({ final: true })
  mock.timers.tick(10)
  ctrl.onRoundDone()
  mock.timers.tick(10000)
  await flush()
  assert.deepEqual(audible(), [])
  assert.equal(ctrl.state, 'listening')
})

test('a reply queued behind the cut one never plays, even if the round ends first', async () => {
  const first = await replyUnderway('claude', REPLY)
  // Claude's text is done, and GPT's reply queues behind it.
  ctrl.onEvent({ type: 'speaker_end', speaker: 'claude' })
  const second = await nextSpeaker('gpt', SECOND)
  assert.equal(ctrl.playing, 2)
  ctrl.interrupt()
  // The round's end clears the drop flag before the chain has moved.
  ctrl.onRoundDone()
  first.speak({ final: true })
  second.speak({ final: true })
  mock.timers.tick(10000)
  await flush()
  assert.equal(ctrl.playing, 0)
  assert.deepEqual(audible(), [], 'neither reply played')
  assert.equal(first.closed && second.closed, true)
})

test('an uncut reply still plays in full on the phone', async () => {
  const ws = await replyUnderway()
  ctrl.onEvent({ type: 'speaker_end', speaker: 'claude' })
  mock.timers.tick(1000)
  await flush()
  assert.deepEqual(audible(), [], 'nothing plays before the stream ends')
  ws.speak({ final: true })
  mock.timers.tick(200)
  await flush()
  assert.equal(audible().length, 1, 'one clip, once the stream ended')
  assert.match(audible()[0], /^blob:/)
})

test('a new reply can be cut again: one cut per barge-in', async () => {
  await replyUnderway()
  let t = elapsed()
  await runUntil(t + 6000, talking(t + 100, t + 1600))
  assert.deepEqual(sent.map((m) => m.text), [SAID])
  // The owner's turn starts a new round, and its reply plays.
  await replyUnderway('gpt', SECOND)
  assert.equal(ctrl.playing, 1)
  t = elapsed()
  await runUntil(t + 6000, talking(t + 100, t + 1600))
  assert.equal(tagged('vad:bargeIn').length, 2)
  assert.equal(aborts(), 2)
  assert.deepEqual(sent.map((m) => m.text), [SAID, SAID_AGAIN])
  assert.deepEqual(audible(), [])
})

test('a round slow to end after the cut is still cut once', async () => {
  // Nothing plays yet: the round is thinking, which shuts the mic too.
  app.streamOpen = true
  app.endsInMs = 1500
  ctrl.onEvent({ type: 'user_saved', message: { id: 1 } })
  ctrl.onEvent({ type: 'speaker_start', speaker: 'claude' })
  const t = elapsed()
  await runUntil(t + 6000, talking(t + 100, t + 2600))
  assert.equal(tagged('vad:bargeIn').length, 1)
  assert.equal(aborts(), 1)
  assert.deepEqual(sent.map((m) => m.text), [SAID])
})

// ---------- a desktop browser with MediaSource ----------

function withMediaSource() {
  globalThis.MediaSource = FakeMediaSource
  URL.createObjectURL = (obj) => {
    if (!(obj instanceof FakeMediaSource)) return realCreateObjectURL(obj)
    const url = `blob:mse-${++mseUrls}`
    FakeMediaSource.byUrl.set(url, obj)
    return url
  }
}

test('with MediaSource a reply streams as it arrives, and a barge-in cuts it once', async () => {
  withMediaSource()
  const ws = await replyUnderway()
  const [src] = audible()
  assert.match(src, /^blob:mse-/, 'playback began before the stream ended')
  const ms = FakeMediaSource.byUrl.get(src)
  assert.equal(ms.sb.appended, 2, 'each chunk went in as it arrived')
  const t = elapsed()
  await runUntil(t + 6000, talking(t + 100, t + 2100))
  assert.equal(tagged('vad:bargeIn').length, 1)
  assert.equal(aborts(), 1)
  assert.ok(pauses.includes(src), 'the playing reply was paused')
  assert.equal(ctrl.playing, 0)
  assert.equal(ws.closed, true)
  ws.speak()
  await flush()
  assert.equal(ms.sb.appended, 2, 'late audio never reaches the stream')
  assert.equal(ms.ended, false)
  assert.deepEqual(sent.map((m) => m.text), [SAID])
})

test('with MediaSource a reply queued behind the cut one never plays', async () => {
  withMediaSource()
  const first = await replyUnderway('claude', REPLY)
  ctrl.onEvent({ type: 'speaker_end', speaker: 'claude' })
  const second = await nextSpeaker('gpt', SECOND)
  ctrl.interrupt()
  ctrl.onRoundDone()
  first.speak({ final: true })
  second.speak({ final: true })
  mock.timers.tick(10000)
  await flush()
  assert.equal(audible().length, 1, 'only the first reply ever started')
  assert.equal(ctrl.playing, 0)
})

// ---------- an iPhone with ManagedMediaSource (iOS 17.1 and later) ----------

function withManagedMediaSource() {
  globalThis.ManagedMediaSource = FakeManagedMediaSource
  URL.createObjectURL = (obj) => {
    if (!(obj instanceof FakeManagedMediaSource)) return realCreateObjectURL(obj)
    const url = `blob:mms-${++mseUrls}`
    FakeManagedMediaSource.byUrl.set(url, obj)
    return url
  }
}

test('the diagnostics say which way a session plays its replies', async () => {
  const played = () => tagged('session:start').map((e) => JSON.parse(e.data).playback)
  assert.deepEqual(played(), ['held'], 'no streaming source: held')
  withManagedMediaSource()
  ctrl.stop()
  await ctrl.start()
  assert.deepEqual(played(), ['held', 'managed'])
})

test('on an iPhone a reply is heard before its speech stream ends, and the trace says so', async () => {
  withManagedMediaSource()
  ctrl._trace.begin('t-phone')
  const ws = await replyUnderway()
  const [src] = audible()
  assert.match(src, /^blob:mms-/, 'playback began before the stream ended')
  const ms = FakeManagedMediaSource.byUrl.get(src)
  assert.equal(ms.readyState, 'open', 'remote playback was off, so the source opened')
  assert.equal(ms.sb.appended, 2, 'each chunk went in as it arrived')
  // The element becomes audible, with the reply's stream still open.
  ctrl.sink.onplaying()
  assert.equal(ws.closed, false)
  const claude = ctrl._trace.current().speakers.claude
  assert.equal(typeof claude.playback, 'number', 'playback is marked now, not at the end')
  assert.ok(claude.playback >= claude.first_audio)
  const stages = ctrl._trace.build().stages.map((st) => st.stage)
  assert.ok(stages.includes('first_audio_to_playback'))
  ws.speak({ final: true })
  await flush()
  assert.equal(ms.sb.appended, 3)
  assert.equal(ms.ended, true, 'the source ends with the stream')
})

test('on an iPhone a barge-in cuts a streaming reply once, and late audio never goes in', async () => {
  withManagedMediaSource()
  const ws = await replyUnderway()
  const [src] = audible()
  const ms = FakeManagedMediaSource.byUrl.get(src)
  const t = elapsed()
  await runUntil(t + 6000, talking(t + 100, t + 2100))
  assert.equal(tagged('vad:bargeIn').length, 1)
  assert.equal(aborts(), 1)
  assert.ok(pauses.includes(src), 'the playing reply was paused')
  assert.equal(ctrl.playing, 0)
  assert.equal(ws.closed, true)
  ws.speak()
  ms.stream(false)
  ms.stream(true)
  await flush()
  assert.equal(ms.sb.appended, 2, 'late audio never reaches the source')
  assert.equal(ms.ended, false)
  assert.deepEqual(sent.map((m) => m.text), [SAID])
  assert.deepEqual(audible(), [src], 'nothing else played')
})

test('on an iPhone a reply queued behind the cut one never plays', async () => {
  withManagedMediaSource()
  const first = await replyUnderway('claude', REPLY)
  ctrl.onEvent({ type: 'speaker_end', speaker: 'claude' })
  const second = await nextSpeaker('gpt', SECOND)
  ctrl.interrupt()
  ctrl.onRoundDone()
  first.speak({ final: true })
  second.speak({ final: true })
  mock.timers.tick(10000)
  await flush()
  assert.equal(audible().length, 1, 'only the first reply ever started')
  assert.equal(ctrl.playing, 0)
})

test('on an iPhone switching voice off stops a streaming reply, and nothing more goes in', async () => {
  withManagedMediaSource()
  const ws = await replyUnderway()
  const [src] = audible()
  const ms = FakeManagedMediaSource.byUrl.get(src)
  ms.stream(false)
  ws.speak()
  await flush()
  assert.equal(ms.sb.appended, 2, 'the chunk waits while the source has enough')
  ctrl.stop()
  await flush()
  assert.ok(pauses.includes(src))
  assert.equal(ctrl.playing, 0)
  ms.stream(true)
  ws.speak({ final: true })
  mock.timers.tick(40000)
  await flush()
  assert.equal(ms.sb.appended, 2, 'the held chunk was thrown away')
  assert.deepEqual(audible(), [src], 'nothing plays after the session ended')
})
