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

// Learning progress toward the sufficiency bar (#28 phase 4, second field
// test: a remembered-but-under-the-bar voice was indistinguishable from a
// failing one). Content-free numbers only - accepted seconds vs the target.
// Works for remembered-voice people ({seconds, sufficient}) and roster rows
// ({anchor_seconds, sufficient}) alike. Null when there is nothing to say.
//
// The bar is TWO-PART since #28 PR-B: the seconds target AND a minimum
// number of short (~1-2s) clips, so quick interjections become identifiable.
// When the snapshot carries the short-clip count (short_clips on people,
// anchor_short_clips on roster rows) and a minShortClips target, the
// progress says honestly which half is still missing; older callers without
// those fields keep the seconds-only reading.
export function sufficiencyProgress(person, sufficientSeconds, minShortClips) {
  if (!person) return null
  const target = Number(sufficientSeconds) > 0 ? Number(sufficientSeconds) : 6
  const raw = person.seconds ?? person.anchor_seconds
  const secs = Math.max(0, Number(raw) || 0)
  if (person.sufficient) {
    return { fraction: 1, done: true, label: 'voice remembered' }
  }
  const shownTarget = Number.isInteger(target) ? String(target) : target.toFixed(1)
  const secondsFrac = Math.min(1, secs / target)
  const secondsLabel = `${secs.toFixed(1)}s of ${shownTarget}s of clear speech heard`
  const shortsRaw = person.short_clips ?? person.anchor_short_clips
  const needShorts = Number(minShortClips) > 0 ? Number(minShortClips) : 0
  if (needShorts > 0 && typeof shortsRaw === 'number') {
    const shorts = Math.max(0, shortsRaw)
    const shortsFrac = Math.min(1, shorts / needShorts)
    if (secondsFrac >= 1 && shorts < needShorts) {
      const missing = needShorts - shorts
      return {
        fraction: Math.min(1, (secondsFrac + shortsFrac) / 2),
        done: false,
        label: `enough long speech heard - needs ${missing} more short `
          + `utterance${missing === 1 ? '' : 's'} (a word or two) so quick `
          + 'interjections can be recognised',
      }
    }
    return {
      fraction: Math.min(1, (secondsFrac + shortsFrac) / 2),
      done: false,
      label: secondsLabel,
    }
  }
  return {
    fraction: secondsFrac,
    done: false,
    label: secondsLabel,
  }
}

// ---- per-person voice chips for the dock (#28, dock refinement) ----------
//
// The dock's status tier used to be one unbounded run of prose ("matcher
// ready · ambient listening · local · 227ms · Alex 3.2s of 6s of clear
// speech heard, Sam ✓") that overflowed its tray the moment a second voice
// existed. It becomes one CHIP per person instead: a name, a state, and
// nothing else, so the row wraps instead of spilling. Every decision here -
// which state, what the chip says, what the collapsed one-liner says, the
// order - is derived in this module and merely rendered by the components.
//
// The three states are the three honest things the app can know about a
// person's voice:
// - confirmed: their bank clears the bar, so a turn of theirs CAN be named
//   on its own evidence - the per-turn label stays the live truth (#139).
//   Shown as a tick beside the name (the owner asked for a tick beside the
//   name rather than swapped label text).
// - learning:  some of their voice is banked but not enough yet, so their
//   turns lean on elimination and stay uncertain. Shows the progress.
// - pending:   nothing of their voice is banked at all.
export const CHIP_CONFIRMED = 'confirmed'
export const CHIP_LEARNING = 'learning'
export const CHIP_PENDING = 'pending'

// Order of the chips: confirmed, then learning, then pending, each group
// keeping its source order (roster order, then remembered order). It reads
// best-known-first, which is also what the collapsed mobile line needs,
// and a person's rank changes only when they cross a bar - not per turn.
const CHIP_RANK = { [CHIP_CONFIRMED]: 0, [CHIP_LEARNING]: 1, [CHIP_PENDING]: 2 }

