// Voice readiness lines on the Voices page (#482 stage 2).
// Run: node --test frontend/src/voiceReadiness.test.js
//
// What these pin: nothing shows while the test is off; a ready voice
// reads "Ready"; a voice short of pieces reads "Needs more speech: N of 20
// pieces"; each other reason the backend gives has its own plain line;
// seconds of speech ride beside it; and the hover title says what ready
// means using the backend's own rules.
import { test } from 'node:test'
import assert from 'node:assert/strict'
import { READINESS_DEFAULTS, readinessExplainer, readinessLine } from './voiceReadiness.js'

const SUMMARY = { state: 'ready', min_pieces: 20, share: 0.95, bar: 0.9, piece_seconds: 2 }

const result = (over = {}) => ({
  ready: false, reason: 'too_few_pieces', pieces: 12, named_right: 12,
  share_right: 1, named_as_other: 0, days: 2, speech_seconds: 14.2, ...over,
})

test('nothing shows while the readiness test is off or unknown', () => {
  assert.equal(readinessLine(result(), { state: 'off' }), null)
  assert.equal(readinessLine(result(), null), null)
  assert.equal(readinessLine(null, undefined), null)
})

test('a ready voice reads Ready, with its seconds of speech', () => {
  const line = readinessLine(result({ ready: true, reason: 'ready', pieces: 40,
                                      share_right: 1, speech_seconds: 52.4 }), SUMMARY)
  assert.equal(line.text, 'Ready')
  assert.equal(line.detail, '52s of speech')
  assert.equal(line.ready, true)
})

test('a voice short of pieces says how many it has of how many', () => {
  const line = readinessLine(result({ pieces: 12, speech_seconds: 8.25 }), SUMMARY)
  assert.equal(line.text, 'Needs more speech: 12 of 20 pieces')
  assert.equal(line.detail, '8.3s of speech')
  assert.equal(line.ready, false)
  // the backend's own minimum wins over the default
  assert.equal(readinessLine(result({ pieces: 3 }), { ...SUMMARY, min_pieces: 25 }).text,
    'Needs more speech: 3 of 25 pieces')
})

test('each other reason has its own plain line', () => {
  assert.equal(readinessLine(result({ reason: 'one_day', pieces: 30 }), SUMMARY).text,
    'Needs speech from another day')
  assert.equal(readinessLine(result({ reason: 'no_calibration', pieces: 30 }), SUMMARY).text,
    'Not checked yet: needs a second person\'s voice to compare with')
  assert.equal(readinessLine(result({ reason: 'named_as_other', pieces: 30,
                                      named_as_other: 2 }), SUMMARY).text,
    'Needs more speech: 2 of 30 pieces sounded like someone else')
  assert.equal(readinessLine(result({ reason: 'share_below', pieces: 40,
                                      share_right: 0.875 }), SUMMARY).text,
    'Needs more speech: 87% of pieces named right, 95% needed')
})

test('before the first build, and when it cannot run, the line says so', () => {
  assert.equal(readinessLine(null, { ...SUMMARY, state: 'waiting' }).text,
    'Checking whether this voice is ready')
  assert.equal(readinessLine(null, { ...SUMMARY, state: 'building' }).text,
    'Checking whether this voice is ready')
  assert.equal(readinessLine(null, { ...SUMMARY, state: 'unavailable' }).text,
    'Readiness can\'t be checked right now')
  // built, but this person has no speech stored at all
  assert.equal(readinessLine(null, SUMMARY).text, 'Needs more speech: 0 of 20 pieces')
})

test('no seconds shown when there are none', () => {
  assert.equal(readinessLine(result({ speech_seconds: 0 }), SUMMARY).detail, '')
})

test('the hover title says what ready means, with the backend rules', () => {
  const title = readinessLine(result(), SUMMARY).title
  assert.match(title, /2-second pieces/)
  assert.match(title, /95 times in 100/)
  assert.match(title, /never as anyone else/)
  assert.match(title, /at least 20 pieces/)
  assert.match(title, /that day's recordings left out/)
  assert.match(readinessExplainer({ min_pieces: 30, share: 0.9 }), /90 times in 100.*30 pieces/)
  assert.deepEqual(READINESS_DEFAULTS, { min_pieces: 20, share: 0.95, piece_seconds: 2 })
})
