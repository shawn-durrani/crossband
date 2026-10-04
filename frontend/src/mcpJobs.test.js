// The background-work strip's rules (#604): merging live mcp_job events,
// which watches show, and the one-line label for each.
// Run: node --test frontend/src/mcpJobs.test.js
import { test } from 'node:test'
import assert from 'node:assert/strict'
import {
  mergeMcpJob, visibleMcpJobs, mcpElapsed, formatElapsed, mcpChipLabel,
  mcpChipTone,
} from './mcpJobs.js'
import { LINGER_SECS } from './guestJobs.js'

const running = {
  id: 1, chat_id: 3, server: 'dovetail', title: 'Dovetail',
  state: 'running', stage: 'adding the drawer runners', steps: 48,
  elapsed_s: 300, waiting_for: null, watching: true, ended: '',
  updated_at: 1000,
}

test('merge keeps one entry per watch and the newest update', () => {
  let jobs = mergeMcpJob([], running)
  jobs = mergeMcpJob(jobs, { ...running, steps: 49, updated_at: 1015 })
  assert.equal(jobs.length, 1)
  assert.equal(jobs[0].steps, 49)
  // an older event arriving late changes nothing
  assert.equal(mergeMcpJob(jobs, { ...running, steps: 2, updated_at: 990 }), jobs)
  assert.equal(mergeMcpJob(jobs, null), jobs)
  assert.equal(mergeMcpJob(jobs, { state: 'running' }), jobs)
})

test('the label reads like the work: stage, step and time', () => {
  assert.equal(mcpChipLabel(running, 1000),
    'Dovetail: adding the drawer runners · step 48 · 5 min')
  // the time moves on between updates while it runs
  assert.equal(mcpElapsed(running, 1060), 360)
  assert.equal(mcpChipLabel({ ...running, stage: '', steps: null }, 1000),
    'Dovetail working… · 5 min')
})

test('a watch waiting on the room says what it waits for', () => {
  const waiting = { ...running, state: 'waiting', waiting_for: 'question' }
  assert.equal(mcpChipLabel(waiting, 1000), 'Dovetail is asking a question')
  assert.equal(mcpChipLabel({ ...waiting, waiting_for: 'preview' }, 1000),
    'Dovetail has a preview waiting')
  assert.equal(mcpChipLabel({ ...waiting, waiting_for: null }, 1000),
    'Dovetail is waiting on you')
  assert.equal(mcpChipTone(waiting), 'blocker')
  assert.equal(mcpChipTone(running), 'running')
})

test('a finished watch lingers briefly, a stopped one never shows', () => {
  const done = { ...running, state: 'done', watching: false, ended: 'done' }
  assert.equal(mcpChipLabel(done, 1000), 'Dovetail finished, handing back')
  assert.equal(mcpChipTone(done), 'done')
  assert.deepEqual(visibleMcpJobs([done], 1010), [done])
  assert.deepEqual(visibleMcpJobs([done], 1000 + LINGER_SECS + 1), [])
  const stopped = { ...running, watching: false, ended: 'stopped' }
  const idle = { ...running, state: 'idle', watching: false, ended: 'idle' }
  assert.deepEqual(visibleMcpJobs([stopped, idle], 1001), [])
  const lost = { ...running, watching: false, ended: 'lost' }
  assert.match(mcpChipLabel(lost, 1000), /lost touch/)
  assert.equal(mcpChipTone(lost), 'failed')
  assert.deepEqual(visibleMcpJobs([running], 99999), [running])
})

test('elapsed time reads like a person would say it', () => {
  assert.equal(formatElapsed(42), '42 s')
  assert.equal(formatElapsed(312), '5 min')
  assert.equal(formatElapsed(2 * 3600 + 5 * 60), '2 h 5 min')
  assert.equal(formatElapsed(undefined), '0 s')
})
