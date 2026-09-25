// #461: a turn the batch path transcribes carries an identity copy, end to
// end through the real voice controller. The realtime relay checks who
// spoke every streamed turn; a batch turn never reached it, so it went
// unchecked, and in one evening that was 15 of 18 unnamed turns. Pins: in
// batch mode the upload carries the turn id and a 16 kHz WAV of the turn,
// under the same id the send uses; a turn the relay never heard (the
// zero-frames salvage) does too; a turn the controller can't place, or a
// recording the browser can't decode, still goes as before, with no copy.
// Run: node --test frontend/src/voiceIdentityCopy.test.js
import assert from 'node:assert/strict'
import { afterEach, beforeEach, test } from 'node:test'
import VoiceController from './voice.js'
import { clear } from './voiceDebug.js'

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

function fakeRecorder() {
  return {
    state: 'recording',
    mimeType: 'audio/webm',
    stop() { this.state = 'inactive'; queueMicrotask(() => this.onstop?.()) },
  }
}

let forms
let controllers
const realDebug = console.debug
const realWarn = console.warn

beforeEach(() => {
  console.debug = () => {}
  console.warn = () => {}
  clear()
  forms = []
  controllers = []
  globalThis.fetch = async (url, opts) => {
    if (/\/api\/chats\/\d+\/stt$/.test(url)) {
      forms.push(opts.body)
      return { ok: true, status: 200, json: async () => ({ text: 'hello there' }) }
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

// Ten seconds of recording at 48 kHz, as the browser decodes it.
function decoder({ fail = false } = {}) {
  return async () => {
    if (fail) throw new Error('EncodingError')
    return { sampleRate: 48000, getChannelData: () => new Float32Array(480_000).fill(0.2) }
  }
}

function session({ realtime, decode = decoder() }) {
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
    decodeAudioData: decode,
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
  return { ctrl, ws, sent }
}

async function speak(ctrl, { speechMs = 2000, frames = 40 } = {}) {
  const now = Date.now()
  ctrl.speechStart = now - speechMs - 2000
  ctrl.lastVoice = now - 2000
  ctrl._utterFrames = frames
  await ctrl._finalizeUtterance('gap')
}

async function settle() {
  for (let i = 0; i < 6; i++) await new Promise((r) => setTimeout(r, 0))
}

async function wavOf(form) {
  const bytes = new Uint8Array(await form.get('pcm').arrayBuffer())
  const view = new DataView(bytes.buffer)
  return { rate: view.getUint32(24, true), dataBytes: view.getUint32(40, true) }
}

test('a batch-mode turn uploads its identity copy under the id it is sent with', async () => {
  const { ctrl, sent } = session({ realtime: false })
  await speak(ctrl)
  await settle()
  assert.equal(forms.length, 1)
  const form = forms[0]
  assert.equal(sent.length, 1)
  assert.equal(form.get('turn_id'), sent[0].turnId)
  const wav = await wavOf(form)
  assert.equal(wav.rate, 16000)
  // the speech, the pre-roll and the short tail; not the ten seconds the
  // recorder held
  const seconds = wav.dataBytes / 2 / 16000
  assert.ok(seconds > 2.5 && seconds < 3.1, `copy was ${seconds}s`)
})

test('a turn the relay never heard is salvaged with its copy', async () => {
  // The VAD heard speech but no frame reached the socket: the salvage is
  // the only path this turn takes, so it carries the check's audio.
  const { ctrl, ws, sent } = session({ realtime: true })
  await speak(ctrl, { frames: 0 })
  await settle()
  assert.equal(ws.sent.map((s) => JSON.parse(s)).filter((m) => m.commit).length, 0)
  assert.equal(forms.length, 1)
  assert.equal(sent.length, 1)
  assert.ok(sent[0].turnId)
  assert.equal(forms[0].get('turn_id'), sent[0].turnId)
  assert.ok(forms[0].get('pcm'))
})

test('a recording the browser cannot decode goes as it always did', async () => {
  const { ctrl, sent } = session({ realtime: false, decode: decoder({ fail: true }) })
  await speak(ctrl)
  await settle()
  assert.equal(forms.length, 1)
  assert.deepEqual([...forms[0].keys()], ['file', 'duration_ms'])
  assert.equal(sent.length, 1)
})