// Under a second of banked speech is not progress worth a number; it reads
// as "learning 0s", which says less than the neutral pending state does.
const LEARNING_MIN_SECONDS = 1

// One person's chip, or null when there is no name to show. Works for
// roster rows (anchor_seconds/anchor_short_clips) and remembered-voice
// people (seconds/short_clips) alike, exactly like sufficiencyProgress.
export function voicePersonChip(person, sufficientSeconds, minShortClips) {
  if (!person || typeof person !== 'object') return null
  // Trimmed here rather than in displayName: a chip is a fixed-width badge,
  // and a whitespace-only name would render an empty one instead of nothing.
  const name = displayName(person).trim()
  if (!name) return null
  const key = (typeof person.person_id === 'string' && person.person_id)
    || name.toLowerCase()
  const secs = Math.max(0, Number(person.seconds ?? person.anchor_seconds) || 0)
  if (person.sufficient) {
    return {
      key, name, state: CHIP_CONFIRMED, label: name, short: `${name} ✓`,
      // #139: this tick is about the PROFILE, and must never read as a
      // verdict on the turn being spoken. The field failure: a guest's
      // tick sat green while their live turns showed "Identity pending",
      // and the tick read as "recognised right now".
      title: `${name}'s voice is remembered: there is enough approved audio `
        + 'to recognise them. The tick describes their profile, not the '
        + 'turn being spoken - each turn’s own label shows who the app '
        + 'actually heard, and a hard turn can still come through uncertain.',
    }
  }
  const prog = sufficiencyProgress(person, sufficientSeconds, minShortClips)
  if (secs >= LEARNING_MIN_SECONDS) {
    const shown = Math.round(secs)
    return {
      key, name, state: CHIP_LEARNING,
      label: `${name} · learning ${shown}s`, short: `${name} ${shown}s`,
      title: `${name}'s voice is still being learned - ${prog ? prog.label : ''}`
        + '. Their turns stay uncertain until then.',
    }
  }
  return {
    key, name, state: CHIP_PENDING, label: name, short: name,
    title: `None of ${name}'s voice has been learned yet, so their turns `
      + 'cannot be named by voice. A turn can still be theirs by elimination '
      + 'when nobody else is in the room, and every such turn teaches it.',
  }
}

// The chips this session should show, ordered and de-duplicated.
//
// Source, and why: the people in the ROOM when there is a roster - those
// are exactly who turns are being attributed between. With no roster (room
// mode off) it falls back to the remembered voices, which is honest for a
// different reason: with the mode off, every spoken turn is still checked
// against precisely that set, so a chip there means "this voice would be
// recognised", not "this person is here". Either way a chip only ever
// describes what the app can do with a voice.
export function voiceChips(roster, people, sufficientSeconds, minShortClips) {
  const present = (roster || []).filter((p) => p && p.status !== 'left')
  const source = present.length ? present : (people || [])
  const out = []
  const seen = new Set()
  for (const p of source) {
    const chip = voicePersonChip(p, sufficientSeconds, minShortClips)
    if (!chip || seen.has(chip.key)) continue
    seen.add(chip.key)
    out.push(chip)
  }
  // Array.prototype.sort is stable, so equal ranks keep their source order.
  return out.sort((a, b) => CHIP_RANK[a.state] - CHIP_RANK[b.state])
}

// Hover/long-press detail for the chip: what the mode costs, plus honesty
// about whose voice is still being learned - with each learner's progress
// toward the bar when the snapshot carries it, so patience is an informed
// choice rather than a guess.
export function rosterTitle(roster, sufficientSeconds, minShortClips) {
  const learning = (roster || [])
    .filter((p) => p && p.status !== 'left' && !p.sufficient)
    .map((p) => {
      const name = displayName(p) || p.name
      const secs = Number(p.anchor_seconds)
      if (Number.isFinite(secs) && secs > 0) {
        const prog = sufficiencyProgress(p, sufficientSeconds, minShortClips)
        return `${name} (${prog.label})`
      }
      return name
    })
  // #28 PR-B: the cloud identity fallback is retired, so the cost story is
  // simpler and the copy says so - overlap splitting is the ONLY second
  // transcription left, and an unplaceable voice stays unnamed, never
  // guessed and never sent to the cloud to be guessed at.
  const base =
    'Room mode is on: turns are attributed by voice, on this device, at no '
    + 'extra cost. A second transcription runs only when voices overlap, to '
    + 'untangle who said what; a voice that cannot be placed stays unnamed. '
    + 'Say "X has left" to remove someone, or "solo mode" to switch off.'
  if (!learning.length) return base
  return `${base} Still learning: ${learning.join(', ')} - their turns stay `
    + 'uncertain until enough of their voice has been heard.'
}

