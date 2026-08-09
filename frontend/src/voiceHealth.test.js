// The voice health strip's derivations (#28). Run:
// node --test frontend/src/voiceHealth.test.js
//
// What these pin: every matcher state maps to an honest plain-English
// readout (degraded states say the cloud does the work, not that voice is
// broken); the mode line tells room-on/solo/ambient apart; the live pulse
// formats path and latency exactly ("local · 227ms", "cloud · 1.9s",
// "pending" only while a session is live); known-voice lines reuse the
// people snapshot's sufficiency; and junk input renders nothing rather
// than crashing.
import { test } from 'node:test'
import assert from 'node:assert/strict'
import {
  healthStrip, knownVoiceLines, matcherReadout, modeReadout, pulseReadout,
} from './voiceHealth.js'

test('matcher readouts cover every state and mark the degraded ones', () => {
  assert.equal(matcherReadout('ready').label, 'matcher ready')
  assert.equal(matcherReadout('ready').degraded, false)
  assert.equal(matcherReadout('fetching').label, 'fetching model…')
  assert.equal(matcherReadout('fetching').degraded, true)
  assert.equal(matcherReadout('cold').label, 'matcher idle')
  assert.equal(matcherReadout('unavailable').label, 'cloud fallback')
  assert.equal(matcherReadout('unavailable').degraded, true)
  assert.equal(matcherReadout('disabled').label, 'matcher off')
  // an unknown state reads as the honest worst case, never a crash
  assert.equal(matcherReadout('what').label, 'cloud fallback')
  assert.equal(matcherReadout(undefined).label, 'cloud fallback')
})

test('degraded readouts say the cloud takes over, not that voice broke', () => {
  for (const state of ['fetching', 'unavailable', 'disabled']) {
    assert.match(matcherReadout(state).title, /cloud/i)
  }
})

test('the mode line tells room on, solo and ambient apart', () => {
  assert.equal(
    modeReadout({ chat: { room_mode: true, ambient_off: false, roster_count: 3 } }).label,
    'room on · 3 in the room')
  assert.equal(
    modeReadout({ chat: { room_mode: true, ambient_off: false, roster_count: 0 } }).label,
    'room on')
  const solo = modeReadout({ chat: { room_mode: false, ambient_off: true, roster_count: 0 } })
  assert.equal(solo.label, 'solo')
  assert.match(solo.title, /solo mode/)
  assert.equal(
    modeReadout({ chat: { room_mode: false, ambient_off: false, roster_count: 0 } }).label,
    'ambient listening')
  // no chat block: no line
  assert.equal(modeReadout({ chat: null }), null)
  assert.equal(modeReadout(null), null)
})

test('the pulse formats path and latency exactly', () => {
  assert.equal(pulseReadout({ path: 'local', ms: 227, age_s: 1 }).label,
    'local · 227ms')
  assert.equal(pulseReadout({ path: 'cloud', ms: 1900, age_s: 2 }).label,
    'cloud · 1.9s')
  assert.equal(pulseReadout({ path: 'local', ms: 999.4, age_s: 0 }).label,
    'local · 999ms')
  assert.equal(pulseReadout({ path: 'cloud', ms: 1000, age_s: 0 }).label,
    'cloud · 1.0s')
})

test('the pulse says pending only while a session is live', () => {
  assert.equal(pulseReadout(null, true).label, 'pending')
  assert.equal(pulseReadout(null, false), null)
  // junk decisions degrade to pending/null, never crash
  assert.equal(pulseReadout({ path: 'teleport', ms: 5 }, true).label, 'pending')
  assert.equal(pulseReadout({ path: 'local', ms: 'fast' }, false), null)
})

test('known-voice lines show display names and honest progress', () => {
  const people = [
    { name: 'Mateo', preferred_name: 'Matteo', seconds: 8.1, sufficient: true },
    { name: 'Sam', preferred_name: 'Sam', seconds: 3.2, sufficient: false },
  ]
  assert.deepEqual(knownVoiceLines(people, 6), [
    { name: 'Matteo', done: true, label: 'remembered' },
    { name: 'Sam', done: false, label: '3.2s of 6s' },
  ])
  assert.deepEqual(knownVoiceLines(null, 6), [])
  assert.deepEqual(knownVoiceLines([null, 42, {}], 6), [])
})

test('the strip assembles all four blocks, and nothing without a snapshot', () => {
  const strip = healthStrip({
    health: {
      matcher: 'ready',
      chat: { room_mode: true, ambient_off: false, roster_count: 2 },
      last_decision: { path: 'local', ms: 120, age_s: 3 },
    },
    people: [{ name: 'Sam', seconds: 7, sufficient: true }],
    sufficientSeconds: 6,
    sessionActive: true,
  })
  assert.equal(strip.matcher.label, 'matcher ready')
  assert.equal(strip.mode.label, 'room on · 2 in the room')
  assert.equal(strip.pulse.label, 'local · 120ms')
  assert.deepEqual(strip.voices, [{ name: 'Sam', done: true, label: 'remembered' }])
  assert.equal(healthStrip({ health: null, people: [], sessionActive: true }), null)
})
