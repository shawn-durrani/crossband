// What the Spend page says BEFORE any detail.
//
// The page used to open with tables. The first questions are simpler: over the
// period I care about, is spend up or down, roughly what's it made of, and is
// the number even accurate? Detail is a drill-down, not a landing.
//
// Pure so the honesty rules are unit-tested rather than argued about in JSX.

// A dollar is only ever "spent" if it was actually billed. `metered` is
// pay-as-you-go API billing we can stand behind; `subscription_equiv` is usage
// covered by a subscription (real usage, not incremental cash); `unknown` has
// no provenance at all. Summing them would invent a number nobody is charged.
export function headline(totals) {
  const t = totals || {}
  return {
    billed: t.metered || 0,
    covered: t.subscription_equiv || 0,
    unknown: t.unknown || 0,
  }
}

// Is the headline number trustworthy on its own? Only when everything in the
// window is billed. Anything else and the page has to say what it's mixing —
// silently showing one total made of three different kinds of dollar is the
// dishonest version of this page.
export function accuracy(totals) {
  const { billed, covered, unknown } = headline(totals)
  if (unknown > 0) {
    return {
      level: 'partial',
      label: 'Partly unverified',
      detail: 'Some usage has no recorded cost, so the real total is higher than shown.',
    }
  }
  if (covered > 0) {
    return {
      level: 'mixed',
      label: 'Billed + subscription',
      detail: 'Some usage was covered by a subscription, so it is shown separately and never added to billed spend.',
    }
  }
  return {
    level: 'exact',
    label: 'Billed spend',
    detail: 'Every dollar here is pay-as-you-go API billing.',
  }
}

// Up or down against the SAME-LENGTH period immediately before. Returns null
// when there is nothing honest to compare against — an all-time view has no
// "before", and a previous period with no spend makes a percentage meaningless
// (anything over zero is infinity, which tells you nothing).
export function direction(totals, previousTotals, spanDays) {
  if (!previousTotals || !spanDays) return null
  const now = headline(totals).billed
  const before = headline(previousTotals).billed
  const period = spanDays === 1 ? 'yesterday' : `the previous ${spanDays} days`
  if (before <= 0) {
    return now > 0
      ? { trend: 'up', pct: null, label: `up from nothing in ${period}`, period }
      : null
  }
  const pct = ((now - before) / before) * 100
  const trend = Math.abs(pct) < 1 ? 'flat' : (pct > 0 ? 'up' : 'down')
  const verb = { up: 'up', down: 'down', flat: 'level with' }[trend]
  return {
    trend,
    pct,
    // The comparison is always named. "Up 40%" without saying against what is
    // the kind of number that starts arguments.
    label: trend === 'flat'
      ? `level with ${period}`
      : `${verb} ${Math.abs(Math.round(pct))}% on ${period}`,
    period,
  }
}

// The rough shape of the window — a few slices, biggest first, with a single
// "everything else" tail. A landing page answers "mostly what?", not "exactly
// what?"; the full table is one click deeper.
//
// Metered only, matching the headline: the "mostly spent on" bars must draw the
// SAME dollar the page states above them. Folding subscription_equiv in here
// made a slice bigger than the metered total it belonged to; a sunk
// subscription cost is not incremental spend and is never summed into a total.
export function roughBreakdown(rows, limit = 4) {
  const sorted = (rows || [])
    .map((r) => ({ label: r.label || r.key, value: r.metered || 0 }))
    .filter((r) => r.value > 0)
    .sort((a, b) => b.value - a.value)
  if (sorted.length <= limit) return sorted
  const head = sorted.slice(0, limit)
  const tail = sorted.slice(limit).reduce((s, r) => s + r.value, 0)
  return [...head, { label: `${sorted.length - limit} others`, value: tail }]
}

export const SPANS = [
  ['today', 'Today'],
  ['7d', '7 days'],
  ['30d', '30 days'],
  ['90d', '90 days'],
  ['all', 'All time'],
]

// Cumulative BILLED spend, day by day. Two deliberate choices:
//
// Cumulative, because the question is "how are we tracking" — on a running
// total the SLOPE is the burn rate, so flattening reads as easing off and
// steepening as accelerating. Daily bars make you integrate by eye.
//
// Billed only, because the headline is billed only. Summing metered +
// subscription + unknown here would draw a line ending at a number the page
// refuses to state anywhere else. Picture the curve finishing near $900 under
// a headline that reads $500: nothing on the page reconciles the two, so the
// reader is left guessing which of them is the bill. A chart that disagrees
// with the figure printed above it is worse than no chart.
export function cumulative(series) {
  let run = 0
  return (series || []).map((d) => {
    run += d.metered || 0
    return { day: d.day, value: run }
  })
}

// Two cumulative lines on one scale: this period, and the same-length period
// before it. Indexed by DAY NUMBER, not date, so day 1 sits above day 1 — the
// only way an at-a-glance comparison means anything when the two periods cover
// different calendar dates.
//
// Returns null when there is nothing to draw. A previous period with no spend
// still draws (a flat line at zero is a true and useful statement); a missing
// one simply doesn't.
export function trendLines(series, previousSeries) {
  const now = cumulative(series)
  const before = cumulative(previousSeries)
  if (!now.length) return null
  const span = Math.max(now.length, before.length)
  const peak = Math.max(
    now.length ? now[now.length - 1].value : 0,
    before.length ? before[before.length - 1].value : 0,
    0.0001)
  // x is the position within the period (0..1), so the two align by day index.
  const path = (pts) => pts.map((p, i) => ({
    x: span > 1 ? i / (span - 1) : 0,
    y: p.value / peak,
    day: p.day,
    value: p.value,
  }))
  return {
    now: path(now),
    before: before.length ? path(before) : null,
    peak,
    // Where this period sits against the last one at the SAME point in time —
    // comparing a part-finished period against a whole one would flatter it.
    aheadBy: before.length
      ? now[now.length - 1].value - (before[Math.min(now.length, before.length) - 1]?.value || 0)
      : null,
  }
}

// An SVG polyline "d" for a set of 0..1 points in a w×h box, y flipped.
export function linePath(points, w, h) {
  if (!points || !points.length) return ''
  if (points.length === 1) return `M0,${h - points[0].y * h} L${w},${h - points[0].y * h}`
  return points.map((p, i) => `${i ? 'L' : 'M'}${(p.x * w).toFixed(2)},${(h - p.y * h).toFixed(2)}`).join(' ')
}
