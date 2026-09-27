// Recording a voice on purpose (#504).
// Run: node --test frontend/src/voiceRecording.test.js
//
// What these pin: the passages are plain, roughly 30 seconds, and taken
// in turn per person so a second recording differs; the recording stops
// by itself at 45 seconds and never sends under 10; the meter moves with
// loudness; a live voice chat blocks a start; the state machine's every
// path; and the words after a recording, including "record again,
// ideally on another day or in another room" when variety is the gap.
import { test } from 'node:test'
import assert from 'node:assert/strict'
import {
  AGAIN_ELSEWHERE, IDLE, MAX_SECONDS, MIN_SECONDS, PASSAGES, TOO_SHORT,
  clock, joinChunks, lastPassage, liveMicWarning, meterLevel, micErrorMessage,
  nextPassageIndex, recordOutcome, recordStep, rememberPassage, savedLine,
  shouldAutoStop, timerLine,
} from './voiceRecording.js'

// ---- passages ----

test('there are several passages, each about 30 seconds read aloud', () => {
  assert.ok(PASSAGES.length >= 4)
  for (const p of PASSAGES) {
    const words = p.split(/\s+/).length
    // an easy reading pace is about 150 words a minute
    assert.ok(words >= 60 && words <= 90, `${words} words`)
    assert.ok(!p.includes('  '))
    // no dashes or semicolons, the house's plain English
    assert.ok(!/[;–—]/.test(p))
  }
  assert.equal(new Set(PASSAGES).size, PASSAGES.length)
})

test('passages are taken in turn, so a second recording differs', () => {
  assert.equal(nextPassageIndex(null), 0)
  assert.equal(nextPassageIndex(undefined), 0)
  assert.equal(nextPassageIndex(0), 1)
  assert.equal(nextPassageIndex(PASSAGES.length - 1), 0)
  assert.equal(nextPassageIndex(-1), 0)
  assert.equal(nextPassageIndex('junk'), 0)
  assert.equal(nextPassageIndex(2, 0), 0)
  for (let i = 0; i < PASSAGES.length; i++) {
    assert.notEqual(nextPassageIndex(i), i)
  }
})

test('the last passage is remembered per person', () => {
  let memory = {}
  assert.equal(lastPassage(memory, 'alex-1'), null)
  memory = rememberPassage(memory, 'alex-1', 2)
  memory = rememberPassage(memory, 'sam-2', 0)
  assert.equal(lastPassage(memory, 'alex-1'), 2)
  assert.equal(lastPassage(memory, 'sam-2'), 0)
  assert.equal(nextPassageIndex(lastPassage(memory, 'alex-1')), 3)
  // storage that came back empty or garbled starts again at the top
  assert.equal(lastPassage(null, 'alex-1'), null)
  assert.equal(lastPassage({ 'alex-1': 'x' }, 'alex-1'), null)
  assert.deepEqual(rememberPassage(null, 'dave-3', 1), { 'dave-3': 1 })
})

// ---- the clock and the meter ----

test('it stops by itself at 45 seconds', () => {
  assert.equal(MAX_SECONDS, 45)
  assert.equal(shouldAutoStop(44999), false)
  assert.equal(shouldAutoStop(45000), true)
  assert.equal(shouldAutoStop(60000), true)
  assert.equal(shouldAutoStop(undefined), false)
})

test('the microphone blocks join into one run, in order', () => {
  const joined = joinChunks([Float32Array.of(0.1, 0.2), null, new Float32Array(0),
                             Float32Array.of(0.3)])
  assert.deepEqual(Array.from(joined), Array.from(Float32Array.of(0.1, 0.2, 0.3)))
  assert.equal(joinChunks(undefined).length, 0)
})

test('the clock and the timer line', () => {
  assert.equal(clock(0), '0:00')
  assert.equal(clock(7400), '0:07')
  assert.equal(clock(65000), '1:05')
  assert.equal(timerLine(12000), '0:12 of about 0:30. It stops by itself at 0:45.')
})

test('the meter moves with loudness and stays between 0 and 1', () => {
  assert.equal(meterLevel(null), 0)
  assert.equal(meterLevel(new Float32Array(512)), 0)
  const tone = (amp) => Float32Array.from({ length: 512 }, (_, i) => amp * Math.sin(i / 5))
  const quiet = meterLevel(tone(0.01))
  const loud = meterLevel(tone(0.5))
  assert.ok(quiet > 0 && quiet < loud && loud <= 1)
  assert.equal(meterLevel(Float32Array.from({ length: 64 }, () => 1)), 1)
})

// ---- starting ----

