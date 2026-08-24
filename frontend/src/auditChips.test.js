// The attribution chip must render only well-formed findings, survive any
// junk in the column, and keep the audit's honest semantics: a check to
// make, never a misquote verdict.
// Run: node --test frontend/src/auditChips.test.js
import { test } from 'node:test'
import assert from 'node:assert/strict'
import { auditChips } from './auditChips.js'

const finding = (who, claim) => ({ kind: 'attribution', who, claim })

test('a finding renders the claim, the speaker, and check-first copy', () => {
  const chips = auditChips(JSON.stringify([
    finding('Claude', 'the deploy was reverted overnight'),
  ]))
  assert.equal(chips.length, 1)
  assert.match(chips[0].label, /the deploy was reverted overnight/)
  assert.match(chips[0].label, /not found in Claude's messages here/)
  assert.match(chips[0].title, /prompt to check, not proof/)
})

test('a long claim is trimmed for the label', () => {
  const chips = auditChips(JSON.stringify([finding('GPT', 'x'.repeat(200))]))
  assert.ok(chips[0].label.includes('…'))
  assert.ok(chips[0].label.length < 130)
})

test('junk never renders and never throws', () => {
  assert.deepEqual(auditChips(''), [])
  assert.deepEqual(auditChips(undefined), [])
  assert.deepEqual(auditChips('not json'), [])
  assert.deepEqual(auditChips('{"kind":"attribution"}'), [])   // not a list
  assert.deepEqual(auditChips(JSON.stringify([
    null,
    { kind: 'attribution' },                    // no who/claim
    { kind: 'other', who: 'Claude', claim: 'x' },  // unknown kind
  ])), [])
})
