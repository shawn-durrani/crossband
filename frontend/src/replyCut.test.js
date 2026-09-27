// The rules for cutting a reply off (replyCut.js), as pure functions.
// On 27 September an iPhone's held reply couldn't be cut: the mic stayed
// shut, the barge-in fired on every frame (about 230 times, an abort
// each), and the reply played 38 seconds late once voice was switched off.
// Pins: a cut reply keeps no audio and never plays; the held player plays
// only a finished, uncut reply with audio in it; a cut reply never takes
// its turn, whatever the drop flag says; and a barge-in cuts once however
// many frames its speech lasts, opens the utterance once, and is armed
// again only by something new to cut.
// Run: node --test frontend/src/replyCut.test.js
import assert from 'node:assert/strict'
import { test } from 'node:test'
import { afterCut, bargeInFrame, heldReplyPlays, keepsAudio, newBargeIn, rearm,
         takesTurn } from './replyCut.js'

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
  state = rearm()
  assert.equal(run(state, Array(50).fill(true)).cuts, 1, 'a new reply or round is cut')
})
