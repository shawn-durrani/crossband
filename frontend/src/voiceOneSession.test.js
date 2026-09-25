// One voice session per page, and one listening loop per session, end to
// end through the real voice controller started with its own start().
// On 25 September every spoken turn reached the chat twice. A restart
// signs the browser out, since sessions live in the server's memory, and
// the lock screen replaced the app while a voice call was live. The old
// voice session was never ended: its microphone and listening loop kept
// running unseen, and when the owner unlocked and started voice again,
// two sessions heard every turn and each sent it. The saved diagnostics
// show it: two sets of screen changes a frame apart, one turn transcribed
// by realtime and the same turn by the backup copy.
// Pins: a second session on the page ends the first, so a turn is sent
// once; a second tap on start opens no second microphone; starting the
// listening loop again never leaves two loops reading the mic, even
// across a stop and start inside one frame; and the paths #451 and #454
// added (the pause after a cut, and the rescue after a realtime error)
// leave exactly one loop, with every later turn finalised and sent once,
// even when both its realtime words and its backup copy come back.
// Run: node --test frontend/src/voiceOneSession.test.js
import assert from 'node:assert/strict'
import { afterEach, beforeEach, mock, test } from 'node:test'
import VoiceController from './voice.js'
import { sttCommitTimeoutMs } from './turnPolicy.js'
import { clear, snapshot } from './voiceDebug.js'

const FRAME_MS = 20
const START = 1_800_000_000_000

// What realtime hears for each commit, in commit order, and what the
// batch transcriber hears from a backup copy. Synthetic words only.
const PIECES = ['Alex measured the deck twice', 'Sam cut the boards',
                'Dave sanded every edge', 'Mateo stacked the offcuts']
const BATCH = 'Sam carried the ladder to Fairhaven'

// ---- the browser, as far as voice.js reaches into it ----

// Every animation-frame callback is kept and run, so a second listening
// loop shows up as a second callback instead of overwriting the first.
let frameQueue = []
globalThis.requestAnimationFrame = (cb) => { frameQueue.push(cb) }

