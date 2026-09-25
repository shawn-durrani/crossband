// Only one transcription wins per committed utterance (#85/#104). A salvaged
// commit drops its late realtime final, which was the doubled-turn race. A
// final that beats the timer stands the salvage down. Ids match exactly, with
// FIFO fallback for id-less finals. Continuation commits carry their buffer
// dispatch, the pending window is bounded, and reset clears the socket's
// flight.
import assert from 'node:assert/strict'
import test from 'node:test'

import { newLedger, onCommit, onFinal, onSalvage, resetLedger } from './commitLedger.js'

test('the doubled-turn race: a salvaged commit drops its late final', () => {
  const l = newLedger()
  onCommit(l, 't1')
  assert.equal(onSalvage(l, 't1'), 'send')     // batch takes over, sends
  onCommit(l, 't2')                            // next turn resets nothing
  assert.equal(onFinal(l, 't1'), null)         // t1's LATE final: dropped -
  // this exact sequence used to send t1's text twice (#85)
  assert.deepEqual(onFinal(l, 't2'), { turnId: 't2', dispatch: 'send' })
})

test('a final that beats the timer wins and the salvage stands down', () => {
  const l = newLedger()
  onCommit(l, 't1')
  assert.deepEqual(onFinal(l, 't1'), { turnId: 't1', dispatch: 'send' })
  assert.equal(onSalvage(l, 't1'), null)       // timer lost: do nothing
})

test('an unknown or repeated id never sends', () => {
  const l = newLedger()
  onCommit(l, 'a')
  assert.equal(onFinal(l, 'stray'), null)      // names no commit we know
  assert.equal(onFinal(l, 'a').turnId, 'a')
  assert.equal(onFinal(l, 'a'), null)          // repeat: consumed
})

test('a continuation commit carries its buffer dispatch to the winner', () => {
  const l = newLedger()
  onCommit(l, 'seg1', 'buffer')
  assert.deepEqual(onFinal(l, 'seg1'), { turnId: 'seg1', dispatch: 'buffer' })
  onCommit(l, 'seg2', 'buffer')
  assert.equal(onSalvage(l, 'seg2'), 'buffer') // salvage buffers too
})

test('an id-less final falls back to oldest-unconsumed order', () => {
  const l = newLedger()
  onCommit(l, 'a')
  onCommit(l, 'b')
  assert.equal(onSalvage(l, 'a'), 'send')
  assert.equal(onFinal(l).turnId, 'b')         // a consumed, b wins
  assert.equal(onFinal(l), null)
})

test('the pending window is bounded against never-arriving finals', () => {
  const l = newLedger()
  for (let i = 0; i < 12; i++) onCommit(l, `t${i}`)
  assert.ok(l.pending.length <= 8)
  assert.equal(onFinal(l, 't0'), null)         // aged out, cannot send
})

test('reset clears everything in flight', () => {
  const l = newLedger()
  onCommit(l, 'x')
  resetLedger(l)
  assert.equal(onFinal(l, 'x'), null)
  assert.equal(onSalvage(l, 'x'), null)
})
