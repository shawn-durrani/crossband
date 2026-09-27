// The rules for how a reply's audio reaches the audio element
// (streamFeed.js). An iPhone has no MediaSource, so every reply used to
// wait for its whole speech stream before it played. Safari on an iPhone
// has had ManagedMediaSource since iOS 17.1, which streams like
// MediaSource but can ask the page to stop and start feeding it, and can
// evict audio it holds.
// Pins: MediaSource comes first where it takes MP3, then
// ManagedMediaSource, then the held path; a browser that can't answer
// the MP3 question holds; chunks are held after `endstreaming` and go in
// again on `startstreaming` or when the element runs dry, and never
// before the first `endstreaming`; the pump appends, ends the source, or
// waits; audio evicted behind the playhead changes nothing, and a gap at
// the playhead is jumped when audio waits beyond it; and only the managed
// path falls back to the held one, only before a word is heard, and
// never over a blocked autoplay.
// Run: node --test frontend/src/streamFeed.test.js
import assert from 'node:assert/strict'
import { test } from 'node:test'
import {
  HELD, MANAGED, MSE, evictionPlan, fallsBack, feedAfter, gapSkip, newFeed,
  playbackPath, pumpStep, rangesOf, starvedPlan,
} from './streamFeed.js'

const takes = (yes) => class { static isTypeSupported(t) { return yes && t === 'audio/mpeg' } }

// ---------- which path ----------

test('MediaSource that takes MP3 streams, as on a desktop browser', () => {
  assert.equal(playbackPath({ MediaSource: takes(true) }), MSE)
})

test('with both, as on Safari on a Mac, MediaSource wins', () => {
  assert.equal(playbackPath({ MediaSource: takes(true), ManagedMediaSource: takes(true) }), MSE)
})

test('ManagedMediaSource that takes MP3 streams, as on an iPhone with iOS 17.1 or later', () => {
  assert.equal(playbackPath({ ManagedMediaSource: takes(true) }), MANAGED)
  assert.equal(playbackPath({ MediaSource: takes(false), ManagedMediaSource: takes(true) }), MANAGED)
})

test('neither, or neither taking MP3, holds the reply for its whole stream', () => {
  assert.equal(playbackPath({}), HELD)
  assert.equal(playbackPath({ ManagedMediaSource: takes(false) }), HELD)
  assert.equal(playbackPath({ MediaSource: takes(false) }), HELD)
  assert.equal(playbackPath({ MediaSource: undefined, ManagedMediaSource: {} }), HELD)
  assert.equal(playbackPath(undefined), HELD)
})

test('a browser that throws when asked about MP3 holds', () => {
  const Throws = class { static isTypeSupported() { throw new Error('no') } }
  assert.equal(playbackPath({ ManagedMediaSource: Throws }), HELD)
})

// ---------- feeding a managed source ----------

test('a new feed is not held, though `streaming` starts false', () => {
  assert.equal(newFeed().held, false)
})

test('endstreaming holds the feed, and startstreaming or running dry resumes it', () => {
  const held = feedAfter(newFeed(), 'endstreaming')
  assert.equal(held.held, true)
  assert.equal(feedAfter(held, 'startstreaming').held, false)
  assert.equal(feedAfter(held, 'starved').held, false)
  assert.equal(feedAfter(held, 'something else'), held)
})

test('the pump appends, ends the source, or waits', () => {
  const base = { stopped: false, ready: true, queued: 1, held: false, ended: false, open: true }
  assert.equal(pumpStep(base), 'append')
  assert.equal(pumpStep({ ...base, held: true }), 'wait', 'held chunks stay out')
  assert.equal(pumpStep({ ...base, held: true, ended: true }), 'wait',
    'a held stream does not end with chunks still out')
  assert.equal(pumpStep({ ...base, queued: 0, ended: true }), 'end')
  assert.equal(pumpStep({ ...base, queued: 0, ended: true, held: true }), 'end',
    'nothing left to hold, so the source can end')
  assert.equal(pumpStep({ ...base, queued: 0, ended: true, open: false }), 'wait')
  assert.equal(pumpStep({ ...base, queued: 0 }), 'wait')
  assert.equal(pumpStep({ ...base, ready: false }), 'wait', 'mid-update or no buffer yet')
  assert.equal(pumpStep({ ...base, stopped: true }), 'wait', 'a cut reply takes nothing in')
  assert.equal(pumpStep({ ...base, stopped: true, queued: 0, ended: true }), 'wait')
})

