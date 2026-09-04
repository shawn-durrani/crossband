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
  adoptRoomMode, AMBIENT_EXPLAINER, askFlag, cleanPreferredName, displayName,
  auditionNotice, selfCollectedNotice,
  flagCopy, FORGET_EXPLAINER, mergeFlag, mismatchByMessage, personSummary,
  reassignOptions, rosterChipText, rosterTitle, sufficiencyProgress,
  CHIP_CONFIRMED, CHIP_LEARNING, CHIP_PENDING, voiceChips, voicePersonChip,
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
  // #28 PR-B: the cloud identity fallback retired, so the hint's cost story
  // simplified - overlap splitting is the ONLY second transcription left,
  // and an unplaceable voice stays unnamed (it is not sent to the cloud to
  // be guessed at). Deliberately updates the pre-PR-B "or a voice cannot be
  // placed locally" wording pin.
  const t = rosterTitle([present('Shawn'), present('Alex', false)])
  assert.match(t, /on this device, at no extra cost/)
  assert.match(t, /second transcription runs only when voices overlap/)
  assert.match(t, /stays unnamed/)
  assert.match(t, /solo mode/)
  assert.match(t, /Still learning: Alex/)
  assert.match(t, /uncertain/)
  assert.doesNotMatch(t, /roughly double/)
  assert.doesNotMatch(t, /cannot be placed locally/)
  assert.doesNotMatch(rosterTitle([present('Shawn')]), /Still learning/)
})

