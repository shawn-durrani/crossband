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
  adoptRoomMode, askFlag, cleanPreferredName, displayName, flagCopy,
  FORGET_EXPLAINER, mergeFlag, mismatchByMessage, personSummary,
  reassignOptions, rosterChipText, rosterTitle, SNIFF_EXPLAINER,
  sufficiencyProgress,
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

test('the roster hint states the cost honestly and who is still being learned', () => {
  // Ambient (#28): known voices are identified on-device at no extra cost;
  // the second transcription is the exception (overlap / unplaceable), not
  // the rule - the copy must say the new true thing, not the old one.
  const t = rosterTitle([present('Shawn'), present('Alex', false)])
  assert.match(t, /identified on this device at no extra cost/)
  assert.match(t, /second transcription runs only/)
  assert.match(t, /solo mode/)
  assert.match(t, /Still learning: Alex/)
  assert.match(t, /uncertain/)
  assert.doesNotMatch(t, /roughly double/)
  assert.doesNotMatch(rosterTitle([present('Shawn')]), /Still learning/)
})

test('the sniff explainer describes ambient arming and the solo escape hatch', () => {
  // Ambient (#28): a known voice switches room mode on by itself via the
  // free on-device matcher, and "solo mode" is the stated privacy override.
  assert.match(SNIFF_EXPLAINER, /remembered voices/)
  assert.match(SNIFF_EXPLAINER, /switches on by itself/)
  assert.match(SNIFF_EXPLAINER, /no extra cost/)
  assert.match(SNIFF_EXPLAINER, /solo mode/)
  assert.doesNotMatch(SNIFF_EXPLAINER, /transcribed twice/)
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
    [{ name: 'Alex', display: 'Alex' }, { name: 'Grandma', display: 'Grandma' }])
  // case-insensitive exclusion and de-dup
  assert.deepEqual(reassignOptions(roster, people, ['alex']),
    [{ name: 'Shawn', display: 'Shawn' }, { name: 'Grandma', display: 'Grandma' }])
  assert.deepEqual(reassignOptions(null, null, null), [])
})

test('mergeFlag returns the newest OPEN merge question only (#28)', () => {
  assert.equal(mergeFlag(null), null)
  assert.equal(mergeFlag([{ kind: 'unknown_voice' }]), null)
  const open = { kind: 'merge_question', label: 'Matteo', suspected: 'Mateo' }
  assert.equal(mergeFlag([
    { kind: 'merge_question', label: 'x', suspected: 'y', resolved_at: 5 },
    open,
  ]), open)
})

test('merge question copy asks plainly and never claims a merge happened', () => {
  const copy = flagCopy({ kind: 'merge_question', label: 'Matteo',
                          suspected: 'Mateo' })
  assert.match(copy, /Is Matteo the same person as Mateo\?/)
  assert.match(copy, /Nothing has been merged/)
  assert.match(copy, /dismiss this if they are different people/)
  // junk survives
  assert.ok(flagCopy({ kind: 'merge_question' }).includes('the same person'))
})

test('reassign options show preferred names but send identity names (#28)', () => {
  // The menu SHOWS the corrected spelling; the correction endpoint receives
  // the identity name, so the fix lands on the existing person instead of
  // minting a twin under the preferred spelling.
  const roster = [present('Mateo')]
  const people = [{ name: 'Mateo', preferred_name: 'Matteo' },
                  { name: 'Sam', preferred_name: 'Samantha' }]
  assert.deepEqual(reassignOptions(roster, people, []), [
    { name: 'Mateo', display: 'Mateo' },  // roster row carries no preferred
    { name: 'Sam', display: 'Samantha' },
  ])
  // a roster row with the backend-joined display_name shows it
  const joined = [{ name: 'Mateo', status: 'present', display_name: 'Matteo' }]
  assert.deepEqual(reassignOptions(joined, [], []),
    [{ name: 'Mateo', display: 'Matteo' }])
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

// ── learning progress toward the sufficiency bar (#28 phase 4) ──────────────

test('sufficiencyProgress reports seconds toward the bar, clamped', () => {
  const p = sufficiencyProgress({ seconds: 4.5, sufficient: false }, 6)
  assert.equal(p.done, false)
  assert.equal(p.fraction, 0.75)
  assert.equal(p.label, '4.5s of 6s of clear speech heard')
  // never past 1, never negative
  assert.equal(sufficiencyProgress({ seconds: 99, sufficient: false }, 6).fraction, 1)
  assert.equal(sufficiencyProgress({ seconds: -2, sufficient: false }, 6).fraction, 0)
})

test('sufficiencyProgress is done for a remembered voice and defensive about junk', () => {
  const done = sufficiencyProgress({ seconds: 7.5, sufficient: true }, 6)
  assert.equal(done.done, true)
  assert.equal(done.fraction, 1)
  assert.equal(done.label, 'voice remembered')
  assert.equal(sufficiencyProgress(null, 6), null)
  // a junk/absent target falls back to the default bar (6s)
  assert.equal(sufficiencyProgress({ seconds: 3, sufficient: false }, 0).fraction, 0.5)
  assert.equal(sufficiencyProgress({ seconds: 3, sufficient: false }, 'x').fraction, 0.5)
  // roster rows carry anchor_seconds instead of seconds - both read
  assert.equal(
    sufficiencyProgress({ anchor_seconds: 3, sufficient: false }, 6).fraction, 0.5)
})

test('the roster hint shows each learner\'s progress when the snapshot carries it', () => {
  const roster = [present('Shawn'),
    { id: 2, name: 'Alex', person_id: 'p2', status: 'present',
      sufficient: false, anchor_seconds: 4.5 }]
  const t = rosterTitle(roster, 6)
  assert.match(t, /Still learning: Alex \(4\.5s of 6s of clear speech heard\)/)
  // rows without the field (older snapshots) keep the bare name
  assert.match(rosterTitle([present('Shawn'), present('Alex', false)], 6),
    /Still learning: Alex -/)
})

// ── adoptRoomMode (#28 room commands) ───────────────────────────────────────
//
// A spoken "solo mode" flips the durable flag server-side; the live session
// must follow it - or the doubled transcription keeps running for the rest
// of the call - without ever overriding a session-only toggle the user set
// by hand.

const session = (roomMode, source, active = true) => ({ active, roomMode, source })

test('a server-side arm is always adopted by a live session', () => {
  assert.equal(adoptRoomMode(true, session(false, 'server')), true)
  assert.equal(adoptRoomMode(true, session(false, 'manual')), true)
})

test('a server-side disarm is adopted only when the flag came from the server', () => {
  assert.equal(adoptRoomMode(false, session(true, 'server')), false)
  // a hand-set session-only toggle is the user's alone to switch off
  assert.equal(adoptRoomMode(false, session(true, 'manual')), null)
})

test('a session already in step is left alone', () => {
  assert.equal(adoptRoomMode(true, session(true, 'server')), null)
  assert.equal(adoptRoomMode(false, session(false, 'server')), null)
  assert.equal(adoptRoomMode(false, session(false, 'manual')), null)
})

test('no live session means nothing to adopt', () => {
  assert.equal(adoptRoomMode(true, session(false, 'server', false)), null)
  assert.equal(adoptRoomMode(true, null), null)
  assert.equal(adoptRoomMode(false, undefined), null)
})
