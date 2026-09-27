// Recording a voice on purpose (#504), on the Voices page.
//
// The owner picks a person, has them read a short passage aloud for about
// 30 seconds, and the recording is banked as introduced clips. Most banks
// hold too little clean speech to pass the readiness test, and before this
// clips only arrived by chance, when a voice chat was sure who spoke.
//
// Everything here is pure so node --test pins it: the passages and which
// one comes next, the 45 second stop, the level meter, the recorder's
// state machine, and the words shown after a recording. The component
// (components/VoiceRecorder.jsx) is wiring and markup only.
import { readinessLine } from './voiceReadiness.js'

export const TARGET_SECONDS = 30
export const MAX_SECONDS = 45
// Under this it isn't worth sending: the server wants 10 seconds of speech.
export const MIN_SECONDS = 10
export const READINESS_POLL_MS = 2000
export const READINESS_GIVE_UP_MS = 120000
export const PASSAGE_STORE_KEY = 'crossband.voiceRecording.passages'

// Made-up passages, each about 30 seconds read at an easy pace. Plain
// sentences with a spread of sounds, and no names, so nobody reads out a
// name the app could mistake for an introduction.
export const PASSAGES = [
  'The kitchen window looks out over a small garden with a lemon tree and '
    + 'a crooked wooden fence. On Sunday mornings we make pancakes, squeeze '
    + 'fresh juice and argue about whose turn it is to wash up. The dog waits '
    + 'by the back door, hoping someone drops a piece of bacon. Later we might '
    + 'walk down to the beach, check the surf, and buy hot chips on the way home.',
  'Every winter the old bus to the valley runs a little later than the '
    + 'timetable says. Passengers huddle under the shelter with thermos flasks, '
    + 'sharing news about the weather, the footy and the price of petrol. When '
    + 'the driver finally arrives, she waves, jokes about the fog and says sorry '
    + 'for the delay. Nobody really minds. The heater works, the seats are soft, '
    + 'and the view of the misty hills is worth the wait.',
  'My neighbour is building a boat in his shed, one plank at a time. He '
    + 'measures twice, cuts once, and hums while he sands each curve smooth. '
    + 'Yesterday he showed me the brass fittings he found at a market, green with '
    + 'age but still strong. He says the boat will be ready by summer. I\'m not '
    + 'so sure, but I\'d happily join him for the first trip across the bay.',
  'Bright jellyfish drift past the jetty as the tide turns. A family of '
    + 'pelicans glides overhead, and a fisherman checks his lines with patient, '
    + 'weathered hands. Children chase each other along the sand, shrieking '
    + 'whenever a wave rushes over their toes. The smell of salt, sunscreen and '
    + 'sizzling sausages fills the air. As the sun sinks lower, the sky shifts '
    + 'from gold to pink to a deep, velvety blue.',
  'Our library has a quiet corner with a huge armchair that everyone wants. '
    + 'Whoever gets there first usually stays for hours, reading thrillers, '
    + 'cookbooks or old maps of the city. The librarian pretends not to notice '
    + 'when people fall asleep. Last Thursday a visiting author gave a talk about '
    + 'writing mysteries, and the room was so full that some of us sat on the '
    + 'floor, listening for every clue.',
]

// The passage after the one this person read last, in turn, so a second
// recording differs from the first. Anything unknown starts at the top.
export function nextPassageIndex(last, count = PASSAGES.length) {
  if (!(count > 0)) return 0
  const n = Number(last)
  if (last === null || last === undefined || !Number.isInteger(n) || n < 0) return 0
  return (n + 1) % count
}

// The per-person memory of the last passage read, as a plain object. The
// component keeps it in localStorage; these two keep the rules here.
export function lastPassage(memory, personId) {
  const n = memory && typeof memory === 'object' ? memory[personId] : undefined
  return Number.isInteger(n) ? n : null
}

export function rememberPassage(memory, personId, index) {
  const base = memory && typeof memory === 'object' ? memory : {}
  return { ...base, [personId]: index }
}

// The recording stops by itself once it has run this long.
export function shouldAutoStop(elapsedMs) {
  return Number(elapsedMs) >= MAX_SECONDS * 1000
}

// How full the level meter is, 0 to 1, from one block of float samples:
// the block's loudness on a 60 dB scale, so a quiet voice still moves it.
export function meterLevel(samples) {
  if (!samples || !samples.length) return 0
  let sum = 0
  for (let i = 0; i < samples.length; i++) sum += samples[i] * samples[i]
  const rms = Math.sqrt(sum / samples.length)
  if (!(rms > 0)) return 0
  const db = 20 * Math.log10(rms)
  return Math.max(0, Math.min(1, (db + 60) / 60))
}

// The microphone's blocks joined into one run of samples, for the WAV.
export function joinChunks(chunks) {
  const list = Array.isArray(chunks) ? chunks.filter((c) => c && c.length) : []
  const out = new Float32Array(list.reduce((n, c) => n + c.length, 0))
  let at = 0
  for (const c of list) { out.set(c, at); at += c.length }
  return out
}

export function clock(ms) {
  const total = Math.max(0, Math.floor((Number(ms) || 0) / 1000))
  return `${Math.floor(total / 60)}:${String(total % 60).padStart(2, '0')}`
}

// The line under the meter while it records.
export function timerLine(elapsedMs) {
  return `${clock(elapsedMs)} of about ${clock(TARGET_SECONDS * 1000)}. `
    + `It stops by itself at ${clock(MAX_SECONDS * 1000)}.`
}

