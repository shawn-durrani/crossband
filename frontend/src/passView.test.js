// Tests for the pass on screen and in the voice (#98, #456, #460): a
// seat's [pass] never draws as text or reaches TTS, a quiet remark in front
// of it goes with it, a real reply keeps its words without the token, a
// seat turn with nothing to show draws no bubble, and Copy chat leaves the
// passes out while keeping the user's turns and real replies.
// Run: node --test frontend/src/passView.test.js
import { test } from 'node:test'
import assert from 'node:assert/strict'
import { isPassShaped, shownText, isSilentSeatTurn, visibleMessages,
         chatTranscript, couldBePass, couldStillBePass, isQuietRemark,
         stripPassTail, PassSpeechGate } from './passView.js'

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

// ---------- a pass with words in front of it (#460) ----------

test('a short quiet remark before the token is still a pass', () => {
  for (const t of ['Nothing to add from me.  [pass]',
                   'The room asked us to stay quiet, so I\u2019m passing.  [pass]',
                   'You two carry on with the timber order, passing. [pass]',
                   'Holding back until someone asks me directly. [PASS]\n',
                   '(still listening) [pass].',
                   'Nothing to add. [pa']) {
    assert.equal(isPassShaped(t), true, JSON.stringify(t))
    assert.equal(shownText(seat(t)), '', JSON.stringify(t))
    assert.equal(isSilentSeatTurn(seat(t)), true, JSON.stringify(t))
  }
})

test('a real reply keeps its words and loses the trailing token', () => {
  assert.equal(shownText(seat('The glue needs 24 hours.  [pass]')), 'The glue needs 24 hours.')
  assert.equal(shownText(seat('Yes.  [pass]')), 'Yes.')
  assert.equal(shownText(seat('Oil it after sanding. [pass].')), 'Oil it after sanding.')
  // A turn is something to say, even beside a quiet phrase.
  assert.equal(shownText(seat('Nothing to add, but Mateo has the cut list. [pass]')),
    'Nothing to add, but Mateo has the cut list.')
  // The token only counts at the end.
  assert.equal(shownText(seat('[pass] and one more thing')), '[pass] and one more thing')
  assert.equal(stripPassTail('Sand it first. [pa'), 'Sand it first.')
  assert.equal(stripPassTail('Nothing to add.'), 'Nothing to add.')
})

test('a quiet remark says so, briefly, and asks and counts nothing', () => {
  assert.equal(isQuietRemark(''), true)
  assert.equal(isQuietRemark('\u2026'), true)
  assert.equal(isQuietRemark("I don't have anything to add."), true)
  assert.equal(isQuietRemark("I'll let you two carry on."), true)
  assert.equal(isQuietRemark('Okay.'), false)
  assert.equal(isQuietRemark('Same here.'), false)
  assert.equal(isQuietRemark('Pass the glue.'), false)
  assert.equal(isQuietRemark('Nothing beats oak.'), false)
  assert.equal(isQuietRemark('Staying quiet, want me to check the 2 quotes?'), false)
  assert.equal(isQuietRemark('For the record, room mode is still on.'), false)
  assert.equal(isQuietRemark('Staying quiet. ' + 'The plan still stands for the bench. '.repeat(4)), false)
})

test('the screen holds a streaming reply only while it is quiet words', () => {
  // The #98 shapes, still held.
  for (const t of ['', '[', '[pa', '[pass', '[pass]', '  [PASS]', '[pass] \n']) {
    assert.equal(couldBePass(t), true, JSON.stringify(t))
  }
  // #460: a quiet remark on its way to the token, a word at a time.
  for (const t of ['N', 'Noth', 'Nothing to', 'Nothing to add', 'Nothing to add. [',
                   'Staying quiet, pass', 'p']) {
    assert.equal(couldBePass(t), true, JSON.stringify(t))
  }
  // Any other word shows it.
  for (const t of ['[pass] actually no', 'Well,', '[passive voice', 'The oven ',
                   'Nothing to add, but the oven', "I'll [", 'Sand it first. [pa']) {
    assert.equal(couldBePass(t), false, JSON.stringify(t))
  }
})

