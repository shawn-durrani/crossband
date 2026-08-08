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
import {
  assignVoiceOrdinals, chipData, chipsForMessage, chipTitle, CHIP_EXPLAINER,
  CROSSTALK_NOTE, crosstalkNote, crosstalkSegments,
} from './voiceChips.js'

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

// ── phase 2 (#28): named chips with per-label uncertainty ──────────────────

const named = (labels, uncertain = [], extra = {}) => ({
  id: 1, speaker: 'user', content: 'hi',
  voice_labels: JSON.stringify({ clusters: ['s0'], labels, uncertain, ...extra }),
})

test('chipData carries per-label uncertainty from the phase-2 payload', () => {
  assert.deepEqual(chipData(named(['Shawn'], [])), [
    { label: 'Shawn', uncertain: false, corrected: false }])
  assert.deepEqual(chipData(named(['Alex'], ['Alex'])), [
    { label: 'Alex', uncertain: true, corrected: false }])
})

test('chipData marks a corrected turn', () => {
  assert.deepEqual(chipData(named(['Alex'], [], { corrected: true })), [
    { label: 'Alex', uncertain: false, corrected: true }])
})

test('chipData on a phase-1 payload degrades to certain ordinals', () => {
  const msg = { id: 1, speaker: 'user',
    voice_labels: JSON.stringify({ clusters: ['a', 'b'],
                                   labels: ['Voice 1', 'Voice 2'] }) }
  assert.deepEqual(chipData(msg).map((c) => [c.label, c.uncertain]),
    [['Voice 1', false], ['Voice 2', false]])
})

test('chipData is as defensive as chipsForMessage', () => {
  assert.deepEqual(chipData({ speaker: 'user', voice_labels: 'junk' }), [])
  assert.deepEqual(chipData({ speaker: 'claude',
    voice_labels: JSON.stringify({ labels: ['Shawn'] }) }), [])
  assert.deepEqual(chipData(null), [])
  // junk uncertain entries are ignored, not crashed on
  assert.deepEqual(chipData(named(['Shawn'], [42, null])), [
    { label: 'Shawn', uncertain: false, corrected: false }])
})

test('chip titles state trust plainly, per state', () => {
  assert.match(chipTitle({ label: 'Shawn', uncertain: false, corrected: false }),
    /Matched to Shawn's remembered voice/)
  assert.match(chipTitle({ label: 'Alex', uncertain: true, corrected: false }),
    /still being learned/)
  assert.match(chipTitle({ label: 'Alex', uncertain: false, corrected: true }),
    /Corrected by you/)
  // a bare ordinal keeps the phase-1 explainer
  assert.equal(chipTitle({ label: 'Voice 2', uncertain: false, corrected: false }),
    CHIP_EXPLAINER)
  assert.equal(chipTitle(null), '')
})

// ── crosstalk (#28 phase 4) ─────────────────────────────────────────────────

const crosstalked = (extra = {}, speaker = 'user') => ({
  id: 9, speaker, content: 'hi',
  voice_labels: JSON.stringify({
    clusters: ['s0', 's1'], labels: ['Shawn', 'Alex'], uncertain: [],
    crosstalk: true, ...extra,
  }),
})

test('the crosstalk note appears only on a marked user turn', () => {
  assert.equal(crosstalkNote(crosstalked()), CROSSTALK_NOTE)
  // plain labelled turn: no note
  assert.equal(crosstalkNote(labelled(['Voice 1', 'Voice 2'])), '')
  // AI turns, junk marker values, junk JSON, absent field: nothing, no crash
  assert.equal(crosstalkNote(crosstalked({}, 'claude')), '')
  assert.equal(crosstalkNote({ id: 1, speaker: 'user', content: 'hi',
    voice_labels: JSON.stringify({ crosstalk: 'yes' }) }), '')
  assert.equal(crosstalkNote({ id: 1, speaker: 'user', content: 'hi',
    voice_labels: '{broken' }), '')
  assert.equal(crosstalkNote(null), '')
})

test('crosstalk segments render bounded, sanitised, with uncertainty kept', () => {
  const msg = crosstalked({ segments: [
    { label: 'Shawn', text: '  pass the salt  ', uncertain: false },
    { label: 'Voice 2', text: 'and pepper' },     // ordinal: uncertain by construction
    { label: 'Alex', text: 'sure', uncertain: true },
  ] })
  assert.deepEqual(crosstalkSegments(msg), [
    { label: 'Shawn', text: 'pass the salt', uncertain: false },
    { label: 'Voice 2', text: 'and pepper', uncertain: true },
    { label: 'Alex', text: 'sure', uncertain: true },
  ])
})

test('crosstalk segments refuse junk and stay bounded', () => {
  // junk entries skipped, never crashed on
  assert.deepEqual(crosstalkSegments(crosstalked({ segments: [
    null, 42, 'x', [], { label: 3, text: 'y' }, { label: 'A' },
    { label: '  ', text: 'y' }, { label: 'A', text: '   ' },
  ] })), [])
  // segments without the marker render nothing (the marker is the license)
  assert.deepEqual(crosstalkSegments({ id: 1, speaker: 'user', content: 'hi',
    voice_labels: JSON.stringify({ labels: ['A'], segments: [
      { label: 'A', text: 'hello' }] }) }), [])
  // count and length bounds hold
  const many = crosstalked({ segments: Array.from({ length: 20 }, (_, i) => (
    { label: `P${i}`, text: 'z'.repeat(500) })) })
  const out = crosstalkSegments(many)
  assert.equal(out.length, 8)
  assert.ok(out.every((s) => s.text.length <= 200))
  assert.deepEqual(crosstalkSegments(null), [])
})
