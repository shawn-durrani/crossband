import { useEffect, useMemo, useState } from 'react'
import { AlertTriangle, BarChart3, Info, Menu } from 'lucide-react'
import { api } from '../api'
import { headline, accuracy, direction, roughBreakdown, trendLines, linePath, SPANS } from '../spendView'
// `headline` is already taken by spendView's own, so alias the cache one.
import {
  headline as headlineCache, cacheRows, verdictOf, formatRatio, fmtTokens,
} from '../cacheHealth'

// Honesty is the whole point of this view: metered spend, estimated
// subscription-equivalent usage, and unknown/unverified cost are shown apart
// and never collapsed into one "you spent $X" number.
const CAT = {
  metered: {
    label: 'Metered spend',
    hint: 'Pay-as-you-go API billing - real cash. Model turns are always here; Claude Code guest turns only when the turn ran on the API key.',
    cls: 'text-ink',
  },
  subscription_equiv: {
    label: 'Subscription-equivalent',
    hint: 'Claude Code guest turns that ran on a Claude subscription (OAuth token) rather than the metered API key. Its reported cost is an API-list-price estimate that the plan already covers; it is not incremental cash.',
    cls: 'text-amber-500',
  },
  unknown: {
    label: 'Unknown / unverified',
    hint: "Guest turns with no recorded auth mode (logged before provenance capture). Can't prove billed or not - shown apart, never added to metered.",
    cls: 'text-ink-dim',
  },
}
const ORDER = ['metered', 'subscription_equiv', 'unknown']
const BAR = { metered: 'bg-link', subscription_equiv: 'bg-amber-500', unknown: 'bg-edge3' }

const money = (n) => (!n ? '$0.00' : `$${n.toFixed(n < 0.1 ? 4 : 2)}`)
const tok = (n) => (n >= 1000 ? `${(n / 1000).toFixed(1)}k` : `${n}`)
// The sum across all three cash kinds - used ONLY as an "is there any activity
// at all here?" test (a subscription-only chat has recorded something, just
// nothing metered). It is never rendered as a spend figure: the headline and
// every breakdown draw metered alone, so subscription-equivalent is never
// blended into a total.
const catTotal = (o) => (o.metered || 0) + (o.subscription_equiv || 0) + (o.unknown || 0)

// Prompt caches bill a WRITE at 1.25x input and a READ at 0.1x, so a write
// costs 12.5x a read. When a prefix starts being re-written instead of
// re-read, neither the dollar total nor the token count on this page moves
// enough to notice, which is how a real cache regression once stayed hidden
// here. Leads with the worst row rather than the window average: the average
// across seats can look healthy while one seat re-writes its prefix every turn
// and spends most of its bill on cache writes.
const CACHE_TONE = {
  poor: 'text-red-400 border-red-400/40',
  watch: 'text-amber-400 border-amber-400/40',
  good: 'text-ink-mid border-edge2',
  neutral: 'text-ink-faint border-edge2',
}

function CacheHealth({ summary }) {
  const h = headlineCache(summary)
  if (!h) return null
  const rows = cacheRows(summary?.breakdown?.by_producer_model)
  return (
    <div className="border border-edge rounded-xl px-3 py-2.5">
      <div className="text-xs font-semibold text-ink-mid mb-1.5">Prompt cache health</div>
      <p className={`text-sm ${h.alert ? 'text-red-400' : 'text-ink-mid'}`}>
        {h.alert && <AlertTriangle size={13} className="inline mb-0.5 mr-1" />}
        {h.text}
      </p>
      {h.shareText && <p className="text-xs text-ink-faint mt-0.5">{h.shareText}</p>}
      <p className="text-[11px] text-ink-faint mt-1.5">
        Storing a conversation in a model&apos;s cache costs about 12&times; more than
        re-reading it. A healthy chat reads far more than it writes; when that flips,
        the prefix is changing between turns and you pay to re-send it every time.
      </p>
      <div className="mt-2 space-y-1">
        {rows.map((g) => {
          const v = verdictOf(g.cache)
          return (
            <div key={g.label} className="flex items-center gap-2 text-xs flex-wrap">
              <span className="text-ink-mid flex-1 min-w-0 break-words">{g.label}</span>
              <span className="text-ink-faint tabular-nums">
                {fmtTokens(g.cache_read)} read / {fmtTokens(g.cache_creation)} written
              </span>
              <span className={`tabular-nums px-1.5 py-0.5 rounded border ${CACHE_TONE[v.tone]}`}
                    title={v.title}>
                {formatRatio(g.cache)}
              </span>
              {g.cache_write_cost > 0 && (
                <span className="text-ink-faint tabular-nums">
                  ${g.cache_write_cost.toFixed(2)} on writes
                </span>
              )}
            </div>
          )
        })}
      </div>
    </div>
  )
}

