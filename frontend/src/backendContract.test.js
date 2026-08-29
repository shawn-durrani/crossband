// Cross-language contract (#234). lifecycle.js mirrors backend literals:
// the seat lifecycle states (backend/provenance.py LIFECYCLE_STATES) and
// the loopback host set (backend/config.py _LOOPBACK_HOSTS). The committed
// fixture tests/fixtures/backend_contract.json is the backend's word for
// both; tests/test_contract_fixture.py fails whenever that file goes
// stale, so a backend change reaches this file as a red test, not quiet
// drift. Run: node --test frontend/src/backendContract.test.js
import { test } from 'node:test'
import assert from 'node:assert/strict'
import { readFileSync } from 'node:fs'
import { lifecycleBadge, isLocalEndpoint } from './lifecycle.js'
import { reasoningOptions, effortSupport,
         normalizeReasoningEffort } from './reasoningEffort.js'

const fixture = JSON.parse(readFileSync(
  new URL('../../tests/fixtures/backend_contract.json', import.meta.url),
  'utf8'))

test('the backend has exactly the two lifecycle states this UI branches on', () => {
  // lifecycleBadge matches the literal 'onboarded' and collapses everything
  // else to trial. That conservative default is only honest while these are
  // the only two states - a third would silently render as Trial.
  assert.deepEqual(fixture.seat_lifecycle.states, ['trial', 'onboarded'])
})

test('every backend lifecycle state survives the badge round-trip', () => {
  for (const state of fixture.seat_lifecycle.states) {
    const badge = lifecycleBadge({ lifecycle: state }, undefined)
    assert.equal(badge.lifecycle, state,
      `backend state '${state}' did not round-trip through lifecycleBadge`)
  }
})

test('every backend loopback host reads as local here', () => {
  // Backend-subset-of-JS by design: WHATWG URL hostnames keep IPv6
  // brackets (so lifecycle.js also lists '[::1]') where Python's urlsplit
  // strips them. Each backend host must count as local; JS may know
  // bracketed spellings the backend never sees.
  for (const host of fixture.seat_lifecycle.loopback_hosts) {
    const literal = host.includes(':') ? `[${host}]` : host
    assert.equal(isLocalEndpoint({ base_url: `http://${literal}:8080` }), true,
      `backend loopback host '${host}' is not local to isLocalEndpoint`)
  }
})

// Reasoning effort (#292): reasoningEffort.js mirrors REASONING_CHOICES
// and _ANTHROPIC_NO_EFFORT_MODEL from backend/providers.py.

test('the effort dropdown offers exactly the backend vocabulary, per provider', () => {
  for (const [provider, choices] of Object.entries(fixture.reasoning_effort.choices)) {
    const offered = reasoningOptions(provider).map((o) => o.value)
    assert.deepEqual(offered, choices,
      `reasoningOptions('${provider}') drifted from backend REASONING_CHOICES`)
  }
})

test('every backend no-effort Claude model marker is greyed out here', () => {
  for (const marker of fixture.reasoning_effort.anthropic_no_effort_models) {
    assert.equal(effortSupport('anthropic', `claude-${marker}-latest`).ok, false,
      `backend gates effort off for '${marker}' models but effortSupport allows it`)
  }
})

test('normalize keeps the backend vocabulary and resets anything outside it', () => {
  for (const [provider, choices] of Object.entries(fixture.reasoning_effort.choices)) {
    for (const value of choices) {
      assert.equal(normalizeReasoningEffort(provider, value), value)
    }
  }
  assert.equal(normalizeReasoningEffort('openai', 'adaptive'), '')
})
