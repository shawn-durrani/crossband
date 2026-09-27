// #407: the Analysis page. The measurements a decision needs numbers from,
// each stating what it costs and what it touches before its Run button, a
// run as a background job with a chip and a status ping, and the stored
// reports newest first. All meaning lives in ../analysisView.js (pure,
// unit-tested); this file is markup and wiring.
//
// Owner-only: every /api/analysis route needs a signed-in session, so an
// install with no owner password shows the server's reason instead.
import { useEffect, useRef, useState } from 'react'
import ReactMarkdown from 'react-markdown'
import remarkGfm from 'remark-gfm'
import { BookOpen, ChevronDown, ChevronRight, Download, FlaskConical, Menu, Play, Square, Trash2, X } from 'lucide-react'

import { api } from '../api.js'
import {
  chipText, confirmText, historyDetail, historyTitle, latestFor, newestFirst,
  pingText, readmeUrl, reportJsonUrl, runHold, shouldPoll, spentLabel,
  stateBadge, whenLabel,
} from '../analysisView.js'

const TONE = {
  live: 'text-ink', ok: 'text-ink-mid', bad: 'text-red-400', muted: 'text-ink-dim',
}

const nowSecs = () => Date.now() / 1000

function RunningChip({ run, now, onStop }) {
  return (
    <div className="flex items-center gap-2 text-xs bg-panel2 border border-edge2 rounded-lg px-3 py-1.5" role="status">
      <span className="running-dot shrink-0" aria-hidden="true" />
      <span className="flex-1 min-w-0">
        <span className="text-ink-mid">{chipText(run, now)}</span>
        {pingText(run, now) && <span className="block text-ink-faint">{pingText(run, now)}</span>}
      </span>
      <button
        className="inline-flex items-center gap-1 rounded-full px-2 py-1 border border-edge2 text-ink-dim hover:text-ink shrink-0"
        onClick={onStop}
      >
        <Square size={11} /> Stop
      </button>
    </div>
  )
}

function MeasurementCard({ m, runs, now, starting, onRun, onStop, onOpen }) {
  const hold = runHold(m, starting)
  const last = latestFor(runs, m.id)
  return (
    <div className="border border-edge rounded-xl px-4 py-3 space-y-2">
      <div className="flex items-start gap-2 flex-wrap">
        <h2 className="text-sm font-semibold text-ink flex-1 min-w-0">{m.title}</h2>
        {m.own_data && (
          <span className="text-[11px] rounded-full px-2 py-0.5 border border-amber-500/40 text-amber-500 shrink-0">
            Reads your own data
          </span>
        )}
      </div>
      <p className="text-sm text-ink-mid">{m.measures}</p>
      <div className="text-xs space-y-1">
        <p><span className="text-ink-dim">What it costs: </span><span className="text-ink-mid">{m.costs}</span></p>
        <p><span className="text-ink-dim">How long: </span><span className="text-ink-mid">{m.takes}</span></p>
        <p><span className="text-ink-dim">What it touches: </span><span className="text-ink-mid">{m.touches}</span></p>
      </div>
      {m.running && <RunningChip run={m.running} now={now} onStop={() => onStop(m.running.run_id)} />}
      <div className="flex flex-wrap items-center gap-2">
        <button
          className="inline-flex items-center gap-1.5 bg-btn text-btn-ink rounded-lg px-3 py-1.5 text-sm font-semibold hover:bg-btn-hover disabled:opacity-50"
          disabled={!!hold}
          onClick={() => onRun(m, false)}
        >
          <Play size={13} /> Run
        </button>
        <button
          className="inline-flex items-center gap-1.5 border border-edge2 rounded-lg px-3 py-1.5 text-sm text-ink-mid hover:text-ink disabled:opacity-50"
          disabled={!!hold}
          onClick={() => onRun(m, true)}
        >
          Practice run, free
        </button>
        {hold && hold !== 'Running now.' && <span className="text-xs text-ink-faint">{hold}</span>}
      </div>
      {last && (
        <button className="block w-full text-left text-xs text-ink-dim hover:text-ink" onClick={() => onOpen(last.run_id)}>
          Last run {whenLabel(last.created_at_unix, now)}: {historyDetail(last) || stateBadge(last).label}
        </button>
      )}
      <p className="text-[11px] text-ink-faint break-words">
        From a terminal: <code>{m.command}</code>
        {readmeUrl(m) && (
          <>
            {' · '}
            <a className="inline-flex items-center gap-0.5 text-link hover:underline" href={readmeUrl(m)} target="_blank" rel="noreferrer">
              <BookOpen size={11} /> How to read the report
            </a>
          </>
        )}
      </p>
    </div>
  )
}

