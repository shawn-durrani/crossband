// The voice health strip (#28): ALL the decision and formatting logic for
// the compact readout in the voice dock and the mobile call screen, in a
// pure module per the house rule - the components only render what these
// return.
//
// Data shapes:
// - health: GET /api/voice/health - {matcher, people_total,
//   people_sufficient, chat: {room_mode, ambient_off, roster_count} | null,
//   last_decision: {path, ms, age_s} | null}. Content-free by backend
//   design: states, counts and milliseconds, never names. With a chat id
//   the snapshot also carries identity_history and label_flow (#304
//   evidence capture); the strip does not render them - they exist for
//   the one-tap diagnostics dump.
// - people: GET /api/voice/people's list - where the names come from,
//   because the caller could already see them there.

import { displayName, sufficiencyProgress, voiceChips } from './roomState.js'

// How the matcher's state reads on the strip. 'ready' is the identity
// path working; since #28 PR-B there is NO cloud identity fallback, so a
// degraded matcher means turns stay unnamed and nothing arms automatically
// - voice itself keeps working, and the manual doors (introductions,
// spoken commands, the switch in the voice settings) still arm room mode.
// The copy says exactly that, never that a cloud check "takes over".
// ("The toggle" became "the switch in the voice settings" when the tray's
// room button became an indicator - #28.)
const MATCHER_READOUTS = {
  ready: {
    label: 'matcher ready',
    title: 'Known voices are identified on this device in a fraction of a second.',
    degraded: false,
  },
  fetching: {
    label: 'fetching model…',
    title: 'The one-time voice-model download is in flight. Until it lands, '
      + 'turns are not named and room mode only switches on by hand '
      + '(an introduction, a spoken command, or the switch in the voice '
      + 'settings).',
    degraded: true,
  },
  cold: {
    label: 'matcher idle',
    title: 'The on-device matcher warms up on the first spoken turn.',
    degraded: false,
  },
  unavailable: {
    label: 'matcher unavailable',
    title: 'The on-device matcher is unavailable, so turns are not named '
      + 'and room mode only switches on by hand. Voice itself still works.',
    degraded: true,
  },
  disabled: {
    label: 'matcher off',
    title: 'Local voice identification is switched off in settings, so '
      + 'turns are not named and room mode only switches on by hand.',
    degraded: true,
  },
}

export function matcherReadout(state) {
  return MATCHER_READOUTS[state] || MATCHER_READOUTS.unavailable
}

// The session's mode, one honest line - and, since the room button became
// an indicator (#28: arming is fully automatic now, so room state is SHOWN
// from the tray, never operated there), the ONE source of truth for the
// mode string. The tray's indicator chip, the mobile call screen's chip and
// every settings readout render exactly this object; nothing else may
// derive a competing mode label. Three states, each with a one-sentence
// tooltip explaining the automation:
// - 'on':        the room is attributing turns ("room on · N");
// - 'listening': the default - the room switches itself on when it is
//                needed (a known or unknown voice, an introduction, a
//                spoken command);
// - 'solo':      the owner durably disarmed it for this chat.
// Null when the snapshot has no chat block - nothing worth a chip.
export function modeReadout(health) {
  const chat = health && health.chat
  if (!chat || typeof chat !== 'object') return null
  if (chat.room_mode) {
    const n = Number(chat.roster_count) || 0
    return {
      state: 'on',
      label: n > 0 ? `room on · ${n}` : 'room on',
      title: 'Turns are attributed by voice; say "solo mode" to switch '
        + 'the room off.',
    }
  }
  if (chat.ambient_off) {
    return {
      state: 'solo',
      label: 'solo',
      title: 'You switched the room off for this chat; say "group mode" or '
        + 'use "switch on now" in the voice settings to bring it back.',
    }
  }
  // Degraded-matcher honesty: without voice recognition nothing arms on a
  // voice (a pinned behaviour), so the listening tooltip must not promise
  // it. The spoken doors and the settings switch still work and the copy
  // says exactly that.
  const degraded = typeof (health && health.matcher) === 'string'
    ? matcherReadout(health.matcher).degraded
    : false
  return {
    state: 'listening',
    label: 'listening',
    title: degraded
      ? 'Voice recognition is unavailable right now, so the room switches '
        + 'on only for a spoken introduction, "group mode", or the switch '
        + 'in the voice settings.'
      : 'The room switches itself on when another voice speaks; say '
        + '"solo mode" to switch that off.',
  }
}

