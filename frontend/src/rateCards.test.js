// Tests for owner-entered rate cards: an owner must be able to price a model
// from the UI, and must NOT be able to invent a rate while doing it. Two
// earlier pricing bugs were both an untranscribed figure standing in for one
// nobody had; a form is a faster way to do that than a text editor was, so the
// source/as-of rule is the load-bearing part of this surface, not a nicety.
// Run: node --test frontend/src/rateCards.test.js
import { test } from 'node:test'
import assert from 'node:assert/strict'
import {
  originLabel, cardSummary, validateDraft, draftToBody, draftFrom, sortRows,
  ESTIMATE, SELF_HOSTED,
} from './rateCards.js'

const BUILTIN = {
  model: 'claude-sonnet-5', origin: 'builtin', priced: true,
  card: { input: 3, output: 15, as_of: '2026-01-01',
          source: 'https://www.anthropic.com/pricing',
          provenance: 'rate_card_estimate',
          cache: { read_mult: 0.1, write_mult: 1.25 }, aliases: [] },
}
const UNPRICED = { model: 'gpt-5.6-terra-pro', origin: 'unpriced', priced: false, card: null }

test('a shipped default and an owner-entered rate never read the same', () => {
  assert.equal(originLabel('builtin').label, 'Built-in')
  assert.equal(originLabel('override').label, 'Your rate')
  assert.notEqual(originLabel('builtin').tone, originLabel('override').tone)
})

test('unpriced is stated outright, with the consequence named', () => {
  const o = originLabel('unpriced')
  assert.equal(o.label, 'Unpriced')
  assert.match(o.title, /cannot be promoted/)
})

test('a priced row summarises rate, cache terms and provenance', () => {
  const s = cardSummary(BUILTIN)
  assert.equal(s.priced, true)
  assert.equal(s.rate, '$3 in / $15 out per 1M tokens')
  // cache terms are shown, not buried: the multipliers are where a past
  // billing surprise actually lived
  assert.match(s.cache, /write 1.25x/)
  assert.match(s.provenance, /as of 2026-01-01/)
  assert.equal(s.action, 'Override')
})

test('a card with no cache terms says it inherits the provider default', () => {
  const row = { ...BUILTIN, card: { ...BUILTIN.card, cache: null } }
  assert.match(cardSummary(row).cache, /provider default/)
})

test('an unpriced row offers the call to action instead of an empty cell', () => {
  const s = cardSummary(UNPRICED)
  assert.equal(s.priced, false)
  assert.equal(s.rate, null)
  assert.equal(s.action, 'Add rate card')
})

test('an owner-entered card offers Edit, not Override', () => {
  assert.equal(cardSummary({ ...BUILTIN, origin: 'override' }).action, 'Edit')
})

test('a rate with no source is refused, and the refusal explains why', () => {
  const v = validateDraft({ input: '2', output: '12', as_of: '2026-07-31', source: '  ' })
  assert.equal(v.ok, false)
  assert.match(v.errors.source, /where this figure came from/)
})

test('a rate with no as-of date is refused', () => {
  const v = validateDraft({ input: '2', output: '12', source: 'x', as_of: '' })
  assert.equal(v.ok, false)
  assert.ok(v.errors.as_of)
})

test('missing or negative rates are refused', () => {
  assert.equal(validateDraft({ input: '', output: '12', source: 'x', as_of: 'd' }).ok, false)
  assert.equal(validateDraft({ input: '-1', output: '12', source: 'x', as_of: 'd' }).ok, false)
  assert.match(validateDraft({ input: '-1', output: '12', source: 'x', as_of: 'd' })
    .errors.input, /negative/)
})

test('a $0 rate is allowed - a declared zero is a real figure, not a gap', () => {
  // As an estimate it still needs a checkable source and a real date...
  assert.equal(validateDraft({
    input: '0', output: '0', source: 'https://example.com/pricing', as_of: '2026-07-31',
  }).ok, true)
  // ...but the honest way to say "local, costs nothing" is the self-hosted
  // provenance, which carries no rates and no date at all.
  assert.equal(validateDraft({
    provenance: SELF_HOSTED, source: 'local (Ollama, self-hosted)',
  }).ok, true)
})


// ---------- ported validation rules ----------