function ReportView({ detail, onDelete }) {
  if (!detail) return <p className="px-3 pb-3 text-xs text-ink-faint">Loading the report…</p>
  const { run, report } = detail
  return (
    <div className="px-3 pb-3 space-y-2">
      {run.error && <p className="text-xs text-red-400 whitespace-pre-wrap break-words">{run.error}</p>}
      {report ? (
        <div className="overflow-x-auto border border-edge rounded-lg px-3 py-2">
          <div className="md-body report-md text-sm">
            <ReactMarkdown remarkPlugins={[remarkGfm]}>{report}</ReactMarkdown>
          </div>
        </div>
      ) : (
        <p className="text-xs text-ink-faint">This run left no report.</p>
      )}
      <div className="flex flex-wrap items-center gap-3 text-xs">
        <span className="text-ink-faint break-all"><code>{run.command}</code></span>
        {run.has_report && (
          <a className="inline-flex items-center gap-1 text-link hover:underline" href={reportJsonUrl(run.run_id)} download>
            <Download size={11} /> JSON
          </a>
        )}
        {run.state !== 'running' && (
          <button className="inline-flex items-center gap-1 text-ink-dim hover:text-red-400" onClick={() => onDelete(run.run_id)}>
            <Trash2 size={11} /> Delete
          </button>
        )}
      </div>
    </div>
  )
}

