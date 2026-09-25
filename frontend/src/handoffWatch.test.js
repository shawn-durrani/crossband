// #304: the hand-off watch behind the automatic diagnostics save. Pins: a
// turn the server never confirms reports once, past the bound and not
// before; a confirmed turn never reports; the stage it stopped at rides
// the report; the watch is bounded and a session end clears it.
// Run: node --test frontend/src/handoffWatch.test.js
import assert from 'node:assert/strict'
import { test } from 'node:test'
import {
  HANDOFF_STALL_MS, MAX_WATCHED, STAGE_EMPTY, STAGE_SENT, STAGE_TRANSCRIBING,
  handoffBegan, handoffConfirmed, handoffStage, newHandoffWatch,
  resetHandoffWatch, takeStalledHandoffs,
} from './handoffWatch.js'

const T0 = 1_000_000

test('a turn the server never confirms reports once, past the bound', () => {
  const w = newHandoffWatch()
  handoffBegan(w, 'turn-a', T0)
  assert.deepEqual(takeStalledHandoffs(w, T0 + HANDOFF_STALL_MS), [],
                   'at the bound itself it is still a slow turn')
  const stalled = takeStalledHandoffs(w, T0 + HANDOFF_STALL_MS + 1)
  assert.deepEqual(stalled, [{ turnId: 'turn-a', stage: STAGE_TRANSCRIBING,
                               waitedMs: HANDOFF_STALL_MS + 1 }])
  // One stall, one report, however often the VAD tick asks afterwards.
  assert.deepEqual(takeStalledHandoffs(w, T0 + HANDOFF_STALL_MS * 5), [])
})

test('a normal turn, confirmed in time, never reports', () => {
  const w = newHandoffWatch()
  handoffBegan(w, 'turn-b', T0)
  handoffStage(w, 'turn-b', STAGE_SENT)
  const closed = handoffConfirmed(w, 'turn-b')
  assert.equal(closed.turnId, 'turn-b')
  assert.equal(closed.reported, false)
  assert.deepEqual(takeStalledHandoffs(w, T0 + HANDOFF_STALL_MS * 10), [])
  assert.equal(w.turns.length, 0)
})

test('the stage the hand-off stopped at rides the report', () => {
  const w = newHandoffWatch()
  handoffBegan(w, 'turn-c', T0)
  handoffStage(w, 'turn-c', STAGE_EMPTY)
  const [s] = takeStalledHandoffs(w, T0 + HANDOFF_STALL_MS + 500)
  assert.equal(s.stage, STAGE_EMPTY)
})

test('a later turn confirming does not clear an earlier stranded one', () => {
  // The field shape: Alex's turn never arrives, Sam speaks again and that
  // turn goes through. The first turn is still a stall and still reports.
  const w = newHandoffWatch()
  handoffBegan(w, 'turn-alex', T0)
  handoffBegan(w, 'turn-sam', T0 + 8000)
  handoffConfirmed(w, 'turn-sam')
  const stalled = takeStalledHandoffs(w, T0 + HANDOFF_STALL_MS + 1)
  assert.deepEqual(stalled.map((s) => s.turnId), ['turn-alex'])
})

test('a confirmation after a report is still recognised as late', () => {
  const w = newHandoffWatch()
  handoffBegan(w, 'turn-d', T0)
  takeStalledHandoffs(w, T0 + HANDOFF_STALL_MS + 1)
  const closed = handoffConfirmed(w, 'turn-d')
  assert.equal(closed.reported, true)
  assert.equal(handoffConfirmed(w, 'turn-d'), null, 'already closed')
})

test('unknown, empty and repeated ids change nothing', () => {
  const w = newHandoffWatch()
  handoffBegan(w, '', T0)
  handoffBegan(w, null, T0)
  handoffBegan(w, 'turn-e', T0)
  handoffBegan(w, 'turn-e', T0 + 20000) // a repeat keeps the first start
  assert.equal(w.turns.length, 1)
  assert.equal(handoffConfirmed(w, undefined), null)
  assert.equal(handoffConfirmed(w, 'never-watched'), null)
  handoffStage(w, 'never-watched', STAGE_SENT)
  assert.equal(takeStalledHandoffs(w, T0 + HANDOFF_STALL_MS + 1).length, 1)
})

test('a session end clears every pending hand-off', () => {
  const w = newHandoffWatch()
  handoffBegan(w, 'turn-f', T0)
  resetHandoffWatch(w)
  assert.deepEqual(takeStalledHandoffs(w, T0 + HANDOFF_STALL_MS * 3), [])
})

test('the watch is bounded to the newest turns', () => {
  const w = newHandoffWatch()
  for (let i = 0; i < MAX_WATCHED + 5; i += 1) handoffBegan(w, `turn-${i}`, T0 + i)
  assert.equal(w.turns.length, MAX_WATCHED)
  assert.equal(w.turns[0].turnId, 'turn-5')
})

test('garbage clocks never report a stall', () => {
  const w = newHandoffWatch()
  handoffBegan(w, 'turn-g', T0)
  for (const now of [NaN, undefined, 'soon']) {
    assert.deepEqual(takeStalledHandoffs(w, now), [], String(now))
  }
})
