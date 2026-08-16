// Tests for the local-model thinking control (#159). The rules that matter:
// the field is offered only where it can actually fire, the vocabulary matches
// the backend's, the /no_think prompt hack is never reached by default, and a
// control that stops applying is cleared rather than quietly stored.
// Run: node --test frontend/src/thinkingControl.test.js
import { test } from 'node:test'
import assert from 'node:assert/strict'
import {
  THINKING_CONTROLS,
  thinkingOptions,
  thinkingSupport,
  normalizeThinkingControl,
  thinkingSummary,
} from './thinkingControl.js'

const LOCAL = 'http://127.0.0.1:8080/v1'

test('vocabulary matches backend/providers.py THINKING_CONTROL_CHOICES', () => {
  assert.deepEqual(THINKING_CONTROLS.map((c) => c.value), [
    '', 'chat_template_kwargs', 'enable_thinking', 'ollama_think', 'no_think_hint',
  ])
})

test('every option carries a label and a plain-English hint', () => {
  for (const c of THINKING_CONTROLS) {
    assert.ok(c.label.length > 0, `${c.value} has no label`)
    assert.ok(c.hint.length > 0, `${c.value} has no hint`)
  }
})

test('the /no_think hack is labelled a prompt hint, not a switch', () => {
  const hack = THINKING_CONTROLS.find((c) => c.value === 'no_think_hint')
  assert.match(hack.label, /no_think/)
  assert.match(hack.hint, /prompt hack/i)
})

test('Anthropic seats are offered Default only', () => {
  assert.deepEqual(thinkingOptions('anthropic').map((c) => c.value), [''])
})

test('OpenAI-compatible seats are offered every mechanism', () => {
  assert.equal(thinkingOptions('openai').length, THINKING_CONTROLS.length)
})

test('supported only for an OpenAI-compatible seat with its own base URL', () => {
  assert.equal(thinkingSupport('openai', LOCAL).ok, true)
  assert.equal(thinkingSupport('openai', '').ok, false)
  assert.equal(thinkingSupport('openai', '   ').ok, false)
  assert.equal(thinkingSupport('anthropic', LOCAL).ok, false)
})

test('the unsupported notes say where the setting lives instead', () => {
  assert.match(thinkingSupport('anthropic', '').note, /Reasoning effort/i)
  assert.match(thinkingSupport('openai', '').note, /base URL/i)
})

test('the supported note promises a loud failure, not a silent drop', () => {
  assert.match(thinkingSupport('openai', LOCAL).note, /rejects/i)
})

test('a supported control survives normalization unchanged', () => {
  assert.equal(
    normalizeThinkingControl('openai', LOCAL, 'chat_template_kwargs'),
    'chat_template_kwargs')
})

test('dropping the base URL clears the control instead of storing a no-op', () => {
  assert.equal(normalizeThinkingControl('openai', '', 'chat_template_kwargs'), '')
})

test('switching a local seat to Anthropic clears the control', () => {
  assert.equal(normalizeThinkingControl('anthropic', LOCAL, 'ollama_think'), '')
})

test('an unknown value never reaches the backend', () => {
  assert.equal(normalizeThinkingControl('openai', LOCAL, 'enable_thinking_pls'), '')
  assert.equal(normalizeThinkingControl('openai', LOCAL, undefined), '')
})

test('summary is empty for a default seat and for one that cannot apply it', () => {
  assert.equal(thinkingSummary({ provider: 'openai', base_url: LOCAL }), '')
  assert.equal(thinkingSummary({}), '')
  assert.equal(
    thinkingSummary({ provider: 'openai', base_url: '', thinking_control: 'ollama_think' }),
    '')
})

test('summary names the prompt hack so it is never mistaken for a real switch', () => {
  assert.equal(
    thinkingSummary({ provider: 'openai', base_url: LOCAL, thinking_control: 'ollama_think' }),
    'thinking off')
  assert.match(
    thinkingSummary({ provider: 'openai', base_url: LOCAL, thinking_control: 'no_think_hint' }),
    /no_think/)
})
