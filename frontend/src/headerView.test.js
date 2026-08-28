// Tests for the chat header's derivations (extracted from App.jsx).
// Run: node --test frontend/src/headerView.test.js
import { test } from 'node:test'
import assert from 'node:assert/strict'
import { contextGauge, usageSummary, fmtTokens, capabilityRows, CONTEXT_BUDGET } from './headerView.js'

const ctx = (total, over = {}) => ({
  total, history: total, research: 0, chat_summary: 0, memory: 0, overhead: 0, ...over,
})

test('one budget, named once', () => {
  // It was written as a bare 150000 in two places. A gauge whose scale is a
  // magic number in two files is a gauge that eventually disagrees with itself.
  assert.equal(CONTEXT_BUDGET, 150000)
  assert.equal(contextGauge(ctx(CONTEXT_BUDGET / 2)).pct, 50)
})

test('the whole gauge comes from one function, so it cannot contradict itself', () => {
  // The bug this replaces: the ring turned red at 85% and the composer bar at
  // 90%, off the same number. Both surfaces now read the same `level`, and each
  // picks its own presentation from it.
  const g = contextGauge(ctx(0.87 * CONTEXT_BUDGET))
  assert.equal(g.level, 'critical')
  assert.equal(g.tone, '#ef4444')          // the ring's inline colour
  assert.equal(g.cssTone, 'var(--t-err)')  // the bar's CSS variable
})

test('the bands escalate in the right order', () => {
  const at = (frac) => contextGauge(ctx(frac * CONTEXT_BUDGET)).level
  assert.equal(at(0.10), 'comfortable')
  assert.equal(at(0.40), 'elevated')
  assert.equal(at(0.70), 'high')
  assert.equal(at(0.90), 'critical')
})

test('band boundaries are inclusive at the lower edge', () => {
  const at = (pct) => contextGauge(ctx((pct / 100) * CONTEXT_BUDGET)).level
  assert.equal(at(32), 'elevated')
  assert.equal(at(31.9), 'comfortable')
  assert.equal(at(85), 'critical')
  assert.equal(at(84.9), 'high')
})

test('the gauge never reads past full', () => {
  // Overrunning the budget is allowed - nothing refuses to send - but a ring
  // drawn at 210% is not a ring.
  const g = contextGauge(ctx(3 * CONTEXT_BUDGET))
  assert.equal(g.pct, 100)
  assert.equal(g.level, 'critical')
})

test('no context reported yields no gauge, not a zeroed one', () => {
  // An empty or still-loading chat has nothing to say; a confident 0% would be
  // a claim we cannot make.
  assert.equal(contextGauge(null), null)
  assert.equal(contextGauge(undefined), null)
  assert.equal(contextGauge({}), null)
})

test('the tooltip states the consequence, not just the size', () => {
  const g = contextGauge(ctx(60000, { research: 5000, memory: 2000, chat_summary: 1000, overhead: 4000 }))
  assert.match(g.title, /What every reply re-reads/)
  assert.match(g.title, /answers get vaguer/)      // why it matters
  assert.match(g.title, /summaries & memory ~3k/)  // memory + chat_summary, combined
  assert.match(g.title, /research logs ~5k/)
})

test('a partial context breakdown does not produce NaN in the tooltip', () => {
  const g = contextGauge({ total: 1000 })
  assert.doesNotMatch(g.title, /NaN|undefined/)
})

test('token formatting rounds to whole thousands for the gauge', () => {
  assert.equal(fmtTokens(999), '999')
  assert.equal(fmtTokens(1000), '1k')
  assert.equal(fmtTokens(45300), '45k')
})

test('usage summary is absent when there is nothing to summarise', () => {
  assert.equal(usageSummary({ tokens: 0 }, null), null)
  assert.equal(usageSummary(null, null), null)
})

test('usage summary keeps one decimal, unlike the gauge', () => {
  // A running total is a measurement and reads exact; the gauge is an
  // indicator and reads round. Deliberately different.
  assert.equal(usageSummary({ tokens: 34700 }, null).tokens, 'Σ 34.7k tok')
  assert.equal(usageSummary({ tokens: 42 }, null).tokens, 'Σ 42 tok')
})

