// Tests for the written-deliverable channel (#80).
// Run: node --test frontend/src/writtenChannel.test.js
import { test } from 'node:test'
import assert from 'node:assert/strict'
import { WRITTEN_TOKEN, WrittenFilter, renderWritten } from './writtenChannel.js'

function speakAll(chunks) {
  const f = new WrittenFilter()
  let out = ''
  for (const c of chunks) out += f.feed(c)
  return out + f.flush()
}

test('no token: everything is spoken, split however the stream splits', () => {
  assert.equal(speakAll(['Here is the ', 'short answer.']), 'Here is the short answer.')
})

test('text after the token is never spoken', () => {
  const spoken = speakAll([
    `Eight issues, three clusters - full table below.\n${WRITTEN_TOKEN}\n`,
    '| rank | issue |\n| 1 | #80 |',
  ])
  assert.equal(spoken, 'Eight issues, three clusters - full table below.\n')
  assert.ok(!spoken.includes('rank'))
})

test('the token split across deltas still splits the stream', () => {
  const spoken = speakAll(['Summary first. [writ', 'ten]\nthe long part'])
  assert.equal(spoken, 'Summary first. ')
})

test('a bracketed phrase that never becomes the token is spoken once decided', () => {
  // "[write it down]" shares a prefix with the token; the holdback must
  // release it the moment the text diverges, not swallow it.
  assert.equal(speakAll(['say this [write it down] aloud']),
    'say this [write it down] aloud')
})

test('a held possible-prefix at end of turn is flushed, not lost', () => {
  // The reply ends mid-holdback ("[writ" and nothing more): flush() must
  // release it so trailing words are never dropped.
  assert.equal(speakAll(['trailing [writ']), 'trailing [writ')
})

test('feed after the token stays silent for the rest of the turn', () => {
  const f = new WrittenFilter()
  f.feed(`ok\n${WRITTEN_TOKEN}\n`)
  assert.equal(f.feed('more written'), '')
  assert.equal(f.flush(), '')
})

test('renderWritten turns the first token into a labelled divider', () => {
  const out = renderWritten(`summary\n${WRITTEN_TOKEN}\n| a table |`)
  assert.ok(!out.includes(WRITTEN_TOKEN))
  assert.ok(out.includes('---'))
  assert.ok(out.includes('Written deliverable'))
  assert.ok(out.includes('| a table |'))
  // token-free text passes through untouched
  assert.equal(renderWritten('plain reply'), 'plain reply')
  assert.equal(renderWritten(''), '')
})