test('the ambient explainer describes ambient arming and the solo escape hatch', () => {
  // Ambient (#28; renamed from the sniff explainer in PR-B - the EL sniff
  // retired): a known voice switches room mode on by itself via the free
  // on-device matcher, and "solo mode" is the stated privacy override.
  // Since the room button became an indicator (#28) this explainer titles
  // the settings disclosure's room row - the old toggle it captioned is
  // gone from the tray.
  assert.match(AMBIENT_EXPLAINER, /remembered voices/)
  assert.match(AMBIENT_EXPLAINER, /switches on by itself/)
  assert.match(AMBIENT_EXPLAINER, /no extra cost/)
  assert.match(AMBIENT_EXPLAINER, /solo mode/)
  assert.doesNotMatch(AMBIENT_EXPLAINER, /transcribed twice/)
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

test('the forget explainer says deletion in both apps, plainly', () => {
  assert.match(FORGET_EXPLAINER, /deletes/)
  assert.match(FORGET_EXPLAINER, /here and in memory/)
  assert.match(FORGET_EXPLAINER, /back to review/)
  assert.doesNotMatch(FORGET_EXPLAINER, /this computer/)
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

// ── the two-part bar and the hygiene guard (#28 PR-B) ───────────────────────

test('sufficiencyProgress reports the missing short-clip half once seconds are met', () => {
  // seconds met, shorts missing: the label says exactly what is missing
  const p = sufficiencyProgress(
    { seconds: 7, short_clips: 0, sufficient: false }, 6, 2)
  assert.equal(p.done, false)
  assert.match(p.label, /enough long speech heard/)
  assert.match(p.label, /2 more short utterances/)
  assert.ok(p.fraction < 1)  // not done means the bar must not read full
  // one of two shorts: singular copy, fraction rises
  const q = sufficiencyProgress(
    { seconds: 7, short_clips: 1, sufficient: false }, 6, 2)
  assert.match(q.label, /1 more short utterance /)
  assert.ok(q.fraction > p.fraction && q.fraction < 1)
  // seconds still short: the seconds count leads regardless of shorts
  const r = sufficiencyProgress(
    { seconds: 3, short_clips: 0, sufficient: false }, 6, 2)
  assert.equal(r.label, '3.0s of 6s of clear speech heard')
  // without short data (older snapshots) the seconds-only reading stands
  assert.equal(
    sufficiencyProgress({ seconds: 7, sufficient: false }, 6, 2).fraction, 1)
})

test('person summary carries the two-part learning copy and the set-aside line', () => {
  const learning = personSummary(
    { name: 'Ben', seconds: 7, short_clips: 0, clip_count: 3,
      sufficient: false }, 6, 2)
  assert.match(learning.status, /still learning/)
  assert.match(learning.status, /short utterance/)
  const aside = personSummary(
    { name: 'Sam', seconds: 8, clip_count: 4, sufficient: true,
      quarantined_count: 2 }, 6, 2)
  assert.match(aside.setAside, /2 clips set aside/)
  assert.match(aside.setAside, /another remembered voice/)
  assert.match(aside.setAside, /no longer take part in matching/)
  const clean = personSummary(
    { name: 'Sam', seconds: 8, clip_count: 4, sufficient: true }, 6, 2)
  assert.equal(clean.setAside, '')
  // #219: noise clips are named as their own problem, not blurred into
  // "sounded like someone else" - the remedies differ.
  const noisy = personSummary(
    { name: 'Sam', seconds: 8, clip_count: 4, sufficient: true,
      quarantined_count: 3, noise_count: 2 }, 6, 2)
  assert.match(noisy.setAside, /1 clip set aside - they sounded more like/)
  assert.match(noisy.setAside, /2 clips set aside - not a voice at all/)
  const allNoise = personSummary(
    { name: 'Sam', seconds: 8, clip_count: 4, sufficient: true,
      quarantined_count: 1, noise_count: 1 }, 6, 2)
  assert.match(allNoise.setAside, /not a voice at all/)
  assert.doesNotMatch(allNoise.setAside, /another remembered voice/)
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
//
// Location note (#28: the room button became an indicator): the tray no
// longer has a room toggle - the manual arm/disarm live in the voice
// settings disclosure as "switch on now" / "switch off for this chat".
// Both go through the same durable plumbing as ever (manualRoomMode /
// roomModeOff in App.jsx), so adoptRoomMode's rules are unchanged and
// these pins stand as they are: 'manual' remains the defensive branch for
// a session-only flag, which no current UI path sets.

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

// ---- per-person voice chips for the dock (#28, the dock refinement) ------
//
// What these pin: the three states are decided by what the app actually
// knows about a voice (remembered, part-learned, nothing yet); the chip
// text says the same thing on every surface; the order is best-known-first
// and stable; and the source is the ROOM when there is one, the remembered
// voices when there is not.

const chipPerson = (name, extra = {}) =>
  ({ person_id: name.toLowerCase(), name, status: 'present', ...extra })

test('a remembered voice is a confirmed chip - a tick beside the name', () => {
  const chip = voicePersonChip(chipPerson('Alex', { sufficient: true, seconds: 9 }), 6, 3)
  assert.equal(chip.state, CHIP_CONFIRMED)
  // The name is NOT rewritten: the owner asked for a tick beside the name
  // rather than swapped label text, so the label stays exactly the name.
  assert.equal(chip.label, 'Alex')
  assert.equal(chip.short, 'Alex ✓')
  assert.match(chip.title, /remembered/)
})

test('the tick never claims the live turn (#139)', () => {
  // The field failure: a green profile tick beside a guest while their
  // live turns still read "Identity pending" - the tick looked like a
  // live recognition verdict. The copy must scope it to the profile and
  // point at the turn's own label for the live answer.
  const chip = voicePersonChip(chipPerson('Alex', { sufficient: true, seconds: 9 }), 6, 3)
  assert.match(chip.title, /not the turn being spoken/)
  assert.match(chip.title, /own label/)
  assert.doesNotMatch(chip.title, /named automatically/)
})

test('a part-learned voice shows progress in seconds', () => {
  const chip = voicePersonChip(
    chipPerson('Sam', { sufficient: false, seconds: 4.4, short_clips: 0 }), 6, 3)
  assert.equal(chip.state, CHIP_LEARNING)
  assert.equal(chip.label, 'Sam · learning 4s')
  assert.equal(chip.short, 'Sam 4s')
  assert.match(chip.title, /still being learned/)
})

test('a voice with nothing banked yet is neutral, not a fake progress bar', () => {
  // Under a second reads as "learning 0s", which says less than nothing -
  // the neutral state is the honest one, and it is exactly where a cold
  // owner starts, before their first turn is banked by elimination.
  for (const seconds of [0, 0.4]) {
    const chip = voicePersonChip(chipPerson('Dave', { sufficient: false, seconds }), 6, 3)
    assert.equal(chip.state, CHIP_PENDING, String(seconds))
    assert.equal(chip.label, 'Dave')
    assert.equal(chip.short, 'Dave')
  }
})

test('roster rows and remembered people both derive a chip', () => {
  // Roster rows carry anchor_seconds; remembered people carry seconds.
  const row = voicePersonChip(
    { name: 'Sam', status: 'present', sufficient: false, anchor_seconds: 3.2 }, 6, 3)
  assert.equal(row.label, 'Sam · learning 3s')
  // and the preferred spelling wins wherever one is set (#28: naming is law)
  const renamed = voicePersonChip(
    { name: 'Dave', preferred_name: 'Mateo', sufficient: true, seconds: 9 }, 6, 3)
  assert.equal(renamed.name, 'Mateo')
})

test('junk people produce no chip rather than a crash', () => {
  assert.equal(voicePersonChip(null, 6, 3), null)
  assert.equal(voicePersonChip({}, 6, 3), null)
  assert.equal(voicePersonChip({ name: '   ' }, 6, 3), null)
})

test('chips read best-known-first and keep source order within a state', () => {
  const roster = [
    chipPerson('Dave', { sufficient: false, anchor_seconds: 0 }),
    chipPerson('Sam', { sufficient: false, anchor_seconds: 4 }),
    chipPerson('Alex', { sufficient: true, anchor_seconds: 9 }),
    chipPerson('Mateo', { sufficient: true, anchor_seconds: 8 }),
  ]
  assert.deepEqual(voiceChips(roster, [], 6, 3).map((c) => c.name),
                   ['Alex', 'Mateo', 'Sam', 'Dave'])
})

test('chips describe the room when there is one, the remembered voices when there is not', () => {
  const roster = [chipPerson('Sam', { sufficient: true, anchor_seconds: 9 })]
  const people = [{ person_id: 'alex', name: 'Alex', sufficient: true, seconds: 9 }]
  // A roster means a room: the chips are who is IN it.
  assert.deepEqual(voiceChips(roster, people, 6, 3).map((c) => c.name), ['Sam'])
  // No roster (room mode off): every spoken turn is still checked against
  // the remembered voices, so those are what a chip can honestly describe.
  assert.deepEqual(voiceChips([], people, 6, 3).map((c) => c.name), ['Alex'])
  // Someone who has left the room is not in it.
  assert.deepEqual(
    voiceChips([{ name: 'Sam', status: 'left', sufficient: true }], people, 6, 3)
      .map((c) => c.name), ['Alex'])
  assert.deepEqual(voiceChips(null, null, 6, 3), [])
})

test('the same person never chips twice', () => {
  const roster = [chipPerson('Sam', { sufficient: true }),
                  chipPerson('Sam', { sufficient: true })]
  assert.equal(voiceChips(roster, [], 6, 3).length, 1)
})

// ---- the tray renders no room on/off control (#28) ------------------------
//
// The room button became an indicator: arming is fully automatic (ambient
// arm on unknown or remembered voices, spoken commands, introductions), so
// a toggle in the tray misled users into thinking they had to press it.
// React components deliberately have no test infrastructure here, so this
// pin greps the JSX the same way the backend pins its one-insert-path rule:
// the tray must SHOW room state (the modeReadout indicator) and never
// operate it, while the two demoted manual doors - "switch on now" and
// "switch off for this chat" - survive in the settings tier for the
// degraded path (matcher unavailable = no automatic arming), day-one
// enrolment, and anyone who cannot speak a command.

import { readFileSync } from 'node:fs'

const componentSrc = (name) =>
  readFileSync(new URL(`./components/${name}`, import.meta.url), 'utf8')

test('the tray renders no room on/off control - only the indicator (#28)', () => {
  for (const name of ['VoiceDock.jsx', 'MobileVoiceCall.jsx']) {
    const src = componentSrc(name)
    // No toggle affordance anywhere: nothing flips the room from a click
    // on its current state, and no control claims a pressed room state.
    assert.doesNotMatch(src, /onRoomModeChange(\?\.)?\(!roomMode\)/, name)
    assert.doesNotMatch(src, /aria-pressed=\{!!roomMode\}/, name)
    // The indicator renders the derived mode - the one source of truth.
    assert.match(src, /mode\.label/, name)
    assert.match(src, /mode\.title/, name)
    // The escape hatches survive, demoted to the settings tier: the manual
    // arm is an explicit switch-ON (never a toggle of current state) and
    // the durable solo-mode disarm is still wired.
    assert.match(src, /onRoomModeChange(\?\.)?\(true\)/, name)
    assert.match(src, /onRoomModeOff/, name)
    assert.match(src, /switch on now/, name)
    assert.match(src, /switch off for this chat/, name)
  }
})

test('the audition ask (#83): only unvouched sufficient banks, honest about pausing', () => {
  assert.equal(auditionNotice(null), null)
  assert.equal(auditionNotice({ needs_audition: false }), null)
  const paused = auditionNotice({ needs_audition: true, id_paused: true,
    name: 'Alex', preferred_name: 'Alex' })
  assert.match(paused, /not naming or seating anyone/)
  assert.match(paused, /Alex/)
  const legacy = auditionNotice({ needs_audition: true, id_paused: false,
    name: 'Sam' })
  assert.match(legacy, /listen to the clips/)
  assert.ok(!/not naming/.test(legacy))
})

test('the outlived-backing ask (#221) says what actually happened', () => {
  // A vouched bank whose human-backed clips rotated out was NOT "learnt
  // without an introduction" - the copy must not claim it was.
  const low = auditionNotice({ needs_audition: true, id_paused: true,
    trust: 'low', name: 'Sam' })
  assert.match(low, /stood behind has been replaced/)
  assert.match(low, /matched only weakly/)
  assert.match(low, /not naming or seating anyone/)
  assert.ok(!/without an introduction/.test(low))
  // high trust: no ask, just the quieter self-collected note
  assert.equal(auditionNotice({ needs_audition: false, trust: 'high' }), null)
  const note = selfCollectedNotice({ trust: 'high', name: 'Sam' })
  assert.match(note, /rotated out/)
  assert.match(note, /self-collected/)
  assert.equal(selfCollectedNotice({ trust: 'human' }), '')
  assert.equal(selfCollectedNotice({ trust: 'low' }), '')
  assert.equal(selfCollectedNotice(null), '')
})
