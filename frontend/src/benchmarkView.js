// Pure meaning for the Models-page benchmark panel (#94): selection rules,
// result shaping, formatting. The backend's results.json is the contract
// (backend/benchmark.py); everything here just reads it. No fetches, no DOM -
// BenchmarkPanel.jsx is markup and wiring only.

export function defaultSelection(catalogue) {
  return {
    models: [],
    cases: (catalogue?.cases || []).map((c) => c.id),
    dimensions: ['text'],
  }
}

export function toggle(list, id) {
  return list.includes(id) ? list.filter((x) => x !== id) : [...list, id]
}

// Why the Run button is disabled, in the user's own terms. Mirrors the
// backend's build_plan refusals so a disabled button and a 400 always agree.
export function selectionProblems(sel, catalogue) {
  const out = []
  if (!sel.models.length) out.push('pick at least one model')
  if (!sel.dimensions.length) out.push('pick at least one test dimension')
  if (sel.dimensions.includes('text') && !sel.cases.length)
    out.push('the text dimension needs at least one case')
  if (sel.dimensions.some((d) => d !== 'text') && catalogue && !catalogue.eleven)
    out.push('the voice dimensions need ElevenLabs configured')
  return out
}

export function runRequest(sel) {
  return { models: sel.models, cases: sel.cases, dimensions: sel.dimensions }
}

export function fmtSeconds(s) {
  if (s == null) return ''
  return s < 10 ? `${s.toFixed(2)} s` : `${s.toFixed(1)} s`
}

// One unit's state as a badge: ok is quiet, skipped says why, failed speaks
// the provider's words. `pending` exists only client-side, for units the
// sequential runner hasn't reached yet.
export function unitBadge(unit) {
  if (!unit || unit.status === 'pending') return { tone: 'muted', label: 'waiting…' }
  if (unit.status === 'unsupported') return { tone: 'muted', label: `skipped - ${unit.reason}` }
  if (unit.status === 'failed') {
    const at = unit.failed_stage ? ` at ${unit.failed_stage}` : ''
    return { tone: 'bad', label: `failed${at} - ${unit.error}` }
  }
  return { tone: 'ok', label: '' }
}

// ✓ / ✗ / blank for a machine-checkable expectation; blank means "a human
// judges the retained reply", never "passed".
export function verdictMark(matches) {
  if (matches === true) return '✓'
  if (matches === false) return '✗'
  return ''
}

export function textRows(results) {
  const rows = []
  if (!results?.config?.dimensions?.includes('text')) return rows
  for (const m of results.models || []) {
    for (const caseId of results.config.cases || []) {
      const unit = results.text?.[m.slug]?.[caseId] || { status: 'pending' }
      rows.push({
        key: `${m.slug}:${caseId}`,
        name: m.name,
        caseLabel: results.case_details?.[caseId]?.label || caseId,
        unit,
        verdict: verdictMark(unit.matches_expected),
      })
    }
  }
  return rows
}

export function ttsRows(results) {
  if (!results?.config?.dimensions?.includes('tts')) return []
  return (results.models || []).map((m) => ({
    key: m.slug,
    name: m.name,
    unit: results.tts?.[m.slug] || { status: 'pending' },
  }))
}

// "listen 0.42 s · think 1.20 s (first word 0.31 s) · speak 0.55 s"
export function stageLine(stages) {
  const bits = []
  if (stages?.stt) bits.push(`listen ${fmtSeconds(stages.stt.seconds)}`)
  if (stages?.model) {
    const first = stages.model.first_word_s != null
      ? ` (first word ${fmtSeconds(stages.model.first_word_s)})` : ''
    bits.push(`think ${fmtSeconds(stages.model.seconds)}${first}`)
  }
  if (stages?.tts) bits.push(`speak ${fmtSeconds(stages.tts.seconds)}`)
  return bits.join(' · ')
}

export function pipelineRows(results) {
  if (!results?.config?.dimensions?.includes('pipeline')) return []
  return (results.models || []).map((m) => {
    const unit = results.pipeline?.[m.slug] || { status: 'pending' }
    return { key: m.slug, name: m.name, unit, stageText: stageLine(unit.stages) }
  })
}

export function sttSummary(results) {
  if (!results?.config?.dimensions?.includes('stt')) return null
  const unit = results.stt || { status: 'pending' }
  return {
    unit,
    verdict: verdictMark(unit.matches_fixture),
    transcript: unit.transcript || '',
  }
}

export function artefactUrl(runId, name) {
  return `/api/benchmark/runs/${encodeURIComponent(runId)}/artefacts/${encodeURIComponent(name)}`
}

// The line above the results while they're not simply "done".
export function progressLine(run) {
  if (!run) return ''
  if (run.state === 'running') {
    const p = run.progress || {}
    return `running - ${p.done ?? 0} of ${p.total ?? '?'} done`
  }
  if (run.state === 'interrupted')
    return 'interrupted - the server restarted mid-run; what was measured is kept below'
  if (run.state === 'failed') return `failed - ${run.error || 'unknown error'}`
  return ''
}

export function historyLine(row) {
  const when = (row.created_at || '').replace('T', ' ')
  const models = row.models?.length === 1 ? '1 model' : `${row.models?.length || 0} models`
  return `${when} · ${row.state} · ${models} · ${(row.dimensions || []).join(' + ')}`
}
