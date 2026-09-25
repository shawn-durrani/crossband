// #453: a long turn cut at the length limit, end to end through the real
// voice controller and its own listening loop. Long speech is cut into
// pieces about 12 seconds in, on the first quiet frame after that, and the
// cut can land on the pause that ends the turn: a remark that stops 10 to
// 12 seconds in was filed as the first piece of a longer turn, and the app
// waited for more speech that never came. The screen stayed on Listening
// and nothing was sent.
// Pins: after a cut, the ordinary pause (the controller's silenceMs, the
// same rule a short turn ends on) still ends the turn, and every piece is
// sent as one turn, once, whether the cut piece's words arrive before the
// pause ends, after it, or only from the backup copy; a speaker who carries
// on still gets one stitched turn; a short turn keeps its timing; mute and
// a too-short tail end a cut turn too; the batch path behaves the same;
// an earlier piece's words no longer switch off the next piece's backup
// or move the screen off Thinking; and the diagnostics hold ids, never
// words.
// Run: node --test frontend/src/voiceLongTurn.test.js
import assert from 'node:assert/strict'
import { afterEach, beforeEach, mock, test } from 'node:test'
import VoiceController from './voice.js'
import { sttCommitTimeoutMs } from './turnPolicy.js'
import { HANDOFF_STALL_MS } from './handoffWatch.js'
import { clear, snapshot } from './voiceDebug.js'

// The browser parts voice.js reaches on these paths, and no more. Audio
// frames are counted, not kept.
class FakeWebSocket {
  static OPEN = 1
  static last = null
  constructor(url) {
    this.url = url
    this.readyState = FakeWebSocket.OPEN
    this.sent = []
    this.audioFrames = 0
    FakeWebSocket.last = this
  }
  send(s) {
    const m = JSON.parse(s)
    if (m.audio) { this.audioFrames++; return }
    this.sent.push(m)
    if (m.commit) relay.heard(this, m.turn_id)
  }
  close() { this.readyState = 3 }
}
globalThis.WebSocket = FakeWebSocket
globalThis.Audio = class { setAttribute() {} }
globalThis.location = { protocol: 'http:', host: 'localhost:8902' }
let nextFrame = null
globalThis.requestAnimationFrame = (cb) => { nextFrame = cb }

const FRAME_MS = 20
const START = 1_800_000_000_000

// What each piece said, in commit order, and what the batch transcriber
// hears from the backup copy. Words no diagnostic has any reason to hold.
const PIECES = ['Alex measured the deck twice', 'then Sam cut the boards',
                'and Dave sanded every edge']
const BATCH = 'Mateo stacked the offcuts by the gate'
const WORDS = ['measured', 'boards', 'sanded', 'offcuts', ...PIECES, BATCH]

// The realtime relay: answers each commit with that piece's words after
// `latencyMs`, except the commits listed in `silent`.
const relay = {
  latencyMs: 400,
  silent: new Set(),
  queue: [],
  commits: [],
  heard(ws, turnId) {
    const i = this.commits.length
    this.commits.push({ turnId, at: Date.now() - START })
    if (this.silent.has(i)) return
    this.queue.push({ ws, turnId, due: Date.now() + this.latencyMs, text: PIECES[i] || '' })
  },
  deliverDue() {
    const due = this.queue.filter((q) => q.due <= Date.now())
    this.queue = this.queue.filter((q) => q.due > Date.now())
    for (const q of due) {
      q.ws.onmessage({ data: JSON.stringify({ final: q.text, turn_id: q.turnId }) })
    }
  },
}

let posts
let sttGate      // when set, /stt waits on it
let controllers
const realDebug = console.debug
const realWarn = console.warn