function BreakdownTable({ title, rows }) {
  if (!rows.length) return null
  return (
    <div>
      <div className="text-xs font-semibold text-ink-mid mb-1.5">{title}</div>
      <div className="space-y-1">
        {rows.map((g) => {
          const total = catTotal(g)
          return (
            <div key={g.key} className="flex items-baseline gap-2 text-xs border-b border-edge/50 pb-1">
              <span className="truncate flex-1 text-ink">{g.label}</span>
              {g.tokens > 0 && <span className="text-ink-faint">{tok(g.tokens)} tok</span>}
              {g.not_tracked && <span className="text-ink-faint italic">not tracked</span>}
              {g.metered > 0 && <span className="text-ink tabular-nums">{money(g.metered)}</span>}
              {g.subscription_equiv > 0 && <span className="text-amber-500 tabular-nums">{money(g.subscription_equiv)}~</span>}
              {g.unknown > 0 && <span className="text-ink-dim tabular-nums">{money(g.unknown)}?</span>}
              {total === 0 && !g.not_tracked && <span className="text-ink-faint">$0.00</span>}
            </div>
          )
        })}
      </div>
    </div>
  )
}


// The trendline: cumulative spend for the selected period, with the previous
// period behind it on the same scale. Cumulative because the question is "how
// are we tracking" - on a running total the SLOPE is the burn rate, so
// flattening reads as easing off and steepening as accelerating, at a glance.
// Daily bars make you integrate by eye.
function TrendLine({ series, previousSeries, spanLabel }) {
  const t = trendLines(series, previousSeries)
  if (!t) return <div className="text-xs text-ink-faint">No dated spend in this period.</div>
  const W = 640, H = 132
  const money = (n) => `$${(n || 0).toFixed(2)}`
  const last = t.now[t.now.length - 1]
  const ahead = t.aheadBy
  return (
    <div>
      <svg viewBox={`0 0 ${W} ${H}`} className="w-full h-36" preserveAspectRatio="none"
        role="img"
        aria-label={`Cumulative spend over ${spanLabel}, ending at ${money(last.value)}`
          + (ahead === null ? '' : `, ${ahead >= 0 ? 'above' : 'below'} the previous period by ${money(Math.abs(ahead))}`)}>
        {t.before && (
          <path d={linePath(t.before, W, H)} fill="none"
            className="stroke-edge3" strokeWidth="2" strokeDasharray="4 4" vectorEffect="non-scaling-stroke" />
        )}
        <path d={linePath(t.now, W, H)} fill="none"
          className="stroke-link" strokeWidth="2.5" vectorEffect="non-scaling-stroke" />
      </svg>
      <div className="flex items-center gap-4 text-[11px] text-ink-faint mt-1 flex-wrap">
        <span className="flex items-center gap-1.5">
          <span className="inline-block w-4 h-0 border-t-2 border-link align-middle" /> this period
        </span>
        {t.before && (
          <span className="flex items-center gap-1.5">
            <span className="inline-block w-4 h-0 border-t-2 border-dashed border-edge3 align-middle" /> previous {spanLabel}
          </span>
        )}
        {ahead !== null && (
          <span className={ahead >= 0 ? 'text-amber-500' : 'text-emerald-500'}>
            {money(Math.abs(ahead))} {ahead >= 0 ? 'more' : 'less'} than the same point last period
          </span>
        )}
        <span className="ml-auto">running total of billed spend</span>
      </div>
    </div>
  )
}

