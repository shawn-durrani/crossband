// How far into a reply you've heard (replyTimeline.js), from ElevenLabs'
// word timings (#634). A live check on 12 October showed how they arrive:
// in batches of a few words, each timed from 0, with the accent tag timed
// like any other text, and drifting about a second behind ten seconds of
// audio. Pins: batches lay end to end; the tag never reaches the text;
// a character counts as heard once its middle has played; once the audio
// is all in, its length stretches the timeline, but never by a silly
// amount; a resume backs up to the start of the phrase, or of the word
// when the phrase began too long ago, and never forward.
// Run: node --test frontend/src/replyTimeline.test.js
import assert from 'node:assert/strict'
import { test } from 'node:test'
import { MAX_REWIND_S, addTimings, base64Bytes, heardChars, newTimeline,
         phraseStart } from './replyTimeline.js'

// A batch timing every character at `ms` each, from 0.
function batch(text, ms = 50) {
  const chars = [...text]
  return { chars, start: chars.map((_, i) => i * ms), dur: chars.map(() => ms) }
}

function timeline(...texts) {
  return texts.reduce((tl, t) => addTimings(tl, batch(t)), newTimeline())
}

test('batches lay end to end, each from where the last one ended', () => {
  const tl = timeline('Soak the', ' crumbs')
  assert.equal(tl.text, 'Soak the crumbs')
  assert.equal(tl.end, 15 * 50)
  assert.equal(tl.starts[8], 8 * 50, 'the second batch starts where the first ended')
})

test('the accent tag never reaches the text, even split across batches', () => {
  const tl = timeline('[Australian ', 'accent] Soak the', ' crumbs. [Australian accent] Then')
  assert.equal(tl.text, ' Soak the crumbs.  Then')
  assert.equal(tl.starts.length, tl.text.length)
})

test('a character counts as heard once its middle has played', () => {
  const tl = timeline('Hi there')
  assert.equal(heardChars(tl, 0), 0)
  assert.equal(heardChars(tl, 0.025), 1)
  assert.equal(heardChars(tl, 0.024), 0)
  assert.equal(heardChars(tl, 10), 8)
})

test("once the audio is all in, its length stretches the timeline", () => {
  const tl = timeline('Hi there') // 400 ms of timings
  assert.equal(heardChars(tl, 0.3), 6)
  assert.equal(heardChars(tl, 0.3, { audioSeconds: 0.48 }), 5, 'stretched by 1.2')
  assert.equal(heardChars(tl, 0.3, { audioSeconds: 0.2 }), 6, 'never shrunk')
  assert.equal(heardChars(tl, 0.3, { audioSeconds: 4 }), 6, 'never stretched past 1.5')
})

test('a resume backs up to the start of the phrase that was playing', () => {
  const text = 'Sand it first, then wipe it down.'
  const tl = timeline(text)
  const at = (i) => (i * 50) / 1000
  const wipe = text.indexOf('wipe')
  const then = text.indexOf('then')
  assert.equal(phraseStart(tl, at(wipe + 2)), at(then), 'back over "then wi"')
  assert.equal(phraseStart(tl, at(then)), at(then), 'already at the start')
  assert.equal(phraseStart(tl, at(3)), 0, 'the first phrase starts the reply')
})

test('a phrase that began too long ago backs up to the word instead', () => {
  const text = 'This is one very long phrase with no comma anywhere in it at all'
  const tl = addTimings(newTimeline(), batch(text, 100)) // 100 ms a character
  const at = (i) => (i * 100) / 1000
  const all = text.indexOf('all')
  assert.ok(at(all) > MAX_REWIND_S)
  assert.equal(phraseStart(tl, at(all + 1)), at(all))
})

test('a resume never goes forward, and needs timings to go anywhere', () => {
  assert.equal(phraseStart(newTimeline(), 2), null)
  const tl = timeline('Hi there')
  assert.ok(phraseStart(tl, 0.3) <= 0.3)
})

test('a base64 chunk is three bytes for every four characters', () => {
  assert.equal(base64Bytes('SUQz'), 3)
  assert.equal(base64Bytes('SUQ='), 2)
  assert.equal(base64Bytes('SQ=='), 1)
  assert.equal(base64Bytes(''), 0)
})
