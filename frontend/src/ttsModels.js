// The voice model picker (#480): which ElevenLabs model speaks replies.
//
// The backend owns every rule (backend/tts_models.py): which models are
// offered, what Automatic picks today, which ids are valid. GET
// /api/voice/models hands this module the result, and it only turns that
// into select rows and plain sentences. Nothing here decides a model, so
// the browser and the relay can never disagree about what will speak.
//
// Pure, like rateCards.js: the component renders what this returns.

export const AUTO = 'auto'

function optionFor(data, id) {
  return (data?.options || []).find((o) => o.value === id) || null
}

function nameOf(data, id) {
  return optionFor(data, id)?.label || id
}

function joinNames(names) {
  if (names.length < 2) return names.join('')
  return `${names.slice(0, -1).join(', ')} and ${names[names.length - 1]}`
}

// "Eleven Flash v2.5 · fastest to start, 32 languages, half price"
export function optionText(o) {
  return o.note ? `${o.label} · ${o.note}` : o.label
}

export function automaticLabel(data) {
  const now = data?.automatic_pick_name || data?.automatic_pick
  return now ? `Automatic: newest supported (now ${now})`
    : 'Automatic: newest supported'
}

function modelRows(data) {
  return [
    { value: AUTO, label: automaticLabel(data) },
    ...(data.options || []).map((o) => ({ value: o.value, label: optionText(o) })),
  ]
}

// A saved value the list no longer offers still shows, greyed out, so the
// select never silently claims a different choice than the one on disk.
function withCurrent(rows, current) {
  if (!current || rows.some((r) => r.value === current)) return rows
  return [...rows, { value: current, label: `${current} (not offered now)`, disabled: true }]
}

// Rows for the app-wide select.
export function appChoiceRows(data) {
  if (!data) return []
  return withCurrent(modelRows(data), data.setting)
}

// What the app setting speaks with, in words: the seat select's first row
// and the per-seat note both say it.
export function appSettingName(data) {
  if (!data) return ''
  if (data.setting === AUTO) {
    return `Automatic, now ${data.automatic_pick_name || data.automatic_pick}`
  }
  return nameOf(data, data.setting)
}

// Rows for one seat's select. Blank follows the app setting.
export function seatChoiceRows(data, current) {
  if (!data) return []
  const rows = [{ value: '', label: `Same as the app setting (${appSettingName(data)})` },
                ...modelRows(data)]
  return withCurrent(rows, current)
}

// The line under a select: what the chosen value means for the next reply.
// A listed model needs no line, since its row already carries its note.
export function choiceNote(data, value) {
  if (!data) return ''
  if (value === '') return 'This seat speaks with whatever the app setting picks.'
  if (value === AUTO) return data.automatic_rule || ''
  const fallback = nameOf(data, data.default)
  const o = optionFor(data, value)
  if (!o) {
    return `ElevenLabs doesn't offer ${value} now, so replies use ${fallback} instead.`
  }
  if (o.refused) {
    return `ElevenLabs refused ${o.label} on live voice today, so replies use ${fallback} for now.`
  }
  return ''
}

// Where the list came from.
export function sourceNote(data) {
  if (!data) return ''
  return data.source === 'live'
    ? 'The list comes from your ElevenLabs account and refreshes every hour.'
    : "ElevenLabs couldn't be reached, so this is Crossband's built-in list."
}

// Models ElevenLabs turned away on the live socket in the last day.
export function refusedNote(data) {
  const names = (data?.refused || []).map((id) => nameOf(data, id))
  if (!names.length) return ''
  const them = names.length > 1 ? 'them' : 'it'
  return `ElevenLabs refused ${joinNames(names)} on live voice in the last day, `
    + `so Automatic skips ${them} for now.`
}

export function lockedNote(data) {
  return data?.locked_by_env
    ? 'CROSSBAND_TTS_MODEL is set in the environment, so the choice is made there. '
      + 'Remove it to choose here.'
    : ''
}
