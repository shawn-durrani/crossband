// Tests for the Ollama keep-alive window. The rules that matter: the value
// vocabulary matches the backend's (Ollama duration or -1), the field is
// offered only on a seat that can actually fire it (openai + base URL), and a
// value that stops applying is cleared rather than quietly stored.
// Run: node --test frontend/src/keepalive.test.js
import { test } from 'node:test'
import assert from 'node:assert/strict'
import {
  validKeepAliveValue,
  keepAliveSupport,
  normalizeKeepAlive,
} from './keepalive.js'

const LOCAL = 'http://127.0.0.1:8999/v1'

test('valid Ollama durations, and the indefinite sentinel (empty = unset)', () => {
  for (const v of ['', '  ', '30m', '1h', '24h', '1h30m', '-1']) {
    assert.equal(validKeepAliveValue(v), true, `${v} should be valid`)
  }
})

test('rejects what Ollama would not parse', () => {
  for (const v of ['45', '30', 'forever', '1.5', '30M', '1h30']) {
    assert.equal(validKeepAliveValue(v), false, `${v} should be invalid`)
  }
})

test('offered only on an openai seat with its own base URL', () => {
  assert.equal(keepAliveSupport('openai', LOCAL).ok, true)
  assert.equal(keepAliveSupport('openai', '').ok, false, 'no base URL = OpenAI proper')
  assert.equal(keepAliveSupport('anthropic', LOCAL).ok, false, 'no local model to hold')
  assert.match(keepAliveSupport('anthropic', LOCAL).note, /Claude seat/)
})

test('normalize clears a value a seat cannot apply, keeps one it can', () => {
  assert.equal(normalizeKeepAlive('openai', LOCAL, '30m'), '30m')
  assert.equal(normalizeKeepAlive('openai', LOCAL, '-1'), '-1')
  assert.equal(normalizeKeepAlive('openai', '', '30m'), '', 'no base URL -> cleared')
  assert.equal(normalizeKeepAlive('anthropic', LOCAL, '30m'), '', 'claude seat -> cleared')
  assert.equal(normalizeKeepAlive('openai', LOCAL, '45'), '', 'unparseable -> cleared')
  assert.equal(normalizeKeepAlive('openai', LOCAL, ''), '')
})

test('a value is trimmed before it is stored or sent', () => {
  assert.equal(normalizeKeepAlive('openai', LOCAL, '  1h  '), '1h')
})
