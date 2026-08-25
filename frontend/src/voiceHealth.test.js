// The voice health strip's derivations (#28). Run:
// node --test frontend/src/voiceHealth.test.js
//
// What these pin: every matcher state maps to an honest plain-English
// readout (#28 PR-B - degraded states say turns stay unnamed and arming is
// manual; they may NOT promise a cloud fallback, because the cloud identity
// path retired); the mode readout is the ONE source of the tray's room
// INDICATOR (#28: the room button became an indicator - room state is
// shown, never operated, from the tray) with its three states and their
// one-sentence automation tooltips; the live pulse formats path and latency
// exactly ("local · 227ms", "cloud · 1.9s", "pending" only while a session
// is live); known-voice lines reuse the people snapshot's two-part
// sufficiency; close-pair warnings name both voices; and junk input renders
// nothing rather than crashing.
import { test } from 'node:test'
import assert from 'node:assert/strict'
import {
  boundedChips, closeVoiceLines, collapsedVoiceSummary, healthStrip,
  knownVoiceLines, learningAge, learningLines, matcherReadout, modeReadout,
  pulseReadout,
} from './voiceHealth.js'

test('matcher readouts cover every state and mark the degraded ones', () => {
  assert.equal(matcherReadout('ready').label, 'matcher ready')
  assert.equal(matcherReadout('ready').degraded, false)
  assert.equal(matcherReadout('fetching').label, 'fetching model…')
  assert.equal(matcherReadout('fetching').degraded, true)
  assert.equal(matcherReadout('cold').label, 'matcher idle')
  // #28 PR-B: 'cloud fallback' retired with the cloud identity path - a
  // degraded matcher now reads as exactly what it is.
  assert.equal(matcherReadout('unavailable').label, 'matcher unavailable')
  assert.equal(matcherReadout('unavailable').degraded, true)
  assert.equal(matcherReadout('disabled').label, 'matcher off')
  // an unknown state reads as the honest worst case, never a crash
  assert.equal(matcherReadout('what').label, 'matcher unavailable')
  assert.equal(matcherReadout(undefined).label, 'matcher unavailable')
})

test('degraded readouts are honest: unnamed turns and manual arming, never a cloud promise', () => {
  // Deliberately replaces the pre-PR-B pin that degraded states "say the
  // cloud takes over" - there is no cloud identity fallback any more (#28
  // PR-B), so promising one would be a lie.
  for (const state of ['fetching', 'unavailable', 'disabled']) {
    const r = matcherReadout(state)
    assert.match(r.title, /not named|unnamed/i, state)
    assert.match(r.title, /by hand|manual/i, state)
    assert.doesNotMatch(r.title, /cloud (check|fallback)|slower cloud/i, state)
    // Since the room button became an indicator (#28) there is no tray
    // toggle for this copy to point at - the manual door it may name is
    // the switch in the voice settings.
    assert.doesNotMatch(r.title, /the toggle/i, state)
  }
})

test('the mode readout is the indicator: room on · N, listening, solo', () => {
  // #28, the room button became an indicator: these labels ARE the tray
  // chip, so they are pinned exactly. Deliberately updates the older
  // "room on · N in the room" / "ambient listening" wordings - the chip
  // must stay short, and modeReadout is the one source of the string.
  const on = modeReadout({ matcher: 'ready',
    chat: { room_mode: true, ambient_off: false, roster_count: 3 } })
  assert.equal(on.label, 'room on · 3')
  assert.equal(on.state, 'on')
  assert.match(on.title, /solo mode/)
  assert.equal(
    modeReadout({ chat: { room_mode: true, ambient_off: false, roster_count: 0 } }).label,
    'room on')
  const solo = modeReadout({ matcher: 'ready',
    chat: { room_mode: false, ambient_off: true, roster_count: 0 } })
  assert.equal(solo.label, 'solo')
  assert.equal(solo.state, 'solo')
  // The solo tooltip names both ways back in: the spoken command and the
  // settings switch (the demoted manual door).
  assert.match(solo.title, /group mode/)
  assert.match(solo.title, /switch on now/)
  const listening = modeReadout({ matcher: 'ready',
    chat: { room_mode: false, ambient_off: false, roster_count: 0 } })
  assert.equal(listening.label, 'listening')
  assert.equal(listening.state, 'listening')
  // One sentence explaining the automation, with the spoken way out. The
  // pre-indicator ambient copy (its PR-C voice-confirmed honesty) moved
  // off this line - the turn chips carry the voice-confirmed marker.
  assert.match(listening.title, /switches itself on when another voice speaks/)
  assert.match(listening.title, /solo mode/)
  // no chat block: no line
  assert.equal(modeReadout({ chat: null }), null)
  assert.equal(modeReadout(null), null)
})

