// Bounded active-turn endpointing (#60): a turn under SOFT_MAX_TURN_MS never
// forces, an unvoiced frame past SOFT_MAX_TURN_MS forces, a voiced frame
// still under HARD_MAX_TURN_MS holds so continuous real speech isn't cut off
// mid-word where avoidable, and HARD_MAX_TURN_MS forces unconditionally.

import test from 'node:test'
import assert from 'node:assert/strict'
import { HARD_MAX_TURN_MS, SOFT_MAX_TURN_MS, shouldForceEndpoint } from './turnPolicy.js'

test('a normal short turn never forces, voiced or not', () => {
  assert.equal(shouldForceEndpoint({ turnMs: 3000, voiced: true }), false)
  assert.equal(shouldForceEndpoint({ turnMs: 3000, voiced: false }), false)
})

test('just under the soft cap holds even on an unvoiced frame', () => {
  assert.equal(shouldForceEndpoint({ turnMs: SOFT_MAX_TURN_MS - 1, voiced: false }), false)
})

test('past the soft cap, an unvoiced frame (a real gap) forces now', () => {
  assert.equal(shouldForceEndpoint({ turnMs: SOFT_MAX_TURN_MS, voiced: false }), true)
  assert.equal(shouldForceEndpoint({ turnMs: SOFT_MAX_TURN_MS + 5000, voiced: false }), true)
})

test('past the soft cap but still voiced holds, to avoid premature truncation', () => {
  assert.equal(shouldForceEndpoint({ turnMs: SOFT_MAX_TURN_MS, voiced: true }), false)
  assert.equal(shouldForceEndpoint({ turnMs: HARD_MAX_TURN_MS - 1, voiced: true }), false)
})

test('the hard cap forces unconditionally, even mid-word', () => {
  assert.equal(shouldForceEndpoint({ turnMs: HARD_MAX_TURN_MS, voiced: true }), true)
  assert.equal(shouldForceEndpoint({ turnMs: HARD_MAX_TURN_MS + 1, voiced: false }), true)
})

test('non-numeric or missing turnMs never forces', () => {
  assert.equal(shouldForceEndpoint({ turnMs: undefined, voiced: false }), false)
  assert.equal(shouldForceEndpoint({ voiced: false }), false)
})

test('the two tiers are ordered: soft cap strictly before hard cap', () => {
  assert.ok(SOFT_MAX_TURN_MS < HARD_MAX_TURN_MS)
})
