// Prompt-cache health for the Spend page.
//
// A prefix that quietly stops being re-read and starts being re-written costs
// real money in plain sight. The tokens were already in usage_json; nothing on
// this page aggregated them. A cache WRITE bills at 1.25x input and a READ at
// 0.1x, so a write costs 12.5x a read, and neither the dollar total nor the
// token total moves much when that switch happens. The ratio is the signal.
//
// THE DESIGN POINT: a window aggregate can read healthy while one seat inside
// it is unhealthy. Picture a window averaging 8:1 read:write across every seat
// while one seat sits below 1:1 and spends most of its own bill on cache
// writes; the healthy rows drown it out. So this module leads with the WORST
// row rather than the average, because an average is what hides that case.
//
// Pure (no React), like spendView.js and lifecycle.js.

export const VERDICTS = {
  good: { label: 'Healthy', tone: 'good',
          title: 'The conversation prefix is being re-read from cache far more '
               + 'often than it is re-written. This is what you want.' },
  watch: { label: 'Watch', tone: 'watch',
           title: 'The prefix is being re-written nearly as often as it is read '
                + 'back. Cache writes cost 12.5x a read, so this is where a bill '
                + 'starts drifting upward.' },
  poor: { label: 'Re-writing', tone: 'poor',
          title: 'The prefix is being re-written MORE often than it is read back '
               + '- the cache is costing money instead of saving it. Something in '
               + 'the prompt is changing between turns.' },
  no_writes: { label: 'No cache writes recorded', tone: 'neutral',
               title: 'Reads without writes - either this provider does not report '
                    + 'written tokens to us, or nothing was cached.' },
  none: { label: 'No cache activity', tone: 'neutral',
          title: 'This model did no prompt caching in this window.' },
}

export function verdictOf(cache) {
  return VERDICTS[cache?.verdict] || VERDICTS.none
}

export function formatRatio(cache) {
  const r = cache?.ratio
  if (r === null || r === undefined) return '—'
  return `${r.toFixed(r < 10 ? 2 : 1)}:1`
}

// Rows worth showing: only those that actually cached. A row with no cache
// activity is noise here, not a finding.
export function cacheRows(byProducerModel) {
  return (byProducerModel || [])
    .filter((g) => (g.cache_read || 0) + (g.cache_creation || 0) > 0)
    .sort((a, b) => rank(a) - rank(b))
}

// Worst first: 'poor' before 'watch' before everything else, and within a
// verdict the one burning the most on writes leads.
function rank(g) {
  const order = { poor: 0, watch: 1, good: 2, no_writes: 3, none: 4 }
  const v = order[g.cache?.verdict] ?? 5
  return v * 1e9 - (g.cache_write_cost || 0)
}

// The one-line answer this block exists to give. Deliberately NOT the window
// average - see the module header.
export function headline(summary) {
  const rows = cacheRows(summary?.breakdown?.by_producer_model)
  const worst = rows[0]
  if (!worst) return null
  const bad = worst.cache?.verdict === 'poor' || worst.cache?.verdict === 'watch'
  const share = summary?.breakdown?.cache?.write_cost_share || 0
  return {
    worst,
    alert: bad,
    text: bad
      ? `${worst.label} is re-writing its prompt cache (${formatRatio(worst.cache)} read:write).`
      : 'Prompt caches are being re-read, not re-written.',
    // Share is a window-level fact and stays window-level - it is the "so
    // what" for the row above, not a per-row claim.
    shareText: share > 0
      ? `${(share * 100).toFixed(1)}% of metered spend went on cache writes.`
      : null,
  }
}

export function fmtTokens(n) {
  const v = Number(n || 0)
  if (v >= 1e6) return `${(v / 1e6).toFixed(1)}M`
  if (v >= 1e3) return `${(v / 1e3).toFixed(1)}k`
  return String(v)
}
