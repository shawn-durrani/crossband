// Prompt-cache health for the Spend page.
//
// A prefix that quietly stops being re-read costs real money in plain sight.
// Input read back from the cache bills at a tenth of the input price or less,
// input sent fresh at the full price, and input written to the cache at 1.25
// times it. So the signal is the share of ALL a model's input that was read
// back from the cache (#561). The read:write ratio this block used to judge
// on ignored the input sent at full price, which had grown to about half of
// what the Claude seats pay, and it rated a seat reading 62% as healthy.
//
// The bars live in backend/accounting.py (GOOD_READ_SHARE, POOR_READ_SHARE),
// which returns the verdict; this module only words and orders it.
//
// THE DESIGN POINT: a window aggregate can read healthy while one seat inside
// it is unhealthy. A Claude Code guest reads almost all of its input from
// cache, and a busy one drowns a struggling chat seat in the average. So this
// module leads with the WORST row rather than the average.
//
// Pure (no React), like spendView.js and lifecycle.js.

export const VERDICTS = {
  good: { label: 'Healthy', tone: 'good',
          title: 'This model read 80% or more of its input back from the cache. '
               + 'This is what you want.' },
  watch: { label: 'Watch', tone: 'watch',
           title: 'This model read between half and 80% of its input from the cache. '
                + 'The rest was sent at full price or written to the cache again, '
                + 'so the bill runs higher than it needs to.' },
  poor: { label: 'Poor', tone: 'poor',
          title: 'This model read less than half of its input from the cache. Most '
               + 'of it was sent at full price or written again on each call: '
               + 'something in the prompt keeps changing, or the cache runs out '
               + 'between calls.' },
  none: { label: 'No cache activity', tone: 'neutral',
          title: 'This model did no prompt caching in this window.' },
}

export function verdictOf(cache) {
  return VERDICTS[cache?.verdict] || VERDICTS.none
}

// The share of input read from the cache, as a whole percentage.
export function formatShare(cache) {
  const s = cache?.read_share
  if (s === null || s === undefined) return '—'
  return `${Math.round(s * 100)}%`
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

// Worst first: 'poor' before 'watch' before 'good', and within a verdict the
// lowest share leads.
function rank(g) {
  const order = { poor: 0, watch: 1, good: 2, none: 3 }
  const v = order[g.cache?.verdict] ?? 4
  return v * 10 + (g.cache?.read_share ?? 1)
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
      ? `${worst.label} read only ${formatShare(worst.cache)} of its input from the cache.`
      : 'Every model read most of its input from the cache.',
    // Share is a window-level fact and stays window-level - it is the "so
    // what" for the row above, not a per-row claim.
    shareText: share > 0
      ? `${(share * 100).toFixed(1)}% of metered spend went on cache writes.`
      : null,
  }
}

// The canonical one-decimal formatter moved to format.js (#236); the
// re-export keeps this module's callers and tests stable.
export { fmtTokens } from './format.js'
