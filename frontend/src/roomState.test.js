// Pure-function tests for room-mode roster/flag state (#28 phase 2).
// Run: node --test frontend/src/roomState.test.js
//
// What these pin: the "In the room" chip names exactly the present people
// (the transparency cue must not lie); the ask-fallback and mismatch copy is
// plain English and never claims a label was changed; the tap-to-correct
// menu offers roster-first names minus the labels the turn already carries;
// and the remembered-voices summary states sufficiency honestly.
import { test } from 'node:test'
import assert from 'node:assert/strict'
import {
  askFlag, cleanPreferredName, displayName, flagCopy, FORGET_EXPLAINER,
  mismatchByMessage, personSummary, reassignOptions, rosterChipText,
  rosterTitle,
} from './roomState.js'

const present = (name, sufficient = true) =>
  ({ id: 1, name, person_id: sufficient ? 'p' : '', status: 'present', sufficient })

test('the roster chip names exactly the present people, in order', () => {
  assert.equal(rosterChipText([present('Shawn'), present('Alex')]),
    'In the room: Shawn, Alex')
  assert.equal(rosterChipText([present('Shawn'),
    { name: 'Dave', status: 'left' }]), 'In the room: Shawn')
})

test('an empty or junk roster shows no chip at all', () => {
  assert.equal(rosterChipText([]), '')
  assert.equal(rosterChipText(null), '')
  assert.equal(rosterChipText([{ status: 'present' }, { name: 42 }]), '')
})

test('the roster hint states the cost and who is still being learned', () => {
  const t = rosterTitle([present('Shawn'), present('Alex', false)])
  assert.match(t, /transcribed twice/)
  assert.match(t, /Still learning: Alex/)
  assert.match(t, /uncertain/)
  assert.doesNotMatch(rosterTitle([present('Shawn')]), /Still learning/)
})

test('askFlag returns the newest OPEN unknown-voice flag only', () => {
  const flags = [
    { id: 1, kind: 'unknown_voice', resolved_at: 123 },
    { id: 2, kind: 'mismatch', resolved_at: null },
    { id: 3, kind: 'unknown_voice', resolved_at: null },
  ]
  assert.equal(askFlag(flags).id, 3)
  assert.equal(askFlag([flags[0], flags[1]]), null)
  assert.equal(askFlag(null), null)
})

test('mismatchByMessage keys open mismatch flags by turn', () => {
  const flags = [
    { id: 1, kind: 'mismatch', message_id: 7, resolved_at: null },
    { id: 2, kind: 'mismatch', message_id: 8, resolved_at: 99 },   // resolved
    { id: 3, kind: 'unknown_voice', message_id: 9, resolved_at: null },
  ]
  const by = mismatchByMessage(flags)
  assert.deepEqual(Object.keys(by), ['7'])
  assert.equal(by[7].id, 1)
})

test('flag copy is plain English and never claims the label changed', () => {
  const ask = flagCopy({ kind: 'unknown_voice' })
  assert.match(ask, /Someone new is speaking/)
  assert.match(ask, /name/)
  const mm = flagCopy({ kind: 'mismatch', label: 'Shawn', suspected: 'Alex' })
  assert.match(mm, /labelled Shawn/)
  assert.match(mm, /reads more like Alex/)
  assert.match(mm, /NOT been changed/)
  const anon = flagCopy({ kind: 'mismatch', label: 'Shawn' })
  assert.match(anon, /reads like someone else/)
  assert.equal(flagCopy(null), '')
  assert.equal(flagCopy({ kind: 'unknown' }), '')
})

test('reassign options: roster first, then remembered people, minus current labels', () => {
  const roster = [present('Shawn'), present('Alex'), { name: 'Old', status: 'left' }]
  const people = [{ name: 'Alex' }, { name: 'Grandma' }]
  assert.deepEqual(reassignOptions(roster, people, ['Shawn']),
    ['Alex', 'Grandma'])
  // case-insensitive exclusion and de-dup
  assert.deepEqual(reassignOptions(roster, people, ['alex']),
    ['Shawn', 'Grandma'])
  assert.deepEqual(reassignOptions(null, null, null), [])
})

test('person summary states sufficiency honestly', () => {
  const known = personSummary(
    { name: 'Alex', seconds: 7.5, clip_count: 4, sufficient: true }, 6)
  assert.equal(known.name, 'Alex')
  assert.match(known.detail, /4 clips, 7\.5s/)
  assert.match(known.status, /voice remembered/)
  const learning = personSummary(
    { name: 'Ben', seconds: 1.5, clip_count: 1, sufficient: false }, 6)
  assert.match(learning.detail, /1 clip, 1\.5s/)
  assert.match(learning.status, /still learning/)
  assert.match(learning.status, /1\.5s of 6s/)
  assert.match(learning.status, /uncertain/)
  assert.equal(personSummary(null, 6), null)
})

test('the forget explainer says deletion, plainly', () => {
  assert.match(FORGET_EXPLAINER, /deletes/)
  assert.match(FORGET_EXPLAINER, /this computer/)
})

// ── preferred display names (#28 phase 3) ───────────────────────────────────

test('displayName prefers the correctable name over the introduced one', () => {
  assert.equal(displayName({ name: 'Lex', display_name: 'Alex' }), 'Alex')
  assert.equal(displayName({ name: 'Lex', preferred_name: 'Alex' }), 'Alex')
  assert.equal(displayName({ name: 'Lex' }), 'Lex')
  assert.equal(displayName({ name: 'Lex', display_name: '' }), 'Lex')
  assert.equal(displayName(null), '')
  assert.equal(displayName({ name: 42 }), '')
})

test('the roster chip and hint show display names', () => {
  const roster = [
    { name: 'Shawn', status: 'present', sufficient: true },
    { name: 'Lex', display_name: 'Alex', status: 'present', sufficient: false },
  ]
  assert.equal(rosterChipText(roster), 'In the room: Shawn, Alex')
  assert.match(rosterTitle(roster), /Still learning: Alex/)
})

test('cleanPreferredName trims, bounds and requires a letter', () => {
  assert.equal(cleanPreferredName('  Alex  '), 'Alex')
  assert.equal(cleanPreferredName('A'.repeat(100)).length, 40)
  assert.equal(cleanPreferredName('!!!'), '')
  assert.equal(cleanPreferredName('   '), '')
  assert.equal(cleanPreferredName(''), '')
  assert.equal(cleanPreferredName(null), '')
  assert.equal(cleanPreferredName(42), '')
})

test('person summary shows the preferred name and stays honest about the heard one', () => {
  const renamed = personSummary(
    { name: 'Lex', preferred_name: 'Alex', seconds: 7.5, clip_count: 4, sufficient: true }, 6)
  assert.equal(renamed.name, 'Alex')
  assert.match(renamed.alias, /heard as "Lex"/)
  const plain = personSummary(
    { name: 'Alex', seconds: 7.5, clip_count: 4, sufficient: true }, 6)
  assert.equal(plain.name, 'Alex')
  assert.equal(plain.alias, '')
})