test('the voice holds a reply while it could still end as a pass', () => {
  for (const t of ['', 'You two carry on with the timber', 'Nothing to add, so',
                   'You two carry on, passing. [pa', 'Sand it first']) {
    assert.equal(couldStillBePass(t), true, JSON.stringify(t))
  }
  // A question, a number, a turn, or more than a quiet remark can hold.
  for (const t of ['Sand it first?', 'Sand it for 2 minutes', 'Nothing to add, but ',
                   'Sand it first. '.repeat(10)]) {
    assert.equal(couldStillBePass(t), false, JSON.stringify(t))
  }
  // An unfinished word is not yet a turn: "but" could be "butter".
  assert.equal(couldStillBePass('Nothing to add, the but'), true)
})

test('a streaming quiet remark shows the thinking dots, a real reply streams', () => {
  assert.equal(shownText(seat('Nothing to add', { streaming: true })), '')
  assert.equal(isSilentSeatTurn(seat('Nothing to add', { streaming: true })), false)
  assert.equal(shownText(seat('Sand it first. [pa', { streaming: true })), 'Sand it first.')
  // Finished without the token, the same words are a real reply.
  assert.equal(shownText(seat('Nothing to add.')), 'Nothing to add.')
})

const spoken = (chunks) => {
  const gate = new PassSpeechGate()
  return chunks.map((c) => gate.feed(c)).join('') + gate.flush()
}

test('the voice never speaks a pass, however it arrives', () => {
  assert.equal(spoken(['[pa', 'ss]']), '')
  assert.equal(spoken(['Nothing to add', ' from me.  [pass]']), '')
  assert.equal(spoken(['You two carry on with the timber order, ', 'passing. [pass]']), '')
  // cut off mid-token
  assert.equal(spoken(['Nothing to add. [pa']), '')
})

test('the voice speaks a real reply without the token', () => {
  assert.equal(spoken(['Sand it first.  [pa', 'ss]']), 'Sand it first.')
  assert.equal(spoken(['Oil it after sanding. [PASS].']), 'Oil it after sanding.')
  assert.equal(spoken(['See [', 'note] below.']), 'See [note] below.')
  assert.equal(spoken(['Nothing to add.']), 'Nothing to add.')
  // A long reply is released once it can't be a pass, and a token later
  // on is still never spoken.
  const long = 'Sand the top with 120 grit, then 180, then oil it. '
  const gate = new PassSpeechGate()
  assert.equal(gate.feed(long), long)
  assert.equal(gate.feed('Done. [pa'), 'Done. ')
  assert.equal(gate.feed('ss]'), '')
  assert.equal(gate.flush(), '')
})

test('the voice releases a reply as soon as it cannot be a pass', () => {
  const gate = new PassSpeechGate()
  assert.equal(gate.feed('Sand it '), '')
  assert.equal(gate.feed('first?'), 'Sand it first?')
  assert.equal(gate.feed(' Then oil it.'), ' Then oil it.')
})

test('Copy chat leaves out a quiet remark pass and strips a stray token', () => {
  const labels = { user: 'Alex', claude: 'Claude', gpt: 'GPT' }
  const md = chatTranscript('Bench build', [
    { id: 1, speaker: 'user', content: 'you can all just listen for a bit' },
    { id: 2, speaker: 'claude', content: 'Nothing to add from me.  [pass]' },
    { id: 3, speaker: 'gpt', content: 'Oil it after sanding.  [pass]' },
  ], (s) => labels[s])
  assert.equal(md, [
    '# Bench build',
    '',
    '**Alex:** you can all just listen for a bit',
    '',
    '**GPT:** Oil it after sanding.',
  ].join('\n') + '\n')
})
