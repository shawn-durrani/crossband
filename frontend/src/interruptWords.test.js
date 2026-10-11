// What words heard over a paused reply say about it (interruptWords.js).
// A phone service that cut a reply at the first word found only 11% of its
// cuts were real interruptions (#631). Pins: listening noises never cut; a
// stop word or a seat's name at the start cuts at once, even as one word;
// a known person's name at the start is an aside, and a name that's also an
// everyday word needs its comma; two words of your own cut and one waits;
// after a question, an answer cuts, "yeah" and "mm-hm" included; and the
// transcript's spelling quirks (spelt-out letters, hyphens, tags, curly
// apostrophes) read the same as plain words.
// Run: node --test frontend/src/interruptWords.test.js
import assert from 'node:assert/strict'
import { test } from 'node:test'
import { endsOnQuestion, judgeWords, peopleWords, seatWords, words } from './interruptWords.js'

const seats = seatWords([{ slug: 'claude', name: 'Claude' }, { slug: 'gpt', name: 'GPT' }])
const people = peopleWords([
  { name: 'Sam', preferred_name: 'Sam', merged_names: ['Samantha'] },
  { name: 'Mateo', preferred_name: 'Mateo' },
  { name: 'Will', preferred_name: 'Will' },
])
const judge = (text, asked = false) => judgeWords(text, { seats, people, asked }).verdict

test('listening noises never cut', () => {
  for (const t of ['yeah', 'Yeah.', 'Mm-hm.', 'mm hmm', 'Uh-huh', 'Right, right.', 'oh wow',
                   'Oh no', 'I see', 'Thank you', 'Got it', 'Okay cool', 'Sounds good',
                   'Yes.', 'Really?', 'That makes sense']) {
    assert.equal(judge(t), 'listen', t)
  }
})

test('a stop word at the start cuts at once, even on its own', () => {
  for (const t of ['Wait.', 'Stop', 'Hang on', 'hold on a sec', 'Oh wait', 'No, no, no',
                   'Hey, hold up', 'Never mind', 'One sec', 'Sorry, wait']) {
    assert.equal(judge(t), 'cut', t)
  }
})

test('a stop word counts only at the start', () => {
  assert.equal(judge('Sam, wait for me'), 'aside', "it's Sam who's asked to wait")
  assert.equal(judge('yeah wait'), 'cut', 'a lead-in before the stop word is skipped')
})

test("a seat's name at the start cuts, spelt out or not", () => {
  for (const t of ['Claude, stop', 'Claude', 'Hey GPT, what is that', 'G P T, hold on',
                   'G.P.T. one thing', 'Okay Claude']) {
    assert.equal(judge(t), 'cut', t)
  }
})

test("a known person's name at the start is an aside", () => {
  for (const t of ['Sam, pass the salt.', 'Sam pass the salt', 'Mateo?', 'Oh Sam, look',
                   'Samantha, can you grab that', 'No, Mateo, the other one']) {
    assert.equal(judge(t), 'aside', t)
  }
})

test('a name that is also an everyday word needs its comma', () => {
  assert.equal(judge('Will, check that'), 'aside')
  assert.equal(judge('Will you check that'), 'cut')
})

test('two words of your own cut, one waits for the next', () => {
  assert.equal(judge('These are not gonna work.'), 'cut')
  assert.equal(judge('Yeah, but what about the eggs?'), 'cut')
  assert.equal(judge('Tomorrow'), 'more')
  assert.equal(judge('Ouch!'), 'more')
  assert.equal(judge('No.'), 'more')
  assert.equal(judge('Um'), 'more', 'a hesitation is the start of a turn')
  assert.equal(judgeWords('Um').content, 0, 'and has no words of its own')
  assert.equal(judgeWords('Tomorrow').content, 1)
})

test('after a question, an answer cuts, "yeah" and "mm-hm" included', () => {
  for (const t of ['yeah', 'Yes please', 'No.', 'Mm-hm.', 'Sounds good', 'Tomorrow', 'Sure']) {
    assert.equal(judge(t, true), 'cut', t)
  }
  for (const t of ['Oh no', 'wow', 'Hmm']) assert.equal(judge(t, true), 'listen', t)
})

test('no words is none', () => {
  for (const t of ['', '   ', '...', '(sizzling)', '[clattering] (fan noise)', undefined]) {
    assert.equal(judge(t), 'none', String(t))
  }
})

test('transcript quirks read as plain words', () => {
  assert.deepEqual(words('G P T, it’s fine').map((w) => w.w), ['gpt', "it's", 'fine'])
  assert.deepEqual(words('Mm-hm (laughs) ok').map((w) => w.w), ['mmhm', 'ok'])
  assert.equal(words('Sam, pass')[0].comma, true)
  assert.equal(words('Sam pass')[0].comma, false)
})

test("the seats' and people's names are heard by every word of them", () => {
  assert.deepEqual([...seatWords([{ slug: 'qwen', name: 'Qwen 3' }])].sort(), ['qwen'])
  assert.ok(peopleWords([{ name: 'Sam', merged_names: ['Sammy'] }]).has('sammy'))
  assert.equal(seatWords(null).size, 0)
  assert.equal(peopleWords(undefined).size, 0)
})

test('whether the reply so far ends on a question', () => {
  assert.equal(endsOnQuestion('Want me to pull the numbers?'), true)
  assert.equal(endsOnQuestion('Want me to pull the numbers?" '), true)
  assert.equal(endsOnQuestion('Here they are. Want me to'), false)
  assert.equal(endsOnQuestion(''), false)
})