// The live pulse: the most recent identification's path and latency.
// "local · 227ms" or "cloud · 1.9s"; 'pending' while a session is live but
// no decision has landed yet; null with no live session and nothing to say.
// Why a turn went unnamed, in plain English (#28, thirteenth field test).
// "Identity pending" hid two different problems: audio too poor to judge,
// versus heard clearly but not sure who. The matcher already computes the
// reason; this just says it.
export const UNRESOLVED_COPY = {
  too_short: ['too short to judge', 'That turn was too brief to identify a voice from.'],
  not_speech: ['not a voice', 'That sound was not speech, so no match was attempted.'],
  below_threshold: ['voice not recognised', 'Heard clearly, but it did not match a remembered voice closely enough.'],
  ambiguous: ['too close to call', 'It sat between two remembered voices, so it was left unnamed rather than guessed.'],
  multi: ['more than one voice', 'Two people spoke over each other, so the turn went to the crosstalk split.'],
  no_candidates: ['no voices learnt yet', 'Nobody in this room has enough voice banked to match against.'],
  unavailable: ['matcher not ready', 'The on-device matcher was not available for that turn.'],
  disabled: ['matching off', 'Voice identification is switched off.'],
  error: ['check failed', 'The voice check did not complete for that turn.'],
}

export function pulseReadout(lastDecision, sessionActive = false) {
  if (lastDecision && typeof lastDecision === 'object'
      && lastDecision.path === 'unresolved') {
    const [label, title] = UNRESOLVED_COPY[lastDecision.reason]
      || ['not named', 'That turn was left unnamed.']
    return { label, title, unresolved: true }
  }
  if (!lastDecision || typeof lastDecision !== 'object'
      || typeof lastDecision.ms !== 'number'
      || (lastDecision.path !== 'local' && lastDecision.path !== 'cloud')) {
    return sessionActive
      ? { label: 'pending', title: 'No spoken turn has been identified yet this session.' }
      : null
  }
  const ms = Math.max(0, lastDecision.ms)
  const shown = ms < 1000 ? `${Math.round(ms)}ms` : `${(ms / 1000).toFixed(1)}s`
  // 'cloud' now means exactly one thing (#28 PR-B): the crosstalk split -
  // the only ElevenLabs pass left after the identity fallback retired.
  const how = lastDecision.path === 'local'
    ? 'identified on this device'
    : 'untangled by the cloud crosstalk split (overlapping voices)'
  return {
    label: `${lastDecision.path} · ${shown}`,
    title: `The last spoken turn was ${how} in ${shown}.`,
  }
}

// Known voices, one entry each: display name plus sufficiency - 'done' for
// a remembered voice, progress while still learning. The two-part bar (#28
// PR-B) reads honestly: the seconds count while it is short, then the
// missing short-clip half once the seconds are met. Reuses the people
// snapshot (names live there, not in the health endpoint).
export function knownVoiceLines(people, sufficientSeconds, minShortClips) {
  const out = []
  for (const p of people || []) {
    if (!p || typeof p !== 'object') continue
    const name = displayName(p)
    if (!name) continue
    const prog = sufficiencyProgress(p, sufficientSeconds, minShortClips)
    let label = 'remembered'
    if (prog && !prog.done) {
      const secsPart =
        `${(Number(p.seconds) || 0).toFixed(1)}s of ${Number(sufficientSeconds) || 6}s`
      const needShorts = Number(minShortClips) > 0 ? Number(minShortClips) : 0
      const shorts = typeof p.short_clips === 'number'
        ? Math.max(0, p.short_clips) : null
      label = needShorts > 0 && shorts !== null && shorts < needShorts
        ? `${secsPart} · ${shorts} of ${needShorts} short clips`
        : secsPart
    }
    out.push({ name, done: !!(prog && prog.done), label })
  }
  return out
}

// Close-pair warnings (#28 PR-B, the hygiene guard): pairs of remembered
// voices whose banks sound alike. Names come from the people snapshot's
// per-person close_to lists (person ids), rendered once per pair. The copy
// states the consequence honestly - matching got stricter, not broken.
export function closeVoiceLines(people) {
  const byId = {}
  for (const p of people || []) {
    if (p && typeof p === 'object' && p.person_id) byId[p.person_id] = p
  }
  const seen = new Set()
  const out = []
  for (const p of people || []) {
    if (!p || typeof p !== 'object' || !Array.isArray(p.close_to)) continue
    for (const otherId of p.close_to) {
      const other = byId[otherId]
      if (!other) continue
      const key = [p.person_id, otherId].sort().join('|')
      if (seen.has(key)) continue
      seen.add(key)
      const a = displayName(p)
      const b = displayName(other)
      if (!a || !b) continue
      out.push(`${a} and ${b} sound close - matching is stricter`)
    }
  }
  return out
}

