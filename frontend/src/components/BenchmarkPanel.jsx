// The Models-page synthetic benchmark (#94). All meaning lives in
// ../benchmarkView.js (pure, unit-tested); this file is markup and wiring.
//
// Non-interactive by contract: this panel never opens the microphone and
// never plays audio on its own. Generated clips are offered as download
// links only - listening is the human's move, on the human's tap.
import { useEffect, useState } from 'react'
import { AlertTriangle, ChevronDown, ChevronRight, Download, Trash2 } from 'lucide-react'

import { api } from '../api.js'
import {
  artefactUrl, defaultSelection, fmtSeconds, historyLine, pipelineRows,
  progressLine, runRequest, selectionProblems, sttSummary, textRows,
  toggle, ttsRows, unitBadge,
} from '../benchmarkView.js'

const TONE = { ok: 'text-ink-mid', muted: 'text-ink-faint', bad: 'text-red-400' }

function Badge({ unit }) {
  const b = unitBadge(unit)
  return b.label ? <span className={TONE[b.tone]}>{b.label}</span> : null
}

function ArtefactLink({ runId, name }) {
  if (!name) return null
  return (
    <a
      className="inline-flex items-center gap-1 text-link hover:underline"
      href={artefactUrl(runId, name)}
      download={name}
    >
      <Download size={11} /> {name}
    </a>
  )
}

function CheckRow({ checked, onChange, label, note, disabled = false }) {
  return (
    <label className={`flex items-start gap-2 text-xs ${disabled ? 'opacity-50' : 'cursor-pointer'}`}>
      <input type="checkbox" className="mt-0.5" checked={checked}
             disabled={disabled} onChange={onChange} />
      <span>
        <span className="text-ink">{label}</span>
        {note && <span className="block text-[11px] text-ink-faint">{note}</span>}
      </span>
    </label>
  )
}

