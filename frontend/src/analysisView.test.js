// The Analysis page's pure meaning (#407): every backend run state has a
// badge and an unknown one never passes for finished, the running chip and
// the status ping, what holds a Run button back, the question before a paid
// run (and none before a free practice run), and the history's order and
// lines. Run: node --test frontend/src/analysisView.test.js
import test from 'node:test'
import assert from 'node:assert/strict'
import { readFileSync } from 'node:fs'

import {
  RUN_STATES, chipText, confirmText, elapsedLabel, historyDetail, historyTitle,
  latestFor, measuring, newestFirst, pingText, readmeUrl, reportJsonUrl,
  runHold, shouldPoll, spentLabel, stateBadge, whenLabel,
} from './analysisView.js'

const fixture = JSON.parse(readFileSync(
  new URL('../../tests/fixtures/backend_contract.json', import.meta.url), 'utf8'))

const CRITIC = {
  id: 'critic', title: 'Critic eval', costs: 'About 2 cents.',
  touches: 'Made-up drafts, sent to Anthropic.', blocked: '', running: null,
  readme: 'eval_critic/README.md',
}

test('the page knows exactly the run states the backend writes', () => {
  assert.deepEqual(RUN_STATES, fixture.analysis.run_states)
  for (const state of RUN_STATES) {
    assert.doesNotMatch(stateBadge({ state }).label, /Unknown/, state)
  }
})

test('an unknown state says so and never reads as done', () => {
  assert.deepEqual(stateBadge({ state: 'paused' }), { label: 'Unknown (paused)', tone: 'muted' })
  assert.equal(stateBadge(null).label, 'Unknown')
  assert.equal(stateBadge({ state: 'done' }).tone, 'ok')
  assert.equal(stateBadge({ state: 'timed out' }).tone, 'bad')
})

test('elapsed time reads in whole minutes', () => {
  assert.equal(elapsedLabel(12), 'under a minute')
  assert.equal(elapsedLabel(61), '1 min')
  assert.equal(elapsedLabel(600), '10 min')
  assert.equal(elapsedLabel(-1), '')
  assert.equal(elapsedLabel(undefined), '')
})

test('the chip says what runs and for how long, and nothing once it ends', () => {
  const run = { state: 'running', created_at_unix: 1000 }
  assert.equal(chipText(run, 1130), 'Running, 2 min so far')
  assert.equal(chipText({ ...run, practice: true }, 1010), 'Practice run going, under a minute so far')
  assert.equal(chipText({ ...run, state: 'done' }, 1130), '')
  assert.equal(chipText(null, 1), '')
})

test('the ping shows the server\'s word only once it has checked in', () => {
  const run = { state: 'running', created_at_unix: 1000, status_label: '', status_at: null }
  assert.equal(pingText(run, 1010), '')
  const pinged = { ...run, status_label: 'Still running', status_at: 1050 }
  assert.equal(pingText(pinged, 1052), 'Still running, checked just now.')
  assert.equal(pingText(pinged, 1080), 'Still running, checked 30 s ago.')
  assert.equal(pingText({ ...pinged, state: 'stopped' }, 1080), '')
})

test('a Run button is held back by a run, a block or a start in flight', () => {
  assert.equal(runHold(CRITIC), '')
  assert.equal(runHold({ ...CRITIC, running: { run_id: 'x' } }), 'Running now.')
  assert.equal(runHold({ ...CRITIC, blocked: 'A voice chat is live.' }), 'A voice chat is live.')
  assert.equal(runHold(CRITIC, true), 'Starting…')
  assert.equal(runHold(null), 'loading')
})

test('a paid run asks first, naming its cost and what it touches', () => {
  const q = confirmText(CRITIC)
  assert.match(q, /^Run the critic eval now\?/)
  assert.match(q, /What it costs: About 2 cents\./)
  assert.match(q, /What it touches: Made-up drafts/)
  assert.equal(confirmText(CRITIC, true), '')  // practice: free, made up
})

test('history is newest first and a card shows its newest settled run', () => {
  const runs = [
    { run_id: 'a', measurement: 'critic', state: 'done', created_at_unix: 1 },
    { run_id: 'b', measurement: 'critic', state: 'running', created_at_unix: 3 },
    { run_id: 'c', measurement: 'critic', state: 'failed', created_at_unix: 2 },
    { run_id: 'd', measurement: 'voice', state: 'done', created_at_unix: 4 },
  ]
  assert.deepEqual(newestFirst(runs).map((r) => r.run_id), ['d', 'b', 'c', 'a'])
  assert.equal(latestFor(runs, 'critic').run_id, 'c')
  assert.equal(latestFor(runs, 'recall'), null)
  assert.deepEqual(newestFirst(undefined), [])
  assert.equal(runs[0].run_id, 'a')  // not sorted in place
})

test('money reads plainly, and nothing when a report states none', () => {
  assert.equal(spentLabel(0.1653), '$0.17')
  assert.equal(spentLabel(1.04), '$1.04')
  assert.equal(spentLabel(0.0004), 'under a cent')
  assert.equal(spentLabel(null), '')
})

test('when a run started, relative for a day then a date', () => {
  const now = 1_790_000_000
  assert.equal(whenLabel(now - 20, now), 'just now')
  assert.equal(whenLabel(now - 300, now), '5 min ago')
  assert.equal(whenLabel(now - 7200, now), '2 h ago')
  assert.match(whenLabel(now - 3 * 86400, now), /^\d{1,2} [A-Z][a-z]{2}$/)
  assert.equal(whenLabel(null, now), '')
})

test('a history line names the run and says how it ended', () => {
  const run = { title: 'Recall replay', measurement: 'recall', state: 'done',
                summary: { headline: 'Replayed 200 turns.' } }
  assert.equal(historyTitle(run), 'Recall replay · Done')
  assert.equal(historyTitle({ ...run, practice: true }), 'Recall replay (practice) · Done')
  assert.equal(historyDetail(run), 'Replayed 200 turns.')
  const failed = { ...run, state: 'failed', summary: undefined,
                   error: 'Traceback\nSystemExit: membro is not answering' }
  assert.equal(historyDetail(failed), 'SystemExit: membro is not answering')
  assert.equal(historyDetail({ state: 'stopped' }), '')
})

test('links are built from ids, escaped', () => {
  assert.equal(reportJsonUrl('recall-20260927-120000'),
    '/api/analysis/runs/recall-20260927-120000/report.json')
  assert.equal(reportJsonUrl('a/b'), '/api/analysis/runs/a%2Fb/report.json')
  assert.equal(readmeUrl(CRITIC),
    'https://github.com/shawn-durrani/crossband/blob/main/eval_critic/README.md')
  assert.equal(readmeUrl({}), '')
})

test('the page polls only while something runs, and the sidebar dot agrees', () => {
  assert.equal(shouldPoll(null), false)
  assert.equal(shouldPoll({ measurements: [CRITIC], runs: [] }), false)
  assert.equal(shouldPoll({ measurements: [{ ...CRITIC, running: {} }], runs: [] }), true)
  assert.equal(shouldPoll({ measurements: [], runs: [{ state: 'running' }] }), true)
  assert.equal(measuring(['recall']), true)
  assert.equal(measuring([]), false)
  assert.equal(measuring(undefined), false)
})