export default function AnalysisPage({ onClose, onOpenMenu, onChanged, openRunId }) {
  const [view, setView] = useState(null)
  const [error, setError] = useState(null)
  const [starting, setStarting] = useState(null)
  const [openId, setOpenId] = useState(null)
  const [detail, setDetail] = useState(null)
  const [now, setNow] = useState(nowSecs)
  const runningKey = useRef('')

  async function load() {
    try {
      const v = await api.analysis()
      setView(v)
      setError(null)
      setNow(nowSecs())
      // The sidebar's dot reads /api/state; refresh it when a run starts or ends.
      const key = v.measurements.filter((m) => m.running).map((m) => m.id).join(',')
      if (key !== runningKey.current) {
        runningKey.current = key
        onChanged?.()
      }
      return v
    } catch (e) {
      setError(e.message || 'Could not load the Analysis page.')
      return null
    }
  }

  useEffect(() => { load() }, [])

  // A chat line's report link names a run to open. It opens once the list
  // has loaded, so the page can bring it into view.
  const loaded = !!view
  useEffect(() => {
    if (openRunId && loaded) openFromCard(openRunId)
  }, [openRunId, loaded])

  // While anything runs, ask every two seconds; stop asking once it's quiet.
  const polling = shouldPoll(view)
  useEffect(() => {
    if (!polling) return
    const t = setInterval(load, 2000)
    return () => clearInterval(t)
  }, [polling])

  // An open report follows its run: when the run settles, fetch it again.
  const openRun = view?.runs?.find((r) => r.run_id === openId)
  useEffect(() => {
    if (!openId) return
    api.analysisRun(openId).then(setDetail).catch((e) => setError(e.message))
  }, [openId, openRun?.state])

  async function run(m, practice) {
    const question = confirmText(m, practice)
    if (question && !window.confirm(question)) return
    setStarting(m.id)
    setError(null)
    try {
      await api.analysisStart(m.id, practice)
      await load()
    } catch (e) {
      setError(e.message || 'Could not start it.')
    } finally {
      setStarting(null)
    }
  }

  async function stop(runId) {
    try { await api.analysisStop(runId) } catch (e) { setError(e.message) }
    load()
  }

  async function remove(runId) {
    if (!window.confirm('Delete this report from this Mac?')) return
    try {
      await api.analysisDelete(runId)
      if (openId === runId) { setOpenId(null); setDetail(null) }
      load()
    } catch (e) {
      setError(e.message)
    }
  }

  // A card's "Last run" line opens that report in the history below and
  // brings it into view, which on a phone is a long way down.
  function openFromCard(runId) {
    setDetail(null)
    setOpenId(runId)
    requestAnimationFrame(() => document.getElementById(`run-${runId}`)
      ?.scrollIntoView({ block: 'start', behavior: 'smooth' }))
  }

  function toggle(runId) {
    setDetail(null)
    setOpenId((id) => (id === runId ? null : runId))
  }

  const runs = newestFirst(view?.runs)

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
              <FlaskConical size={18} className="text-ink-dim" /> Analysis
            </h1>
            <p className="text-sm text-ink-mid mt-0.5">
              Measurements you run when a decision needs numbers. Each one says
              what it costs and what it touches before you press Run, and it
              keeps going in the background if you leave. A practice run is
              free: it checks the harness works, with made-up numbers. Reports
              stay on this Mac, and only you can open this page.
            </p>
          </div>
          <button
            className="text-xs inline-flex items-center gap-1 rounded-full px-2 py-1.5 border border-edge2 text-ink-dim hover:text-ink shrink-0"
            onClick={onClose}
          >
            <X size={13} /> Back to chat
          </button>
        </header>

        {error && <p className="text-sm text-red-400 break-words" role="alert">{error}</p>}

        {view && (
          <section className="space-y-3" aria-label="Measurements">
            {view.measurements.map((m) => (
              <MeasurementCard key={m.id} m={m} runs={runs} now={now}
                starting={starting === m.id} onRun={run} onStop={stop}
                onOpen={openFromCard} />
            ))}
          </section>
        )}

        {view && (
          <section className="space-y-2" aria-label="Past reports">
            <h2 className="text-sm font-semibold text-ink">Past reports</h2>
            {!runs.length && <p className="text-xs text-ink-faint">No runs yet.</p>}
            {runs.length > 0 && (
              <ul className="border border-edge rounded-xl divide-y divide-edge">
                {runs.map((r) => {
                  const badge = stateBadge(r)
                  const spent = spentLabel(r.summary?.spent_usd)
                  const open = openId === r.run_id
                  return (
                    <li key={r.run_id} id={`run-${r.run_id}`}>
                      <button className="w-full text-left px-3 py-2 hover:bg-panel" aria-expanded={open}
                        onClick={() => toggle(r.run_id)}>
                        <span className="flex items-center gap-2 text-sm">
                          {open ? <ChevronDown size={13} className="shrink-0 text-ink-dim" /> : <ChevronRight size={13} className="shrink-0 text-ink-dim" />}
                          <span className={`flex-1 min-w-0 truncate ${TONE[badge.tone]}`}>{historyTitle(r)}</span>
                          <span className="text-xs text-ink-faint shrink-0">{whenLabel(r.created_at_unix, now)}</span>
                        </span>
                        {(historyDetail(r) || spent) && (
                          <span className="block text-xs text-ink-dim mt-0.5 ml-5 break-words">
                            {historyDetail(r)}{spent && ` Spent ${spent}.`}
                          </span>
                        )}
                      </button>
                      {open && <ReportView detail={detail} onDelete={remove} />}
                    </li>
                  )
                })}
              </ul>
            )}
          </section>
        )}

        <p className="text-xs text-ink-faint">
          Guards aren&apos;t here. The silence eval and the doc style test run in CI
          on every change, pass or fail, and never need a button.
        </p>
      </div>
    </div>
  )
}
