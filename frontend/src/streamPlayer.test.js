// One reply's player (streamPlayer.js) on all three of its paths, with a
// fake shared audio element. Without a streaming source, as on an older
// iPhone, the player holds a reply's audio until its speech stream ends
// and plays it as one clip. With MediaSource, as in desktop Chrome, it
// streams each chunk in as it arrives. With ManagedMediaSource, as on an
// iPhone with iOS 17.1 or later, it streams the same way and also does
// what that source asks. On 27 September a cut couldn't reach the held
// player while it waited, so the reply went on counting as playing and
// later played after voice was switched off.
// Pins: stop() ends a held player's wait at once and it never plays; audio
// and a stream end arriving after stop() change nothing; a stop mid-clip
// pauses it; a player stopped before its turn never touches the shared
// element; the MediaSource path still streams chunks as they arrive, and a
// stop pauses it and takes no more; stop closes the reply's socket once;
// and a finished reply's stop never pauses the next reply's audio.
// The managed path: remote playback is off before the source is attached,
// chunks stream in and the reply is heard before its stream ends, chunks
// wait after `endstreaming` until `startstreaming` or the element runs
// dry, an eviction at the playhead jumps the gap, a stop takes nothing
// more in whatever the source asks, and a source that fails before a
// word is heard plays the reply whole once its stream ends, but never
// over a blocked autoplay and never once the reply has been heard.
// Run: node --test frontend/src/streamPlayer.test.js
import assert from 'node:assert/strict'
import { afterEach, mock, test } from 'node:test'
import { StreamPlayer } from './streamPlayer.js'
import { SOURCE_OPEN_MS } from './streamFeed.js'

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
  delete globalThis.ManagedMediaSource
  URL.createObjectURL = realCreateObjectURL
  mock.timers.reset()
})

// ---------- the held path (no MediaSource) ----------