export default function BenchmarkPanel() {
  const [open, setOpen] = useState(false)
  const [cat, setCat] = useState(null)
  const [sel, setSel] = useState(null)
  const [view, setView] = useState(null) // the run whose results are showing
  const [runs, setRuns] = useState([])
  const [starting, setStarting] = useState(false)
  const [error, setError] = useState(null)

  async function load() {
    try {
      const c = await api.benchmarkCatalogue()
      setCat(c)
      setSel((s) => s || defaultSelection(c))
      const r = await api.benchmarkRuns()
      setRuns(r.runs)
      // a run left going from a previous visit resumes showing live
      if (c.active_run_id) setView(await api.benchmarkRun(c.active_run_id))
      setError(null)
    } catch (e) {
      setError(e.message || 'Could not load the benchmark.')
    }
  }

  useEffect(() => { if (open && cat === null) load() }, [open])

  // While a run is going, poll it; when it settles, refresh the history once.
  useEffect(() => {
    if (view?.state !== 'running') return
    const t = setInterval(async () => {
      try {
        const r = await api.benchmarkRun(view.run_id)
        setView(r)
        if (r.state !== 'running') setRuns((await api.benchmarkRuns()).runs)
      } catch { /* keep the last snapshot; the next tick retries */ }
    }, 1500)
    return () => clearInterval(t)
  }, [view?.state, view?.run_id])

  async function start() {
    setStarting(true); setError(null)
    try {
      const { run_id } = await api.benchmarkStart(runRequest(sel))
      setView(await api.benchmarkRun(run_id))
    } catch (e) {
      setError(e.message || 'Could not start the run.')
    } finally {
      setStarting(false)
    }
  }

  async function removeRun(runId) {
    if (!confirm('Delete this run and its saved audio?')) return
    try {
      await api.benchmarkDelete(runId)
      setRuns((await api.benchmarkRuns()).runs)
      if (view?.run_id === runId) setView(null)
    } catch (e) {
      setError(e.message)
    }
  }

  async function show(runId) {
    try { setView(await api.benchmarkRun(runId)) } catch (e) { setError(e.message) }
  }

  const problems = sel && cat ? selectionProblems(sel, cat) : ['loading']
  const running = view?.state === 'running'
  const progress = progressLine(view)
  const texts = view ? textRows(view) : []
  const ttss = view ? ttsRows(view) : []
  const pipes = view ? pipelineRows(view) : []
  const stt = view ? sttSummary(view) : null
  const cell = 'py-1 pr-3 align-top'

  return (
    <section className="border border-edge rounded-xl">
      <h3>
        <button
          className="w-full flex items-center gap-2 px-3 py-2.5 text-left"
          onClick={() => setOpen((v) => !v)}
          aria-expanded={open}
        >
          {open ? <ChevronDown size={15} /> : <ChevronRight size={15} />}
          <span className="text-sm font-medium">Benchmark</span>
          <span className="text-[11px] text-ink-faint">compare seats on scripted cases</span>
          <span className="ml-auto text-xs text-ink-faint">{open ? 'Hide' : 'Show'}</span>
        </button>
      </h3>

      {open && (
        <div className="px-3 pb-3 space-y-3">
          <p className="text-xs text-ink-faint">
            Runs identical scripted cases through the seats you pick and compares stage
            timings side by side. Synthetic on purpose: minimal calls outside any chat,
            so results compare seats rather than predict a live turn. No microphone,
            no playback - generated audio is saved for you to listen to yourself, and
            each run can be deleted below when you&apos;re done with it.
          </p>

          {error && (
            <div role="alert" className="flex items-center gap-1.5 text-sm text-red-400">
              <AlertTriangle size={14} /> {error}
            </div>
          )}
          {cat === null && !error && <p className="text-sm text-ink-mid">Loading…</p>}

          {cat && sel && (
            <div className="grid grid-cols-1 sm:grid-cols-3 gap-3">
              <fieldset className="space-y-1.5">
                <legend className="text-xs text-ink-mid mb-1">Models</legend>
                {cat.seats.map((s) => (
                  <CheckRow key={s.slug}
                    checked={sel.models.includes(s.slug)}
                    onChange={() => setSel({ ...sel, models: toggle(sel.models, s.slug) })}
                    label={s.name}
                    note={`${s.model}${s.voice_id ? '' : ' · no voice'}`} />
                ))}
                {!cat.seats.length && (
                  <p className="text-[11px] text-ink-faint">No enabled seats to test.</p>
                )}
              </fieldset>
              <fieldset className="space-y-1.5">
                <legend className="text-xs text-ink-mid mb-1">Dimensions</legend>
                {cat.dimensions.map((d) => (
                  <CheckRow key={d.id}
                    checked={sel.dimensions.includes(d.id)}
                    onChange={() => setSel({ ...sel, dimensions: toggle(sel.dimensions, d.id) })}
                    label={d.label}
                    note={d.id !== 'text' && !cat.eleven ? 'needs ElevenLabs' : ''} />
                ))}
              </fieldset>
              {sel.dimensions.includes('text') && (
                <fieldset className="space-y-1.5">
                  <legend className="text-xs text-ink-mid mb-1">Cases</legend>
                  {cat.cases.map((c) => (
                    <CheckRow key={c.id}
                      checked={sel.cases.includes(c.id)}
                      onChange={() => setSel({ ...sel, cases: toggle(sel.cases, c.id) })}
                      label={c.label}
                      note={c.prompt} />
                  ))}
                </fieldset>
              )}
            </div>
          )}

          {cat && sel && (
            <div className="flex items-center gap-2.5">
              <button
                className="border border-edge2 rounded-lg px-3 py-1.5 text-sm hover:border-edge3 disabled:opacity-50 disabled:cursor-not-allowed"
                disabled={!!problems.length || starting || running}
                title={problems.join('; ')}
                onClick={start}
              >
                {running ? 'Running…' : starting ? 'Starting…' : 'Run benchmark'}
              </button>
              <span className="text-[11px] text-ink-faint">
                {problems.length ? problems.join('; ')
                  : 'runs one call at a time, so a big selection takes a while'}
              </span>
            </div>
          )}

          {view && (
            <div className="border-t border-edge pt-2.5 space-y-2.5 text-xs">
              <div className="flex items-baseline gap-2 flex-wrap">
                <span className="font-medium text-sm">{view.run_id}</span>
                <span className="text-ink-faint">{(view.created_at || '').replace('T', ' ')}</span>
                {progress && <span className={view.state === 'failed' ? 'text-red-400' : 'text-amber-500'}>{progress}</span>}
              </div>

              {stt && (
                <div>
                  <div className="text-ink-mid font-medium">Speech-to-text (one spoken fixture, shared)</div>
                  <div className="mt-0.5">
                    {stt.unit.status === 'ok' ? (
                      <span>
                        {fmtSeconds(stt.unit.seconds)}
                        {stt.verdict && <span className="ml-1.5">{stt.verdict} transcript {stt.verdict === '✓' ? 'matches' : 'differs from'} the fixture sentence</span>}
                        <span className="block text-ink-faint">heard: “{stt.transcript}”</span>
                      </span>
                    ) : <Badge unit={stt.unit} />}
                  </div>
                </div>
              )}

              {texts.length > 0 && (
                <div className="overflow-x-auto">
                  <div className="text-ink-mid font-medium">Text replies</div>
                  <table className="mt-1 w-full text-left">
                    <thead>
                      <tr className="text-[11px] text-ink-faint">
                        <th className={cell}>model</th><th className={cell}>case</th>
                        <th className={cell}>first word</th><th className={cell}>total</th>
                        <th className={cell}>reply</th>
                      </tr>
                    </thead>
                    <tbody>
                      {texts.map((r) => (
                        <tr key={r.key} className="border-t border-edge">
                          <td className={cell}>{r.name}</td>
                          <td className={cell}>{r.caseLabel}</td>
                          <td className={`${cell} tabular-nums`}>{fmtSeconds(r.unit.first_word_s)}</td>
                          <td className={`${cell} tabular-nums`}>{fmtSeconds(r.unit.seconds)}</td>
                          <td className={cell}>
                            {r.unit.status === 'ok'
                              ? <span>{r.verdict && <span className="mr-1">{r.verdict}</span>}<span className="text-ink-faint">{r.unit.reply}</span></span>
                              : <Badge unit={r.unit} />}
                          </td>
                        </tr>
                      ))}
                    </tbody>
                  </table>
                </div>
              )}

              {ttss.length > 0 && (
                <div>
                  <div className="text-ink-mid font-medium">Text-to-speech (fixed sentence, each seat&apos;s voice)</div>
                  {ttss.map((r) => (
                    <div key={r.key} className="mt-0.5 flex items-baseline gap-2 flex-wrap">
                      <span className="min-w-24">{r.name}</span>
                      {r.unit.status === 'ok'
                        ? <>
                            <span className="tabular-nums">{fmtSeconds(r.unit.seconds)}</span>
                            <ArtefactLink runId={view.run_id} name={r.unit.artefact} />
                          </>
                        : <Badge unit={r.unit} />}
                    </div>
                  ))}
                </div>
              )}

              {pipes.length > 0 && (
                <div>
                  <div className="text-ink-mid font-medium">Full pipeline (listen, think, speak)</div>
                  {pipes.map((r) => (
                    <div key={r.key} className="mt-0.5 flex items-baseline gap-2 flex-wrap">
                      <span className="min-w-24">{r.name}</span>
                      {r.unit.status === 'ok'
                        ? <>
                            <span className="tabular-nums">{fmtSeconds(r.unit.total_s)}</span>
                            <span className="text-ink-faint">{r.stageText}</span>
                            <ArtefactLink runId={view.run_id} name={r.unit.artefact} />
                          </>
                        : <Badge unit={r.unit} />}
                    </div>
                  ))}
                </div>
              )}

              <p className="text-[11px] text-ink-faint">
                Everything above is saved under data/benchmarks/runs/{view.run_id}/ -
                timings, configuration and audio - labelled synthetic, with no key
                values in any file.
              </p>
            </div>
          )}

          {runs.length > 0 && (
            <div className="border-t border-edge pt-2.5">
              <div className="text-xs text-ink-mid font-medium">Past runs</div>
              {runs.map((r) => (
                <div key={r.run_id} className="mt-1 flex items-center gap-2 text-[11px]">
                  <button className="text-link hover:underline" onClick={() => show(r.run_id)}>
                    {r.run_id}
                  </button>
                  <span className="text-ink-faint truncate">{historyLine(r)}</span>
                  <button
                    className="ml-auto text-ink-faint hover:text-red-400"
                    title="Delete this run and its audio"
                    aria-label={`Delete ${r.run_id}`}
                    onClick={() => removeRun(r.run_id)}
                  >
                    <Trash2 size={12} />
                  </button>
                </div>
              ))}
            </div>
          )}
        </div>
      )}
    </section>
  )
}
