// Tests for the Spend page: it must lead with an honest answer, not a table.
// Run: node --test frontend/src/spendView.test.js
import { test } from 'node:test'
import assert from 'node:assert/strict'
import { headline, accuracy, direction, roughBreakdown, cumulative, trendLines, linePath } from './spendView.js'

test('the three kinds of dollar are never summed', () => {
  // Adding billed + subscription-covered + unknown invents a number nobody is
  // charged. They stay apart, always.
  const h = headline({ metered: 10, subscription_equiv: 5, unknown: 2 })
  assert.deepEqual(h, { billed: 10, covered: 5, unknown: 2 })
})

test('accuracy says exactly what the number is made of', () => {
  assert.equal(accuracy({ metered: 10 }).level, 'exact')
  assert.equal(accuracy({ metered: 10, subscription_equiv: 5 }).level, 'mixed')
  // unknown wins: an unpriced turn means the real total is HIGHER than shown,
  // which the user has to be told rather than left to infer
  assert.equal(accuracy({ metered: 10, subscription_equiv: 5, unknown: 1 }).level, 'partial')
  assert.match(accuracy({ metered: 1, unknown: 1 }).detail, /higher than shown/)
})

test('direction is measured against the same-length previous period, and says so', () => {
  const d = direction({ metered: 14 }, { metered: 10 }, 7)
  assert.equal(d.trend, 'up')
  assert.equal(Math.round(d.pct), 40)
  assert.match(d.label, /previous 7 days/)   // the comparison is always named
})

test('down and flat read correctly', () => {
  assert.equal(direction({ metered: 5 }, { metered: 10 }, 30).trend, 'down')
  assert.equal(direction({ metered: 100 }, { metered: 100.2 }, 30).trend, 'flat')
  assert.match(direction({ metered: 100 }, { metered: 100.2 }, 30).label, /level with/)
})

test('no direction where there is nothing honest to compare', () => {
  assert.equal(direction({ metered: 10 }, null, null), null)       // all time
  assert.equal(direction({ metered: 0 }, { metered: 0 }, 7), null)  // nothing either side
})

test('a zero previous period gives no percentage rather than infinity', () => {
  const d = direction({ metered: 10 }, { metered: 0 }, 7)
  assert.equal(d.pct, null)
  assert.match(d.label, /up from nothing/)
})

test('today compares against yesterday, not "the previous 1 days"', () => {
  assert.match(direction({ metered: 2 }, { metered: 1 }, 1).label, /yesterday/)
})

test('the rough breakdown is a few slices with one honest tail', () => {
  const rows = [
    { label: 'a', metered: 10 }, { label: 'b', metered: 8 }, { label: 'c', metered: 6 },
    { label: 'd', metered: 4 }, { label: 'e', metered: 2 }, { label: 'f', metered: 1 },
  ]
  const out = roughBreakdown(rows, 4)
  assert.equal(out.length, 5)
  assert.equal(out[0].label, 'a')            // biggest first
  assert.equal(out[4].label, '2 others')     // the tail names how many it hides
  assert.equal(out[4].value, 3)
})

test('empty and zero rows are dropped, not rendered as noise', () => {
  assert.deepEqual(roughBreakdown([]), [])
  assert.deepEqual(roughBreakdown([{ label: 'z', metered: 0 }]), [])
})

test('the rough breakdown is metered only - subscription is never blended in', () => {
  // the "mostly spent on" bars draw the same dollar the headline states.
  // A row that is entirely subscription-covered contributes nothing here, and a
  // mixed row shows only its metered part - a sunk subscription is not spend.
  const out = roughBreakdown([
    { label: 'metered-only', metered: 5 },
    { label: 'mixed', metered: 2, subscription_equiv: 100 },
    { label: 'sub-only', metered: 0, subscription_equiv: 50 },
  ], 4)
  assert.deepEqual(out, [
    { label: 'metered-only', value: 5 },
    { label: 'mixed', value: 2 },
  ])
})

// ── the trendline ───────────────────────────────────────────────────────────

test('cumulative tracks BILLED spend only, matching the headline', () => {
  // The chart must end where the headline says. Summing the three kinds would
  // draw a line ending at a total the page refuses to state; found in practice,
  // with the chart ending well above its own headline.
  const out = cumulative([
    { day: '1', metered: 1 },
    { day: '2', metered: 2, subscription_equiv: 1 },
    { day: '3', unknown: 0.5 },
  ])
  assert.deepEqual(out.map((d) => d.value), [1, 3, 3])
})

test('the two periods align by DAY NUMBER, not by date', () => {
  // Day 1 must sit above day 1 - the periods cover different calendar dates, so
  // aligning any other way makes the comparison meaningless.
  const t = trendLines([{ day: 'a', metered: 1 }, { day: 'b', metered: 1 }],
                       [{ day: 'y', metered: 4 }, { day: 'z', metered: 4 }])
  assert.equal(t.now[0].x, 0)
  assert.equal(t.before[0].x, 0)
  assert.equal(t.now[1].x, 1)
  assert.equal(t.before[1].x, 1)
})

test('both lines share one scale, so being under last period LOOKS under', () => {
  const t = trendLines([{ day: 'a', metered: 2 }], [{ day: 'y', metered: 10 }])
  assert.equal(t.peak, 10)
  assert.ok(t.now[0].y < t.before[0].y)
})

test('aheadBy compares at the same point in the period, not end-to-end', () => {
  // Two days in against a finished four-day period: comparing against the whole
  // of last period would flatter a part-finished one.
  const t = trendLines(
    [{ day: 'a', metered: 3 }, { day: 'b', metered: 3 }],
    [{ day: 'w', metered: 1 }, { day: 'x', metered: 1 }, { day: 'y', metered: 10 }, { day: 'z', metered: 10 }])
  assert.equal(t.aheadBy, 6 - 2)   // 6 so far vs 2 at the same point
})

test('no previous period draws one line, not a fake second', () => {
  const t = trendLines([{ day: 'a', metered: 1 }], [])
  assert.equal(t.before, null)
  assert.equal(t.aheadBy, null)
})

test('nothing to draw returns null rather than an empty chart', () => {
  assert.equal(trendLines([], []), null)
})

test('linePath maps 0..1 points into the box with y flipped', () => {
  const d = linePath([{ x: 0, y: 0 }, { x: 1, y: 1 }], 100, 50)
  assert.equal(d, 'M0.00,50.00 L100.00,0.00')
  assert.equal(linePath([], 10, 10), '')
})
