// The rules for cutting a reply off (replyCut.js), as pure functions.
// On 27 September an iPhone's held reply couldn't be cut: the mic stayed
// shut, the barge-in fired on every frame (about 230 times, an abort
// each), and the reply played 38 seconds late once voice was switched off.
// Pins: a cut reply keeps no audio and never plays; the held player plays
// only a finished, uncut reply with audio in it; a cut reply never takes
// its turn, whatever the drop flag says; and a barge-in cuts once however
// many frames its speech lasts, opens the utterance once, and is armed
// again only by something new to cut.
// On 11 October two replies were cut by sounds with no words in them
// (#620). Pins: with live transcription a sound holds the reply instead
// of cutting it; words that cut (interruptWords.js) cut; a sound that stops, or runs on too long,
// without a word is let go; one let go is listened for again once it
// stops; and a hold survives something new to cut.
// Run: node --test frontend/src/replyCut.test.js
import assert from 'node:assert/strict'
import { test } from 'node:test'
import { HOLD_MAX_MS, HOLD_QUIET_MS, afterCut, bargeInFrame, heldReplyPlays,
         keepsAudio, newBargeIn, rearm, takesTurn } from './replyCut.js'

// ---------- a reply's audio ----------

test('a reply keeps its audio until it is cut off', () => {
  assert.equal(keepsAudio({ stopped: false }), true)
  assert.equal(keepsAudio({ stopped: true }), false)
})

test('the held player plays only a finished, uncut reply with audio', () => {
  const chunks = [new Uint8Array(1)]
  assert.equal(heldReplyPlays({ stopped: false, ended: true, chunks }), true)
  assert.equal(heldReplyPlays({ stopped: false, ended: false, chunks }), false,
               'still streaming')
  assert.equal(heldReplyPlays({ stopped: false, ended: true, chunks: [] }), false,
               'nothing to play')
  assert.equal(heldReplyPlays({ stopped: true, ended: true, chunks }), false,
               'cut off, then its stream ended: the late-play case')
})

test('a cut reply never takes its turn, even once the drop flag has cleared', () => {
  assert.equal(takesTurn({ stopped: false }, { dropQueue: false }), true)
  assert.equal(takesTurn({ stopped: false }, { dropQueue: true }), false)
  assert.equal(takesTurn({ stopped: true }, { dropQueue: false }), false)
  assert.equal(takesTurn({ stopped: false, abandoned: true }, { dropQueue: false }), false)
})

// ---------- talking over the AIs ----------

// Frames of the gated listening loop, as the loop runs them: the first
// frame of steady speech opens the utterance, later ones find it open.
function run(state, frames) {
  let s = state
  let speaking = false
  let cuts = 0
  let opens = 0
  for (const confirmed of frames) {
    const f = bargeInFrame(s, { confirmed, speaking })
    s = f.state
    if (f.cut) cuts++
    if (f.open) { opens++; speaking = true }
  }
  return { state: s, cuts, opens }
}

test('speech that goes on after the cut cuts once, however many frames', () => {
  // The incident: the gate stayed shut and the speech went on for about
  // 230 frames.
  const { cuts, opens, state } = run(newBargeIn(), Array(230).fill(true))
  assert.equal(cuts, 1)
  assert.equal(opens, 1)
  assert.equal(state.armed, false)
})

test('frames without steady speech neither cut nor open', () => {
  const f = bargeInFrame(newBargeIn(), { confirmed: false, speaking: false })
  assert.deepEqual([f.cut, f.open, f.state.armed], [false, false, true])
})

test('an utterance already open is kept, not restarted, by the cut', () => {
  // Someone was already talking when a reply started over them.
  const f = bargeInFrame(newBargeIn(), { confirmed: true, speaking: true })
  assert.equal(f.cut, true)
  assert.equal(f.open, false)
})

test('a spent cut still opens a new utterance, so the words are heard', () => {
  // The stop button spent the cut, and someone starts talking while the
  // round is still ending.
  const f = bargeInFrame(afterCut(), { confirmed: true, speaking: false })
  assert.equal(f.cut, false)
  assert.equal(f.open, true)
})

test('something new to cut arms it again', () => {
  let { state } = run(newBargeIn(), Array(50).fill(true))
  assert.equal(run(state, Array(50).fill(true)).cuts, 0, 'still the same barge-in')
  state = rearm(state)
  assert.equal(run(state, Array(50).fill(true)).cuts, 1, 'a new reply or round is cut')
})

// ---------- a sound with no words in it ----------

const FRAME = 20