class FakeWebSocket {
  static OPEN = 1
  static all = []
  constructor(url) {
    this.url = url
    this.readyState = FakeWebSocket.OPEN
    this.sent = []
    FakeWebSocket.all.push(this)
  }
  send(s) {
    const m = JSON.parse(s)
    if (m.audio) return
    this.sent.push(m)
    if (m.commit) relay.heard(this, m.turn_id)
  }
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

// One room, one microphone: every session on the page hears the same
// speech. Each getUserMedia call hands out a new stream whose track can
// be stopped, so a test can count the live microphones.
const room = { voiced: false }
let streams
let micGate       // when set, the browser's mic prompt waits on it
Object.defineProperty(globalThis, 'navigator', {
  configurable: true,
  value: {
    userAgent: 'node',
    mediaDevices: {
      async getUserMedia() {
        if (micGate) await micGate
        const track = { enabled: true, live: true, stop() { this.live = false } }
        const s = { track, getTracks: () => [track], getAudioTracks: () => [track] }
        streams.push(s)
        return s
      },
    },
  },
})

let processors
globalThis.AudioContext = class {
  constructor() {
    this.state = 'running'
    this.sampleRate = 48000
    this.destination = {}
  }
  createMediaStreamSource() { return { connect() {} } }
  createAnalyser() {
    return {
      fftSize: 2048,
      frequencyBinCount: 1024,
      getFloatTimeDomainData(buf) { buf.fill(room.voiced ? 0.1 : 0) },
      // Speech-shaped: energy below 1.2 kHz, none up high.
      getByteFrequencyData(buf) { for (let i = 0; i < buf.length; i++) buf[i] = i < 52 ? 200 : 0 },
    }
  }
  createScriptProcessor() {
    const p = { connect() {}, disconnect() { processors.delete(p) } }
    processors.add(p)
    return p
  }
  resume() { return Promise.resolve() }
  close() { this.state = 'closed'; return Promise.resolve() }
}

// The backup recorder. Every recording holds some audio, so a backup
// transcription always has words to return.
globalThis.MediaRecorder = class {
  static isTypeSupported() { return true }
  constructor() { this.state = 'inactive'; this.mimeType = 'audio/webm' }
  start() {
    this.state = 'recording'
    this.ondataavailable?.({ data: new Blob(['opus frames']) })
  }
  stop() { this.state = 'inactive'; queueMicrotask(() => this.onstop?.()) }
}

// The realtime relay: answers each commit with that piece's words after
// `latency(i)` ms, except the commits listed in `silent`.
const relay = {
  latency: () => 400,
  silent: new Set(),
  queue: [],
  commits: [],
  heard(ws, turnId) {
    const i = this.commits.length
    this.commits.push({ turnId, at: Date.now() - START })
    if (this.silent.has(i)) return
    this.queue.push({ ws, turnId, due: Date.now() + this.latency(i), text: PIECES[i] || '' })
  },
  deliverDue() {
    const due = this.queue.filter((q) => q.due <= Date.now())
    this.queue = this.queue.filter((q) => q.due > Date.now())
    for (const q of due) {
      q.ws.onmessage?.({ data: JSON.stringify({ final: q.text, turn_id: q.turnId }) })
    }
  },
}

let posts
let sent          // every message any session on the page sent
let sessions
let ticksPerFrame // how many listening-loop ticks did work in each frame
const realDebug = console.debug
const realWarn = console.warn

beforeEach(() => {
  console.debug = () => {}
  console.warn = () => {}
  mock.timers.enable({ apis: ['setTimeout', 'Date'], now: START })
  clear()
  frameQueue = []
  FakeWebSocket.all = []
  streams = []
  micGate = null
  processors = new Set()
  relay.latency = () => 400
  relay.silent = new Set()
  relay.queue = []
  relay.commits = []
  posts = []
  sent = []
  sessions = []
  ticksPerFrame = []
  room.voiced = false
  globalThis.fetch = async (url) => {
    posts.push({ url, at: Date.now() - START })
    if (/\/api\/chats\/\d+\/stt$/.test(url)) {
      return { ok: true, status: 200, json: async () => ({ text: BATCH }) }
    }
    return { ok: true, status: 200, json: async () => ({ ok: true }) }
  }
})

afterEach(() => {
  for (const s of sessions) s.ctrl.stop()
  mock.timers.reset()
  console.debug = realDebug
  console.warn = realWarn
  delete globalThis.fetch
})

// A controller the way App builds one, started the way App starts it.
// The server saves every turn it's sent and runs a round that ends at
// once, so the hand-off watch sees each turn confirmed.
async function startSession(name) {
  const pending = []
  let ticks = 0
  const ctrl = new VoiceController({
    getChatId: () => 7,
    getParticipants: () => [],
    sendText: (text, turnId) => {
      sent.push({ from: name, text, turnId, at: Date.now() - START })
      pending.push(turnId)
    },
    onState: () => {},
    onError: () => {},
    onPartial: () => {},
  })
  // Count the listening loop's ticks that do work: _checkHandoffs runs
  // once per working tick, before any of the tick's early returns.
  const check = ctrl._checkHandoffs.bind(ctrl)
  ctrl._checkHandoffs = (now) => { ticks++; return check(now) }
  const s = {
    name,
    ctrl,
    pending,
    takeTicks() { const n = ticks; ticks = 0; return n },
    socket() { return FakeWebSocket.all.filter((w) => w === ctrl.sttWs)[0] },
  }
  sessions.push(s)
  await ctrl.start()
  ctrl.sttWs?.onopen?.()
  return s
}

// One animation frame for the whole page: the clock moves, the room
// is voiced or not, audio flows to every realtime socket, every queued
// loop callback runs, the relay answers what's due, and the server
// confirms what it was sent.
const pcm = new Float32Array(960)
async function frame(voicedAt) {
  mock.timers.tick(FRAME_MS)
  room.voiced = voicedAt(Date.now() - START)
  for (const p of [...processors]) p.onaudioprocess?.({ inputBuffer: { getChannelData: () => pcm } })
  const due = frameQueue
  frameQueue = []
  for (const cb of due) cb()
  ticksPerFrame.push(sessions.reduce((n, s) => n + s.takeTicks(), 0))
  relay.deliverDue()
  for (const s of sessions) {
    while (s.pending.length) {
      s.ctrl.onEvent({ type: 'user_saved', message: { id: 1, voice_turn_id: s.pending.shift() } })
      s.ctrl.onRoundDone()
    }
  }
  for (let i = 0; i < 4; i++) await new Promise((r) => setImmediate(r))
}

async function runUntil(untilMs, voicedAt) {
  while (Date.now() - START < untilMs) await frame(voicedAt)
}

// Speech from `from` to `to` ms, with the short breaks real speech has: a
// 60 ms dip every 1.5 s, too short to end a turn.
function speech(...spans) {
  return (t) => spans.some(([from, to]) => t >= from && t < to && (t - from) % 1500 < 1440)
}

const ring = (tag) => snapshot().filter((e) => e.tag === tag)
const sttPosts = () => posts.filter((p) => /\/api\/chats\/\d+\/stt$/.test(p.url))
const maxTicks = () => Math.max(0, ...ticksPerFrame)
const liveMics = () => streams.filter((s) => s.track.live).length

test('a second session on the page ends the first, so each turn is sent once', async () => {
  // The session the lock screen left running. The restart that signed the
  // page out also cut its realtime socket, so it fell back to its backup
  // recording, as the diagnostics showed.
  const left = await startSession('left behind')
  left.socket().onerror()
  assert.equal(left.ctrl.sttRealtime, false)
  // The owner unlocks and starts voice again.
  const fresh = await startSession('fresh')
  // One remark.
  await runUntil(9000, speech([500, 3500]))
  assert.deepEqual(sent.map((m) => [m.from, m.text]), [['fresh', PIECES[0]]])
  assert.equal(ring('finalizeUtterance').length, 1)
  assert.equal(ring('handoff:begin').length, 1)
  assert.equal(ring('stt:final').length, 1)
  assert.equal(ring('stt:batch').length, 0, 'no backup copy was transcribed')
  assert.equal(maxTicks(), 1, 'one listening loop on the page')
  assert.equal(left.ctrl.active, false, 'the older session ended')
  assert.equal(left.ctrl.state, 'off')
  assert.equal(liveMics(), 1, "the older session's microphone is off")
  // The ended session's diagnostics say why it stopped: ids only.
  assert.equal(ring('session:superseded').length, 1)
  assert.equal(fresh.ctrl.active, true)
})

test('an older session ending late leaves the new one running', async () => {
  const left = await startSession('left behind')
  const fresh = await startSession('fresh')
  // The old app's cleanup gets to it after the new session took over.
  left.ctrl.stop()
  assert.equal(fresh.ctrl.active, true)
  await runUntil(9000, speech([500, 3500]))
  assert.deepEqual(sent.map((m) => m.from), ['fresh'])
  // And a third start still ends the one that's live.
  const third = await startSession('third')
  assert.equal(fresh.ctrl.active, false)
  assert.equal(third.ctrl.active, true)
  assert.equal(liveMics(), 1)
})

test('a second tap on start opens no second microphone and no second loop', async () => {
  const s = await startSession('only')
  await s.ctrl.start()
  // Two taps racing: the second arrives while the first still waits on
  // the microphone.
  s.ctrl.stop()
  await Promise.all([s.ctrl.start(), s.ctrl.start()])
  assert.equal(streams.length, 2, 'one microphone per real start')
  assert.equal(liveMics(), 1)
  await runUntil(9000, speech([500, 3500]))
  assert.equal(maxTicks(), 1)
  assert.equal(sent.length, 1)
  assert.equal(ring('finalizeUtterance').length, 1)
})

test('a session stopped or replaced while the browser asks for the mic stays off', async () => {
  let allow
  micGate = new Promise((r) => { allow = r })
  // The app is replaced while its start waits on the mic prompt: its
  // cleanup ends the session before the prompt is answered.
  const first = new VoiceController({ getChatId: () => 7, getParticipants: () => [],
                                      sendText: () => { sent.push({ from: 'first' }) } })
  sessions.push({ ctrl: first, pending: [], takeTicks: () => 0 })
  const starting = first.start()
  first.stop()
  // A second session starts while another is still waiting on the mic.
  const second = new VoiceController({ getChatId: () => 7, getParticipants: () => [],
                                       sendText: () => { sent.push({ from: 'second' }) } })
  sessions.push({ ctrl: second, pending: [], takeTicks: () => 0 })
  const secondStarting = second.start()
  const third = new VoiceController({ getChatId: () => 7, getParticipants: () => [],
                                      sendText: () => { sent.push({ from: 'third' }) } })
  sessions.push({ ctrl: third, pending: [], takeTicks: () => 0 })
  const thirdStarting = third.start()
  allow()
  await Promise.all([starting, secondStarting, thirdStarting])
  assert.equal(first.active, false)
  assert.equal(second.active, false)
  assert.equal(third.active, true)
  assert.equal(liveMics(), 1, 'the mics the others were handed are off')
  assert.equal(frameQueue.length, 1, 'one listening loop')
})

test('starting the listening loop while one runs never adds a second', async () => {
  const s = await startSession('only')
  await runUntil(200, () => false)
  s.ctrl._vadLoop()
  s.ctrl._vadLoop()
  await runUntil(9000, speech([500, 3500]))
  assert.equal(maxTicks(), 1)
  assert.equal(ring('finalizeUtterance').length, 1)
  assert.equal(sent.length, 1)
})

test('a stop and a start inside one frame leave one loop', async () => {
  const s = await startSession('only')
  await runUntil(200, () => false)
  // End voice and start it again before the next frame: the old loop's
  // queued tick must not carry on beside the new loop.
  s.ctrl.stop()
  await s.ctrl.start()
  s.ctrl.sttWs?.onopen?.()
  await runUntil(9000, speech([500, 3500]))
  assert.equal(maxTicks(), 1)
  assert.equal(sent.length, 1)
})

test('after a long turn ends on the pause after a cut, the next remark is finalised and sent once', async () => {
  await startSession('only')
  // The later remark's realtime words come back after its backup copy has
  // already gone out, so both transcripts of it arrive.
  relay.latency = (i) => (i === 1 ? sttCommitTimeoutMs(3000) + 2000 : 400)
  const talk = speech([500, 11500], [16000, 19000])
  // The long turn: cut at the length limit, then ended by the pause.
  await runUntil(15500, talk)
  assert.equal(ring('turn:endAfterCut').length, 1)
  assert.equal(ring('vad:finalize').filter((e) => e.data.includes('pauseAfterCut')).length, 1)
  assert.deepEqual(sent.map((m) => [m.text, m.turnId]), [[PIECES[0], relay.commits[0].turnId]])
  const finalisedBefore = ring('finalizeUtterance').length
  const handoffsBefore = ring('handoff:begin').length
  // The later, ordinary remark.
  await runUntil(40000, talk)
  assert.equal(relay.commits.length, 2)
  assert.equal(ring('finalizeUtterance').length - finalisedBefore, 1, 'finalised once')
  assert.equal(ring('handoff:begin').length - handoffsBefore, 1, 'one hand-off')
  assert.equal(sttPosts().length, 1, 'the backup copy went out')
  assert.deepEqual(sent.map((m) => [m.text, m.turnId]),
                   [[PIECES[0], relay.commits[0].turnId], [BATCH, relay.commits[1].turnId]],
                   'one message per spoken turn, though both transcripts of the remark arrived')
  assert.equal(maxTicks(), 1, 'one listening loop throughout')
})

test('a rescue after a realtime error leaves one loop, and later turns go once', async () => {
  const s = await startSession('only')
  relay.silent = new Set([0])
  // The first remark is committed; the relay errors before its words
  // come back, and the backup copy rescues it (#451).
  await runUntil(6000, speech([500, 3500]))
  assert.equal(relay.commits.length, 1)
  s.socket().onmessage({ data: JSON.stringify({ error: 'upstream closed' }) })
  await runUntil(7000, () => false)
  assert.equal(ring('stt:rescue').length, 1)
  assert.deepEqual(sent.map((m) => [m.text, m.turnId]), [[BATCH, relay.commits[0].turnId]])
  // Later remarks go through the backup recording, each once.
  await runUntil(30000, speech([8000, 10000], [16000, 18500]))
  assert.equal(sent.length, 3)
  assert.equal(ring('finalizeUtterance').length, 3)
  assert.equal(maxTicks(), 1)
  assert.equal(liveMics(), 1)
})