// ---- the bounded chip row (#28, dock refinement) ------------------------
//
// The tray has a fixed width, so the chip row must have a fixed ceiling:
// past MAX_DOCK_CHIPS the rest collapse into one "+N" whose title names
// them. Bounding lives here, not in the components, so the desktop tray and
// the mobile one-liner overflow by the same rule.
export const MAX_DOCK_CHIPS = 4

export function boundedChips(chips, max = MAX_DOCK_CHIPS) {
  const all = (Array.isArray(chips) ? chips : []).filter(
    (c) => c && typeof c === 'object' && c.name)
  const n = Number(max)
  const limit = Number.isFinite(n) && n > 0 ? Math.floor(n) : MAX_DOCK_CHIPS
  const shown = all.slice(0, limit)
  const rest = all.slice(limit)
  return {
    all,
    shown,
    hidden: rest.length,
    more: rest.length ? `+${rest.length}` : '',
    moreTitle: rest.length ? `Also: ${rest.map((c) => c.name).join(', ')}` : '',
  }
}

// The mobile call screen's single line - "Listening · Shawn ✓ +1" - which
// expands on tap into the same chips the desktop tray shows. `status` is
// the already-derived status line (voiceView.statusFor); its trailing
// ellipsis is dropped so the separators read as one sentence. With no
// chips it is just the status, exactly as the call screen has always read.
export function collapsedVoiceSummary(status, chips) {
  const head = String(status || '').replace(/…+$/, '').trim()
  const { shown, more } = boundedChips(chips, 1)
  const parts = [head]
  if (shown.length) parts.push(more ? `${shown[0].short} ${more}` : shown[0].short)
  return parts.filter(Boolean).join(' · ')
}

// The assembled strip: everything the dock and the call screen render, or
// null when there is nothing useful to show (no health snapshot yet).
//
// `chips` (#28, dock refinement) is the two-tier dock's status tier: one
// bounded chip per person, replacing the unbounded prose run that used to
// overflow the tray. `voices` and the readouts stay - they moved behind the
// settings disclosure rather than away.
export function healthStrip({ health, people, roster, sufficientSeconds,
                              minShortClips, sessionActive,
                              maxChips = MAX_DOCK_CHIPS }) {
  if (!health || typeof health !== 'object') return null
  return {
    matcher: matcherReadout(health.matcher),
    mode: modeReadout(health),
    pulse: pulseReadout(health.last_decision, !!sessionActive),
    voices: knownVoiceLines(people, sufficientSeconds, minShortClips),
    close: closeVoiceLines(people),
    learning: learningLines(people, health.learning),
    chips: boundedChips(
      voiceChips(roster, people, sufficientSeconds, minShortClips), maxChips),
  }
}

// "Is it still learning?" answered in the app (#28, thirteenth field test).
// Until now the only way to know whether a voice bank was still growing was
// to read the store file by hand. Every number here is already in the
// snapshot; this only phrases it.
export function learningAge(seconds) {
  if (typeof seconds !== 'number' || seconds < 0) return 'not yet'
  if (seconds < 90) return 'just now'
  if (seconds < 3600) return `${Math.round(seconds / 60)}m ago`
  if (seconds < 86400) return `${Math.round(seconds / 3600)}h ago`
  return `${Math.round(seconds / 86400)}d ago`
}

export function learningLines(people, learning) {
  if (!Array.isArray(people) || !learning || typeof learning !== 'object') {
    return []
  }
  return people
    .map((p) => {
      const l = learning[p && p.person_id]
      if (!l || typeof l !== 'object') return null
      const name = p.preferred_name || p.name
      if (!name) return null
      // At capacity the bank stops growing and starts REFRESHING: a better
      // clip evicts a worse one. Saying "still learning" there would be a
      // lie, and saying nothing was the old silence this fixes.
      const what = l.at_capacity ? 'refreshing' : 'still learning'
      const clips = `${l.clips || 0} clip${l.clips === 1 ? '' : 's'}`
      return {
        name,
        label: `${name} · ${what} · ${clips}`,
        title: `${clips}, ${Math.round(l.seconds || 0)}s banked, last learned `
          + `${learningAge(l.last_learned_age_s)}. `
          + (l.at_capacity
            ? 'The bank is full, so new clips replace weaker ones rather than adding.'
            : 'New clips are still being added as this voice is heard.'),
      }
    })
    .filter(Boolean)
}