export default function SpendPage({ onClose, onOpenMenu }) {
  const [data, setData] = useState(null)
  const [window, setWindow] = useState('30d')
  const [detail, setDetail] = useState(false)
  const [err, setErr] = useState(null)

  useEffect(() => {
    let live = true
    api.usageSummary(window).then((d) => { if (live) setData(d) }).catch((e) => setErr(e.message))
    return () => { live = false }
  }, [window])

  const b = data?.breakdown
  const notTracked = useMemo(() => b?.not_tracked || [], [b])
  const money = (n) => `$${(n || 0).toFixed(2)}`

  const h = b ? headline(b.totals) : null
  const acc = b ? accuracy(b.totals) : null
  const dir = b ? direction(b.totals, data.previous?.totals, data.span_days) : null
  // Producer-aware landing: draw the model bars from by_producer_model, so a
  // Claude Code guest's Sonnet spend is its OWN bar and never silently inflates
  // the chat participant's. That merge was what made "Sonnet vs GPT" a lie.
  // Falls back to by_model on an older server that doesn't send the split yet.
  const rough = b ? roughBreakdown(b.by_producer_model || b.by_model) : []
  const roughMax = rough.reduce((m, r) => Math.max(m, r.value), 0)

  const TREND_TONE = { up: 'text-amber-500', down: 'text-emerald-500', flat: 'text-ink-mid' }
  const ACC_TONE = { exact: 'text-emerald-500', mixed: 'text-ink-mid', partial: 'text-amber-500' }

  return (
    <div className="flex-1 min-h-0 overflow-y-auto">
      <div className="mx-auto w-full max-w-3xl px-4 sm:px-6 py-6 space-y-5">
        <header className="flex items-start gap-3">
          {onOpenMenu && (
            <button className="sm:hidden text-ink-mid hover:text-ink p-1 -ml-1 shrink-0"
              aria-label="Open chats & settings" onClick={onOpenMenu}>
              <Menu size={20} />
            </button>
          )}
          <div className="flex-1 min-w-0">
            <h1 className="text-lg font-semibold flex items-center gap-2">
              <BarChart3 size={18} className="text-ink-dim" /> Spend
            </h1>
            <p className="text-sm text-ink-mid mt-0.5">
              What you&apos;ve spent across every chat. Billed spend is kept apart from
              usage a subscription already covers - they are never added together.
            </p>
          </div>
        </header>

        <div className="flex items-center gap-1.5 text-xs flex-wrap">
          {SPANS.map(([k, label]) => (
            <button key={k} onClick={() => setWindow(k)}
              className={`rounded-lg px-2.5 py-1 border ${window === k ? 'border-link text-link' : 'border-edge text-ink-mid hover:border-edge2'}`}>
              {label}
            </button>
          ))}
        </div>

        {err && <div className="text-sm text-red-500">Couldn&apos;t load usage: {err}</div>}
        {!data && !err && <div className="text-sm text-ink-dim">Loading…</div>}

        {data && h && (
          <>
            {/* The answer, before any detail. */}
            <div className="border border-edge2 rounded-xl bg-panel p-5">
              <div className="flex items-baseline gap-3 flex-wrap">
                <span className="text-3xl font-semibold tabular-nums">{money(h.billed)}</span>
                <span className="text-sm text-ink-mid">billed</span>
                {dir && <span className={`text-sm ${TREND_TONE[dir.trend]}`}>{dir.label}</span>}
              </div>
              <div className={`text-xs mt-2 ${ACC_TONE[acc.level]}`}>
                {acc.label} - <span className="text-ink-mid">{acc.detail}</span>
              </div>
              {(h.covered > 0 || h.unknown > 0) && (
                <div className="flex gap-4 mt-3 pt-3 border-t border-edge text-xs text-ink-mid">
                  {h.covered > 0 && <span>{money(h.covered)} covered by subscription</span>}
                  {h.unknown > 0 && <span>{money(h.unknown)} unverified</span>}
                </div>
              )}
            </div>

            <div>
              <div className="text-xs font-semibold text-ink-mid mb-1.5">How you&apos;re tracking</div>
              <TrendLine
                series={data.series}
                previousSeries={data.previous_series}
                spanLabel={(SPANS.find(([k]) => k === window) || [null, 'period'])[1].toLowerCase()}
              />
            </div>

            {rough.length > 0 && (
              <div>
                <div className="text-xs font-semibold text-ink-mid mb-2">Mostly spent on</div>
                <div className="space-y-1.5">
                  {rough.map((r) => (
                    <div key={r.label} className="flex items-center gap-2 text-xs">
                      <span className="w-40 shrink-0 truncate text-ink-mid" title={r.label}>{r.label}</span>
                      <span className="flex-1 h-2 bg-app rounded-sm overflow-hidden">
                        <span className="block h-full bg-link/60 rounded-sm"
                          style={{ width: `${roughMax ? (r.value / roughMax) * 100 : 0}%` }} />
                      </span>
                      <span className="tabular-nums text-ink-mid w-14 text-right">{money(r.value)}</span>
                    </div>
                  ))}
                </div>
              </div>
            )}

            <div className="flex items-start gap-2 text-[11px] text-ink-dim bg-app border border-edge rounded-lg px-3 py-2">
              <Info size={13} className="mt-0.5 shrink-0 text-ink-faint" />
              <span><span className="text-ink-mid">{data.auth.label}.</span> {data.auth.note}</span>
            </div>

            <button onClick={() => setDetail((d) => !d)}
              className="text-xs text-ink-mid hover:text-ink border border-edge2 rounded-lg px-3 py-1.5">
              {detail ? 'Hide the detail' : 'Show the detail'}
            </button>

            {detail && (
              <div className="space-y-4">
                {/* The signal that would have caught the cache regression on
                    day one. Sits ABOVE the tables, because a re-writing cache
                    is a finding rather than a column. */}
                <CacheHealth summary={data} />
                {b && catTotal(b.totals) === 0 && b.not_tracked.length === 0 ? (
                  <div className="text-sm text-ink-dim">Nothing recorded in this window.</div>
                ) : (
                  <div className="grid md:grid-cols-2 gap-4">
                    <BreakdownTable title="By producer (chat vs agents)" rows={b.by_producer} />
                    <BreakdownTable title="By model · producer" rows={b.by_producer_model} />
                    <BreakdownTable title="By source" rows={b.by_source} />
                    <BreakdownTable title="By model" rows={b.by_model} />
                    <BreakdownTable title="By provider" rows={b.by_provider} />
                    <BreakdownTable title="By chat" rows={b.by_chat} />
                  </div>
                )}
                {notTracked.length > 0 && (
                  <div className="text-[11px] text-ink-faint">
                    Not tracked (events exist, no cost captured): {notTracked.join(', ')}.
                  </div>
                )}
                <div className="text-[11px] text-ink-faint border-t border-edge pt-2 flex gap-3 flex-wrap">
                  {ORDER.map((c) => (
                    <span key={c} className={CAT[c].cls} title={CAT[c].hint}>
                      <span className={`inline-block w-2 h-2 rounded-sm mr-1 align-middle ${BAR[c]}`} />
                      {CAT[c].label}
                    </span>
                  ))}
                  <span className="ml-auto">~ = estimate · ? = unverified</span>
                </div>
              </div>
            )}
          </>
        )}

        <div className="flex justify-end pt-2">
          <button className="border border-edge2 rounded-lg px-4 py-2 text-sm text-ink-mid hover:border-edge3"
            onClick={onClose}>
            Back to chat
          </button>
        </div>
      </div>
    </div>
  )
}
