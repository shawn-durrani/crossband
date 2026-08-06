// Tests for the Spend page's cache-health block: it must surface a per-seat
// cache regression immediately rather than days later.
//
// The load-bearing behaviour: a window aggregate can read "good" on the
// strength of a few healthy rows while one seat sits at "poor" and spends most
// of its own bill re-writing the cache. Averaging is what hides that, so this
// must lead with the WORST row.
// Run: node --test frontend/src/cacheHealth.test.js
import { test } from 'node:test'
import assert from 'node:assert/strict'
import {
  verdictOf, formatRatio, cacheRows, headline, fmtTokens, VERDICTS,
} from './cacheHealth.js'

// An invented window carrying the mix this block exists for: healthy rows, one
// poor seat, one seat whose writes are never reported, one idle seat.
//
// The arithmetic is internally consistent on purpose, because the whole point
// of the module is that the aggregate and the rows disagree about the STORY
// while agreeing about the SUM. Every row's ratio is its own cache_read /
// cache_creation, and the aggregate is the column-wise sum of the rows
// (30,000,000 / 6,000,000 = 5.00:1, which reads "good"). Keep it that way if
// you edit these numbers: a fixture whose totals do not add up can make the
// sorting look right for the wrong reason.
const WINDOW = {
  breakdown: {
    cache: { read: 30000000, written: 6000000, ratio: 5.0, verdict: 'good',
             write_cost: 20.00, write_cost_share: 0.70 },
    by_producer_model: [
      { label: 'claude-opus-4-8 · Agents / coding guests',
        cache_read: 22152000, cache_creation: 1200000, cache_write_cost: 0,
        cache: { ratio: 18.46, verdict: 'good' } },
      { label: 'claude-sonnet-5 · Chat participants',
        cache_read: 4080000, cache_creation: 4800000, cache_write_cost: 20.00,
        cache: { ratio: 0.85, verdict: 'poor' } },
      { label: 'gpt-5.6-terra · Chat participants',
        cache_read: 3768000, cache_creation: 0, cache_write_cost: 0,
        cache: { ratio: null, verdict: 'no_writes' } },
      { label: 'claude-haiku-4-5 · Utility',
        cache_read: 0, cache_creation: 0, cache_write_cost: 0,
        cache: { ratio: null, verdict: 'none' } },
    ],
  },
}

test('the fixture adds up, so every assertion below rests on real arithmetic', () => {
  const { cache, by_producer_model: rows } = WINDOW.breakdown
  const sum = (k) => rows.reduce((total, row) => total + row[k], 0)
  assert.equal(sum('cache_read'), cache.read)
  assert.equal(sum('cache_creation'), cache.written)
  assert.equal(cache.read / cache.written, cache.ratio)
  for (const row of rows) {
    if (row.cache.ratio === null) continue
    assert.equal(row.cache_read / row.cache_creation, row.cache.ratio, row.label)
  }
})

test('the worst row leads, not the healthy majority', () => {
  const rows = cacheRows(WINDOW.breakdown.by_producer_model)
  assert.match(rows[0].label, /claude-sonnet-5 · Chat/)
})

test('rows with no cache activity at all are dropped as noise', () => {
  const rows = cacheRows(WINDOW.breakdown.by_producer_model)
  assert.equal(rows.length, 3)
  assert.ok(!rows.some((r) => /haiku/.test(r.label)))
})

test('the headline names the culprit rather than reporting the average', () => {
  const h = headline(WINDOW)
  assert.equal(h.alert, true)
  assert.match(h.text, /claude-sonnet-5 · Chat participants/)
  assert.match(h.text, /0\.85:1/)
  // the window aggregate said "good"; that must NOT be what surfaces
  assert.doesNotMatch(h.text, /5\.00/)
})

test('the window-level spend share rides along as the "so what"', () => {
  assert.match(headline(WINDOW).shareText, /70\.0% of metered spend/)
})

test('an all-healthy window says so plainly and does not alert', () => {
  const ok = { breakdown: { cache: { write_cost_share: 0 }, by_producer_model: [
    { label: 'a', cache_read: 100, cache_creation: 10, cache: { ratio: 10, verdict: 'good' } },
  ] } }
  const h = headline(ok)
  assert.equal(h.alert, false)
  assert.match(h.text, /re-read, not re-written/)
  assert.equal(h.shareText, null)
})

test('a window with no cache activity has no headline at all', () => {
  assert.equal(headline({ breakdown: { by_producer_model: [] } }), null)
  assert.equal(headline(undefined), null)
})

test('"poor" is explained as costing money, not as a neutral statistic', () => {
  assert.equal(VERDICTS.poor.tone, 'poor')
  assert.match(VERDICTS.poor.title, /costing money instead of saving it/)
})

test('reads-with-no-writes is neutral, never a failure', () => {
  const v = verdictOf({ verdict: 'no_writes' })
  assert.equal(v.tone, 'neutral')
  // the OpenAI seat lands here because we do not capture its writes
  assert.match(v.title, /does not report/)
})

test('a null ratio renders as a dash, never 0:1 or NaN', () => {
  assert.equal(formatRatio({ ratio: null }), '—')
  assert.equal(formatRatio(undefined), '—')
  // both figures are straight off the fixture rows: below 10 keeps two
  // decimals (the sonnet seat), at or above 10 drops to one (the opus seat)
  assert.equal(formatRatio({ ratio: 0.85 }), '0.85:1')
  assert.equal(formatRatio({ ratio: 18.46 }), '18.5:1')
})

test('token counts abbreviate readably', () => {
  assert.equal(fmtTokens(4800000), '4.8M')   // the sonnet seat's writes
  assert.equal(fmtTokens(4200), '4.2k')
  assert.equal(fmtTokens(0), '0')
})