beforeEach(() => {
  console.debug = () => {}
  console.warn = () => {}
  mock.timers.enable({ apis: ['setTimeout', 'Date'], now: START })
  clear()
  relay.latencyMs = 400
  relay.silent = new Set()
  relay.queue = []
  relay.commits = []
  posts = []
  sttGate = null
  controllers = []
  nextFrame = null
  globalThis.fetch = async (url, opts) => {
    posts.push({ url, at: Date.now() - START })
    if (/\/api\/chats\/\d+\/stt$/.test(url)) {
      if (sttGate) await sttGate
      // The backup copy holds speech only if it was never thrown away.
      const size = opts.body.get('file').size
      return { ok: true, status: 200, json: async () => ({ text: size ? BATCH : '' }) }
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

// A live session in its listening loop, with a microphone the test
// scripts: `mic.voiced` decides what the analyser reads on each frame. The
// server saves every turn it's sent and runs a round that ends at once, as
// a healthy one would, so the hand-off watch sees each turn confirmed.
function liveSession({ realtime = true, serverSaves = true } = {}) {
  const sent = []
  const mic = { voiced: false }
  let proc = null
  const pending = []
  const ctrl = new VoiceController({
    getChatId: () => 7,
    getParticipants: () => [],
    sendText: (text, turnId) => {
      sent.push({ text, turnId, at: Date.now() - START })
      if (serverSaves) pending.push(turnId)
    },
    onState: () => {},
    onError: () => {},
    onPartial: () => {},
  })
  ctrl.active = true
  ctrl.state = 'listening'
  ctrl.recorder = fakeRecorder()
  ctrl.recChunks = [new Blob(['opus frames'])]
  ctrl.recStarted = Date.now()
  // The backup recorder's restart, as _startRecorder does it: a fresh,
  // empty recording. Counted, so a test can see when the copy was dropped.
  const restarts = []
  ctrl._startRecorder = function () {
    this.recorder = fakeRecorder()
    this.recChunks = []
    this.recStarted = Date.now()
    this.speechStart = 0
    this.lastVoice = 0
    restarts.push(Date.now() - START)
  }
  ctrl.audioCtx = {
    state: 'running', sampleRate: 48000, destination: {},
    createScriptProcessor: () => (proc = { connect() {}, disconnect() {} }),
    close: async () => {},
  }
  ctrl.micSource = { connect() {} }
  ctrl.analyser = {
    fftSize: 2048,
    frequencyBinCount: 1024,
    getFloatTimeDomainData(buf) { buf.fill(mic.voiced ? 0.1 : 0) },
    // Speech-shaped: energy below 1.2 kHz, none up high.
    getByteFrequencyData(buf) { for (let i = 0; i < buf.length; i++) buf[i] = i < 52 ? 200 : 0 },
  }
  if (realtime) {
    ctrl._openSttStream()
    FakeWebSocket.last.onopen()
  } else {
    ctrl.sttRealtime = false
  }
  ctrl._vadLoop()
  controllers.push(ctrl)
  const pcm = new Float32Array(960)
  // One animation frame: the clock moves, the mic reads, audio flows to the
  // realtime socket, the relay answers what's due, and the server confirms.
  async function frame(voicedAt) {
    mock.timers.tick(FRAME_MS)
    mic.voiced = voicedAt(Date.now() - START)
    proc?.onaudioprocess?.({ inputBuffer: { getChannelData: () => pcm } })
    const f = nextFrame
    nextFrame = null
    f?.()
    relay.deliverDue()
    while (pending.length) {
      ctrl.onEvent({ type: 'user_saved', message: { id: 1, voice_turn_id: pending.shift() } })
      ctrl.onRoundDone()
    }
    for (let i = 0; i < 4; i++) await new Promise((r) => setImmediate(r))
  }
  // Run the loop until `untilMs` into the session, with `voicedAt(t)`
  // saying whether the mic hears speech at t.
  async function runUntil(untilMs, voicedAt) {
    while (Date.now() - START < untilMs) await frame(voicedAt)
  }
  return { ctrl, sent, runUntil, restarts, now: () => Date.now() - START }
}

function fakeRecorder() {
  return {
    state: 'recording',
    mimeType: 'audio/webm',
    stop() { this.state = 'inactive'; queueMicrotask(() => this.onstop?.()) },
  }
}

// Speech from `from` to `to` ms, with the short breaks real speech has: a
// 60 ms dip every 1.5 s, too short to end a turn.
function speech(...spans) {
  return (t) => spans.some(([from, to]) => t >= from && t < to && (t - from) % 1500 < 1440)
}

const sttPosts = () => posts.filter((p) => /\/api\/chats\/\d+\/stt$/.test(p.url))
const beacons = () => posts.filter((p) => p.url === '/api/voice/stall')

// The last voiced frame of a span that ends at `to`.
const lastVoiceOf = (to) => to - FRAME_MS

test('an 11-second remark then silence is sent once, after the ordinary pause', async () => {
  const s = liveSession()
  const talk = speech([500, 11500])
  // Past the cut but short of the pause: the piece is committed, its words
  // are in, and nothing has been sent.
  await s.runUntil(13300, talk)
  assert.equal(relay.commits.length, 1, 'the remark was cut into one piece')
  assert.deepEqual(s.sent, [], 'nothing goes before the pause has run its length')
  // The same pause a short turn ends on: silenceMs after the last voice.
  await s.runUntil(lastVoiceOf(11500) + s.ctrl.silenceMs + 2 * FRAME_MS, talk)
  assert.deepEqual(s.sent.map((m) => [m.text, m.turnId]),
                   [[PIECES[0], relay.commits[0].turnId]])
  // Thirty more seconds of silence sends nothing more, and the hand-off
  // was confirmed, so no stall is reported.
  await s.runUntil(45000, talk)
  assert.equal(s.sent.length, 1)
  assert.equal(beacons().length, 0)
  assert.equal(sttPosts().length, 0, 'realtime answered, so the backup never ran')
  assert.equal(s.ctrl.state, 'listening')
})

test('the pause ends the turn even while the cut piece is still being transcribed', async () => {
  const s = liveSession()
  relay.latencyMs = 3000
  const talk = speech([500, 11500])
  await s.runUntil(lastVoiceOf(11500) + s.ctrl.silenceMs + 2 * FRAME_MS, talk)
  // The turn is over: the screen says Thinking while the words come back.
  assert.equal(s.ctrl.state, 'transcribing')
  assert.deepEqual(s.sent, [])
  await s.runUntil(30000, talk)
  assert.deepEqual(s.sent.map((m) => [m.text, m.turnId]),
                   [[PIECES[0], relay.commits[0].turnId]])
  assert.equal(s.ctrl.state, 'listening')
  assert.equal(beacons().length, 0)
})

test('a cut piece whose words never come back goes out from the backup copy', async () => {
  const s = liveSession()
  relay.silent = new Set([0])
  const talk = speech([500, 11500])
  await s.runUntil(13300, talk)
  const cutAt = relay.commits[0].at
  const endAt = lastVoiceOf(11500) + s.ctrl.silenceMs + 2 * FRAME_MS
  await s.runUntil(endAt, talk)
  // The backup copy holds the cut piece's audio, so it's kept through the
  // pause instead of being restarted as idle audio.
  assert.deepEqual(s.restarts.filter((t) => t > cutAt && t <= endAt), [])
  const speechMs = 11500 - 400
  await s.runUntil(cutAt + sttCommitTimeoutMs(speechMs) + 1000, talk)
  assert.equal(sttPosts().length, 1)
  assert.deepEqual(s.sent.map((m) => [m.text, m.turnId]),
                   [[BATCH, relay.commits[0].turnId]])
  await s.runUntil(45000, talk)
  assert.equal(s.sent.length, 1)
})

test('a 25-second turn is stitched and sent once, as before', async () => {
  const s = liveSession()
  const talk = speech([500, 25500])
  await s.runUntil(25500 + s.ctrl.silenceMs - 200, talk)
  assert.ok(relay.commits.length >= 2, 'long speech was cut into pieces')
  assert.deepEqual(s.sent, [], 'still inside the pause')
  await s.runUntil(40000, talk)
  const n = relay.commits.length
  assert.deepEqual(s.sent.map((m) => [m.text, m.turnId]),
                   [[PIECES.slice(0, n).filter(Boolean).join(' '), relay.commits[n - 1].turnId]])
  assert.ok(s.sent[0].at >= lastVoiceOf(25500) + s.ctrl.silenceMs, 'the ordinary pause ended it')
  assert.equal(beacons().length, 0)
})

test('a speaker who pauses after a cut and carries on still gets one turn', async () => {
  const s = liveSession()
  // The pause after the cut is just short of the 2 s that ends a turn.
  const talk = speech([500, 11500], [11500 + s.ctrl.silenceMs - 200, 16000])
  await s.runUntil(30000, talk)
  assert.equal(relay.commits.length, 2)
  assert.deepEqual(s.sent.map((m) => [m.text, m.turnId]),
                   [[`${PIECES[0]} ${PIECES[1]}`, relay.commits[1].turnId]])
})

test('a short turn is unchanged', async () => {
  const s = liveSession()
  const talk = speech([500, 3700])
  await s.runUntil(lastVoiceOf(3700) + s.ctrl.silenceMs, talk)
  assert.equal(relay.commits.length, 0, 'the pause has not run out yet')
  await s.runUntil(lastVoiceOf(3700) + s.ctrl.silenceMs + 2 * FRAME_MS, talk)
  assert.equal(relay.commits.length, 1)
  assert.equal(s.ctrl.state, 'transcribing')
  await s.runUntil(20000, talk)
  assert.deepEqual(s.sent.map((m) => [m.text, m.turnId]),
                   [[PIECES[0], relay.commits[0].turnId]])
  assert.equal(s.ctrl.state, 'listening')
})

test('the pause after a cut follows the silence setting', async () => {
  const s = liveSession()
  s.ctrl.setSilenceMs(3500)
  const talk = speech([500, 11500])
  await s.runUntil(lastVoiceOf(11500) + 3500 - 100, talk)
  assert.deepEqual(s.sent, [])
  await s.runUntil(lastVoiceOf(11500) + 3500 + 2 * FRAME_MS, talk)
  assert.equal(s.sent.length, 1)
})

test('muting after a cut sends the turn at once', async () => {
  const s = liveSession()
  const talk = speech([500, 11500])
  await s.runUntil(12900, talk)
  assert.equal(relay.commits.length, 1)
  assert.deepEqual(s.sent, [])
  s.ctrl.setMuted(true)
  assert.deepEqual(s.sent.map((m) => m.text), [PIECES[0]])
})

test('a too-short tail after a cut still sends the piece in flight', async () => {
  const s = liveSession()
  relay.latencyMs = 3000
  // A brief sound after the cut opens a new piece that is too short to
  // transcribe. The pause after it ends the turn while the cut piece's
  // words are still on their way.
  const talk = (t) => speech([500, 11500])(t) || (t >= 12600 && t < 12900)
  await s.runUntil(30000, talk)
  assert.deepEqual(s.sent.map((m) => m.text), [PIECES[0]])
})

test('without realtime, a remark cut at the limit is sent once after the pause', async () => {
  const s = liveSession({ realtime: false })
  const talk = speech([500, 11500])
  await s.runUntil(13300, talk)
  assert.equal(sttPosts().length, 1, 'the cut piece went to the batch transcriber')
  assert.deepEqual(s.sent, [], 'its words wait for the end of the turn')
  await s.runUntil(lastVoiceOf(11500) + s.ctrl.silenceMs + 2 * FRAME_MS, talk)
  assert.deepEqual(s.sent.map((m) => m.text), [BATCH])
  await s.runUntil(40000, talk)
  assert.equal(s.sent.length, 1)
  assert.equal(s.ctrl.state, 'listening')
})

test('without realtime, the pause can end the turn while the cut piece is still out', async () => {
  const s = liveSession({ realtime: false })
  let open
  sttGate = new Promise((r) => { open = r })
  const talk = speech([500, 11500])
  const endAt = lastVoiceOf(11500) + s.ctrl.silenceMs + 2 * FRAME_MS
  await s.runUntil(endAt, talk)
  assert.equal(sttPosts().length, 1)
  assert.deepEqual(s.sent, [])
  assert.equal(s.ctrl.state, 'transcribing', 'the turn is over: Thinking')
  open()
  await s.runUntil(endAt + 200, talk)
  assert.deepEqual(s.sent.map((m) => m.text), [BATCH])
  await s.runUntil(40000, talk)
  assert.equal(s.sent.length, 1)
  assert.equal(s.ctrl.state, 'listening')
})

test("an earlier piece's words no longer switch off the next piece's backup", async () => {
  const s = liveSession()
  // The first piece's words land after the last piece is committed, and
  // the last piece's words never come back.
  relay.latencyMs = 6000
  relay.silent = new Set([1])
  const talk = speech([500, 16000])
  const endAt = lastVoiceOf(16000) + s.ctrl.silenceMs + 2 * FRAME_MS
  await s.runUntil(endAt, talk)
  assert.equal(relay.commits.length, 2)
  assert.equal(s.ctrl.state, 'transcribing')
  const last = relay.commits[1]
  // The first piece's words land. The screen stays on Thinking, since the
  // last piece is still out, and the backup recording is kept.
  await s.runUntil(relay.commits[0].at + relay.latencyMs + 200, talk)
  assert.deepEqual(s.sent, [])
  assert.equal(s.ctrl.state, 'transcribing')
  assert.deepEqual(s.restarts.filter((t) => t > relay.commits[0].at), [])
  await s.runUntil(last.at + sttCommitTimeoutMs(16000 - 12400) + 1000, talk)
  // The backup ran for the last piece and the turn went out once. The
  // backup is the whole recording, so its words can repeat the first
  // piece's: a separate problem, not pinned here.
  assert.equal(sttPosts().length, 1)
  assert.equal(s.sent.length, 1)
  assert.equal(s.sent[0].turnId, last.turnId)
  assert.ok(s.sent[0].text.includes(BATCH))
  assert.equal(s.ctrl.state, 'listening')
})

test("a long turn's end is watched like any other hand-off", async () => {
  const s = liveSession({ serverSaves: false })
  const talk = speech([500, 11500])
  await s.runUntil(14000, talk)
  assert.equal(s.sent.length, 1)
  // The server never saves it: the watch reports the stall.
  await s.runUntil(14000 + HANDOFF_STALL_MS + 1000, talk)
  assert.equal(beacons().length, 1)
})

test('the diagnostics hold ids and counts, never what was said', async () => {
  const s = liveSession()
  relay.latencyMs = 3000
  await s.runUntil(30000, speech([500, 11500]))
  assert.equal(s.sent.length, 1)
  const ring = snapshot()
  const end = ring.find((e) => e.tag === 'turn:endAfterCut')
  assert.ok(end, 'the ring shows the turn ending after a cut')
  assert.ok(end.data.includes(relay.commits[0].turnId))
  const all = JSON.stringify(ring)
  for (const word of WORDS) {
    assert.ok(!all.includes(word), `"${word}" reached the diagnostics ring`)
  }
})
