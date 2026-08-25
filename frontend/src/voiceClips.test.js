// Clip audition rows (#68/#90). Every capture path has a plain-English label,
// and unknown sources render verbatim rather than guessed. Only elimination-
// earned clips get the closer-listen flag; owner-reassigned ones drop it and
// say so. Move targets are every other person by display name. Row derivation
// keeps the file token and formats duration, and the delete explainer says
// what it actually does.
import assert from 'node:assert/strict'
import test from 'node:test'

import { clipRow, DELETE_CLIP_EXPLAINER, moveTargets, needsEar,
         sourceLabel } from './voiceClips.js'

test('a reassigned clip says so and drops the closer-listen flag', () => {
  const row = clipRow({ file: 'a', source: 'cold-start', seconds: 2,
                        moved: true })
  assert.equal(row.source, 'reassigned by you')
  assert.equal(row.needsEar, false)
})

test('a set-aside clip says WHY, and noise is not "sounded like" (#219)', () => {
  const mixed = clipRow({ file: 'a', source: 'accumulated', seconds: 2,
                          quarantined: true,
                          quarantine_reason: 'contaminated' })
  assert.equal(mixed.quarantineChip, 'set aside')
  assert.match(mixed.quarantineTitle, /another remembered voice/)
  const noise = clipRow({ file: 'b', source: 'accumulated', seconds: 2,
                          quarantined: true, quarantine_reason: 'not_speech' })
  assert.equal(noise.quarantineChip, 'not a voice')
  assert.match(noise.quarantineTitle, /no speech was heard/)
  // pre-#219 quarantines carry no reason and read as the pairwise verdict
  const legacy = clipRow({ file: 'c', source: 'accumulated', seconds: 2,
                           quarantined: true })
  assert.equal(legacy.quarantineChip, 'set aside')
  // a live clip carries no quarantine copy at all
  const live = clipRow({ file: 'd', source: 'accumulated', seconds: 2 })
  assert.equal(live.quarantineChip, '')
  assert.equal(live.quarantineTitle, '')
})

test('move targets are every other person by display name', () => {
  const people = [
    { person_id: 'a-1', name: 'Catriona', preferred_name: 'Cat' },
    { person_id: 'b-2', name: 'Alex' },
  ]
  assert.deepEqual(moveTargets(people, 'a-1'),
                   [{ person_id: 'b-2', name: 'Alex' }])
  assert.deepEqual(moveTargets(people, 'b-2'),
                   [{ person_id: 'a-1', name: 'Cat' }])
  assert.deepEqual(moveTargets([], 'a-1'), [])
})

test('every known capture path has a plain-English label', () => {
  assert.equal(sourceLabel('accumulated'), 'captured live')
  assert.equal(sourceLabel('cold-start'), 'learnt by elimination')
  assert.equal(sourceLabel('introduction'), 'from an introduction')
  assert.equal(sourceLabel('correction'), 'from your correction')
  assert.equal(sourceLabel('harvested-short'), 'short interjection sample')
})

test('an unknown source renders as itself, never a guess', () => {
  assert.equal(sourceLabel('field-test-9'), 'field-test-9')
  assert.equal(sourceLabel(''), 'unknown')
  assert.equal(sourceLabel(undefined), 'unknown')
})

test('only elimination-earned clips are flagged for a closer listen', () => {
  assert.equal(needsEar({ source: 'cold-start' }), true)
  assert.equal(needsEar({ source: 'accumulated' }), false)
  assert.equal(needsEar({ source: 'introduction' }), false)
})

test('clipRow derives duration, badge state and keeps the file token', () => {
  const row = clipRow({ file: 'a-1.wav', source: 'cold-start', seconds: 2.84,
                        added_at: 1755000000, quarantined: true })
  assert.equal(row.file, 'a-1.wav')
  assert.equal(row.duration, '2.8s')
  assert.equal(row.source, 'learnt by elimination')
  assert.equal(row.needsEar, true)
  assert.equal(row.quarantined, true)
  assert.ok(row.when.length > 0)
})

test('a clip with no timestamp renders an empty when, not epoch', () => {
  assert.equal(clipRow({ file: 'x', source: 'accumulated', seconds: 1 }).when, '')
})

test('the delete explainer says what actually happens', () => {
  assert.match(DELETE_CLIP_EXPLAINER, /re-learns/)
  assert.match(DELETE_CLIP_EXPLAINER, /known but unlearnt/)
})
