// Room-mode roster + attribution-flag state (#28 phase 2): ALL the decision
// logic behind the "In the room" chip, the ask-fallback strip, the mismatch
// note on a turn, the tap-to-correct menu and the remembered-voices panel -
// in a pure module per the house rule. The JSX only renders what these
// return.
//
// Data shapes, from the backend:
// - roster row: {id, name, person_id, status, sufficient}  (present rows only
//   in the snapshot; `sufficient` is the anchor-store verdict joined in)
// - flag row:   {id, chat_id, message_id, kind, label, suspected,
//                resolved_at} - kind 'unknown_voice' | 'mismatch'
// - person:     {person_id, name, seconds, clip_count, sufficient}

const MAX_MENU_NAMES = 12
const MAX_PREFERRED_CHARS = 40 // matches the backend's roster/display bound

// The name a person is SHOWN under (#28 phase 3): their correctable
// preferred display name when one is set, otherwise the name they were
// introduced as. Works for roster rows (display_name, joined in by the
// backend) and remembered-voice people (preferred_name) alike. The plain
// `name` stays the identity key that voice labels are written with - only
// display surfaces call this.
export function displayName(p) {
  if (!p) return ''
  const preferred = typeof p.display_name === 'string' && p.display_name
    ? p.display_name
    : (typeof p.preferred_name === 'string' ? p.preferred_name : '')
  if (preferred) return preferred
  return typeof p.name === 'string' ? p.name : ''
}

// Validation for an edited preferred name: trimmed, bounded, must contain a
// letter. Returns the cleaned name, or '' when there is nothing sendable -
// the caller disables Save on ''. One rule, shared by every rename surface.
export function cleanPreferredName(raw) {
  if (typeof raw !== 'string') return ''
  const name = raw.trim().slice(0, MAX_PREFERRED_CHARS).trim()
  return /[a-zA-Z]/.test(name) ? name : ''
}

// The transparency cue: multi-voice processing is on, and THIS is who the
// app is trying to tell apart. Empty roster = no chip at all.
export function rosterChipText(roster) {
  const names = (roster || [])
    .filter((p) => p && p.status !== 'left' && displayName(p))
    .map((p) => displayName(p))
  if (!names.length) return ''
  return `In the room: ${names.join(', ')}`
}

// Hover/long-press detail for the chip: what the mode costs, plus honesty
// about whose voice is still being learned.
export function rosterTitle(roster) {
  const learning = (roster || [])
    .filter((p) => p && p.status !== 'left' && !p.sufficient)
    .map((p) => displayName(p) || p.name)
  const base =
    'Room mode is on: turns are attributed by voice, and audio is '
    + 'transcribed twice while it is on (roughly double voice spend). '
    + 'Say "X has left" to remove someone.'
  if (!learning.length) return base
  return `${base} Still learning: ${learning.join(', ')} - their turns stay `
    + 'uncertain until enough of their voice has been heard.'
}

// The newest OPEN "someone new is speaking" ask, or null. One at a time by
// backend design; defensive here anyway.
export function askFlag(flags) {
  const open = (flags || []).filter(
    (f) => f && f.kind === 'unknown_voice' && !f.resolved_at)
  return open.length ? open[open.length - 1] : null
}

// Open mismatch flags keyed by the turn they doubt, for per-message render.
export function mismatchByMessage(flags) {
  const out = {}
  for (const f of flags || []) {
    if (f && f.kind === 'mismatch' && !f.resolved_at && f.message_id != null) {
      out[f.message_id] = f
    }
  }
  return out
}

// Plain-English copy for a flag. One place, so the wording is consistent
// across desktop and the mobile call screen.
export function flagCopy(flag) {
  if (!flag) return ''
  if (flag.kind === 'unknown_voice') {
    return 'Someone new is speaking - who? Say or type their name '
      + '(e.g. "that\'s Dave") and their turns will be named.'
  }
  if (flag.kind === 'mismatch') {
    const who = flag.suspected
      ? `reads more like ${flag.suspected}`
      : 'reads like someone else'
    return `This turn is labelled ${flag.label || 'a speaker'} but ${who}. `
      + 'The label has NOT been changed - tap the name on the turn to correct it.'
  }
  return ''
}

// Names offered by the tap-to-correct menu: everyone present in the room
// plus every remembered voice, minus the labels the turn already carries.
// Order: roster first (most likely correction), then remembered strangers.
export function reassignOptions(roster, people, currentLabels) {
  const taken = new Set((currentLabels || []).map((l) => String(l).toLowerCase()))
  const out = []
  const push = (name) => {
    if (typeof name !== 'string' || !name) return
    const key = name.toLowerCase()
    if (taken.has(key) || out.some((n) => n.toLowerCase() === key)) return
    if (out.length < MAX_MENU_NAMES) out.push(name)
  }
  for (const p of roster || []) {
    if (p && p.status !== 'left') push(p.name)
  }
  for (const p of people || []) push(p && p.name)
  return out
}

// The remembered-voices panel's per-person line: honest about what is stored
// and whether it is enough to identify them.
export function personSummary(person, sufficientSeconds) {
  if (!person) return null
  const secs = Number(person.seconds) || 0
  const status = person.sufficient
    ? 'voice remembered - identified automatically when they speak'
    : `still learning - ${secs.toFixed(1)}s of ${Number(sufficientSeconds) || 6}s `
      + 'of clear speech stored; turns stay uncertain until then'
  const shown = displayName(person) || person.name
  return {
    name: shown,
    // Honesty when a preferred spelling differs from the introduced name:
    // the label chips keep writing the introduced name, so say so.
    alias: shown !== person.name ? `heard as "${person.name}"` : '',
    detail: `${person.clip_count || 0} clip${person.clip_count === 1 ? '' : 's'}, `
      + `${secs.toFixed(1)}s`,
    status,
  }
}

// What forgetting means, stated before the click - deletion copy lives with
// the logic so every surface says the same true thing.
export const FORGET_EXPLAINER =
  'Forget deletes this person\'s stored voice audio from this computer. '
  + 'They can be re-learned only by being introduced and heard again.'
