// #461: a turn the batch path transcribes carries an identity copy, end to
// end through the real voice controller. The realtime relay checks who
// spoke every streamed turn; a batch turn never reached it, so it went
// unchecked, and in one evening that was 15 of 18 unnamed turns. Pins: in
// batch mode the upload carries the turn id and a 16 kHz WAV of the turn,
// under the same id the send uses; a turn the relay never heard (the
// zero-frames salvage) does too; a turn the controller can't place, or a
// recording the browser can't decode, still goes as before, with no copy.
// #540: a piece of a long turn that goes by the backup copy names the
// piece before it, however it got there, so the server links the pieces
// even when the relay never heard this one.
// Run: node --test frontend/src/voiceIdentityCopy.test.js
import assert from 'node:assert/strict'
import { afterEach, beforeEach, mock, test } from 'node:test'
import VoiceController from './voice.js'
import { sttCommitTimeoutMs } from './turnPolicy.js'
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

async function speak(ctrl, { speechMs = 2000, frames = 40, cause = 'gap' } = {}) {
  const now = Date.now()
  ctrl.speechStart = now - speechMs - 2000
  ctrl.lastVoice = now - 2000
  ctrl._utterFrames = frames
  await ctrl._finalizeUtterance(cause)
}

const commits = (ws) => ws.sent.map((s) => JSON.parse(s)).filter((m) => m.commit)

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
  // No turn id and no copy. The one addition is why it took this path
  // (#470), a word the server logs.
  assert.deepEqual([...forms[0].keys()], ['file', 'duration_ms', 'why'])
  assert.equal(forms[0].get('why'), 'batch')
  assert.equal(sent.length, 1)
})

test("a long turn's last piece the relay never heard names the piece before it", async () => {
  const { ctrl, ws, sent } = session({ realtime: true })
  // The first piece is cut at the length limit and committed live. No
  // frame of the last piece reaches the socket, so only its backup copy
  // tells the server it follows the first.
  await speak(ctrl, { cause: 'cap' })
  const [first] = commits(ws)
  await speak(ctrl, { frames: 0 })
  await settle()
  assert.equal(commits(ws).length, 1)
  assert.equal(forms.length, 1)
  assert.equal(forms[0].get('turn_id'), sent[0].turnId)
  assert.notEqual(sent[0].turnId, first.turn_id)
  assert.equal(forms[0].get('after'), first.turn_id)
})

test('a last piece spoken across a socket change names the piece before it', async () => {
  const { ctrl, ws } = session({ realtime: true })
  await speak(ctrl, { cause: 'cap' })
  const [first] = commits(ws)
  // The socket was replaced while the last piece was spoken.
  ctrl._utterGen = -1
  await speak(ctrl)
  await settle()
  const [, last] = commits(ws)
  assert.equal(last.after, first.turn_id, 'the commit names it')
  assert.equal(forms.length, 1)
  assert.equal(forms[0].get('turn_id'), last.turn_id)
  assert.equal(forms[0].get('after'), first.turn_id, 'and so does the backup copy')
})

test("a last piece whose words never come back names the piece before it", async () => {
  const { ctrl, ws } = session({ realtime: true })
  mock.timers.enable({ apis: ['setTimeout'] })
  try {
    await speak(ctrl, { cause: 'cap' })
    await speak(ctrl)
    mock.timers.tick(sttCommitTimeoutMs(2000) + 1)
  } finally {
    mock.timers.reset()
  }
  await settle()
  const [first, last] = commits(ws)
  assert.equal(forms.length, 1)
  assert.equal(forms[0].get('turn_id'), last.turn_id)
  assert.equal(forms[0].get('after'), first.turn_id)
})

test('a turn of one piece names none on its backup copy', async () => {
  const { ctrl } = session({ realtime: true })
  await speak(ctrl, { frames: 0 })
  await settle()
  assert.equal(forms.length, 1)
  assert.ok(forms[0].get('turn_id'))
  assert.equal(forms[0].has('after'), false)
})
