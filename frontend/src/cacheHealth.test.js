// Tests for the Spend page's cache-health block: it must surface a seat whose
// cache hit rate has dropped immediately rather than days later.
//
// The load-bearing behaviour: a window aggregate can read "good" on the
// strength of one busy row while a chat seat reads barely half its input from
// cache. Averaging is what hides that, so this must lead with the WORST row.
// Run: node --test frontend/src/cacheHealth.test.js
import { test } from 'node:test'
import assert from 'node:assert/strict'
import {
  verdictOf, formatShare, formatRatio, cacheRows, headline, fmtTokens,
  VERDICTS,
} from './cacheHealth.js'

// An invented window carrying the mix this block exists for: a busy guest
// that reads nearly everything from cache, a chat seat that reads 62%, a seat
// whose writes are never reported, and an idle seat.
//
// The arithmetic is internally consistent on purpose. Every row's share is
// its own cache_read over cache_read + cache_creation + uncached_input, and
// the aggregate is the column-wise sum of the rows (30,000,000 read of
// 33,600,000 input, 89%, which reads "good"). Keep it that way if you edit
// these numbers: a fixture whose totals don't add up can make the sorting
// look right for the wrong reason.
const WINDOW = {
  breakdown: {
    cache: { read: 30000000, written: 1600000, uncached: 2000000,
             read_share: 30000000 / 33600000, ratio: 30000000 / 1600000,
             verdict: 'good', write_cost: 4.00, write_cost_share: 0.25 },
    by_producer_model: [
      { label: 'claude-opus-4-8 · Agents / coding guests',
        cache_read: 23800000, cache_creation: 200000, uncached_input: 0,
        cache_write_cost: 0,
        cache: { read_share: 23800000 / 24000000, ratio: 119, verdict: 'good' } },
      { label: 'claude-sonnet-5 · Chat participants',
        cache_read: 3100000, cache_creation: 1400000, uncached_input: 500000,
        cache_write_cost: 3.50,
        cache: { read_share: 0.62, ratio: 3100000 / 1400000, verdict: 'watch' } },
      { label: 'gpt-5.6-terra · Chat participants',
        cache_read: 3100000, cache_creation: 0, uncached_input: 1500000,
        cache_write_cost: 0,
        cache: { read_share: 3100000 / 4600000, ratio: null, verdict: 'watch' } },
      { label: 'claude-haiku-4-5 · Utility',
        cache_read: 0, cache_creation: 0, uncached_input: 0, cache_write_cost: 0,
        cache: { read_share: null, ratio: null, verdict: 'none' } },
    ],
  },
}

test('the fixture adds up, so every assertion below rests on real arithmetic', () => {
  const { cache, by_producer_model: rows } = WINDOW.breakdown
  const sum = (k) => rows.reduce((total, row) => total + row[k], 0)
  assert.equal(sum('cache_read'), cache.read)
  assert.equal(sum('cache_creation'), cache.written)
  assert.equal(sum('uncached_input'), cache.uncached)
  for (const row of rows) {
    const input = row.cache_read + row.cache_creation + row.uncached_input
    if (!input) continue
    assert.ok(Math.abs(row.cache_read / input - row.cache.read_share) < 1e-9, row.label)
  }
})

test('the worst row leads, not the healthy majority', () => {
  const rows = cacheRows(WINDOW.breakdown.by_producer_model)
  // both chat seats are "watch"; the lower share leads
  assert.match(rows[0].label, /claude-sonnet-5 · Chat/)
  assert.match(rows[1].label, /gpt-5\.6-terra/)
  assert.match(rows[2].label, /Agents/)
})

test('rows with no cache activity at all are dropped as noise', () => {
  const rows = cacheRows(WINDOW.breakdown.by_producer_model)
  assert.equal(rows.length, 3)
  assert.ok(!rows.some((r) => /haiku/.test(r.label)))
})

test('the headline names the culprit and its hit rate, not the average', () => {
  const h = headline(WINDOW)
  assert.equal(h.alert, true)
  assert.match(h.text, /claude-sonnet-5 · Chat participants/)
  assert.match(h.text, /read only 62% of its input from the cache/)
  // the window aggregate said "good" at 89%; that must NOT be what surfaces
  assert.doesNotMatch(h.text, /89%/)
})

test('the window-level spend share rides along as the "so what"', () => {
  assert.match(headline(WINDOW).shareText, /25\.0% of metered spend/)
})

test('an all-healthy window says so plainly and does not alert', () => {
  const ok = { breakdown: { cache: { write_cost_share: 0 }, by_producer_model: [
    { label: 'a', cache_read: 90, cache_creation: 5, uncached_input: 5,
      cache: { read_share: 0.9, ratio: 18, verdict: 'good' } },
  ] } }
  const h = headline(ok)
  assert.equal(h.alert, false)
  assert.match(h.text, /read most of its input from the cache/)
  assert.equal(h.shareText, null)
})

test('a window with no cache activity has no headline at all', () => {
  assert.equal(headline({ breakdown: { by_producer_model: [] } }), null)
  assert.equal(headline(undefined), null)
})

test('each verdict says what it means in plain words', () => {
  assert.equal(VERDICTS.poor.tone, 'poor')
  assert.match(VERDICTS.poor.title, /less than half of its input/)
  assert.match(VERDICTS.watch.title, /between half and 80%/)
  assert.match(VERDICTS.good.title, /80% or more/)
  assert.equal(verdictOf({ verdict: 'none' }).tone, 'neutral')
  assert.equal(verdictOf(undefined).tone, 'neutral')
})

test('a seat whose writes are never reported still gets a real verdict', () => {
  // the OpenAI seat: reads but no reported writes, judged on its share
  const gpt = WINDOW.breakdown.by_producer_model[2]
  assert.equal(verdictOf(gpt.cache).label, 'Watch')
  assert.equal(formatShare(gpt.cache), '67%')
  assert.equal(formatRatio(gpt.cache), '—')
})

test('a missing share or ratio renders as a dash, never 0% or NaN', () => {
  assert.equal(formatShare({ read_share: null }), '—')
  assert.equal(formatShare(undefined), '—')
  assert.equal(formatShare({ read_share: 0.62 }), '62%')
  assert.equal(formatRatio({ ratio: null }), '—')
  assert.equal(formatRatio({ ratio: 2.214 }), '2.21:1')
  assert.equal(formatRatio({ ratio: 119 }), '119.0:1')
})

test('token counts abbreviate readably', () => {
  assert.equal(fmtTokens(1400000), '1.4M')   // the sonnet seat's writes
  assert.equal(fmtTokens(4200), '4.2k')
  assert.equal(fmtTokens(0), '0')
})
