// One mic setting and clean 16 kHz audio (#505), end to end through the
// real voice controller started with its own start(). Solo used to ask the mic for noise suppression and
// auto gain, room mode for neither, and changing mode re-asked a live mic.
// A voice saved in one mode then sounded different in the other. And the
// stream to the relay kept every third sample, so sounds above 8 kHz
// folded down into the speech band.
// Pins: the mic is asked for the same setting in solo and in room mode;
// changing mode mid-session leaves the mic alone and sends the relay
// nothing (#482: the server reads the room from the chat on every turn);
// and the relay still gets one 16 kHz PCM-16 frame per mic
// callback, pre-roll included, made by the resampler with its filter
// carried across callbacks, and a pause in capture starts it afresh.
// Run: node --test frontend/src/voiceMicSetting.test.js
import assert from 'node:assert/strict'
import { afterEach, beforeEach, test } from 'node:test'
import VoiceController from './voice.js'
import { Resampler } from './resample.js'

// ---- the browser, as far as voice.js reaches into it ----

globalThis.requestAnimationFrame = () => {}

class FakeWebSocket {
  static OPEN = 1
  static all = []
  constructor(url) {
    this.url = url
    this.readyState = FakeWebSocket.OPEN
    this.sent = []
    FakeWebSocket.all.push(this)
  }
  send(s) { this.sent.push(JSON.parse(s)) }
  close() { this.readyState = 3 }
}
globalThis.WebSocket = FakeWebSocket
globalThis.Audio = class {
  setAttribute() {}
  play() { return Promise.resolve() }
}
globalThis.location = { protocol: 'http:', host: 'localhost:8902' }
globalThis.window = { addEventListener() {}, removeEventListener() {} }
globalThis.document = { hidden: false, addEventListener() {}, removeEventListener() {} }

// Every mic request and every later change to a live track.
let asked
let reapplied
Object.defineProperty(globalThis, 'navigator', {
  configurable: true,
  value: {
    userAgent: 'node',
    mediaDevices: {
      async getUserMedia(constraints) {
        asked.push(constraints)
        const track = {
          enabled: true,
          stop() {},
          applyConstraints(c) { reapplied.push(c); return Promise.resolve() },
        }
        return { getTracks: () => [track], getAudioTracks: () => [track] }
      },
    },
  },
})

let rate
let processors
globalThis.AudioContext = class {
  constructor() {
    this.state = 'running'
    this.sampleRate = rate
    this.destination = {}
  }
  createMediaStreamSource() { return { connect() {} } }
  createAnalyser() {
    return { fftSize: 2048, frequencyBinCount: 1024,
             getFloatTimeDomainData(b) { b.fill(0) }, getByteFrequencyData(b) { b.fill(0) } }
  }
  createScriptProcessor() {
    const p = { connect() {}, disconnect() {} }
    processors.push(p)
    return p
  }
  resume() { return Promise.resolve() }
  close() { return Promise.resolve() }
}
globalThis.MediaRecorder = class {
  static isTypeSupported() { return true }
  constructor() { this.state = 'inactive'; this.mimeType = 'audio/webm' }
  start() { this.state = 'recording' }
  stop() { this.state = 'inactive' }
}

let controllers
const realDebug = console.debug

beforeEach(() => {
  console.debug = () => {}
  asked = []
  reapplied = []
  processors = []
  controllers = []
  rate = 48000
  FakeWebSocket.all = []
  globalThis.fetch = async () => ({ ok: true, status: 200, json: async () => ({ ok: true }) })
})

afterEach(() => {
  for (const c of controllers) c.stop()
  console.debug = realDebug
  delete globalThis.fetch
})

async function started({ roomMode = false } = {}) {
  const ctrl = new VoiceController({
    getChatId: () => 7,
    getParticipants: () => [],
    sendText: () => {},
    onState: () => {},
    onError: () => {},
    onPartial: () => {},
  })
  controllers.push(ctrl)
  ctrl.roomMode = roomMode
  await ctrl.start()
  ctrl.sttWs.onopen()
  return ctrl
}