// ---------- audio the browser dropped ----------

const ranges = (...pairs) => ({
  length: pairs.length, start: (i) => pairs[i][0], end: (i) => pairs[i][1],
})

test('rangesOf reads a TimeRanges into pairs', () => {
  assert.deepEqual(rangesOf(ranges([0, 1.5], [2, 4])), [[0, 1.5], [2, 4]])
  assert.deepEqual(rangesOf(undefined), [])
})

test('audio evicted behind the playhead was heard, and nothing changes', () => {
  const plan = evictionPlan({ removed: [[0, 4]], buffered: [[4, 12]], currentTime: 5 })
  assert.deepEqual(plan, { unheard: false, seekTo: null })
})

test('audio evicted ahead of the playhead is lost, and playback carries on to the gap', () => {
  const plan = evictionPlan({ removed: [[8, 10]], buffered: [[0, 8], [10, 12]], currentTime: 5 })
  assert.deepEqual(plan, { unheard: true, seekTo: null })
})

test('a gap at the playhead jumps to the audio beyond it', () => {
  const plan = evictionPlan({ removed: [[4, 8]], buffered: [[0, 4], [8, 12]], currentTime: 5 })
  assert.deepEqual(plan, { unheard: true, seekTo: 8 })
})

test('with nothing left beyond the gap, there is nowhere to jump', () => {
  const plan = evictionPlan({ removed: [[4, 12]], buffered: [[0, 4]], currentTime: 5 })
  assert.deepEqual(plan, { unheard: true, seekTo: null })
})

test('gapSkip only jumps from outside buffered audio, and only forward', () => {
  assert.equal(gapSkip([[0, 4], [6, 9]], 2), null)
  assert.equal(gapSkip([[0, 4], [6, 9]], 4), 6, 'at the end of one range, with another ahead')
  assert.equal(gapSkip([[0, 4], [6, 9], [7, 8]], 5), 6)
  assert.equal(gapSkip([[0, 4]], 4), null, 'an underrun at the end is not a gap')
  assert.equal(gapSkip([], 0), null, 'nothing buffered yet')
})

test('an element that runs dry while chunks are held resumes the feed', () => {
  assert.deepEqual(starvedPlan({ held: true, buffered: [[0, 30]], currentTime: 30 }),
    { resume: true, seekTo: null })
  assert.deepEqual(starvedPlan({ held: false, buffered: [[0, 3]], currentTime: 3 }),
    { resume: false, seekTo: null }, 'a plain underrun waits for the next chunk')
  assert.deepEqual(starvedPlan({ held: false, buffered: [[0, 3], [5, 9]], currentTime: 3 }),
    { resume: false, seekTo: 5 })
})

// ---------- falling back to the held path ----------

test('the managed path falls back before a word is heard', () => {
  for (const cause of ['no-open', 'no-buffer', 'element-error', 'stalled']) {
    assert.equal(fallsBack({ path: MANAGED, started: false, cause }), true, cause)
  }
  assert.equal(fallsBack({ path: MANAGED, started: false, cause: 'play-rejected',
                           errorName: 'NotSupportedError' }), true)
})

test('it never falls back once heard, over a blocked autoplay, or on desktop', () => {
  assert.equal(fallsBack({ path: MANAGED, started: true, cause: 'element-error' }), false)
  assert.equal(fallsBack({ path: MANAGED, started: false, cause: 'play-rejected',
                           errorName: 'NotAllowedError' }), false)
  assert.equal(fallsBack({ path: MSE, started: false, cause: 'no-buffer' }), false)
  assert.equal(fallsBack({ path: HELD, started: false, cause: 'element-error' }), false)
})