test('a live voice chat blocks a start, and nothing live lets it go', () => {
  assert.equal(liveMicWarning([]), null)
  assert.equal(liveMicWarning(undefined), null)
  assert.match(liveMicWarning([{ sid: 'a', chat_id: 3 }]), /voice chat is still listening/)
})

test('a microphone error reads as a plain next step', () => {
  assert.match(micErrorMessage({ name: 'NotAllowedError' }), /wasn't allowed/)
  assert.match(micErrorMessage({ name: 'NotFoundError' }), /No microphone/)
  assert.match(micErrorMessage({ name: 'NotReadableError' }), /busy/)
  assert.match(micErrorMessage(new Error('x')), /couldn't start/)
  assert.match(micErrorMessage(null), /couldn't start/)
})

// ---- the state machine ----

const run = (events, from = IDLE) => events.reduce(recordStep, from)

test('the happy path: start, record, stop, save, check, done', () => {
  let s = run([{ type: 'start' }])
  assert.equal(s.phase, 'starting')
  s = recordStep(s, { type: 'mic_ready' })
  assert.deepEqual(s, { phase: 'recording', elapsedMs: 0, level: 0 })
  s = recordStep(s, { type: 'tick', elapsedMs: 31000, level: 0.6 })
  assert.deepEqual(s, { phase: 'recording', elapsedMs: 31000, level: 0.6 })
  s = recordStep(s, { type: 'stop' })
  assert.deepEqual(s, { phase: 'saving', elapsedMs: 31000, auto: false })
  const answer = { saved: 4, seconds: 30, state: 'checking', readiness: null,
                   summary: { state: 'ready', min_pieces: 20 } }
  s = recordStep(s, { type: 'saved', answer })
  assert.equal(s.phase, 'checking')
  // still building: stays checking
  assert.equal(recordStep(s, { type: 'readiness', payload: { state: 'checking' } }), s)
  const result = { ready: false, reason: 'one_day', pieces: 28, days: 1 }
  s = recordStep(s, { type: 'readiness',
                      payload: { state: 'current', readiness: result, summary: { state: 'ready' } } })
  assert.equal(s.phase, 'done')
  assert.equal(s.readinessState, 'current')
  assert.equal(s.readiness, result)
  assert.equal(s.answer, answer)
})

test('at 45 seconds a tick stops it by itself and saves', () => {
  const s = run([{ type: 'start' }, { type: 'mic_ready' },
                 { type: 'tick', elapsedMs: 45000, level: 0.4 }])
  assert.deepEqual(s, { phase: 'saving', elapsedMs: 45000, auto: true })
  // later ticks change nothing
  assert.equal(recordStep(s, { type: 'tick', elapsedMs: 46000 }), s)
})

test('stopping under 10 seconds sends nothing and says why', () => {
  assert.equal(MIN_SECONDS, 10)
  const s = run([{ type: 'start' }, { type: 'mic_ready' },
                 { type: 'tick', elapsedMs: 9000 }, { type: 'stop' }])
  assert.deepEqual(s, { phase: 'refused', message: TOO_SHORT })
})

test('a refusal, a blocked start and a failed mic each end with their message', () => {
  const saving = run([{ type: 'start' }, { type: 'mic_ready' },
                      { type: 'tick', elapsedMs: 20000 }, { type: 'stop' }])
  assert.deepEqual(recordStep(saving, { type: 'refused', message: 'no speech' }),
                   { phase: 'refused', message: 'no speech' })
  assert.deepEqual(run([{ type: 'start' }, { type: 'blocked', message: 'busy' }]),
                   { phase: 'blocked', message: 'busy' })
  assert.deepEqual(run([{ type: 'start' }, { type: 'mic_failed', message: 'denied' }]),
                   { phase: 'failed', message: 'denied' })
})

test('start works again from every end, and not mid-recording', () => {
  for (const end of [IDLE, { phase: 'blocked' }, { phase: 'failed' },
                     { phase: 'refused' }, { phase: 'done' }]) {
    assert.equal(recordStep(end, { type: 'start' }).phase, 'starting')
  }
  const recording = { phase: 'recording', elapsedMs: 5000, level: 0 }
  assert.equal(recordStep(recording, { type: 'start' }), recording)
  const saving = { phase: 'saving', elapsedMs: 30000, auto: false }
  assert.equal(recordStep(saving, { type: 'start' }), saving)
})

test('cancel goes back to idle from anywhere', () => {
  for (const s of [{ phase: 'starting' }, { phase: 'recording', elapsedMs: 3 },
                   { phase: 'saving' }, { phase: 'checking', answer: {} }]) {
    assert.equal(recordStep(s, { type: 'cancel' }), IDLE)
  }
})

test('events out of order change nothing', () => {
  assert.equal(recordStep(IDLE, { type: 'stop' }), IDLE)
  assert.equal(recordStep(IDLE, { type: 'mic_ready' }), IDLE)
  assert.equal(recordStep(IDLE, { type: 'saved', answer: {} }), IDLE)
  assert.equal(recordStep(IDLE, { type: 'readiness', payload: { state: 'current' } }), IDLE)
  assert.equal(recordStep(IDLE, { type: 'give_up' }), IDLE)
  assert.equal(recordStep(IDLE, { type: 'nonsense' }), IDLE)
  assert.equal(recordStep(undefined, undefined), IDLE)
})

test('a save with the test off, or already current, is done at once', () => {
  const saving = { phase: 'saving', elapsedMs: 30000, auto: false }
  const off = recordStep(saving, { type: 'saved', answer: { saved: 3, seconds: 28, state: 'off' } })
  assert.equal(off.phase, 'done')
  assert.equal(off.readinessState, 'off')
  const now = recordStep(saving, { type: 'saved', answer: {
    saved: 3, seconds: 28, state: 'current', readiness: { ready: true } } })
  assert.equal(now.phase, 'done')
  assert.deepEqual(now.readiness, { ready: true })
})

test('giving up on the check ends done, still checking', () => {
  const s = recordStep({ phase: 'checking', answer: { saved: 2, summary: { state: 'building' } } },
                       { type: 'give_up' })
  assert.equal(s.phase, 'done')
  assert.equal(s.readinessState, 'checking')
})

// ---- the words after ----

test('what was kept', () => {
  assert.equal(savedLine({ saved: 4, seconds: 29.6 }), 'Kept 4 clips, 30 seconds of speech.')
  assert.equal(savedLine({ saved: 1, seconds: 1 }), 'Kept 1 clip, 1 second of speech.')
  assert.equal(savedLine(null), 'Kept 0 clips, 0 seconds of speech.')
})

const summary = { state: 'ready', min_pieces: 20, share: 0.95, piece_seconds: 2 }
const done = (readiness, state = 'current') => ({
  phase: 'done', answer: {}, readinessState: state, readiness, summary })
const result = (over) => ({ ready: false, reason: 'one_day', pieces: 28,
                            named_right: 0, share_right: 0, named_as_other: 0,
                            days: 1, speech_seconds: 27, ...over })

test('a ready voice says so, and needs nothing more', () => {
  const out = recordOutcome(done(result({ ready: true, reason: 'ready', days: 2 })))
  assert.equal(out.text, 'Ready')
  assert.equal(out.ready, true)
  assert.match(out.advice, /name them on a day it hasn't heard them/)
})

test('when variety is the gap, it asks for another day or another room', () => {
  assert.equal(AGAIN_ELSEWHERE, 'Record again, ideally on another day or in another room.')
  const oneDay = recordOutcome(done(result({ reason: 'one_day' })))
  assert.equal(oneDay.text, 'Needs speech from another day')
  assert.equal(oneDay.advice, AGAIN_ELSEWHERE)
  const below = recordOutcome(done(result({ reason: 'share_below', days: 2, share_right: 0.8 })))
  assert.equal(below.text, 'Needs more speech: 80% of pieces named right, 95% needed')
  assert.equal(below.advice, AGAIN_ELSEWHERE)
  const other = recordOutcome(done(result({ reason: 'named_as_other', days: 2, named_as_other: 2 })))
  assert.match(other.text, /2 of 28 pieces sounded like someone else/)
  assert.match(other.advice, /Listen to their clips/)
  assert.ok(other.advice.endsWith(AGAIN_ELSEWHERE))
})

test('too few pieces asks for more speech, and no calibration asks for someone else', () => {
  const few = recordOutcome(done(result({ reason: 'too_few_pieces', pieces: 12 })))
  assert.equal(few.text, 'Needs more speech: 12 of 20 pieces')
  assert.equal(few.advice, 'Record again to add more speech.')
  const alone = recordOutcome(done(result({ reason: 'no_calibration', days: 2 })))
  assert.match(alone.text, /second person's voice/)
  assert.equal(alone.advice, 'Record someone else\'s voice too.')
})

test('off says nothing, and the other states say plainly where it is', () => {
  assert.deepEqual(recordOutcome(done(null, 'off')), { text: '', advice: '', ready: false })
  assert.deepEqual(recordOutcome(null), { text: '', advice: '', ready: false })
  assert.match(recordOutcome(done(null, 'checking')).text, /Still checking/)
  assert.match(recordOutcome(done(null, 'unavailable')).text, /can't be checked right now/)
  assert.match(recordOutcome(done(null, 'failed')).text, /can't be checked right now/)
  assert.equal(recordOutcome(done(null, 'current')).text, 'Not checked yet.')
})
