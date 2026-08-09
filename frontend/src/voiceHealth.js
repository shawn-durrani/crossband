// The voice health strip (#28): ALL the decision and formatting logic for
// the compact readout in the voice dock and the mobile call screen, in a
// pure module per the house rule - the components only render what these
// return.
//
// Data shapes:
// - health: GET /api/voice/health - {matcher, people_total,
//   people_sufficient, chat: {room_mode, ambient_off, roster_count} | null,
//   last_decision: {path, ms, age_s} | null}. Content-free by backend
//   design: states, counts and milliseconds, never names.
// - people: GET /api/voice/people's list - where the names come from,
//   because the caller could already see them there.

import { displayName, sufficiencyProgress } from './roomState.js'

// How the matcher's state reads on the strip. 'ready' is the identity
// path working; since #28 PR-B there is NO cloud identity fallback, so a
// degraded matcher means turns stay unnamed and nothing arms automatically
// - voice itself keeps working, and the manual doors (introductions,
// spoken commands, the toggle) still arm room mode. The copy says exactly
// that, never that a cloud check "takes over".
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
      + '(an introduction, a spoken command, or the toggle).',
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

// The session's mode, one honest line: room on (and how many the roster
// holds), solo because the owner disarmed it, or ambient listening (the
// default: a known voice would switch the room on by itself). Null when the
// snapshot has no chat block - nothing worth a line.
export function modeReadout(health) {
  const chat = health && health.chat
  if (!chat || typeof chat !== 'object') return null
  if (chat.room_mode) {
    const n = Number(chat.roster_count) || 0
    return {
      label: n > 0 ? `room on · ${n} in the room` : 'room on',
      title: 'Turns are attributed by voice. Say "solo mode" to switch off.',
    }
  }
  if (chat.ambient_off) {
    return {
      label: 'solo',
      title: 'You switched automatic listening off ("solo mode"). Room mode '
        + 'stays off until you re-enable it.',
    }
  }
  // #28 PR-C: the owner's confidently-matched turns are now labelled even
  // with room mode off, so the old "your own voice changes nothing" copy
  // would be dishonest - the session stays solo, but the turn says so.
  return {
    label: 'ambient listening',
    title: 'Room mode is off. A remembered voice switches it on by itself; '
      + 'your own voice keeps the session solo, with your turns marked '
      + 'voice-confirmed.',
  }
}

// The live pulse: the most recent identification's path and latency.
// "local · 227ms" or "cloud · 1.9s"; 'pending' while a session is live but
// no decision has landed yet; null with no live session and nothing to say.
export function pulseReadout(lastDecision, sessionActive = false) {
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

// The assembled strip: everything the dock and the call screen render, or
// null when there is nothing useful to show (no health snapshot yet).
export function healthStrip({ health, people, sufficientSeconds, minShortClips,
                              sessionActive }) {
  if (!health || typeof health !== 'object') return null
  return {
    matcher: matcherReadout(health.matcher),
    mode: modeReadout(health),
    pulse: pulseReadout(health.last_decision, !!sessionActive),
    voices: knownVoiceLines(people, sufficientSeconds, minShortClips),
    close: closeVoiceLines(people),
  }
}
