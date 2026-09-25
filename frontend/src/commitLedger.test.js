// Only one transcription wins per committed utterance (#85/#104). A salvaged
// commit drops its late realtime final, which was the doubled-turn race. A
// final that beats the timer stands the salvage down. Ids match exactly, with
// FIFO fallback for id-less finals. Continuation commits carry their buffer
// dispatch, the pending window is bounded, and reset clears the socket's
// flight. #304: when realtime transcription fails, the commits still in
// flight are handed to the batch path once, and the rescue rule picks what
// to salvage. #453: a long turn that ends while its cut piece is in flight
// makes that piece the turn's last.
import assert from 'node:assert/strict'
import test from 'node:test'

import { endTurn, newLedger, onCommit, onFinal, onSalvage, rescuePlan, resetLedger,
         takeInFlight } from './commitLedger.js'

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

test('a failure takes what is in flight, once, and no late final sends it (#304)', () => {
  const l = newLedger()
  onCommit(l, 'done', 'send', 1800)
  assert.equal(onFinal(l, 'done').turnId, 'done')  // already transcribed
  onCommit(l, 'seg', 'buffer', 12000)
  onCommit(l, 'end', 'send', 3000)
  assert.deepEqual(takeInFlight(l), [
    { turnId: 'seg', dispatch: 'buffer', speechMs: 12000 },
    { turnId: 'end', dispatch: 'send', speechMs: 3000 },
  ])
  assert.deepEqual(takeInFlight(l), [])         // taken once
  assert.equal(onFinal(l, 'end'), null)          // the late final drops
  assert.equal(onFinal(l), null)                 // id-less too
  assert.equal(onSalvage(l, 'seg'), null)        // and the timer stands down
})

test('a commit without a speech length keeps the old shape', () => {
  const l = newLedger()
  onCommit(l, 't1', 'send', undefined)
  assert.deepEqual(onFinal(l, 't1'), { turnId: 't1', dispatch: 'send' })
})

test('the rescue rule: a finished turn in flight is salvaged and sent (#304)', () => {
  assert.equal(rescuePlan([]), null)             // nothing in flight: today's path
  assert.equal(rescuePlan(undefined), null)
  assert.deepEqual(
    rescuePlan([{ turnId: 'end', dispatch: 'send', speechMs: 2100 }]),
    { turnId: 'end', dispatch: 'send', speechMs: 2100 })
  // Capped segments before it share the one recording and go with it,
  // under the finished turn's id, whether or not someone is talking.
  const flight = [{ turnId: 'seg', dispatch: 'buffer', speechMs: 12000 },
                  { turnId: 'end', dispatch: 'send', speechMs: 3000 }]
  for (const speaking of [false, true]) {
    assert.deepEqual(rescuePlan(flight, { speaking }),
                     { turnId: 'end', dispatch: 'send', speechMs: 15000 })
  }
})

test('the rescue rule: capped segments alone wait for the turn, or buffer in a pause', () => {
  const flight = [{ turnId: 'seg1', dispatch: 'buffer', speechMs: 12000 },
                  { turnId: 'seg2', dispatch: 'buffer' }]
  // Still talking: the batch path ends this turn from the same recording.
  assert.equal(rescuePlan(flight, { speaking: true }), null)
  // A pause: buffer them, as their realtime transcripts would have.
  assert.deepEqual(rescuePlan(flight, { speaking: false }),
                   { turnId: 'seg2', dispatch: 'buffer', speechMs: 12000 })
})

test('a turn that ends while its cut piece is in flight makes it the last piece (#453)', () => {
  const l = newLedger()
  onCommit(l, 'cut', 'buffer', 11000)
  assert.deepEqual(endTurn(l, 'cut'), { turnId: 'cut', dispatch: 'send', speechMs: 11000 })
  // Whichever copy wins now sends the turn: the realtime final...
  assert.equal(onFinal(l, 'cut').dispatch, 'send')
  // ...or the salvage timer, or a rescue.
  const l2 = newLedger()
  onCommit(l2, 'cut', 'buffer')
  endTurn(l2, 'cut')
  assert.equal(onSalvage(l2, 'cut'), 'send')
  const l3 = newLedger()
  onCommit(l3, 'cut', 'buffer')
  endTurn(l3, 'cut')
  assert.equal(rescuePlan(takeInFlight(l3)).dispatch, 'send')
})

test('ending a turn changes nothing once the cut piece is no longer waiting', () => {
  const l = newLedger()
  onCommit(l, 'cut', 'buffer')
  onFinal(l, 'cut')                              // its words already came in
  assert.equal(endTurn(l, 'cut'), null)
  onCommit(l, 'salvaged', 'buffer')
  onSalvage(l, 'salvaged')                       // the batch path owns it
  assert.equal(endTurn(l, 'salvaged'), null)
  assert.equal(endTurn(l, 'unknown'), null)
  assert.equal(endTurn(l, null), null)
  // Only the named piece changes: an earlier one still buffers.
  onCommit(l, 'first', 'buffer')
  onCommit(l, 'second', 'buffer')
  endTurn(l, 'second')
  assert.equal(onFinal(l, 'first').dispatch, 'buffer')
  assert.equal(onFinal(l, 'second').dispatch, 'send')
})
