// The link row's rule: which sibling apps a page can offer, from where it
// was opened (workbench#100).
import { test } from 'node:test'
import assert from 'node:assert/strict'
import { isLoopbackHost, linkRow, siblingHref } from './appLinks.js'

const TAILNET = 'my-mac.my-tailnet.ts.net'
const APPS = [
  { name: 'membro', origin: `https://${TAILNET}:8443`, local: 'http://127.0.0.1:8901' },
  { name: 'spendglass', origin: 'http://127.0.0.1:8903', local: 'http://127.0.0.1:8903' },
  { name: 'threadfold', origin: '', local: 'http://127.0.0.1:8904' },
]

test('loopback names read as this machine, and nothing else does', () => {
  for (const h of ['127.0.0.1', 'localhost', 'LOCALHOST', '[::1]', '::1', '127.0.0.2']) {
    assert.equal(isLoopbackHost(h), true, h)
  }
  for (const h of [TAILNET, '192.168.1.5', '', undefined, 'localhost.example.com']) {
    assert.equal(isLoopbackHost(h), false, String(h))
  }
})

test('on the Mac, every sibling that answered opens at its loopback address', () => {
  assert.deepEqual(linkRow('127.0.0.1', 'crossband', APPS), [
    { name: 'crossband', href: '', current: true },
    { name: 'membro', href: 'http://127.0.0.1:8901/', current: false },
    { name: 'spendglass', href: 'http://127.0.0.1:8903/', current: false },
    { name: 'threadfold', href: 'http://127.0.0.1:8904/', current: false },
  ])
})

test('a page on localhost keeps the name, so a passkey made there still offers itself', () => {
  assert.equal(siblingHref('localhost', APPS[0]), 'http://localhost:8901/')
})

test('on the tailnet, only siblings served there show', () => {
  assert.deepEqual(linkRow(TAILNET, 'crossband', APPS), [
    { name: 'crossband', href: '', current: true },
    { name: 'membro', href: `https://${TAILNET}:8443/`, current: false },
  ])
})

test('no sibling that can open means no row at all', () => {
  assert.deepEqual(linkRow(TAILNET, 'crossband', APPS.slice(1)), [])
  assert.deepEqual(linkRow('127.0.0.1', 'crossband', []), [])
  assert.deepEqual(linkRow('127.0.0.1', 'crossband', undefined), [])
})

test('the current app is never linked, even if the server lists it', () => {
  const row = linkRow('127.0.0.1', 'crossband',
    [...APPS, { name: 'crossband', origin: '', local: 'http://127.0.0.1:8902' }])
  assert.deepEqual(row.filter((l) => l.name === 'crossband'),
    [{ name: 'crossband', href: '', current: true }])
})

test('an address that is not plain http or https never becomes a link', () => {
  for (const origin of ['javascript:alert(1)', 'data:text/html,hi', 'not a url', null]) {
    assert.equal(siblingHref(TAILNET, { name: 'x', origin, local: '' }), '')
  }
  // A loopback page never trusts a "local" address that isn't loopback.
  assert.equal(siblingHref('127.0.0.1', { name: 'x', local: `https://${TAILNET}` }), '')
})