test('a degraded matcher makes the listening tooltip honest about arming', () => {
  // Pinned behaviour: matcher unavailable = no automatic arming. The
  // indicator still reads "listening" (the spoken doors do still work),
  // but the tooltip must not promise the room arms on a voice - and it
  // names the settings switch, the door that always works (#28: the room
  // button became an indicator; the manual arm lives in the settings).
  for (const state of ['fetching', 'unavailable', 'disabled']) {
    const r = modeReadout({ matcher: state,
      chat: { room_mode: false, ambient_off: false, roster_count: 0 } })
    assert.equal(r.label, 'listening', state)
    assert.match(r.title, /unavailable/, state)
    assert.match(r.title, /voice settings/, state)
    assert.doesNotMatch(r.title, /switches itself on when another voice speaks/, state)
  }
  // and a healthy or absent matcher keeps the plain automation sentence
  for (const matcher of ['ready', 'cold', undefined]) {
    const r = modeReadout({ matcher,
      chat: { room_mode: false, ambient_off: false, roster_count: 0 } })
    assert.match(r.title, /switches itself on/, String(matcher))
  }
})

test('the pulse formats path and latency exactly', () => {
  assert.equal(pulseReadout({ path: 'local', ms: 227, age_s: 1 }).label,
    'local · 227ms')
  assert.equal(pulseReadout({ path: 'cloud', ms: 1900, age_s: 2 }).label,
    'cloud · 1.9s')
  assert.equal(pulseReadout({ path: 'local', ms: 999.4, age_s: 0 }).label,
    'local · 999ms')
  assert.equal(pulseReadout({ path: 'cloud', ms: 1000, age_s: 0 }).label,
    'cloud · 1.0s')
})

test('the pulse says pending only while a session is live', () => {
  assert.equal(pulseReadout(null, true).label, 'pending')
  assert.equal(pulseReadout(null, false), null)
  // junk decisions degrade to pending/null, never crash
  assert.equal(pulseReadout({ path: 'teleport', ms: 5 }, true).label, 'pending')
  assert.equal(pulseReadout({ path: 'local', ms: 'fast' }, false), null)
})

test('known-voice lines show display names and honest progress', () => {
  const people = [
    { name: 'Mateo', preferred_name: 'Matteo', seconds: 8.1, sufficient: true },
    { name: 'Sam', preferred_name: 'Sam', seconds: 3.2, sufficient: false },
  ]
  assert.deepEqual(knownVoiceLines(people, 6), [
    { name: 'Matteo', done: true, label: 'remembered' },
    { name: 'Sam', done: false, label: '3.2s of 6s' },
  ])
  assert.deepEqual(knownVoiceLines(null, 6), [])
  assert.deepEqual(knownVoiceLines([null, 42, {}], 6), [])
})

test('the two-part bar surfaces the missing short-clip half (#28 PR-B)', () => {
  const people = [
    // seconds met, shorts missing: the label says so
    { name: 'Sam', seconds: 7.0, short_clips: 0, sufficient: false },
    // both missing: the seconds count leads, shorts ride along
    { name: 'Dave', seconds: 3.0, short_clips: 1, sufficient: false },
  ]
  const lines = knownVoiceLines(people, 6, 2)
  assert.equal(lines[0].label, '7.0s of 6s · 0 of 2 short clips')
  assert.equal(lines[1].label, '3.0s of 6s · 1 of 2 short clips')
  // without a short target (older snapshot), the seconds-only label stands
  assert.equal(knownVoiceLines(people, 6)[0].label, '7.0s of 6s')
})

