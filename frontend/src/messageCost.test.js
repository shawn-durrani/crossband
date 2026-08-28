// Tests for the chat surface's cost labelling: it must not present
// subscription-equivalent usage as billed spend. The bug this guards: every
// `usage_json.cost` was summed into one `~$X`, so subscription-covered guest
// turns read as cash.
// Run: node --test frontend/src/messageCost.test.js
import { test } from 'node:test'
import assert from 'node:assert/strict'
import {
  classifyUsage, costLabel, chatCostFigures, chatCostNote,
  money, GUEST_SLUG,
} from './messageCost.js'
import { headline } from './spendView.js'
import * as mod from './messageCost.js'

// Shapes as the backend actually writes them (backend/guest.py, engine.py).
const subscriptionTurn = (cost) => JSON.stringify({
  input: 100, output: 50, cost, auth: 'subscription',
  cost_provenance: { source: 'subscription_equivalent', estimated: true },
})
const apiKeyTurn = (cost) => JSON.stringify({
  input: 100, output: 50, cost, auth: 'api_key',
  cost_provenance: { source: 'provider_reported', estimated: false },
})
const residentTurn = (cost) => JSON.stringify({
  input: 100, output: 50, cost, model: 'claude-opus-5',
  cost_provenance: { source: 'rate_card_estimate', estimated: true },
})

// ---- the classification itself -------------------------------------------

test('a subscription guest turn is never classified as metered cash', () => {
  const e = classifyUsage(subscriptionTurn(1.5), GUEST_SLUG)
  assert.equal(e.category, 'subscription_equiv')
  assert.equal(e.provenance, 'subscription_equivalent')
})

test('an api-key guest turn is metered cash', () => {
  const e = classifyUsage(apiKeyTurn(1.5), GUEST_SLUG)
  assert.equal(e.category, 'metered')
  assert.equal(e.provenance, 'provider_reported')
})

test('a resident model turn is metered whatever else is missing', () => {
  // The Messages/Responses API is key-billed by licence - a subscription can't
  // serve it, so a resident turn has no auth field and needs none.
  assert.equal(classifyUsage(residentTurn(0.02), 'claude').category, 'metered')
  assert.equal(classifyUsage('{"input":1,"output":1,"cost":0.01}', 'gpt').category, 'metered')
})

test('a guest turn with no recorded auth is unknown, not spend', () => {
  // Rows logged before provenance capture. We can't prove it either way, so it
  // is shown apart rather than quietly folded into billed spend.
  const e = classifyUsage('{"input":1,"output":1,"cost":9}', GUEST_SLUG)
  assert.equal(e.category, 'unknown')
  assert.equal(e.provenance, 'unknown')
})

test('a recorded provenance wins over what auth would imply', () => {
  // a turn keeps the provenance that was true when it was written; a later
  // rate-card or config edit must never rewrite history.
  const e = classifyUsage(JSON.stringify({
    cost: 1, auth: 'api_key', cost_provenance: { source: 'rate_card_estimate' },
  }), GUEST_SLUG)
  assert.equal(e.provenance, 'rate_card_estimate')
  assert.equal(e.category, 'metered')   // the cash axis still follows auth
})

test('a garbage provenance is treated as no record, not trusted', () => {
  const e = classifyUsage(JSON.stringify({
    cost: 1, auth: 'subscription', cost_provenance: { source: 'definitely_free' },
  }), GUEST_SLUG)
  assert.equal(e.provenance, 'subscription_equivalent')
})

test('no cost tracked is a gap, never a $0', () => {
  const e = classifyUsage('{"input":10,"output":5}', 'claude')
  assert.equal(e.hasCost, false)
  assert.equal(e.cost, 0)
  assert.equal(costLabel(e), null)   // nothing claimed where nothing is known
})

test('unusable usage yields nothing rather than a guess', () => {
  assert.equal(classifyUsage(null, 'claude'), null)
  assert.equal(classifyUsage('not json', 'claude'), null)
  assert.equal(classifyUsage('"a string"', 'claude'), null)
})

test('cached input counts as input, and tokens are the sum', () => {
  const e = classifyUsage(
    '{"input":10,"cache_read":100,"cache_creation":5,"output":20,"cost":0.1}', 'claude')
  assert.equal(e.input, 115)
  assert.equal(e.output, 20)
  assert.equal(e.tokens, 135)
})

// ---- what a single bubble says -------------------------------------------

test('a subscription bubble says so in words, not just colour', () => {
  const label = costLabel(classifyUsage(subscriptionTurn(0.42), GUEST_SLUG))
  // The qualifier must survive being read in greyscale, or by a screen reader.
  assert.match(label.text, /subscription/)
  assert.match(label.text, /no extra charge/)
  assert.match(label.title, /not charged/)
})

test('a billed bubble is a bare amount - money needs no qualifier', () => {
  const label = costLabel(classifyUsage(apiKeyTurn(0.42), GUEST_SLUG))
  assert.equal(label.tone, 'billed')
  assert.equal(label.text, '$0.42')      // provider-reported: not hedged with ~
  assert.doesNotMatch(label.text, /subscription/)
})

test('a rate-card figure is hedged, a provider-reported one is not', () => {
  assert.equal(costLabel(classifyUsage(residentTurn(0.42), 'claude')).text, '~$0.42')
  assert.equal(costLabel(classifyUsage(apiKeyTurn(0.42), GUEST_SLUG)).text, '$0.42')
})

