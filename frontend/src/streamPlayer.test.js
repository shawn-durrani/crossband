// One reply's player (streamPlayer.js) on both of its paths, with a fake
// shared audio element. Without MediaSource, as on an iPhone, the player
// holds a reply's audio until its speech stream ends and plays it as one
// clip. With MediaSource, as in desktop Chrome, it streams each chunk in
// as it arrives. On 27 September a cut couldn't reach the held player
// while it waited, so the reply went on counting as playing and later
// played after voice was switched off.
// Pins: stop() ends a held player's wait at once and it never plays; audio
// and a stream end arriving after stop() change nothing; a stop mid-clip
// pauses it; a player stopped before its turn never touches the shared
// element; the MediaSource path still streams chunks as they arrive, and a
// stop pauses it and takes no more; stop closes the reply's socket once;
// and a finished reply's stop never pauses the next reply's audio.
// Run: node --test frontend/src/streamPlayer.test.js
import assert from 'node:assert/strict'
import { afterEach, test } from 'node:test'
import { StreamPlayer } from './streamPlayer.js'

const CHUNK = 'SUQz'

function sink(src = '') {
  return {
    src,
    currentTime: 0,
    plays: [],
    pauses: [],
    play() { this.plays.push(this.src); return Promise.resolve() },
    pause() { this.pauses.push(this.src) },
  }
}

async function flush() {
  for (let i = 0; i < 4; i++) await new Promise((r) => setImmediate(r))
}

// Whether a promise has settled yet, without waiting on it.
async function settled(p) {
  let done = false
  p.then(() => { done = true })
  await flush()
  return done
}

// ---- MediaSource, for the desktop path ----

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
  constructor() { this.readyState = 'closed'; this.listeners = []; this.ended = false }
  addEventListener(type, fn) { if (type === 'sourceopen') this.listeners.push(fn) }
  addSourceBuffer() { this.sb = new FakeSourceBuffer(); return this.sb }
  endOfStream() { this.readyState = 'ended'; this.ended = true }
  open() { this.readyState = 'open'; for (const fn of this.listeners) fn() }
}
const realCreateObjectURL = URL.createObjectURL

function withMediaSource() {
  globalThis.MediaSource = FakeMediaSource
  URL.createObjectURL = (obj) => (obj instanceof FakeMediaSource
    ? 'blob:mse' : realCreateObjectURL(obj))
}

afterEach(() => {
  delete globalThis.MediaSource
  URL.createObjectURL = realCreateObjectURL
})

// ---------- the held path (no MediaSource) ----------

test('the held path plays one clip once the stream ends', async () => {
  const el = sink()
  const p = new StreamPlayer(el)
  assert.equal(p.useMse, false)
  p.push(CHUNK)
  const playing = p.play()
  await flush()
  assert.deepEqual(el.plays, [], 'nothing plays while the stream runs')
  p.push(CHUNK)
  p.end()
  await flush()
  assert.equal(el.plays.length, 1)
  assert.match(el.plays[0], /^blob:/)
  el.onended()
  assert.equal(await settled(playing), true)
})

test('a stop while the held player waits ends the wait at once, unplayed', async () => {
  const el = sink()
  const p = new StreamPlayer(el)
  p.push(CHUNK)
  const playing = p.play()
  await flush()
  p.stop()
  assert.equal(await settled(playing), true, 'play() returned without the stream ending')
  assert.deepEqual(el.plays, [])
  assert.equal(el.src, '', 'the shared element was never given the reply')
})

test('audio and the stream end arriving after a stop change nothing', async () => {
  const el = sink()
  const p = new StreamPlayer(el)
  p.push(CHUNK)
  const playing = p.play()
  p.stop()
  p.push(CHUNK)
  p.end()
  await flush()
  assert.equal(p.chunks.length, 0, 'the late chunk was dropped')
  assert.deepEqual(el.plays, [], 'the late-play case: never played')
  assert.equal(await settled(playing), true)
})

test('a stop mid-clip pauses it and returns', async () => {
  const el = sink()
  const p = new StreamPlayer(el)
  p.push(CHUNK)
  p.end()
  const playing = p.play()
  await flush()
  assert.equal(el.plays.length, 1)
  p.stop()
  assert.deepEqual(el.pauses.at(-1), el.plays[0])
  assert.equal(await settled(playing), true)
})

test('a player stopped before its turn never touches the shared element', async () => {
  const el = sink('blob:the-reply-now-playing')
  const p = new StreamPlayer(el)
  p.push(CHUNK)
  p.stop()
  await p.play()
  assert.equal(el.src, 'blob:the-reply-now-playing')
  assert.deepEqual(el.pauses, [])
  assert.deepEqual(el.plays, [])
})

test('stop closes the reply\'s socket once, and abandon is a stop', () => {
  const p = new StreamPlayer(sink())
  let closes = 0
  p.onStop = () => { closes++ }
  p.stop()
  p.stop()
  assert.equal(closes, 1)
  const q = new StreamPlayer(sink())
  q.abandon()
  assert.equal(q.stopped && q.abandoned, true)
})

test('a finished reply\'s stop never pauses the next reply', async () => {
  const el = sink()
  const p = new StreamPlayer(el)
  p.push(CHUNK)
  p.end()
  const playing = p.play()
  await flush()
  el.onended()
  await playing
  el.src = 'blob:the-next-reply'
  const before = el.pauses.length
  p.stop()
  assert.equal(el.pauses.length, before, 'the next reply plays on')
})

// ---------- the MediaSource path ----------

test('with MediaSource each chunk streams in as it arrives, and plays before the end', async () => {
  withMediaSource()
  const el = sink()
  const p = new StreamPlayer(el)
  assert.equal(p.useMse, true)
  p.push(CHUNK)
  p.play()
  assert.deepEqual(el.plays, ['blob:mse'], 'playback began with the stream still open')
  p.mediaSource.open()
  await flush()
  assert.equal(p.mediaSource.sb.appended, 1)
  p.push(CHUNK)
  await flush()
  assert.equal(p.mediaSource.sb.appended, 2)
  p.end()
  await flush()
  assert.equal(p.mediaSource.ended, true)
  el.onended()
})

test('with MediaSource a stop pauses the reply and takes no more audio', async () => {
  withMediaSource()
  const el = sink()
  const p = new StreamPlayer(el)
  p.push(CHUNK)
  const playing = p.play()
  p.mediaSource.open()
  await flush()
  p.stop()
  assert.deepEqual(el.pauses.at(-1), 'blob:mse')
  assert.equal(await settled(playing), true)
  p.push(CHUNK)
  p.end()
  await flush()
  assert.equal(p.mediaSource.sb.appended, 1)
  assert.equal(p.mediaSource.ended, false)
})

test('with MediaSource a stop before the source opens takes nothing in', async () => {
  withMediaSource()
  const p = new StreamPlayer(sink())
  p.push(CHUNK)
  p.play()
  p.stop()
  p.mediaSource.open()
  await flush()
  assert.equal(p.mediaSource.sb, undefined, 'no buffer was ever added')
})
