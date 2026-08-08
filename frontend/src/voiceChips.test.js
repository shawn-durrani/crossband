// Pure-function tests for the room-mode "Voice N" chips (#28 phase 1).
// Run: node --test frontend/src/voiceChips.test.js
//
// What these pin: chips appear ONLY on user turns that actually carry
// diarization labels; malformed or absent data renders nothing (a label is
// best-effort metadata, never worth a crash); and ordinal assignment from raw
// cluster data is first-seen order, stable across calls - the same rule the
// backend's session assignment follows.
import { test } from 'node:test'
import assert from 'node:assert/strict'
import { assignVoiceOrdinals, chipsForMessage, CHIP_EXPLAINER } from './voiceChips.js'

const labelled = (labels, clusters = ['s0', 's1'], speaker = 'user') => ({
  id: 1, speaker, content: 'hi',
  voice_labels: JSON.stringify({ clusters, labels }),
})

test('a labelled user turn shows its labels, in order', () => {
  assert.deepEqual(chipsForMessage(labelled(['Voice 1', 'Voice 2'])),
    ['Voice 1', 'Voice 2'])
})

test('an unlabelled turn shows nothing - absent, empty or null field', () => {
  assert.deepEqual(chipsForMessage({ id: 1, speaker: 'user', content: 'hi' }), [])
  assert.deepEqual(chipsForMessage({ id: 1, speaker: 'user', voice_labels: '' }), [])
  assert.deepEqual(chipsForMessage(null), [])
})

test('only user turns carry chips - labels describe who SPOKE', () => {
  assert.deepEqual(chipsForMessage(labelled(['Voice 1'], ['s0'], 'claude')), [])
  assert.deepEqual(chipsForMessage(labelled(['Voice 1'], ['s0'], 'system')), [])
})

test('malformed persisted data renders nothing, never throws', () => {
  for (const bad of ['not json', '[]', '"x"', '123',
                     JSON.stringify({ labels: 'Voice 1' }),
                     JSON.stringify({ labels: [1, 2] })]) {
    assert.deepEqual(chipsForMessage({ speaker: 'user', voice_labels: bad }), [])
  }
})

test('labels are de-duplicated, trimmed and bounded', () => {
  const chips = chipsForMessage(labelled(
    [' Voice 1 ', 'Voice 1', 'Voice 2', '', 'x'.repeat(60)]))
  assert.deepEqual(chips.slice(0, 2), ['Voice 1', 'Voice 2'])
  assert.ok(chips.every((c) => c.length <= 24))
})

test('clusters without labels derive ordinals - but only for a real multi-voice turn', () => {
  // two clusters, no labels persisted: derive per-message ordinals
  assert.deepEqual(
    chipsForMessage(labelled([], ['sA', 'sB'])), ['Voice 1', 'Voice 2'])
  // a single cluster says nothing worth a chip
  assert.deepEqual(chipsForMessage(labelled([], ['sA'])), [])
})

test('ordinal assignment is first-seen order and stable across calls', () => {
  const first = assignVoiceOrdinals(['s3', 's7'])
  assert.deepEqual(first.labels, ['Voice 1', 'Voice 2'])
  const second = assignVoiceOrdinals(['s7'], first.map)
  assert.deepEqual(second.labels, ['Voice 2'])          // stable
  const third = assignVoiceOrdinals(['s1', 's3'], second.map)
  assert.deepEqual(third.labels, ['Voice 3', 'Voice 1']) // new voice, old kept
})

test('ordinal assignment never mutates its input map', () => {
  const map = { s0: 'Voice 1' }
  assignVoiceOrdinals(['s1'], map)
  assert.deepEqual(map, { s0: 'Voice 1' })
})

test('ordinal assignment skips junk cluster ids', () => {
  assert.deepEqual(assignVoiceOrdinals(['s0', '', null, 42, 's1']).labels,
    ['Voice 1', 'Voice 2'])
  assert.deepEqual(assignVoiceOrdinals(null).labels, [])
})

test('the explainer is plain English and honest about best-effort', () => {
  assert.match(CHIP_EXPLAINER, /Best effort/)
  assert.match(CHIP_EXPLAINER, /not named yet/)
})
