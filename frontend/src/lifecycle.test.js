// Tests for the Participants UI: it must SHOW trial vs onboarded, make a trial
// seat's manual-invoke-only behaviour understandable (so it never looks like it
// silently vanished from a round), and offer a promotion path that is enabled
// only when the seat is actually onboardable.
// Run: node --test frontend/src/lifecycle.test.js
import { test } from 'node:test'
import assert from 'node:assert/strict'
import { lifecycleBadge, promoteState, isLocalEndpoint, addSeatNotice } from './lifecycle.js'

test('no lifecycle known at all → no badge', () => {
  assert.equal(lifecycleBadge(undefined, undefined), null)
  assert.equal(lifecycleBadge(null, null), null)
})

test('falls back to the participants-row lifecycle before the readout loads', () => {
  const b = lifecycleBadge(undefined, 'trial')
  assert.equal(b.label, 'Trial')
  assert.equal(b.tone, 'trial')
})

test('onboarded seat reads as a normal seat', () => {
  const b = lifecycleBadge({ lifecycle: 'onboarded' }, 'trial')
  assert.equal(b.label, 'Onboarded')
  assert.equal(b.tone, 'onboarded')
})

test('trial badge explains it is manual-invoke-only (the disappearance guard)', () => {
  const b = lifecycleBadge({ lifecycle: 'trial' }, 'onboarded')
  assert.equal(b.label, 'Trial')
  assert.match(b.title, /manual-invoke only/i)
  assert.match(b.title, /@mention|by name/i)
})

test('unknown/garbage lifecycle is treated conservatively as trial', () => {
  const b = lifecycleBadge({ lifecycle: 'weird' }, undefined)
  assert.equal(b.lifecycle, 'trial')
})

test('promote is hidden for an already-onboarded seat', () => {
  assert.equal(promoteState({ lifecycle: 'onboarded' }).show, false)
})

test('trial + known provenance → promote enabled (recovery path)', () => {
  const s = promoteState({
    lifecycle: 'trial', onboardable: true,
    cost_provenance_label: 'Rate-card estimate',
  })
  assert.equal(s.show, true)
  assert.equal(s.enabled, true)
  assert.match(s.reason, /Rate-card estimate/)
})

test('trial + unknown provenance → promote shown but disabled with the reason', () => {
  const s = promoteState({ lifecycle: 'trial', onboardable: false })
  assert.equal(s.show, true)
  assert.equal(s.enabled, false)
  assert.match(s.reason, /no cost provenance/i)
})

test('trial with readout not yet loaded → button shown, backend is source of truth', () => {
  const s = promoteState(undefined, 'trial')
  assert.equal(s.show, true)
  assert.equal(s.enabled, true)
})

// ── the add form has to say what a Trial seat DOES ──────────────────────────
// The documented zero-key path dead-ended silently: the seat never spoke and
// nothing on screen said why. These pin the explainer, and the local/hosted
// split that decides whether promotion is available at all.

test('keyless loopback endpoints read as local', () => {
  for (const base_url of [
    'http://localhost:11434/v1',   // Ollama default
    'http://127.0.0.1:1234/v1',    // LM Studio default
    'http://127.0.0.5:8080/v1',
    'http://[::1]:11434/v1',
    'localhost:11434/v1',          // typed without a scheme
  ]) assert.equal(isLocalEndpoint({ base_url }), true, base_url)
})

test('hosted, LAN, keyed and empty endpoints do NOT read as local', () => {
  assert.equal(isLocalEndpoint({ base_url: 'https://api.groq.com/openai/v1' }), false)
  assert.equal(isLocalEndpoint({ base_url: 'http://192.168.1.1:11434/v1' }), false)
  // a key means something is being authenticated to - possibly a tunnel
  assert.equal(isLocalEndpoint({ base_url: 'http://localhost:11434/v1', api_key_env: 'K' }), false)
  assert.equal(isLocalEndpoint({ base_url: '' }), false)
  assert.equal(isLocalEndpoint({}), false)
  assert.equal(isLocalEndpoint(null), false)
})

test('a malformed base_url is not local (and does not throw)', () => {
  assert.equal(isLocalEndpoint({ base_url: 'http://[not-an-ipv6/v1' }), false)
})

test('adding a seat always warns it will be a Trial, and what that means', () => {
  const n = addSeatNotice({ name: 'X', model: 'llama3.1' })
  assert.match(n.title, /Trial/)
  assert.match(n.body, /@mention|by name/i)
  assert.match(n.body, /sits out/i)          // the behaviour, not just the label
  assert.match(n.body, /Promote/i)           // and the way out
})

test('a local seat is told it can be promoted straight away', () => {
  const n = addSeatNotice({ model: 'llama3.1', base_url: 'http://localhost:11434/v1' })
  assert.equal(n.local, true)
  assert.match(n.promotion, /straight away/i)
  assert.match(n.promotion, /\$0|no API key/i)
})

test('a hosted unpriced seat is told promotion needs a known cost', () => {
  const n = addSeatNotice({ model: 'mystery', base_url: 'https://api.example.com/v1' })
  assert.equal(n.local, false)
  assert.match(n.promotion, /known cost/i)
  // …and points at the two real ways out rather than dead-ending
  assert.match(n.promotion, /rate-card/i)
  assert.match(n.promotion, /locally/i)
})

test('editing an existing seat shows no add-notice (its row badge covers it)', () => {
  assert.equal(addSeatNotice({ id: 7, model: 'llama3.1' }), null)
  assert.equal(addSeatNotice(null), null)
})