// Frames of the gated loop with live transcription on, one every 20 ms
// from `from`. `voiced(t)` says whether a frame reads as speech, steady
// enough once it has for 500 ms; `cutAt` is when words that cut showed.
function listen(state, { from = 0, until, voiced, cutAt = 0 }) {
  let s = state
  let speaking = false
  const did = { holds: 0, cuts: 0, releases: 0, opens: 0, at: {} }
  let since = null
  for (let now = from; now < until; now += FRAME) {
    const v = voiced(now)
    since = v ? (since ?? now) : null
    const confirmed = since !== null && now - since >= 500
    // The word shows on the frame it arrives, not before.
    const shown = cutAt && now >= cutAt ? cutAt : 0
    const f = bargeInFrame(s, { confirmed, voiced: v, speaking, live: true, cutAt: shown, now })
    s = f.state
    for (const k of ['hold', 'cut', 'release', 'open']) {
      if (!f[k]) continue
      did[`${k}s`]++
      did.at[k] ??= now
    }
    if (f.open) speaking = true
    if (f.release) speaking = false
  }
  return { state: s, ...did }
}

const between = (a, b) => (t) => t >= a && t < b

test('with live transcription a sound holds the reply instead of cutting it', () => {
  const r = listen(newBargeIn(), { until: 800, voiced: between(0, 2000) })
  assert.equal(r.holds, 1)
  assert.equal(r.cuts, 0)
  assert.equal(r.opens, 1, 'the sound is captured, so its words can be heard')
  assert.equal(r.at.hold, 500)
})

test('words that cut, heard in the held sound, cut the reply', () => {
  const r = listen(newBargeIn(), { until: 2000, voiced: between(0, 2000), cutAt: 760 })
  assert.equal(r.holds, 1)
  assert.equal(r.cuts, 1)
  assert.equal(r.at.cut, 760)
  assert.equal(r.releases, 0)
  assert.equal(r.state.armed, false, 'the cut is spent')
})

test('words that cut from before the hold do not cut it', () => {
  const r = listen(newBargeIn(), { from: 1000, until: 2400, voiced: between(1000, 1800),
                                   cutAt: 900 })
  assert.equal(r.cuts, 0)
  assert.equal(r.releases, 1)
})

test('a held sound that stops without a word is let go', () => {
  const r = listen(newBargeIn(), { until: 3000, voiced: between(0, 1200) })
  assert.equal(r.holds, 1)
  assert.equal(r.cuts, 0)
  assert.equal(r.releases, 1)
  // The last voiced frame was at 1180.
  assert.equal(r.at.release, 1180 + HOLD_QUIET_MS)
  assert.equal(r.state.armed, true, 'listened for again once it stopped')
})

test('a short gap inside the held sound does not let it go', () => {
  const r = listen(newBargeIn(), { until: 1500,
                                   voiced: (t) => t < 2000 && !(t >= 700 && t < 1000) })
  assert.equal(r.releases, 0)
})

test('a held sound that runs on without a word is let go, and not held again until it stops', () => {
  const r = listen(newBargeIn(), { until: 9000, voiced: between(0, 8000) })
  assert.equal(r.holds, 1, 'held once, however long it runs')
  assert.equal(r.releases, 1)
  assert.equal(r.at.release, 500 + HOLD_MAX_MS)
  assert.equal(r.cuts, 0)
  assert.equal(r.state.armed, true, 'armed again once it stopped')
  const again = listen(r.state, { from: 9000, until: 10000, voiced: between(9000, 10000) })
  assert.equal(again.holds, 1, 'a new sound is held again')
})

test('a sound let go is held again when there is something new to cut', () => {
  const ran = listen(newBargeIn(), { until: 5000, voiced: between(0, 8000) })
  assert.equal(ran.state.letGo, true)
  const next = listen(rearm(ran.state), { from: 5000, until: 5100, voiced: between(0, 8000) })
  assert.equal(next.holds, 0, 'not until the sound is steady again')
  const f = bargeInFrame(rearm(ran.state), { confirmed: true, speaking: false, live: true, now: 5100 })
  assert.equal(f.hold, true, 'a new reply starting is something new to cut')
})

test('something new to cut keeps a hold that is under way', () => {
  const r = listen(newBargeIn(), { until: 700, voiced: between(0, 2000) })
  assert.ok(r.state.held)
  assert.equal(rearm(r.state), r.state)
})

test('without live transcription a sound still cuts at once', () => {
  const f = bargeInFrame(newBargeIn(), { confirmed: true, speaking: false, live: false, now: 500 })
  assert.deepEqual([f.cut, f.hold, f.open], [true, false, true])
})
