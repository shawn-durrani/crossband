// The silence-start speculative hint's firing rule (#28 PR-B): fires once
// per silence gap after the confirmation window, re-arms on resumed speech,
// and never fires outside an utterance or without the realtime stream.

import test from 'node:test'
import assert from 'node:assert/strict'
import { SPECULATIVE_SILENCE_MS, speculativeStep } from './speculative.js'

const base = { inUtterance: true, streaming: true, voiced: false }

test('fires once when silence crosses the confirmation window', () => {
  let s = speculativeStep(false, { ...base, silenceMs: SPECULATIVE_SILENCE_MS - 1 })
  assert.deepEqual(s, { fired: false, fire: false })
  s = speculativeStep(s.fired, { ...base, silenceMs: SPECULATIVE_SILENCE_MS })
  assert.deepEqual(s, { fired: true, fire: true })
  // deeper silence in the same gap: already fired, stays quiet
  s = speculativeStep(s.fired, { ...base, silenceMs: SPECULATIVE_SILENCE_MS + 900 })
  assert.deepEqual(s, { fired: true, fire: false })
})

test('resumed speech re-arms, and the next gap fires a fresh hint', () => {
  let s = speculativeStep(true, { ...base, voiced: true, silenceMs: 0 })
  assert.deepEqual(s, { fired: false, fire: false })
  s = speculativeStep(s.fired, { ...base, silenceMs: SPECULATIVE_SILENCE_MS + 10 })
  assert.equal(s.fire, true)
})

test('never fires outside an utterance or without realtime streaming', () => {
  assert.deepEqual(
    speculativeStep(true, { ...base, inUtterance: false, silenceMs: 5000 }),
    { fired: false, fire: false })
  assert.deepEqual(
    speculativeStep(false, { ...base, streaming: false, silenceMs: 5000 }),
    { fired: false, fire: false })
})

test('holds unfired state through shallow silence', () => {
  const s = speculativeStep(false, { ...base, silenceMs: 100 })
  assert.deepEqual(s, { fired: false, fire: false })
})

test('non-numeric silence never fires', () => {
  assert.equal(speculativeStep(false, { ...base, silenceMs: undefined }).fire,
               false)
})