test('a self-hosted declaration needs only a note - no rates, no date', () => {
  const v = validateDraft({ provenance: SELF_HOSTED, source: '' })
  assert.equal(v.ok, false)
  assert.match(v.errors.source, /auditable/)
  assert.equal(v.errors.input, undefined)   // rates are meaningless here
  assert.equal(v.errors.as_of, undefined)
})

test('an estimate source must be a link someone can actually open', () => {
  const v = validateDraft({ input: '2', output: '12', as_of: '2026-07-31', source: 'the pricing page' })
  assert.equal(v.ok, false)
  assert.match(v.errors.source, /http\(s\) link/)
})

test('an as-of date must be a real YYYY-MM-DD', () => {
  for (const bad of ['sometime', '31-07-2026', '2026-13-45']) {
    const v = validateDraft({ input: '2', output: '12', source: 'https://x.io/p', as_of: bad })
    assert.equal(v.ok, false, bad)
    assert.match(v.errors.as_of, /real date/)
  }
})

test('a fat-fingered rate is refused as a typo, not stored as a price', () => {
  const v = validateDraft({ input: '1e9', output: '12', source: 'https://x.io/p', as_of: '2026-07-31' })
  assert.equal(v.ok, false)
  assert.match(v.errors.input, /too large/)
})

test('the body carries an explicit provenance, and self-hosted sends no rates', () => {
  const est = draftToBody({ input: '2', output: '12', source: 'https://x.io/p', as_of: '2026-07-31' })
  assert.equal(est.provenance, ESTIMATE)
  const local = draftToBody({ provenance: SELF_HOSTED, source: 'local', input: '9', as_of: 'x' })
  assert.equal(local.provenance, SELF_HOSTED)
  assert.equal(local.input, undefined)   // empty values would be refused upstream
  assert.equal(local.as_of, undefined)
})

test('editing a self-hosted card seeds the form back into self-hosted mode', () => {
  const row = { model: 'llama-local', origin: 'override', priced: true,
                card: { input: 0, output: 0, provenance: SELF_HOSTED,
                        source: 'local (Ollama)', as_of: '2026-07-31', aliases: [] } }
  assert.equal(draftFrom(row).provenance, SELF_HOSTED)
})

test('a complete draft passes', () => {
  assert.equal(validateDraft({
    input: '2', output: '12', source: 'https://developers.openai.com/api/docs/pricing',
    as_of: '2026-07-31',
  }).ok, true)
})

test('cache terms are omitted unless BOTH are given, so the card inherits the default', () => {
  const base = { input: '2', output: '12', source: 's', as_of: 'd' }
  assert.equal(draftToBody(base).cache, undefined)
  assert.equal(draftToBody({ ...base, read_mult: '0.1' }).cache, undefined)
  assert.deepEqual(draftToBody({ ...base, read_mult: '0.1', write_mult: '1.25' }).cache,
    { read_mult: 0.1, write_mult: 1.25 })
})

test('a 0 cache multiplier survives (it is a real value, not "unset")', () => {
  const b = draftToBody({ input: '2', output: '12', source: 's', as_of: 'd',
                          read_mult: '0', write_mult: '0' })
  assert.deepEqual(b.cache, { read_mult: 0, write_mult: 0 })
})

test('rates and aliases are sent as numbers and a trimmed list', () => {
  const b = draftToBody({ input: '2', output: '12', source: ' s ', as_of: ' d ',
                          aliases: 'a-1, , b-2 ' })
  assert.equal(b.input, 2)
  assert.equal(b.source, 's')
  assert.deepEqual(b.aliases, ['a-1', 'b-2'])
})

test('editing seeds the form from the existing card; adding starts blank', () => {
  const d = draftFrom(BUILTIN)
  assert.equal(d.input, '3')
  assert.equal(d.write_mult, '1.25')
  assert.equal(d.source, 'https://www.anthropic.com/pricing')
  const blank = draftFrom(UNPRICED)
  assert.equal(blank.model, 'gpt-5.6-terra-pro')
  assert.equal(blank.input, '')
  assert.equal(blank.source, '')
})

test('unpriced models sort first - they are why the page was opened', () => {
  const rows = sortRows([BUILTIN, UNPRICED, { model: 'aaa', priced: true, origin: 'builtin' }])
  assert.equal(rows[0].model, 'gpt-5.6-terra-pro')
  assert.equal(rows[1].model, 'aaa')
})
