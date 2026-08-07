// Tests for the per-seat model-status readout: it must not misread on a narrow
// screen. We can't render at 390px here (no DOM), so we lock the meaning that
// the wrapping fix has to preserve: ordering + tone hierarchy. The visual
// grouping ("label stays with its value") is enforced structurally in the
// component by keeping each line one flowing prose span; this guards the intent.
// Run: node --test frontend/src/modelReadout.test.js
import { test } from 'node:test'
import assert from 'node:assert/strict'
import { modelReadoutLines } from './modelReadout.js'

test('no status → nothing to show (memoryless / not loaded yet)', () => {
  assert.deepEqual(modelReadoutLines(null), [])
  assert.deepEqual(modelReadoutLines(undefined), [])
})

test('all agree → silent (no drift, no pending, nothing to flag)', () => {
  const lines = modelReadoutLines({
    configured: 'gpt-5.6-sol', last_used: 'gpt-5.6-sol',
    seed: 'gpt-5.6-sol', seed_drift: false, pending: false,
  })
  // last reply confirmed on the live model still shows, calmly, as reassurance.
  assert.equal(lines.length, 1)
  assert.equal(lines[0].tone, 'confirm')
})

test('last reply confirmed reads as confirmation, not alarm', () => {
  const [line] = modelReadoutLines({
    last_used: 'gpt-5.6-sol', seed: 'gpt-5.6-sol', seed_drift: false, pending: false,
  })
  assert.equal(line.tone, 'confirm')
  assert.equal(line.icon, 'check')
  assert.notEqual(line.tone, 'attention') // must never render as an alarm
  assert.equal(line.model, 'gpt-5.6-sol')
})

test('pending change gets the attention tone (the eye), not silence', () => {
  const [line] = modelReadoutLines({
    configured: 'gpt-5.6-sol', last_used: 'gpt-5.1', seed_drift: false, pending: true,
  })
  assert.equal(line.tone, 'attention')
  assert.equal(line.icon, 'alert')
  assert.equal(line.model, 'gpt-5.1') // the model that produced the *last* reply
})

test('the reported case: switch landed + config seed drifted', () => {
  // GPT seat: live gpt-5.6-sol, last reply already on it, config seed still gpt-5.1.
  const lines = modelReadoutLines({
    configured: 'gpt-5.6-sol', last_used: 'gpt-5.6-sol',
    seed: 'gpt-5.1', seed_drift: true, pending: false,
  })
  assert.equal(lines.length, 2)
  // Confirmation comes first and is muted-confirm - it answers "did it take? yes".
  assert.equal(lines[0].key, 'confirmed')
  assert.equal(lines[0].tone, 'confirm')
  // The stale seed is LAST and muted - context, never the headline number.
  const seedLine = lines[1]
  assert.equal(seedLine.key, 'seed')
  assert.equal(seedLine.tone, 'muted')
  assert.equal(seedLine.model, 'gpt-5.1')
  assert.equal(seedLine.icon, null) // no alarm glyph on the drift note
  // The seed line must never be more prominent than the confirmation above it:
  // neither drift nor confirm may carry the 'attention' weight here.
  assert.ok(lines.every((l) => l.tone !== 'attention'))
})

test('seed drift while a change is still pending keeps drift last and muted', () => {
  const lines = modelReadoutLines({
    configured: 'gpt-5.6-sol', last_used: 'gpt-5.1',
    seed: 'gpt-4.9', seed_drift: true, pending: true,
  })
  assert.equal(lines[0].tone, 'attention') // pending is the loud line
  assert.equal(lines.at(-1).key, 'seed')
  assert.equal(lines.at(-1).tone, 'muted')
})
