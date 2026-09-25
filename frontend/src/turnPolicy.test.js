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


test('#104: commit patience scales with the audio committed', async (t2) => {
  const { sttCommitTimeoutMs } = await import('./turnPolicy.js')
  assert.equal(sttCommitTimeoutMs(2000), 5000)     // floor: short remarks
  assert.equal(sttCommitTimeoutMs(12000), 9000)    // 0.75x mid-length
  assert.equal(sttCommitTimeoutMs(20000), 15000)   // a hard-capped segment
  assert.equal(sttCommitTimeoutMs(60000), 20000)   // ceiling: batch wins past this
  assert.equal(sttCommitTimeoutMs(undefined), 5000)
  assert.equal(sttCommitTimeoutMs(-5), 5000)
})

test('#104: the total-turn bound outlasts the per-segment caps', async () => {
  const { HARD_MAX_TURN_MS, MAX_TURN_TOTAL_MS, SOFT_MAX_TURN_MS } =
    await import('./turnPolicy.js')
  // segments cap early and often; the LOGICAL turn is bounded much later -
  // that gap is what lets a monologue stay one message while a zero-gap
  // noise wall still eventually sends (#60's guarantee, relocated).
  assert.ok(MAX_TURN_TOTAL_MS >= 2 * HARD_MAX_TURN_MS)
  assert.ok(SOFT_MAX_TURN_MS < HARD_MAX_TURN_MS)
})
