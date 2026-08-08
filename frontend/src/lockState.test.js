import { test } from 'node:test'
import assert from 'node:assert/strict'
import { gateView, setupProblem } from './lockState.js'

test('probing until the session answer arrives', () => {
  assert.equal(gateView(null), 'probing')
  assert.equal(gateView(undefined), 'probing')
})

test('unenrolled installs stay open - the gate is enrolment-activated', () => {
  assert.equal(gateView({ enrolled: false, authenticated: true, passkey: false }), 'open')
})

test('a live session opens the app', () => {
  assert.equal(gateView({ enrolled: true, authenticated: true, passkey: false }), 'open')
  assert.equal(gateView({ enrolled: true, authenticated: true, passkey: true }, true), 'open')
})

test('enrolled and anonymous locks to the password form', () => {
  assert.equal(gateView({ enrolled: true, authenticated: false, passkey: false }), 'password')
})

test('passkey leads only where one exists AND the browser can do WebAuthn', () => {
  const s = { enrolled: true, authenticated: false, passkey: true }
  assert.equal(gateView(s, true), 'passkey')
  assert.equal(gateView(s, false), 'password') // e.g. an old browser
  // no passkey for this origin (e.g. browsing via 127.0.0.1): password form
  assert.equal(gateView({ ...s, passkey: false }, true), 'password')
})

test('setup validation mirrors the server minimum', () => {
  assert.ok(setupProblem({ recovery: '', password: 'long-enough-1', confirm: 'long-enough-1' }))
  assert.ok(setupProblem({ recovery: 'r', password: 'short', confirm: 'short' }))
  assert.ok(setupProblem({ recovery: 'r', password: 'long-enough-1', confirm: 'different-1' }))
  assert.equal(setupProblem({ recovery: 'r', password: 'long-enough-1', confirm: 'long-enough-1' }), null)
})