// Should a LIVE voice session's room-mode flag follow the chat's durable
// room mode? (#28 room commands: "solo mode" spoken mid-session must
// actually end the doubled transcription and the room capture profile, not
// just flip the server flag.) `session` is {active, roomMode, source} where
// source says who last set the session flag: 'server' (seeded from the chat
// at session start, or adopted from a server-side arm) or 'manual' (the
// user's own session-only toggle). Returns true (adopt on), false (adopt
// off) or null (leave it alone).
// - Arm always adopts: the server only arms on real evidence (an
//   introduction, a recognised voice, a spoken command), and running the
//   pass without the matching capture profile would half-work.
// - Disarm adopts ONLY a server-sourced flag: a session-only room mode the
//   user switched on by hand (durable mode off the whole time) is theirs
//   alone to switch off.
export function adoptRoomMode(serverOn, session) {
  if (!session || !session.active) return null
  if (serverOn && !session.roomMode) return true
  if (!serverOn && session.roomMode && session.source === 'server') return false
  return null
}

// The newest OPEN "someone new is speaking" ask, or null. One at a time by
// backend design; defensive here anyway.
export function askFlag(flags) {
  const open = (flags || []).filter(
    (f) => f && f.kind === 'unknown_voice' && !f.resolved_at)
  return open.length ? open[open.length - 1] : null
}

// The newest OPEN merge question (#28: naming is law), or null: an
// introduced name sits close to a remembered one, and the app asked instead
// of guessing. Rendered through the same ask strip as unknown_voice.
export function mergeFlag(flags) {
  const open = (flags || []).filter(
    (f) => f && f.kind === 'merge_question' && !f.resolved_at)
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
  if (flag.kind === 'merge_question') {
    // The merge question (#28: naming is law): honest that nothing was
    // merged, and says exactly how to answer either way.
    const a = flag.label || 'The new name'
    const b = flag.suspected || 'someone already remembered'
    return `Is ${a} the same person as ${b}? Nothing has been merged - `
      + `rename ${a} to ${b} under Remembered voices to combine them, `
      + 'or dismiss this if they are different people.'
  }
  return ''
}

// Names offered by the tap-to-correct menu: everyone present in the room
// plus every remembered voice, minus the labels the turn already carries.
// Order: roster first (most likely correction), then remembered strangers.
// Each option is {name, display}: `name` is the IDENTITY name the correction
// endpoint must receive (so the fix lands on the existing person instead of
// minting a twin), `display` is the preferred spelling the menu shows (#28:
// naming is law).
export function reassignOptions(roster, people, currentLabels) {
  const taken = new Set((currentLabels || []).map((l) => String(l).toLowerCase()))
  const out = []
  const push = (name, display) => {
    if (typeof name !== 'string' || !name) return
    const key = name.toLowerCase()
    if (taken.has(key) || out.some((o) => o.name.toLowerCase() === key)) return
    if (out.length < MAX_MENU_NAMES) {
      out.push({ name, display: displayName(display) || name })
    }
  }
  for (const p of roster || []) {
    if (p && p.status !== 'left') push(p.name, p)
  }
  for (const p of people || []) {
    if (p) push(p.name, p)
  }
  return out
}

