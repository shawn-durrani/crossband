// Bounded escape hatch for a wedged voice turn gate. `roundActive` is set
// by round events and cleared by onRoundDone(); a round stream that hangs
// without ever erroring (a half-open mobile connection, a detached-round
// replay nothing can abort) leaves it true forever. A stuck gate pins the
// VAD to barge-in-only, so normal-volume speech is silently discarded and
// the session reads as stuck on "Listening" until a page reload.
//
// The rule: a round that has emitted NO events for ROUND_EVENT_IDLE_MS,
// with nothing playing, is treated as gone and the gate force-clears.
// Event idleness, not state duration, deliberately: tool-using rounds
// emit work_status/tool_activity for minutes with no audio, and a
// "round active with nothing playing" duration rule would cut them off.
// Audio playing is proof of life on its own, so the guard never fires
// mid-speech; the StreamPlayer's own watchdog bounds a wedged `playing`.
//
// Cost of a rare misfire (a seat thinking silently past the bound): the
// mic un-gates early and a send can reach the server while its round
// still runs. The round loop holds that send and retries it, so nothing
// the user said is lost either way.
export const ROUND_EVENT_IDLE_MS = 60000

// `idleMs`: elapsed ms since the last round event reached onEvent().
// Pure, per the house rule - voice.js only acts on what this returns.
export function shouldForceRoundDone({ roundActive, playing, idleMs }) {
  if (!roundActive) return false
  if ((playing || 0) > 0) return false
  const ms = Number(idleMs)
  if (!Number.isFinite(ms)) return false
  return ms > ROUND_EVENT_IDLE_MS
}

// The stall beacon's second trigger (#171): the user has spoken - a
// barge-in opened capture - but the gate is still up this long after
// their voice stopped, so nothing can finalize or send. This is the
// "talking into a stuck app" moment the telemetry was blind to: the
// latency trace only begins at finalize, so a stranded turn used to
// leave no row anywhere.
export const STRANDED_SPEECH_MS = 10000

export function speechStranded({ roundActive, playing, speechStart, lastVoice, now }) {
  if (!roundActive && (playing || 0) === 0) return false
  if (!speechStart || !lastVoice) return false
  const quiet = Number(now) - Number(lastVoice)
  if (!Number.isFinite(quiet)) return false
  return quiet > STRANDED_SPEECH_MS
}
