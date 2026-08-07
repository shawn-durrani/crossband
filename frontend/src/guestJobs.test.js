// Pure-function tests for the guest job status chip.
// Run: node --test frontend/src/guestJobs.test.js
import { test } from 'node:test'
import assert from 'node:assert/strict'
import {
  mergeGuestJob, currentJob, isActive, chipLabel, chipTone,
  hasVisibleJob, LINGER_SECS,
} from './guestJobs.js'

test('merge dedupes by id and applies the newer update', () => {
  let jobs = mergeGuestJob([], { id: 1, status: 'running', updated_at: 1 })
  jobs = mergeGuestJob(jobs, { id: 1, status: 'completed', kind: 'result', updated_at: 2 })
  assert.equal(jobs.length, 1)
  assert.equal(jobs[0].status, 'completed')
  assert.equal(jobs[0].kind, 'result')
})

test('a stale (older updated_at) event never clobbers fresher state', () => {
  const jobs = mergeGuestJob([{ id: 1, status: 'completed', updated_at: 5 }],
    { id: 1, status: 'running', updated_at: 3 })
  assert.equal(jobs[0].status, 'completed')
})

test('merge ignores malformed events', () => {
  const cur = [{ id: 1, status: 'running' }]
  assert.equal(mergeGuestJob(cur, null), cur)
  assert.equal(mergeGuestJob(cur, { status: 'running' }), cur)
})

test('currentJob prefers a running job over finished ones', () => {
  const jobs = [
    { id: 1, status: 'completed' },
    { id: 2, status: 'running' },
  ]
  assert.equal(currentJob(jobs).id, 2)
})

test('currentJob falls back to the most recent finished job', () => {
  const jobs = [
    { id: 1, status: 'completed' },
    { id: 2, status: 'failed' },
  ]
  assert.equal(currentJob(jobs).id, 2)
  assert.equal(currentJob([]), null)
})

test('isActive is true only while running', () => {
  assert.equal(isActive({ status: 'running' }), true)
  assert.equal(isActive({ status: 'completed' }), false)
  assert.equal(isActive(null), false)
})

test('chip label is a single quiet line, no reasoning', () => {
  assert.equal(chipLabel({ status: 'running', mode: 'investigate' }), 'Claude Code working…')
  assert.equal(chipLabel({ status: 'running', mode: 'implement', step_count: 3 }),
    'Claude Code building… · 3 steps')
  assert.equal(chipLabel({ status: 'completed', kind: 'result' }),
    'Claude Code finished - handing back')
  assert.equal(chipLabel({ status: 'completed', kind: 'blocker' }),
    'Claude Code needs input')
  assert.equal(chipLabel({ status: 'failed' }), 'Claude Code hit an error')
  assert.equal(chipLabel({ status: 'cancelled' }), 'Claude Code run stopped')
})

test('chip label surfaces the ephemeral status ping label while running', () => {
  assert.equal(
    chipLabel({ status: 'running', mode: 'investigate', step_count: 4, status_label: 'Investigating the code' }),
    'Claude Code working… · 4 steps · Investigating the code',
  )
  // no ping yet (fresh job, under the threshold), so the label stays plain
  assert.equal(chipLabel({ status: 'running', mode: 'investigate', status_label: '' }),
    'Claude Code working…')
})

test('chip tone folds the blocker completion path into its own token', () => {
  assert.equal(chipTone({ status: 'running' }), 'running')
  assert.equal(chipTone({ status: 'completed', kind: 'blocker' }), 'blocker')
  assert.equal(chipTone({ status: 'completed', kind: 'result' }), 'done')
  assert.equal(chipTone({ status: 'failed' }), 'failed')
})

test('hasVisibleJob: running shows, fresh finish lingers, old finish clears', () => {
  const now = 1000000
  // Running always shows, however old the record.
  assert.equal(hasVisibleJob([{ id: 1, status: 'running', updated_at: now - 9999 }], now), true)
  // A finished job lingers briefly (its result arrives as a message)…
  assert.equal(hasVisibleJob([{ id: 1, status: 'completed', updated_at: now - 10 }], now), true)
  // …then clears, so the layout wrapper must not reserve space for it -
  // the phantom-spacer bug this rule exists to prevent.
  assert.equal(hasVisibleJob([{ id: 1, status: 'completed', updated_at: now - LINGER_SECS - 1 }], now), false)
  assert.equal(hasVisibleJob([], now), false)
})
