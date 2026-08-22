// The benchmark panel's pure meaning (#94): selection rules that mirror the
// backend's refusals, result shaping for the three tables, badge/verdict
// honesty (a blank verdict means "a human judges", never "passed"), and the
// formatting helpers. The panel component stays markup-only because
// everything decidable is decided here.
import test from 'node:test'
import assert from 'node:assert/strict'

import {
  artefactUrl, defaultSelection, fmtSeconds, historyLine, pipelineRows,
  progressLine, runRequest, selectionProblems, stageLine, sttSummary,
  textRows, toggle, ttsRows, unitBadge, verdictMark,
} from './benchmarkView.js'

const CATALOGUE = {
  eleven: true,
  cases: [{ id: 'echo' }, { id: 'arithmetic' }],
  seats: [],
}

test('default selection: every case, text only, no models presumed', () => {
  const sel = defaultSelection(CATALOGUE)
  assert.deepEqual(sel, { models: [], cases: ['echo', 'arithmetic'], dimensions: ['text'] })
  assert.deepEqual(defaultSelection(null).cases, [])
})

test('toggle adds and removes without mutating', () => {
  const a = ['x']
  assert.deepEqual(toggle(a, 'y'), ['x', 'y'])
  assert.deepEqual(toggle(a, 'x'), [])
  assert.deepEqual(a, ['x'])
})

test('selection problems mirror the backend refusals', () => {
  const ok = { models: ['a'], cases: ['echo'], dimensions: ['text'] }
  assert.deepEqual(selectionProblems(ok, CATALOGUE), [])
  assert.match(selectionProblems({ ...ok, models: [] }, CATALOGUE)[0], /one model/)
  assert.match(selectionProblems({ ...ok, dimensions: [] }, CATALOGUE)[0], /dimension/)
  assert.match(selectionProblems({ ...ok, cases: [] }, CATALOGUE)[0], /at least one case/)
  const voiceless = { ...CATALOGUE, eleven: false }
  assert.match(selectionProblems({ ...ok, dimensions: ['tts'] }, voiceless)[0], /ElevenLabs/)
  // text-only never needs ElevenLabs; cases don't matter without text
  assert.deepEqual(selectionProblems(ok, voiceless), [])
  assert.deepEqual(selectionProblems({ ...ok, cases: [], dimensions: ['tts'] }, CATALOGUE), [])
})

test('runRequest carries exactly the three lists', () => {
  const sel = { models: ['a'], cases: ['echo'], dimensions: ['text'], junk: 1 }
  assert.deepEqual(runRequest(sel), { models: ['a'], cases: ['echo'], dimensions: ['text'] })
})

test('seconds format is stable and never crashes on nulls', () => {
  assert.equal(fmtSeconds(0.316), '0.32 s')
  assert.equal(fmtSeconds(12.34), '12.3 s')
  assert.equal(fmtSeconds(null), '')
})

test('unit badges: quiet ok, reasoned skip, spoken failure, waiting', () => {
  assert.deepEqual(unitBadge({ status: 'ok' }), { tone: 'ok', label: '' })
  assert.equal(unitBadge({ status: 'unsupported', reason: 'no voice configured' }).label,
    'skipped - no voice configured')
  assert.equal(unitBadge({ status: 'failed', error: 'boom', failed_stage: 'tts' }).label,
    'failed at tts - boom')
  assert.equal(unitBadge(undefined).label, 'waiting…')
})

test('verdicts: only a machine-checked expectation gets a mark', () => {
  assert.equal(verdictMark(true), '✓')
  assert.equal(verdictMark(false), '✗')
  assert.equal(verdictMark(null), '')
})

const RESULTS = {
  run_id: 'bench-20260821-120000',
  models: [{ slug: 'a', name: 'Alpha' }, { slug: 'b', name: 'Beta' }],
  config: { dimensions: ['text', 'stt', 'tts', 'pipeline'], cases: ['echo'] },
  case_details: { echo: { label: 'Instruction echo' } },
  text: { a: { echo: { status: 'ok', seconds: 0.5, matches_expected: true } } },
  stt: { status: 'ok', seconds: 0.4, transcript: 'please tell me', matches_fixture: true },
  tts: { a: { status: 'ok', seconds: 0.6, artefact: 'tts-a.mp3' } },
  pipeline: {
    a: {
      status: 'ok',
      stages: {
        stt: { seconds: 0.42 },
        model: { seconds: 1.2, first_word_s: 0.31 },
        tts: { seconds: 0.55 },
      },
    },
  },
}

test('text rows cover every model x case; unreached units wait', () => {
  const rows = textRows(RESULTS)
  assert.equal(rows.length, 2)
  assert.deepEqual(rows.map((r) => [r.name, r.unit.status, r.verdict]),
    [['Alpha', 'ok', '✓'], ['Beta', 'pending', '']])
  assert.equal(rows[0].caseLabel, 'Instruction echo')
  assert.deepEqual(textRows({ ...RESULTS, config: { dimensions: ['tts'], cases: [] } }), [])
})

test('voice rows: per-seat tts and pipeline, suite-level stt', () => {
  assert.deepEqual(ttsRows(RESULTS).map((r) => [r.name, r.unit.status]),
    [['Alpha', 'ok'], ['Beta', 'pending']])
  const pipes = pipelineRows(RESULTS)
  assert.equal(pipes[0].stageText,
    'listen 0.42 s · think 1.20 s (first word 0.31 s) · speak 0.55 s')
  assert.equal(pipes[1].stageText, '')
  const stt = sttSummary(RESULTS)
  assert.equal(stt.verdict, '✓')
  assert.equal(sttSummary({ ...RESULTS, config: { dimensions: ['text'], cases: [] } }), null)
})

test('stage line copes with a partial (failed mid-way) pipeline', () => {
  assert.equal(stageLine({ stt: { seconds: 0.4 } }), 'listen 0.40 s')
  assert.equal(stageLine(undefined), '')
})

test('artefact links stay inside the api and encode their parts', () => {
  assert.equal(artefactUrl('bench-1', 'tts a.mp3'),
    '/api/benchmark/runs/bench-1/artefacts/tts%20a.mp3')
})

test('progress line speaks each non-done state', () => {
  assert.equal(progressLine({ state: 'running', progress: { done: 3, total: 9 } }),
    'running - 3 of 9 done')
  assert.match(progressLine({ state: 'interrupted' }), /restarted mid-run/)
  assert.equal(progressLine({ state: 'failed', error: 'x' }), 'failed - x')
  assert.equal(progressLine({ state: 'done' }), '')
  assert.equal(progressLine(null), '')
})

test('history lines read as one glance', () => {
  assert.equal(
    historyLine({ created_at: '2026-08-21T12:00:00', state: 'done', models: ['a'], dimensions: ['text', 'tts'] }),
    '2026-08-21 12:00:00 · done · 1 model · text + tts')
})