// The remembered-voices panel's per-person line: honest about what is stored
// and whether it is enough to identify them. `minShortClips` feeds the
// two-part bar's copy (#28 PR-B); `setAside` surfaces clips the hygiene
// audit quarantined - kept on disk, excluded from matching.
export function personSummary(person, sufficientSeconds, minShortClips) {
  if (!person) return null
  const secs = Number(person.seconds) || 0
  let status
  if (person.sufficient) {
    status = 'voice remembered - identified automatically when they speak'
  } else {
    const prog = sufficiencyProgress(person, sufficientSeconds, minShortClips)
    status = `still learning - ${prog ? prog.label : ''}; `
      + 'turns stay uncertain until then'
  }
  const shown = displayName(person) || person.name
  const aside = Number(person.quarantined_count) || 0
  const noise = Number(person.noise_count) || 0
  const mixed = aside - noise
  const bits = []
  if (mixed > 0) {
    bits.push(`${mixed} clip${mixed === 1 ? '' : 's'} set aside - they `
      + 'sounded more like another remembered voice')
  }
  if (noise > 0) {
    // #219: the second reason, named separately - a noise clip belongs to
    // nobody, so "sounded like someone else" would misdirect the remedy.
    bits.push(`${noise} clip${noise === 1 ? '' : 's'} set aside - not a `
      + 'voice at all (noise, not speech)')
  }
  return {
    name: shown,
    // Honesty when a preferred spelling differs from the introduced name:
    // the label chips keep writing the introduced name, so say so.
    alias: shown !== person.name ? `heard as "${person.name}"` : '',
    detail: `${person.clip_count || 0} clip${person.clip_count === 1 ? '' : 's'}, `
      + `${secs.toFixed(1)}s`,
    status,
    setAside: bits.length
      ? `${bits.join('; ')}; they no longer take part in matching`
      : '',
  }
}

// Ambient arming honesty (#28; renamed from SNIFF_EXPLAINER in PR-B - the
// EL session-start sniff retired with the cloud identity path, so ambient
// local detection is the only automatic door and the "listens twice" cost
// caveat is gone for real).
export const AMBIENT_EXPLAINER =
  'With remembered voices in the house, room mode switches on by itself '
  + 'when a known voice speaks - identified on this device at no extra cost. '
  + 'Say "solo mode" to keep a session private.'

// What forgetting means, stated before the click - deletion copy lives with
// the logic so every surface says the same true thing.
export const FORGET_EXPLAINER =
  'Forget deletes this person\'s stored voice audio from this computer. '
  + 'They can be re-learned only by being introduced and heard again.'

// #83: a bank that crossed sufficiency with no human ever standing behind
// it (no introduction, no owner correction) is exactly the phantom shape
// from the #65 incident - surface it for the owner's ear. Returns null
// when there is nothing to ask. #221 adds the outlived-backing shape: the
// bank WAS vouched, but accumulation replaced every human-backed clip and
// the replacements matched only weakly - the ask must say that, not
// "learnt without an introduction", which would be untrue.
export function auditionNotice(person) {
  if (!person || !person.needs_audition) return null
  const name = person.preferred_name || person.name
  if (person.trust === 'low') {
    return person.id_paused
      ? 'Every recording a person stood behind has been replaced, and the '
        + 'newer ones matched only weakly - not naming or seating anyone '
        + `until you listen and confirm this is really ${name}.`
      : 'Every recording a person stood behind has been replaced - listen '
        + `and confirm this is really ${name}.`
  }
  return person.id_paused
    ? `Learnt without an introduction - not naming or seating anyone until you listen and confirm this is really ${name}.`
    : `Learnt without an introduction - listen to the clips and confirm this is really ${name}.`
}

// #221's quieter half: the bank keeps working (its save-time scores read
// strong), but it now rests wholly on self-collected audio - say so in
// Remembered voices so the owner knows a listen is worth their while.
export function selfCollectedNotice(person) {
  if (!person || person.trust !== 'high') return ''
  return 'Every recording a person stood behind has rotated out; this '
    + 'voice now rests on self-collected audio. Worth a listen when you '
    + 'have a moment.'
}
