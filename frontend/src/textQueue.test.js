// Invariant tests for per-chat text batching + pre-ingestion cancel.
// Run: node --test frontend/src/textQueue.test.js
//
// The load-bearing test is the atomic-flip race: a cancel and a flip issued in
// the same tick must resolve to EITHER "nothing sends" OR "one send commits" -
// never a partial send. Everything else guards the surrounding state machine.
import { test } from 'node:test'
import assert from 'node:assert/strict'
import {
  createBatch, addFragment, cancelBatch, flipBatch, combineFragments,
  pendingCount, isCommitted, isPending,
} from './textQueue.js'

test('queued fragments combine into one user turn, in order', () => {
  let b = createBatch()
  b = addFragment(b, 'first', 1000)
  b = addFragment(b, 'second', 1001)
  b = addFragment(b, 'third', 1002)
  assert.equal(pendingCount(b), 3)
  assert.equal(combineFragments(b), 'first\n\nsecond\n\nthird')
})

test('fragments retain their timestamps as metadata', () => {
  let b = createBatch()
  b = addFragment(b, 'a', 111)
  b = addFragment(b, 'b', 222)
  assert.deepEqual(b.fragments.map((f) => f.ts), [111, 222])
})

test('empty / whitespace-only fragments are ignored (no empty turns)', () => {
  let b = createBatch()
  b = addFragment(b, '   ', 1)
  b = addFragment(b, '', 2)
  assert.equal(pendingCount(b), 0)
  assert.equal(isPending(b), false)
})

test('attachment ids ride along with the batch', () => {
  let b = createBatch()
  b = addFragment(b, 'see this', 1, ['att-1'])
  b = addFragment(b, 'and this', 2, ['att-2', 'att-3'])
  assert.deepEqual(b.attachmentIds, ['att-1', 'att-2', 'att-3'])
})

test('cancel before the flip sends nothing', () => {
  let b = createBatch()
  b = addFragment(b, 'never sent', 1)
  b = cancelBatch(b)
  const { committed } = flipBatch(b)
  assert.equal(committed, false, 'a cancelled batch must not commit')
  assert.equal(pendingCount(b), 0)
})

test('cancel AFTER the flip cleanly no-ops (message already ingesting)', () => {
  let b = createBatch()
  b = addFragment(b, 'in flight', 1)
  const flip = flipBatch(b)
  assert.equal(flip.committed, true)
  const after = cancelBatch(flip.batch)
  assert.equal(after, flip.batch, 'cancel returns the same committed batch untouched')
  assert.equal(isCommitted(after), true)
})

// THE CORE CORRECTNESS REQUIREMENT: the queued→in-flight flip is a single
// atomic decision. We prove both interleavings of a same-tick cancel+flip.
test('atomic flip - cancel-first wins: nothing sends', () => {
  let b = createBatch()
  b = addFragment(b, 'racing', 1)
  // cancel executes first this tick…
  const cancelled = cancelBatch(b)
  // …then the worker's flip runs against the resulting state
  const { committed } = flipBatch(cancelled)
  assert.equal(committed, false)
})

test('atomic flip - flip-first wins: send commits, later cancel no-ops', () => {
  let b = createBatch()
  b = addFragment(b, 'racing', 1)
  // flip executes first this tick…
  const { batch: flipped, committed } = flipBatch(b)
  assert.equal(committed, true)
  // …then a cancel arrives against the already-committed state
  const after = cancelBatch(flipped)
  assert.equal(isCommitted(after), true, 'cancel cannot undo a committed send')
})

test('a batch commits at most once (no double send)', () => {
  let b = createBatch()
  b = addFragment(b, 'once', 1)
  const first = flipBatch(b)
  assert.equal(first.committed, true)
  const second = flipBatch(first.batch)
  assert.equal(second.committed, false, 'an in-flight batch cannot flip again')
})

test('flipping an empty batch never commits', () => {
  assert.equal(flipBatch(createBatch()).committed, false)
})

test('fragments cannot be added after the flip (no partial send)', () => {
  let b = createBatch()
  b = addFragment(b, 'a', 1)
  const { batch: flipped } = flipBatch(b)
  const tampered = addFragment(flipped, 'b', 2)
  assert.equal(tampered, flipped, 'committed batch is immutable')
  assert.equal(tampered.fragments.length, 1)
})
