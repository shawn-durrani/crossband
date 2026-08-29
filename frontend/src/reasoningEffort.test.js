// The per-vendor reasoning-effort capability table (#236), mirrored from
// backend/providers.py - keep the two in sync.
// Run: node --test frontend/src/reasoningEffort.test.js
import { test } from 'node:test'
import assert from 'node:assert/strict'
import { reasoningOptions, effortSupport, normalizeReasoningEffort } from './reasoningEffort.js'

test('vocabulary matches backend REASONING_CHOICES per provider', () => {
  assert.deepEqual(reasoningOptions('anthropic').map((o) => o.value),
    ['', 'low', 'medium', 'high', 'max', 'adaptive'])
  assert.deepEqual(reasoningOptions('openai').map((o) => o.value),
    ['', 'low', 'medium', 'high', 'max'])
})

test('every option carries a label; Adaptive names its latency cost', () => {
  for (const o of reasoningOptions('anthropic'))
    assert.ok(o.label.length > 0, `${o.value} has no label`)
  const adaptive = reasoningOptions('anthropic').find((o) => o.value === 'adaptive')
  assert.match(adaptive.label, /voice/i)
})

test('unsupported Claude families are gated, matching _ANTHROPIC_NO_EFFORT_MODEL', () => {
  for (const m of ['claude-haiku-4-5', 'claude-3-opus', 'claude-sonnet-4-5',
    'claude-sonnet-4.5', 'claude-sonnet-4-0'])
    assert.equal(effortSupport('anthropic', m).ok, false, m)
  assert.equal(effortSupport('anthropic', 'claude-opus-4-8').ok, true)
  assert.equal(effortSupport('anthropic', 'CLAUDE-HAIKU-4-5').ok, false)
})

test('OpenAI support means reasoning models only (gpt-5 and o-series prefixes)', () => {
  assert.equal(effortSupport('openai', 'gpt-5.1').ok, true)
  assert.equal(effortSupport('openai', 'o3-mini').ok, true)
  assert.equal(effortSupport('openai', 'gpt-4o').ok, false)
  assert.equal(effortSupport('openai', '').ok, false)
})

test('the unsupported notes say the setting would be ignored', () => {
  assert.match(effortSupport('anthropic', 'claude-haiku-4-5').note, /ignored/i)
  assert.match(effortSupport('openai', 'gpt-4o').note, /ignored/i)
})

test('a value in the provider vocabulary survives normalisation', () => {
  assert.equal(normalizeReasoningEffort('anthropic', 'adaptive'), 'adaptive')
  assert.equal(normalizeReasoningEffort('openai', 'high'), 'high')
  assert.equal(normalizeReasoningEffort('openai', ''), '')
})

test('adaptive on a non-Anthropic seat resets to Default, not a 400 at save', () => {
  assert.equal(normalizeReasoningEffort('openai', 'adaptive'), '')
})

test('an unknown or missing value never reaches the backend', () => {
  assert.equal(normalizeReasoningEffort('anthropic', 'ultra'), '')
  assert.equal(normalizeReasoningEffort('openai', undefined), '')
})

test('an unknown provider gets the restrictive OpenAI set, like reasoning_choices', () => {
  assert.equal(normalizeReasoningEffort('mystery', 'adaptive'), '')
  assert.equal(normalizeReasoningEffort('mystery', 'high'), 'high')
})
