// Tests for per-chat running-task state.
// Run: node --test frontend/src/runningState.test.js
import { test } from 'node:test'
import assert from 'node:assert/strict'
import { computeRunningChats, isChatRunning, shouldPollRunning } from './runningState.js'

test('server ids populate the running set', () => {
  const set = computeRunningChats([1, 2, 3])
  assert.equal(isChatRunning(set, 1), true)
  assert.equal(isChatRunning(set, 2), true)
  assert.equal(isChatRunning(set, 9), false)
})

test('optimistic id lights up immediately, without duplicating', () => {
  const set = computeRunningChats([1], 5)
  assert.equal(isChatRunning(set, 5), true)
  assert.equal(isChatRunning(set, 1), true)
  const dedup = computeRunningChats([5], 5)
  assert.equal(dedup.size, 1) // same id from both sources collapses
})

test('a background chat stays running even when it is not the active view', () => {
  // chat 7 runs in the background while the user views chat 3
  const set = computeRunningChats([7])
  assert.equal(isChatRunning(set, 7), true)
})

test('empty / missing inputs are safe and mean "nothing running"', () => {
  assert.equal(computeRunningChats(undefined).size, 0)
  assert.equal(computeRunningChats(null).size, 0)
  assert.equal(isChatRunning(computeRunningChats([]), 1), false)
  assert.equal(isChatRunning(null, 1), false)
  assert.equal(isChatRunning(new Set([1]), null), false)
})

test('membership is strict - no numeric/string coercion', () => {
  const set = computeRunningChats([7])
  assert.equal(isChatRunning(set, 7), true)
  assert.equal(isChatRunning(set, '7'), false)
})

test('poll only while something runs', () => {
  assert.equal(shouldPollRunning(computeRunningChats([])), false)
  assert.equal(shouldPollRunning(computeRunningChats([1])), true)
  assert.equal(shouldPollRunning(null), false)
})
