// #161: the seat-save voice rule. The clobber this pins: an unrelated
// seat edit re-sent the editor's stale blank voice_id, clearing an
// assignment made after the editor was seeded; the next voice start then
// re-rolled a different voice.
import assert from 'node:assert/strict'
import test from 'node:test'

import { voiceIdPatch } from './seatSave.js'

test('an untouched selector sends nothing, blank or assigned alike', () => {
  assert.deepEqual(voiceIdPatch({ isNew: false, voiceId: '', seedVoiceId: '' }), {})
  assert.deepEqual(voiceIdPatch({ isNew: false, voiceId: 'abc', seedVoiceId: 'abc' }), {})
  assert.deepEqual(voiceIdPatch({ isNew: false, voiceId: null, seedVoiceId: undefined }), {})
})

test('a changed selector sends its value; blank is an explicit clear', () => {
  assert.deepEqual(voiceIdPatch({ isNew: false, voiceId: 'xyz', seedVoiceId: 'abc' }),
                   { voice_id: 'xyz' })
  assert.deepEqual(voiceIdPatch({ isNew: false, voiceId: '', seedVoiceId: 'abc' }),
                   { voice_id: '' })
})

test('a new seat always carries its selector state', () => {
  assert.deepEqual(voiceIdPatch({ isNew: true, voiceId: '', seedVoiceId: undefined }),
                   { voice_id: '' })
  assert.deepEqual(voiceIdPatch({ isNew: true, voiceId: 'abc', seedVoiceId: undefined }),
                   { voice_id: 'abc' })
})
