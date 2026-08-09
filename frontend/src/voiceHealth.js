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

// How the matcher's state reads on the strip. 'ready' is the local
// fast path; anything else means identification falls back to the cloud
// batch pass (slower, still correct), and the strip says so plainly.
const MATCHER_READOUTS = {
  ready: {
    label: 'matcher ready',
    title: 'Known voices are identified on this device in a fraction of a second.',
    degraded: false,
  },
  fetching: {
    label: 'fetching model…',
    title: 'The one-time voice-model download is in flight. Until it lands, '
      + 'identification uses the slower cloud check.',
    degraded: true,
  },
  cold: {
    label: 'matcher idle',
    title: 'The on-device matcher warms up on the first spoken turn.',
    degraded: false,
  },
  unavailable: {
    label: 'cloud fallback',
    title: 'The on-device matcher is unavailable, so identification uses the '
      + 'slower cloud check. Voice still works.',
    degraded: true,
  },
  disabled: {
    label: 'matcher off',
    title: 'Local voice identification is switched off in settings; the '
      + 'cloud check does the work instead.',
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
  return {
    label: 'ambient listening',
    title: 'Room mode is off. A remembered voice switches it on by itself; '
      + 'your own voice changes nothing.',
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
  const how = lastDecision.path === 'local'
    ? 'identified on this device'
    : 'identified by the cloud second listen'
  return {
    label: `${lastDecision.path} · ${shown}`,
    title: `The last spoken turn was ${how} in ${shown}.`,
  }
}

// Known voices, one entry each: display name plus sufficiency - 'done' for
// a remembered voice, seconds-progress while still learning. Reuses the
// people snapshot (names live there, not in the health endpoint).
export function knownVoiceLines(people, sufficientSeconds) {
  const out = []
  for (const p of people || []) {
    if (!p || typeof p !== 'object') continue
    const name = displayName(p)
    if (!name) continue
    const prog = sufficiencyProgress(p, sufficientSeconds)
    out.push({
      name,
      done: !!(prog && prog.done),
      label: prog && !prog.done
        ? `${(Number(p.seconds) || 0).toFixed(1)}s of ${Number(sufficientSeconds) || 6}s`
        : 'remembered',
    })
  }
  return out
}

// The assembled strip: everything the dock and the call screen render, or
// null when there is nothing useful to show (no health snapshot yet).
export function healthStrip({ health, people, sufficientSeconds, sessionActive }) {
  if (!health || typeof health !== 'object') return null
  return {
    matcher: matcherReadout(health.matcher),
    mode: modeReadout(health),
    pulse: pulseReadout(health.last_decision, !!sessionActive),
    voices: knownVoiceLines(people, sufficientSeconds),
  }
}