const socket = (ctrl) => FakeWebSocket.all.find((w) => w === ctrl.sttWs)
const controlFrames = (ctrl) => socket(ctrl).sent.filter((m) => !('audio' in m))
const audioFrames = (ctrl) => socket(ctrl).sent.filter((m) => 'audio' in m)

// One mic callback: the processor hands voice.js a chunk.
function callback(chunk) {
  for (const p of processors) p.onaudioprocess?.({ inputBuffer: { getChannelData: () => chunk } })
}

function speechLike(n, seed) {
  const x = new Float32Array(n)
  for (let i = 0; i < n; i++) {
    const t = (seed * n + i) / rate
    x[i] = 0.3 * Math.sin(2 * Math.PI * 220 * t) + 0.1 * Math.sin(2 * Math.PI * 3100 * t) +
           0.05 * Math.sin(2 * Math.PI * 11000 * t)
  }
  return x
}

function decode(frame) {
  const bin = atob(frame.audio)
  const bytes = new Uint8Array(bin.length)
  for (let i = 0; i < bin.length; i++) bytes[i] = bin.charCodeAt(i)
  return new Int16Array(bytes.buffer)
}

function pcm16(samples) {
  return Int16Array.from(samples, (v) => {
    const s = Math.max(-1, Math.min(1, v))
    return s < 0 ? s * 0x8000 : s * 0x7fff
  })
}

const SETTING = { echoCancellation: true, noiseSuppression: false, autoGainControl: false }

test('solo and room mode ask the mic for the same setting', async () => {
  const solo = await started({ roomMode: false })
  solo.stop()
  const room = await started({ roomMode: true })
  room.stop()
  assert.deepEqual(asked, [{ audio: SETTING }, { audio: SETTING }])
})

test('changing mode leaves the live mic alone and sends the relay nothing', async () => {
  const ctrl = await started()
  ctrl.setRoomMode(true)
  ctrl.setRoomMode(false)
  ctrl.setRoomMode(true)
  assert.equal(ctrl.roomMode, true)
  assert.deepEqual(reapplied, [], 'the live track was never re-asked')
  assert.equal(asked.length, 1, 'and no second mic was opened')
  assert.deepEqual(controlFrames(ctrl), [{ chat_id: 7, sample_rate: 16000 }])
})

for (const micRate of [48000, 44100]) {
  test(`${micRate} Hz: the relay gets one resampled 16 kHz frame per callback`, async () => {
    rate = micRate
    const ctrl = await started()
    const chunks = Array.from({ length: 12 }, (_, i) => speechLike(4096, i))
    // Before the turn: pre-roll, of which the last eight chunks are kept.
    for (const c of chunks.slice(0, 10)) callback(c)
    assert.equal(audioFrames(ctrl).length, 0)
    ctrl._sttStartStreaming()
    for (const c of chunks.slice(10)) callback(c)
    const frames = audioFrames(ctrl)
    assert.equal(frames.length, 10, 'eight pre-roll frames and one per callback after')
    for (const f of frames) assert.equal(f.sample_rate, 16000)
    // The same samples the pure resampler makes from the same callbacks,
    // with its filter carried across them. The first two chunks' frames
    // aged out of the pre-roll.
    const r = new Resampler(micRate)
    const want = chunks.map((c) => pcm16(r.push(c))).slice(2)
    assert.deepEqual(frames.map(decode), want)
    const each = (4096 * 16000) / micRate
    for (const f of frames.slice(1)) assert.ok(Math.abs(decode(f).length - each) < 1)
  })
}

test('a pause in capture starts the filter afresh', async () => {
  const ctrl = await started()
  ctrl._sttStartStreaming()
  const a = speechLike(4096, 0)
  const b = speechLike(4096, 5)
  callback(a)
  ctrl.muted = true
  callback(a)
  ctrl.muted = false
  callback(b)
  const frames = audioFrames(ctrl)
  assert.equal(frames.length, 2, 'nothing is sent while muted')
  assert.deepEqual(decode(frames[1]), pcm16(new Resampler(48000).push(b)),
                   'the chunk after the pause is not spliced onto the one before it')
})
