// One home for the compact number formats (#236). Six copies of the token
// shortener and four of money() had drifted; the drift that mattered: only
// two token formatters had an M tier, so a multi-million-token figure
// rendered as a five-digit "k" number, easy to misread by a factor of a
// thousand. That fix now applies everywhere.

// Running totals and per-message counts: one decimal, because a total is a
// measurement and reads exact (12.4k / 3.1M).
export function fmtTokens(n) {
  const v = Number(n || 0)
  if (v >= 1e6) return `${(v / 1e6).toFixed(1)}M`
  if (v >= 1e3) return `${(v / 1e3).toFixed(1)}k`
  return String(v)
}

// The context gauge: whole thousands, because the gauge is an indicator and
// reads round. Deliberately different from fmtTokens - see headerView.js.
// The M tier applies here too: a gauge reading "2400k" carries the same
// misread hazard the totals fix removed.
export function fmtTokensRound(n) {
  const v = Number(n || 0)
  if (v >= 1e6) return `${(v / 1e6).toFixed(1)}M`
  if (v >= 1e3) return `${(v / 1e3).toFixed(0)}k`
  return String(v)
}

// Graded decimals: sub-cent costs stay visible without dressing a $12
// figure in four decimal places.
export function money(n) {
  if (!n) return '$0.00'
  return `$${n.toFixed(n < 0.01 ? 4 : n < 0.1 ? 3 : 2)}`
}
