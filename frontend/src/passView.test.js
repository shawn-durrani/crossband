// Tests for the pass on screen (#98, #456): a seat's [pass] never draws as
// text, a seat turn with nothing to show draws no bubble, and Copy chat
// leaves both out while keeping the user's turns and real replies.
// Run: node --test frontend/src/passView.test.js
import { test } from 'node:test'
import assert from 'node:assert/strict'
import { isPassShaped, shownText, isSilentSeatTurn, visibleMessages,
         chatTranscript } from './passView.js'

const seat = (content, extra = {}) =>
  ({ id: `live-claude-${content}`, speaker: 'claude', content, ...extra })

test('the pass token and every start of it are pass-shaped', () => {
  for (const t of ['[', '[p', '[pa', '[pas', '[pass', '[pass]', '  [PASS ', '[pass]\n']) {
    assert.equal(isPassShaped(t), true, JSON.stringify(t))
  }
  for (const t of ['', '   ', null, undefined, '[note] something', '[passed', '[pass] and more',
                   'pass', '[ pass']) {
    assert.equal(isPassShaped(t), false, JSON.stringify(t))
  }
})

test('a seat shows no pass text, streaming or not, and a user turn shows as typed', () => {
  // The flicker: "[pa" drew as text while the reply streamed, until the
  // passed event removed the bubble.
  assert.equal(shownText(seat('[pa', { streaming: true })), '')
  assert.equal(shownText(seat('[pass]')), '')
  // A reply that only starts with a bracket is a real reply.
  assert.equal(shownText(seat('[note] something')), '[note] something')
  assert.equal(shownText({ speaker: 'user', content: '[pass]' }), '[pass]')
})

test('a passed or empty seat turn draws no bubble once it has stopped', () => {
  // The barge-in leftovers: the reader aborted before `passed` arrived.
  assert.equal(isSilentSeatTurn(seat('[pass]')), true)
  assert.equal(isSilentSeatTurn(seat('[pass')), true)
  assert.equal(isSilentSeatTurn(seat('')), true)
})

test('a seat turn with anything to show still draws', () => {
  // Still streaming: the thinking dots hold its place.
  assert.equal(isSilentSeatTurn(seat('', { streaming: true })), false)
  assert.equal(isSilentSeatTurn(seat('[pa', { streaming: true })), false)
  assert.equal(isSilentSeatTurn(seat('', { error: 'returned an empty reply' })), false)
  assert.equal(isSilentSeatTurn(seat('[pass]', { tool_events: [{ id: 't0', tool: 'web_search' }] })), false)
  assert.equal(isSilentSeatTurn(seat('', { attachments: [{ id: 3 }] })), false)
  assert.equal(isSilentSeatTurn(seat('A real reply.')), false)
  assert.equal(isSilentSeatTurn({ speaker: 'user', content: '' }), false)
})

test('visibleMessages drops only the silent seat turns', () => {
  const msgs = [
    { id: 1, speaker: 'user', content: 'what do you both think' },
    { id: 2, speaker: 'claude', content: 'Here is my take.' },
    seat('[pass'),
    { id: 'live-gpt-1', speaker: 'gpt', content: '' },
    { id: 3, speaker: 'user', content: '' },
  ]
  assert.deepEqual(visibleMessages(msgs).map((m) => m.id), [1, 2, 3])
})

test('Copy chat leaves out passed and empty seat turns and keeps the rest', () => {
  const labels = { user: 'Alex', claude: 'Claude', gpt: 'GPT', system: 'System' }
  const msgs = [
    { id: 1, speaker: 'user', content: 'Sam asked about the bench top' },
    { id: 2, speaker: 'claude', content: 'Sand it first. ' },
    { id: 'live-gpt-a', speaker: 'gpt', content: '' },
    { id: 'live-claude-b', speaker: 'claude', content: '[pass]' },
    { id: 'live-gpt-c', speaker: 'gpt', content: '[pass' },
    { id: 3, speaker: 'user', content: '' },
    { id: 4, speaker: 'gpt', content: '[note] oil it after.' },
    { id: 5, speaker: 'system', content: 'deploy notice' },
  ]
  const md = chatTranscript('Bench build', msgs, (s) => labels[s])
  assert.equal(md, [
    '# Bench build',
    '',
    '**Alex:** Sam asked about the bench top',
    '',
    '**Claude:** Sand it first.',
    '',
    '**Alex:** (no text)',
    '',
    '**GPT:** [note] oil it after.',
    '',
    '**System:** deploy notice',
  ].join('\n') + '\n')
  assert.doesNotMatch(md, /\[pass/)
})
