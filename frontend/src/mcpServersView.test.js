// Tests for the MCP panel's derivations.
// Run: node --test frontend/src/mcpServersView.test.js
import { test } from 'node:test'
import assert from 'node:assert/strict'
import {
  parseCommandLine, parseEnvLines, buildSpec, draftFromRow, rowStatus,
} from './mcpServersView.js'

test('command line splits into exec array, never shell', () => {
  assert.deepEqual(parseCommandLine('/x/python -m pkg.server'),
    { command: '/x/python', args: ['-m', 'pkg.server'] })
  assert.equal(parseCommandLine('   '), null)
})

test('env lines: KEY=VALUE, values may contain =, junk is named', () => {
  assert.deepEqual(parseEnvLines('A=1\nB=x=y\n\n').env, { A: '1', B: 'x=y' })
  assert.match(parseEnvLines('NOTANENV').error, /not KEY=VALUE/)
  assert.match(parseEnvLines('=value').error, /not KEY=VALUE/)
})

test('buildSpec round-trips a full draft and mirrors server validation', () => {
  const out = buildSpec({
    name: 'ledger', slot: 'models',
    commandLine: '/x/python -m ledger_store.mcp_server',
    envText: 'PYTHONPATH=/x', label: ' Checking spending ',
  })
  assert.equal(out.errors, undefined)
  assert.deepEqual(out.payload, {
    command: '/x/python', args: ['-m', 'ledger_store.mcp_server'],
    env: { PYTHONPATH: '/x' }, label: 'Checking spending',
  })
  // bad name + missing command both reported in one pass
  const bad = buildSpec({ name: 'Bad Name', commandLine: '', envText: '' })
  assert.equal(bad.errors.length, 2)
})

test('edit-in-place: draftFromRow(buildSpec(d)) preserves meaning', () => {
  const row = {
    name: 'x', slot: 'guests', command: '/bin/s', args: ['a', 'b'],
    env: { K: 'v' }, label: 'L',
  }
  const draft = draftFromRow(row)
  const spec = buildSpec(draft)
  assert.deepEqual(spec.payload,
    { command: '/bin/s', args: ['a', 'b'], env: { K: 'v' }, label: 'L' })
  assert.equal(spec.slot, 'guests')
})

test('status: error outranks pending; guests never claim live state', () => {
  assert.equal(rowStatus({ slot: 'models', error: 'boom', pending_restart: true }).tone, 'error')
  assert.equal(rowStatus({ slot: 'models', connected: true, tools: ['a'] }).tone, 'ok')
  assert.equal(rowStatus({ slot: 'models', connected: false, pending_restart: true }).tone, 'pending')
  assert.equal(rowStatus({ slot: 'guests', pending_restart: false }).tone, 'ok')
  assert.match(rowStatus({ slot: 'guests', pending_restart: true }).text, /restart/)
})