test('close-pair warnings name both voices once, by display name', () => {
  const people = [
    { person_id: 'p1', name: 'Shawn', seconds: 8, sufficient: true, close_to: ['p2'] },
    { person_id: 'p2', name: 'Sam', preferred_name: 'Sammy', seconds: 8,
      sufficient: true, close_to: ['p1'] },
  ]
  assert.deepEqual(closeVoiceLines(people),
    ['Shawn and Sammy sound close - matching is stricter'])
  // an id with no person behind it renders nothing; junk never crashes
  assert.deepEqual(closeVoiceLines([{ person_id: 'p1', name: 'A', close_to: ['nope'] }]), [])
  assert.deepEqual(closeVoiceLines(null), [])
  assert.deepEqual(closeVoiceLines([null, {}]), [])
})

test('the strip assembles all blocks, and nothing without a snapshot', () => {
  const strip = healthStrip({
    health: {
      matcher: 'ready',
      chat: { room_mode: true, ambient_off: false, roster_count: 2 },
      last_decision: { path: 'local', ms: 120, age_s: 3 },
    },
    people: [{ name: 'Sam', seconds: 7, sufficient: true }],
    sufficientSeconds: 6,
    sessionActive: true,
  })
  assert.equal(strip.matcher.label, 'matcher ready')
  // "room on · 2" since the room button became an indicator (#28): the
  // strip's mode IS the tray chip, kept short.
  assert.equal(strip.mode.label, 'room on · 2')
  assert.equal(strip.pulse.label, 'local · 120ms')
  assert.deepEqual(strip.voices, [{ name: 'Sam', done: true, label: 'remembered' }])
  assert.deepEqual(strip.close, [])
  assert.equal(healthStrip({ health: null, people: [], sessionActive: true }), null)
})

// ---- the bounded chip row and the collapsed line (#28, dock refinement) --
//
// The tray's status used to be one unbounded run of prose that overflowed
// as soon as a second voice existed. What these pin: the row has a hard
// ceiling and the rest collapse into a "+N" that still names them; the
// mobile one-liner is exactly "<status> · <first chip> +N"; and the strip
// hands the components a ready-made bounded row, so the two surfaces can
// never overflow by different rules.

const chip = (name, state = 'confirmed') =>
  ({ key: name.toLowerCase(), name, state, label: name, short: `${name} ✓`, title: '' })

test('the chip row is bounded, and the overflow still names who is missing', () => {
  const all = ['Alex', 'Sam', 'Dave', 'Mateo'].map((n) => chip(n))
  const bounded = boundedChips(all, 2)
  assert.deepEqual(bounded.shown.map((c) => c.name), ['Alex', 'Sam'])
  assert.equal(bounded.hidden, 2)
  assert.equal(bounded.more, '+2')
  assert.equal(bounded.moreTitle, 'Also: Dave, Mateo')
  // The default ceiling fits all four, and nothing hidden means no "+N" at
  // all - an empty badge is noise.
  assert.equal(boundedChips(all).more, '')
  const few = boundedChips(all.slice(0, 2), 4)
  assert.equal(few.more, '')
  assert.equal(few.moreTitle, '')
})

test('the bounded row survives junk and silly limits', () => {
  assert.deepEqual(boundedChips(null).shown, [])
  assert.deepEqual(boundedChips([null, {}, chip('Alex')]).shown.map((c) => c.name),
                   ['Alex'])
  assert.equal(boundedChips([chip('Alex'), chip('Sam')], 0).shown.length, 2)
  assert.equal(boundedChips([chip('Alex')], 1).shown.length, 1)
})