// A voice chat still listening would hear the reading too, and send it to
// the chat as a turn. Null when no microphone is live anywhere.
export function liveMicWarning(captures) {
  if (!Array.isArray(captures) || !captures.length) return null
  return 'A voice chat is still listening, so it would hear the reading '
    + 'too. End it, then press Start again.'
}

// What went wrong asking for the microphone, from the error's name.
export function micErrorMessage(error) {
  const name = error && error.name
  if (name === 'NotAllowedError' || name === 'SecurityError') {
    return 'The browser wasn\'t allowed to use the microphone. Allow it for '
      + 'this site, then try again.'
  }
  if (name === 'NotFoundError' || name === 'OverconstrainedError') {
    return 'No microphone was found. Plug one in, then try again.'
  }
  if (name === 'NotReadableError') {
    return 'The microphone is busy in another app. Close that, then try again.'
  }
  return 'The microphone couldn\'t start. Try again.'
}

export const TOO_SHORT = 'That was too short to keep. Read the whole passage, '
  + 'which takes about 30 seconds.'

// ---- the recorder's state machine ----
//
// idle -> starting -> recording -> saving -> checking -> done
// Starting can end blocked (a voice chat is listening) or failed (no mic).
// Saving can end refused (the server's plain reason). Checking waits for
// the readiness build and ends done, or done still checking when it gives
// up. Cancel goes back to idle from anywhere; start works from any end.
export const IDLE = Object.freeze({ phase: 'idle' })
const ENDS = new Set(['idle', 'blocked', 'failed', 'refused', 'done'])

export function recordStep(state, event) {
  const s = state || IDLE
  const e = event || {}
  switch (e.type) {
    case 'cancel':
      return IDLE
    case 'start':
      return ENDS.has(s.phase) ? { phase: 'starting' } : s
    case 'blocked':
      return s.phase === 'starting' ? { phase: 'blocked', message: e.message } : s
    case 'mic_failed':
      return s.phase === 'starting' ? { phase: 'failed', message: e.message } : s
    case 'mic_ready':
      return s.phase === 'starting' ? { phase: 'recording', elapsedMs: 0, level: 0 } : s
    case 'tick': {
      if (s.phase !== 'recording') return s
      const elapsedMs = Number(e.elapsedMs) || 0
      if (shouldAutoStop(elapsedMs)) return { phase: 'saving', elapsedMs, auto: true }
      return { phase: 'recording', elapsedMs, level: Number(e.level) || 0 }
    }
    case 'stop': {
      if (s.phase !== 'recording') return s
      if (s.elapsedMs < MIN_SECONDS * 1000) return { phase: 'refused', message: TOO_SHORT }
      return { phase: 'saving', elapsedMs: s.elapsedMs, auto: false }
    }
    case 'refused':
      return s.phase === 'saving' ? { phase: 'refused', message: e.message } : s
    case 'saved': {
      if (s.phase !== 'saving') return s
      const answer = e.answer || {}
      if (answer.state === 'checking') return { phase: 'checking', answer }
      return { phase: 'done', answer, readinessState: answer.state || 'off',
               readiness: answer.readiness || null, summary: answer.summary || null }
    }
    case 'readiness': {
      if (s.phase !== 'checking') return s
      const p = e.payload || {}
      if (!p.state || p.state === 'checking') return s
      return { phase: 'done', answer: s.answer, readinessState: p.state,
               readiness: p.readiness || null, summary: p.summary || s.answer.summary || null }
    }
    case 'give_up':
      return s.phase === 'checking'
        ? { phase: 'done', answer: s.answer, readinessState: 'checking',
            readiness: null, summary: s.answer.summary || null }
        : s
    default:
      return s
  }
}

// ---- what the page says after a recording ----

export const AGAIN_ELSEWHERE = 'Record again, ideally on another day or in another room.'

// "Kept 4 clips, 30 seconds of speech."
export function savedLine(answer) {
  const n = Number(answer && answer.saved) || 0
  const secs = Math.round(Number(answer && answer.seconds) || 0)
  return `Kept ${n} clip${n === 1 ? '' : 's'}, ${secs} second${secs === 1 ? '' : 's'} of speech.`
}

// The readiness half, once the build has landed (or it gave up):
// { text, advice, ready }. text is empty while the test is off.
export function recordOutcome(done) {
  const d = done || {}
  const state = d.readinessState
  if (!state || state === 'off') return { text: '', advice: '', ready: false }
  if (state === 'checking') {
    return { text: 'Still checking whether this voice is ready. The line under '
      + 'their name shows the result when it\'s in.', advice: '', ready: false }
  }
  if (state !== 'current') {
    return { text: 'Readiness can\'t be checked right now.', advice: '', ready: false }
  }
  const r = d.readiness
  if (!r) return { text: 'Not checked yet.', advice: '', ready: false }
  const line = readinessLine(r, { state: 'ready', ...(d.summary || {}) })
  const text = line ? line.text : ''
  if (r.ready) {
    return { text, ready: true,
             advice: 'The app should now name them on a day it hasn\'t heard them.' }
  }
  let advice = AGAIN_ELSEWHERE
  if (r.reason === 'too_few_pieces') advice = 'Record again to add more speech.'
  else if (r.reason === 'no_calibration') advice = 'Record someone else\'s voice too.'
  else if (r.reason === 'named_as_other') {
    advice = `Listen to their clips in case one is someone else's voice. ${AGAIN_ELSEWHERE}`
  }
  return { text, advice, ready: false }
}
