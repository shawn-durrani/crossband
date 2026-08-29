// The compact number formats, one home (#236). Six token formatters and
// four money() copies drifted apart; these pin the consolidated rules.
// Run: node --test frontend/src/format.test.js
import { test } from 'node:test'
import assert from 'node:assert/strict'
import { fmtTokens, fmtTokensRound, money } from './format.js'

test('totals keep one decimal and get the M tier', () => {
  assert.equal(fmtTokens(0), '0')
  assert.equal(fmtTokens(999), '999')
  assert.equal(fmtTokens(4200), '4.2k')
  // The fix that motivated the module: 4800000 as "4800.0k" misreads by a
  // factor of a thousand.
  assert.equal(fmtTokens(4800000), '4.8M')
  assert.equal(fmtTokens(null), '0')
})

test('the gauge rounds to whole thousands - an indicator, not a measurement', () => {
  assert.equal(fmtTokensRound(999), '999')
  assert.equal(fmtTokensRound(1000), '1k')
  assert.equal(fmtTokensRound(45300), '45k')
  // The M tier applies to the gauge too, one decimal so it stays trustable.
  assert.equal(fmtTokensRound(2400000), '2.4M')
})

test('money grades its decimals so sub-cent spend stays visible', () => {
  assert.equal(money(0), '$0.00')
  assert.equal(money(1.5), '$1.50')
  assert.equal(money(0.05), '$0.050')
  assert.equal(money(0.0012), '$0.0012')
  assert.equal(money(null), '$0.00')
})