test('the mobile one-liner reads "Listening · Alex ✓ +1"', () => {
  assert.equal(
    collapsedVoiceSummary('Listening…', [chip('Alex'), chip('Sam')]),
    'Listening · Alex ✓ +1')
  // one voice, no overflow badge
  assert.equal(collapsedVoiceSummary('Listening…', [chip('Alex')]),
               'Listening · Alex ✓')
  // no voices: the line is exactly the status the screen always showed
  assert.equal(collapsedVoiceSummary('Listening…', []), 'Listening')
  assert.equal(collapsedVoiceSummary('Thinking…', null), 'Thinking')
  assert.equal(collapsedVoiceSummary('', [chip('Alex')]), 'Alex ✓')
})

test('the strip hands over a ready-bounded chip row', () => {
  const strip = healthStrip({
    health: { matcher: 'ready', chat: { room_mode: true, roster_count: 2 },
              last_decision: null },
    roster: [{ person_id: 'a', name: 'Alex', status: 'present', sufficient: true,
               anchor_seconds: 9 },
             { person_id: 's', name: 'Sam', status: 'present', sufficient: false,
               anchor_seconds: 4 }],
    people: [],
    sufficientSeconds: 6,
    sessionActive: true,
  })
  assert.deepEqual(strip.chips.shown.map((c) => c.label),
                   ['Alex', 'Sam · learning 4s'])
  assert.equal(strip.chips.more, '')
  // and the bound is honoured from the strip too
  const many = healthStrip({
    health: { matcher: 'ready', chat: null, last_decision: null },
    roster: ['Alex', 'Sam', 'Dave', 'Mateo'].map((n) => (
      { person_id: n, name: n, status: 'present', sufficient: true })),
    people: [], sufficientSeconds: 6, sessionActive: true, maxChips: 2,
  })
  assert.equal(many.chips.shown.length, 2)
  assert.equal(many.chips.more, '+2')
})

test('the pulse says WHY a turn went unnamed, not just that it did', () => {
  // #28, thirteenth field test: "identity pending" hid two different
  // problems. The matcher already knows which; the strip now says it.
  const quiet = pulseReadout({ path: 'unresolved', reason: 'too_short', ms: 40 })
  assert.match(quiet.label, /too short/)
  assert.equal(quiet.unresolved, true)
  const unsure = pulseReadout({ path: 'unresolved', reason: 'below_threshold', ms: 90 })
  assert.match(unsure.label, /not recognised/)
  assert.match(unsure.title, /did not match a remembered voice/)
  // the speech gate's defer reads as "not a voice", never as a shrug
  const noise = pulseReadout({ path: 'unresolved', reason: 'not_speech', ms: 12 })
  assert.match(noise.label, /not a voice/)
  assert.match(noise.title, /not speech/)
  // an unknown reason still degrades to something honest
  assert.match(pulseReadout({ path: 'unresolved', reason: 'wat', ms: 1 }).label,
    /not named/)
  // and a normal identification is unchanged
  assert.match(pulseReadout({ path: 'local', ms: 227 }).label, /local · 227ms/)
})

test('learning lines distinguish still-growing from refreshing', () => {
  const people = [{ person_id: 'p1', name: 'Alex' },
                  { person_id: 'p2', name: 'Sam' }]
  const learning = {
    p1: { clips: 3, seconds: 21.4, at_capacity: false, last_learned_age_s: 45 },
    p2: { clips: 8, seconds: 54.4, at_capacity: true, last_learned_age_s: 7200 },
  }
  const [alex, sam] = learningLines(people, learning)
  assert.match(alex.label, /Alex · still learning · 3 clips/)
  assert.match(alex.title, /just now/)
  assert.match(sam.label, /Sam · refreshing · 8 clips/)
  assert.match(sam.title, /2h ago/)
  assert.match(sam.title, /replace weaker ones/)
  // no snapshot, no invented lines
  assert.deepEqual(learningLines(people, null), [])
  assert.deepEqual(learningLines(people, { p9: {} }), [])
})

test('learning age reads in plain english', () => {
  assert.equal(learningAge(10), 'just now')
  assert.equal(learningAge(600), '10m ago')
  assert.equal(learningAge(7200), '2h ago')
  assert.equal(learningAge(172800), '2d ago')
  assert.equal(learningAge(null), 'not yet')
})