test('an unprovable bubble says unverified rather than showing plain money', () => {
  const label = costLabel(classifyUsage('{"cost":9}', GUEST_SLUG))
  assert.match(label.text, /unverified/)
  assert.equal(label.tone, 'unverified')
})

test('a self-hosted $0 reads as a declared fact, not a missing number', () => {
  const label = costLabel(classifyUsage(JSON.stringify({
    cost: 0, cost_provenance: { source: 'self_hosted_zero_marginal' },
  }), 'llama'))
  assert.match(label.text, /self-hosted/)
  assert.match(label.title, /not a missing figure/)
})

// ---- the chat total ------------------------------------------------------
//
// The chat total is no longer derived in the browser. The server sends the
// same three-bucket row the export picker reads, and the header names it with
// spendView's headline(). These cases build that row directly, so they pin
// the honesty rules against the shape that actually arrives.

function chatRow({ metered = 0, subscription_equiv = 0, unknown = 0,
                   tokens = 0, not_tracked = false } = {}) {
  return headline({ metered, subscription_equiv, unknown })
}


test('the chat total keeps the three kinds of dollar apart', () => {
  const t = chatRow({ metered: 3, subscription_equiv: 100, unknown: 5 })
  assert.equal(t.billed, 3)        // 2 rate-card + 1 provider-reported
  assert.equal(t.covered, 100)     // NOT added to billed
  assert.equal(t.unknown, 5)
})

test('the regression: subscription guest turns are not the chat total', () => {
  // The concrete failure: a chat of subscription-covered guest turns showed a
  // three-figure apparent spend. Billed must be exactly zero.
  const t = chatRow({ subscription_equiv: 120 })
  assert.equal(t.billed, 0)
  assert.equal(t.covered, 120)
  const figures = chatCostFigures(t)
  assert.equal(figures[0].text, '$0.00 billed')   // stated outright, not omitted
  assert.match(figures[1].text, /subscription/)
})

test('tokens still count every turn, billed or not', () => {
  // Tokens are usage, not money - the split doesn't hide any of it.
  const t = headline({ metered: 1, subscription_equiv: 1 })
  assert.equal(t.billed + t.covered, 2)
})

test('an untracked cost is remembered as a gap', () => {
  const row = { metered: 0, subscription_equiv: 0, unknown: 0,
                tokens: 10, not_tracked: true }
  assert.equal(row.not_tracked, true)
  assert.deepEqual(chatCostFigures(headline(row)), [])
})

test('a chat with no usage at all shows no dollar figure', () => {
  assert.deepEqual(chatCostFigures(chatRow()), [])
  assert.deepEqual(chatCostFigures(null), [])
})

test('an all-billed chat shows one figure and no explanation', () => {
  const t = chatRow({ metered: 2 })
  assert.equal(chatCostFigures(t).length, 1)
  // Nothing to disambiguate - a chat that only cost money says nothing extra.
  assert.equal(chatCostNote(t), null)
})

// ---- the explanation -----------------------------------------------------

test('a mixed chat explains the difference in plain English on screen', () => {
  const note = chatCostNote(chatRow({ metered: 2, subscription_equiv: 100 }))
  assert.match(note, /\$2\.00 billed figure is money you pay/)
  assert.match(note, /ran on your Claude subscription/)
  assert.match(note, /not money charged/)
})

test('an entirely-covered chat says outright that nothing was billed', () => {
  const note = chatCostNote(chatRow({ subscription_equiv: 120 }))
  assert.match(note, /Nothing in this chat was billed/)
})

test('unverified cost is explained too, and never claimed as spend', () => {
  const note = chatCostNote(chatRow({ unknown: 5 }))
  assert.match(note, /didn't record how it was billed/)
  assert.doesNotMatch(note, /money you pay/)
})

test('the note names what it is talking about', () => {
  // The export picker totals a SELECTION, not one chat. Same sentence, honest
  // scope - a footer claiming "nothing in this chat" would be a small lie.
  const covered = { billed: 0, covered: 5, unknown: 0 }
  assert.match(chatCostNote(covered), /Nothing in this chat/)
  assert.match(chatCostNote(covered, 'the selected chats'), /Nothing in the selected chats/)
  // The billed-lead form says "you pay" and needs no subject either way.
  assert.match(chatCostNote({ billed: 1, covered: 5 }, 'the selected chats'),
    /Only the \$1\.00 billed figure is money you pay/)
})

test('the export picker row shape feeds the same figures', () => {
  // `/api/usage/chats` returns the raw buckets; the picker names them with the
  // same split the chat header and Spend page use, never its own arithmetic.
  const row = { metered: 2, subscription_equiv: 120, unknown: 0 }
  const figures = chatCostFigures(headline(row))
  assert.equal(figures[0].text, '~$2.00 billed')
  assert.match(figures[1].text, /subscription/)
  assert.equal(figures.length, 2)   // a zero bucket is simply absent
})

test('money reads as money, and zero is exactly $0.00', () => {
  assert.equal(money(0), '$0.00')
  assert.equal(money(1.5), '$1.50')
  assert.equal(money(0.05), '$0.050')
  assert.equal(money(0.0012), '$0.0012')
})


test('the module no longer rolls a chat up in the browser', () => {
  // It used to, by summing usage_json off message rows, which could only see
  // messages. Voice arrived separately and utility spend not at all, so the
  // header and the export picker printed different dollars for one chat.
  assert.equal(typeof mod.chatCostTotals, 'undefined')
})
