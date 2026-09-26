// The voice model picker's rows and sentences (#480).
// Run: node --test frontend/src/ttsModels.test.js
import { test } from 'node:test'
import assert from 'node:assert/strict'
import {
  AUTO, appChoiceRows, appSettingName, automaticLabel, choiceNote,
  lockedNote, optionText, refusedNote, seatChoiceRows, sourceNote,
} from './ttsModels.js'

// Shaped like GET /api/voice/models.
function payload(over = {}) {
  return {
    setting: 'eleven_flash_v2_5',
    speaking: 'eleven_flash_v2_5',
    automatic_pick: 'eleven_v3_conversational',
    automatic_pick_name: 'Eleven v3 Conversational',
    automatic_rule: 'Automatic picks the newest model.',
    options: [
      { value: 'eleven_v3_conversational', label: 'Eleven v3 Conversational',
        note: 'most expressive for live talk', refused: false },
      { value: 'eleven_flash_v2_5', label: 'Eleven Flash v2.5',
        note: 'fastest to start', refused: false },
      { value: 'eleven_turbo_v2_5', label: 'Eleven Turbo v2.5', note: '', refused: false },
    ],
    refused: [],
    source: 'live',
    default: 'eleven_flash_v2_5',
    locked_by_env: false,
    ...over,
  }
}

test('every option shows its name and its note', () => {
  assert.equal(optionText({ label: 'Eleven Flash v2.5', note: 'fastest to start' }),
    'Eleven Flash v2.5 · fastest to start')
  assert.equal(optionText({ label: 'Eleven Turbo v2.5', note: '' }), 'Eleven Turbo v2.5')
})

test('the app select leads with Automatic and names what it picks today', () => {
  const rows = appChoiceRows(payload())
  assert.deepEqual(rows[0], { value: AUTO,
    label: 'Automatic: newest supported (now Eleven v3 Conversational)' })
  assert.deepEqual(rows.map((r) => r.value),
    [AUTO, 'eleven_v3_conversational', 'eleven_flash_v2_5', 'eleven_turbo_v2_5'])
  assert.equal(automaticLabel({}), 'Automatic: newest supported')
})

test('a saved model the list no longer offers still shows, greyed out', () => {
  const rows = appChoiceRows(payload({ setting: 'eleven_old_v1' }))
  const last = rows[rows.length - 1]
  assert.deepEqual(last, { value: 'eleven_old_v1',
    label: 'eleven_old_v1 (not offered now)', disabled: true })
  assert.equal(appChoiceRows(null).length, 0)
})

test("a seat's first row follows the app setting and says what that is", () => {
  const rows = seatChoiceRows(payload(), '')
  assert.deepEqual(rows[0], { value: '',
    label: 'Same as the app setting (Eleven Flash v2.5)' })
  assert.equal(rows[1].value, AUTO)
  const auto = seatChoiceRows(payload({ setting: AUTO }), '')
  assert.equal(auto[0].label,
    'Same as the app setting (Automatic, now Eleven v3 Conversational)')
  assert.equal(seatChoiceRows(payload(), 'gone_v9').at(-1).disabled, true)
})

test('the app setting reads in words', () => {
  assert.equal(appSettingName(payload()), 'Eleven Flash v2.5')
  assert.equal(appSettingName(payload({ setting: AUTO })),
    'Automatic, now Eleven v3 Conversational')
})

test('the note under a choice says what the next reply will use', () => {
  const data = payload()
  assert.equal(choiceNote(data, AUTO), 'Automatic picks the newest model.')
  assert.equal(choiceNote(data, ''), 'This seat speaks with whatever the app setting picks.')
  // a listed model's row already says what it's good at
  assert.equal(choiceNote(data, 'eleven_flash_v2_5'), '')
  assert.equal(choiceNote(data, 'gone_v9'),
    "ElevenLabs doesn't offer gone_v9 now, so replies use Eleven Flash v2.5 instead.")
})

test('a refused pick says the reply falls back, and Automatic says it skips', () => {
  const data = payload({
    refused: ['eleven_v3_conversational'],
    options: payload().options.map((o) => (
      o.value === 'eleven_v3_conversational' ? { ...o, refused: true } : o)),
  })
  assert.equal(choiceNote(data, 'eleven_v3_conversational'),
    'ElevenLabs refused Eleven v3 Conversational on live voice today, '
    + 'so replies use Eleven Flash v2.5 for now.')
  assert.equal(refusedNote(data),
    'ElevenLabs refused Eleven v3 Conversational on live voice in the last day, '
    + 'so Automatic skips it for now.')
  assert.equal(refusedNote(payload({ refused: ['eleven_v4', 'eleven_v5'] })),
    'ElevenLabs refused eleven_v4 and eleven_v5 on live voice in the last day, '
    + 'so Automatic skips them for now.')
  assert.equal(refusedNote(payload()), '')
})

test('the list says where it came from, and an environment lock says so', () => {
  assert.match(sourceNote(payload()), /your ElevenLabs account/)
  assert.match(sourceNote(payload({ source: 'pinned' })), /built-in list/)
  assert.equal(lockedNote(payload()), '')
  assert.match(lockedNote(payload({ locked_by_env: true })), /CROSSBAND_TTS_MODEL/)
})