test('voice rides along only when there is voice, and never re-states its cost', () => {
  assert.equal(usageSummary({ tokens: 100 }, { tts_chars: 0 }).voice, '')
  const v = usageSummary({ tokens: 100 }, { tts_chars: 800, stt_seconds: 12.4, cost: 0.085 })
  // Chars and seconds are usage and appear nowhere else. The dollar figure
  // used to ride here because the header's total counted messages alone; the
  // total now comes from the server and already contains voice, so printing
  // it again would state the same money twice.
  assert.equal(v.voice, ' · voice 800 ch / 12s')
  assert.doesNotMatch(v.voice, /\$/)
})

test('a chat with only voice still reports', () => {
  // Voice-only usage is real usage; suppressing it because no tokens moved
  // would hide spend.
  const s = usageSummary({ tokens: 0 }, { tts_chars: 1200 })
  assert.equal(s.tokens, 'Σ 0 tok')
  assert.match(s.voice, /1\.2k ch/)
})

test('running totals grow into an M tier', () => {
  // A multi-million-token total once rendered as "Σ 12400.0k tok", a number
  // formatted to be misread.
  assert.equal(usageSummary({ tokens: 12400000 }, null).tokens, 'Σ 12.4M tok')
  assert.equal(usageSummary({ tokens: 999999 }, null).tokens, 'Σ 1000.0k tok')
  assert.equal(usageSummary({ tokens: 34700 }, null).tokens, 'Σ 34.7k tok')
})

test('capabilityRows: state from the chat, availability from the machine', () => {
  const chat = { voice_mode: 1, memory_enabled: 0, web_enabled: 1, code_enabled: 1 }
  const cfg = { search: { tavily: true }, code: { available: true, writes: false } }
  const rows = capabilityRows(chat, cfg)
  assert.deepEqual(rows.map((r) => [r.key, r.on]),
    [['voice', true], ['memory', false], ['web', true], ['code', true]])
  // every row keeps a plain-English explainer - collapsing chrome must not
  // lose the explanations
  assert.ok(rows.every((r) => r.hint.length > 10))
  assert.match(rows[2].hint, /Tavily/)
  assert.match(rows[3].hint, /read-only/)
})

test('capabilityRows: code is absent when the machine offers neither repo path', () => {
  const rows = capabilityRows({ voice_mode: 0 }, {})
  assert.deepEqual(rows.map((r) => r.key), ['voice', 'memory', 'web'])
  assert.deepEqual(capabilityRows(null, {}), [])
})

// ---- the gauge must show attachments and upload, not just text -----------

test('gauge surfaces attachment weight and the per-turn upload', () => {
  // the shape a chat takes when its weight is images rather than text
  const g = contextGauge({
    history: 1200, attachments: 16000, research: 200, chat_summary: 0,
    memory: 2000, overhead: 3600, total: 23000,
    upload_bytes: 24_000_000, image_count: 6,
  })
  assert.ok(g.title.includes('attachments'), 'breakdown must name attachments')
  assert.ok(g.title.includes('6 images'), 'and say how many')
  assert.equal(g.uploadMb, 24)
  assert.ok(/re-uploaded EVERY turn/i.test(g.title),
    'the megabytes are what actually hurt - they must be stated, not implied')
})

test('gauge stays quiet about upload when there is nothing to report', () => {
  const g = contextGauge({
    history: 900, attachments: 0, research: 0, chat_summary: 0,
    memory: 0, overhead: 3600, total: 4500, upload_bytes: 0, image_count: 0,
  })
  assert.equal(g.uploadMb, 0)
  assert.ok(!/re-uploaded/i.test(g.title), 'no noise on a light chat')
  assert.ok(!g.title.includes('attachments'))
})

test('a photo-heavy chat no longer reads as green', () => {
  const light = contextGauge({ history: 1200, attachments: 0, total: 4600, upload_bytes: 0 })
  const heavy = contextGauge({ history: 1200, attachments: 16000, total: 23000,
    upload_bytes: 24_000_000, image_count: 6 })
  assert.ok(heavy.pct > light.pct * 3,
    'the whole bug: this chat used to look identical to an empty one')
})