test('the held path plays one clip once the stream ends', async () => {
  const el = sink()
  const p = new StreamPlayer(el)
  assert.equal(p.path, 'held')
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
  assert.equal(p.path, 'mse')
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

// ---------- the ManagedMediaSource path (an iPhone, iOS 17.1 and later) ----------

class Emitter {
  constructor() { this.handlers = {} }
  addEventListener(type, fn) { (this.handlers[type] ||= []).push(fn) }
  emit(type, ev = {}) { for (const fn of this.handlers[type] || []) fn(ev) }
}
const timeRanges = (pairs) => ({
  length: pairs.length, start: (i) => pairs[i][0], end: (i) => pairs[i][1],
})
class FakeManagedSourceBuffer extends Emitter {
  constructor() { super(); this.updating = false; this.appended = 0; this.ranges = [] }
  get buffered() { return timeRanges(this.ranges) }
  appendBuffer() {
    this.appended++
    this.updating = true
    queueMicrotask(() => { this.updating = false; this.emit('updateend') })
  }
}
class FakeManagedMediaSource extends Emitter {
  static isTypeSupported(type) { return type === 'audio/mpeg' }
  constructor() { super(); this.readyState = 'closed'; this.ended = false; this.streaming = false }
  addSourceBuffer() { this.sb = new FakeManagedSourceBuffer(); return this.sb }
  endOfStream() { this.readyState = 'ended'; this.ended = true }
  // WebKit opens a managed source only once remote playback is off.
  open(el) {
    if (!el.disableRemotePlayback) return false
    this.readyState = 'open'
    this.emit('sourceopen')
    return true
  }
  stream(on) { this.streaming = on; this.emit(on ? 'startstreaming' : 'endstreaming') }
}

function withManagedMediaSource() {
  globalThis.ManagedMediaSource = FakeManagedMediaSource
  URL.createObjectURL = (obj) => (obj instanceof FakeManagedMediaSource
    ? 'blob:mms' : realCreateObjectURL(obj))
}

// The shared element, recording whether remote playback was already off
// each time a source was attached.
function phoneSink({ rejectWith } = {}) {
  const el = sink()
  let src = ''
  el.remoteOffAtAttach = []
  Object.defineProperty(el, 'src', {
    get: () => src,
    set: (v) => { src = v; el.remoteOffAtAttach.push(!!el.disableRemotePlayback) },
  })
  if (rejectWith) {
    el.play = function () { this.plays.push(this.src); return Promise.reject(rejectWith) }
  }
  return el
}

function logged(p) {
  const lines = []
  p.log = (tag, data) => lines.push({ tag, ...data })
  return lines
}

test('on an iPhone each chunk streams in as it arrives, and the reply is heard before its end', async () => {
  withManagedMediaSource()
  const el = phoneSink()
  const p = new StreamPlayer(el)
  assert.equal(p.path, 'managed')
  let heardAt = null
  p.push(CHUNK)
  p.play(undefined, () => { heardAt = { ended: p.ended } })
  assert.deepEqual(el.plays, ['blob:mms'], 'playback began with the stream still open')
  assert.deepEqual(el.remoteOffAtAttach, [true], 'remote playback was off before the source went on')
  assert.equal(p.mediaSource.open(el), true, 'so the source can open')
  await flush()
  assert.equal(p.mediaSource.sb.appended, 1)
  p.push(CHUNK)
  await flush()
  assert.equal(p.mediaSource.sb.appended, 2)
  el.onplaying()
  assert.deepEqual(heardAt, { ended: false }, 'the trace marks playback before the stream ends')
  p.end()
  await flush()
  assert.equal(p.mediaSource.ended, true)
  el.onended()
})

test('on an iPhone whose source does not take MP3 the reply is held', () => {
  globalThis.ManagedMediaSource = class { static isTypeSupported() { return false } }
  const p = new StreamPlayer(sink())
  assert.equal(p.path, 'held')
  assert.equal(p.mediaSource, undefined)
})

test('after endstreaming chunks wait for startstreaming, and the source ends only once they are in', async () => {
  withManagedMediaSource()
  const el = phoneSink()
  const p = new StreamPlayer(el)
  const lines = logged(p)
  p.push(CHUNK)
  p.play()
  p.mediaSource.open(el)
  await flush()
  const ms = p.mediaSource
  assert.equal(ms.sb.appended, 1)
  ms.stream(false)
  p.push(CHUNK)
  p.push(CHUNK)
  p.end()
  await flush()
  assert.equal(ms.sb.appended, 1, 'nothing goes in while the source has enough')
  assert.equal(ms.ended, false, 'and the source waits for the held chunks')
  ms.stream(true)
  await flush()
  assert.equal(ms.sb.appended, 3)
  assert.equal(ms.ended, true)
  assert.deepEqual(lines.map((l) => l.tag), ['tts:hold', 'tts:resume'])
  p.stop()
})

test('an element that runs dry while chunks wait takes them without a startstreaming', async () => {
  withManagedMediaSource()
  const el = phoneSink()
  const p = new StreamPlayer(el)
  p.push(CHUNK)
  p.play()
  p.mediaSource.open(el)
  await flush()
  const ms = p.mediaSource
  ms.sb.ranges = [[0, 30]]
  ms.stream(false)
  p.push(CHUNK)
  await flush()
  assert.equal(ms.sb.appended, 1)
  el.currentTime = 30
  el.onwaiting()
  await flush()
  assert.equal(ms.sb.appended, 2)
  p.stop()
})

test('an eviction behind the playhead changes nothing, and a gap at the playhead is jumped', async () => {
  withManagedMediaSource()
  const el = phoneSink()
  const p = new StreamPlayer(el)
  const lines = logged(p)
  p.push(CHUNK)
  p.play()
  p.mediaSource.open(el)
  await flush()
  const sb = p.mediaSource.sb
  sb.ranges = [[6, 12]]
  el.currentTime = 7
  sb.emit('bufferedchange', { addedRanges: timeRanges([]), removedRanges: timeRanges([[0, 6]]) })
  assert.equal(el.currentTime, 7, 'heard audio went, and playback carries on')
  assert.deepEqual(lines, [])
  sb.ranges = [[0, 4], [8, 12]]
  el.currentTime = 5
  sb.emit('bufferedchange', { addedRanges: timeRanges([]), removedRanges: timeRanges([[4, 8]]) })
  assert.equal(el.currentTime, 8, 'the playhead jumped to the audio beyond the gap')
  assert.deepEqual(lines.map((l) => l.tag), ['tts:evicted'])
  p.stop()
})

test('a stop pauses an iPhone reply, and nothing the source asks puts more in', async () => {
  withManagedMediaSource()
  const el = phoneSink()
  const p = new StreamPlayer(el)
  p.push(CHUNK)
  const playing = p.play()
  p.mediaSource.open(el)
  await flush()
  p.stop()
  assert.equal(el.pauses.at(-1), 'blob:mms')
  assert.equal(await settled(playing), true)
  const ms = p.mediaSource
  p.push(CHUNK)
  ms.stream(false)
  ms.stream(true)
  el.onwaiting?.()
  p.end()
  await flush()
  assert.equal(ms.sb.appended, 1)
  assert.equal(ms.ended, false)
})

test('a stop while chunks wait on endstreaming throws them away', async () => {
  withManagedMediaSource()
  const el = phoneSink()
  const p = new StreamPlayer(el)
  p.play()
  p.mediaSource.open(el)
  await flush()
  const ms = p.mediaSource
  ms.stream(false)
  p.push(CHUNK)
  p.push(CHUNK)
  p.stop()
  ms.stream(true)
  await flush()
  assert.equal(ms.sb.appended, 0, 'the held chunks never went in')
})

test('a stop before the managed source opens takes nothing in', async () => {
  withManagedMediaSource()
  const el = phoneSink()
  const p = new StreamPlayer(el)
  p.push(CHUNK)
  p.play()
  p.stop()
  p.mediaSource.open(el)
  await flush()
  assert.equal(p.mediaSource.sb, undefined)
})

test('a source that never opens plays the reply whole once its stream ends', async () => {
  mock.timers.enable({ apis: ['setTimeout', 'setInterval'] })
  withManagedMediaSource()
  const el = phoneSink()
  const p = new StreamPlayer(el)
  const lines = logged(p)
  p.push(CHUNK)
  const playing = p.play()
  mock.timers.tick(SOURCE_OPEN_MS)
  await flush()
  assert.deepEqual(lines.map((l) => [l.tag, l.cause]), [['tts:fallback', 'no-open']])
  assert.equal(p.path, 'held')
  p.push(CHUNK)
  p.end()
  await flush()
  assert.equal(el.plays.length, 2)
  assert.match(el.plays[1], /^blob:nodedata/, 'the whole reply, as one clip')
  assert.equal(p.chunks.length, 2, 'every chunk, from before and after the fallback')
  el.onended()
  assert.equal(await settled(playing), true)
})

test('an element error before a word is heard plays the reply whole', async () => {
  withManagedMediaSource()
  const el = phoneSink()
  const p = new StreamPlayer(el)
  p.push(CHUNK)
  p.play()
  p.mediaSource.open(el)
  p.push(CHUNK)
  await flush()
  assert.equal(p.mediaSource.sb.appended, 2)
  el.error = { code: 4 }
  el.onerror()
  el.error = null
  p.end()
  await flush()
  assert.equal(el.plays.length, 2)
  assert.equal(p.chunks.length, 2, 'the chunks already in the source were kept')
  p.stop()
})

test('a stalled source with the stream over and nothing heard plays the reply whole', async () => {
  mock.timers.enable({ apis: ['setTimeout', 'setInterval'] })
  withManagedMediaSource()
  const el = phoneSink()
  const p = new StreamPlayer(el)
  const lines = logged(p)
  p.push(CHUNK)
  p.play()
  p.mediaSource.open(el)
  await flush()
  p.end()
  await flush()
  mock.timers.tick(6000)
  await flush()
  assert.deepEqual(lines.map((l) => [l.tag, l.cause]), [['tts:fallback', 'stalled']])
  assert.equal(el.plays.length, 2)
})

test('once a word is heard an error just ends the reply', async () => {
  withManagedMediaSource()
  const el = phoneSink()
  const p = new StreamPlayer(el)
  p.push(CHUNK)
  const playing = p.play()
  p.mediaSource.open(el)
  await flush()
  el.onplaying()
  el.error = { code: 3 }
  el.onerror()
  assert.equal(await settled(playing), true)
  p.end()
  await flush()
  assert.deepEqual(el.plays, ['blob:mms'], 'never started again from the top')
})

test('a blocked autoplay on an iPhone is reported, not fallen back from', async () => {
  withManagedMediaSource()
  const blocked = Object.assign(new Error('blocked'), { name: 'NotAllowedError' })
  const el = phoneSink({ rejectWith: blocked })
  const p = new StreamPlayer(el)
  const failures = []
  p.push(CHUNK)
  const playing = p.play((err) => failures.push(err.name))
  assert.equal(await settled(playing), true)
  assert.deepEqual(failures, ['NotAllowedError'])
  assert.equal(p.path, 'managed')
})

test('a reply cut while it waits after a fallback never plays', async () => {
  mock.timers.enable({ apis: ['setTimeout', 'setInterval'] })
  withManagedMediaSource()
  const el = phoneSink()
  const p = new StreamPlayer(el)
  p.push(CHUNK)
  const playing = p.play()
  mock.timers.tick(SOURCE_OPEN_MS)
  await flush()
  p.stop()
  p.end()
  assert.equal(await settled(playing), true)
  assert.deepEqual(el.plays, ['blob:mms'], 'the whole clip never played')
})
