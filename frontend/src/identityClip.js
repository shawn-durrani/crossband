// The identity copy of a batch-transcribed turn (#461), in a pure module
// per the house rule so the cut and the encoding are unit-tested.
//
// The realtime relay hears every streamed turn and checks who spoke it on
// the server. A turn the batch path transcribes never reaches the relay:
// the fallback once realtime fails, and the salvage for a turn realtime
// lost. For those the client sends a second copy with the batch upload,
// PCM-16 mono at 16 kHz in a WAV, which is what the server's check reads.
//
// The batch recording can start well before this turn and, for a salvaged
// turn, run on after it. So it can hold the models' own speech and other
// people talking. Only the stretch that covers this turn goes: the speech
// itself, half a second before it, which is what the relay's own copy
// holds, and a short tail after it.

export const IDENTITY_RATE = 16000
export const PRE_ROLL_MS = 500
export const TAIL_MS = 300
// The server keeps no more than two minutes of one turn.
export const MAX_CLIP_MS = 120000

// Where this turn sits in the recording, counted back from its end, as
// { keepMs, dropMs }: drop the last `dropMs`, then keep the `keepMs`
// before that. Null when the turn can't be placed, and no copy is sent
// then. `speechMs` is how long the person spoke, `endedAt` the Date.now()
// of their last voiced frame, and `stoppedAt` the Date.now() when the
// recording stopped.
export function clipWindow({ speechMs, endedAt, stoppedAt } = {}) {
  if (!Number.isFinite(speechMs) || speechMs <= 0) return null
  if (!Number.isFinite(endedAt) || endedAt <= 0) return null
  if (!Number.isFinite(stoppedAt) || stoppedAt < endedAt) return null
  const after = stoppedAt - endedAt
  const tail = Math.min(after, TAIL_MS)
  return { keepMs: Math.min(MAX_CLIP_MS, Math.round(speechMs + PRE_ROLL_MS + tail)),
           dropMs: Math.round(after - tail) }
}

// Float32 samples at `srcRate` -> a WAV file as a Uint8Array at
// IDENTITY_RATE, cut to `win` from clipWindow. Null when nothing of the
// turn is left, as when the recording began after the turn ended. The
// decimation is the one the realtime stream uses (voice.js
// _pcm16Base64), so both paths hand the matcher the same kind of audio,
// and the voices it learnt from the stream still match.
export function identityWav(samples, srcRate, win) {
  if (!samples || !samples.length || !(srcRate > 0) || !win) return null
  const toSamples = (ms) => Math.max(0, Math.round((ms / 1000) * srcRate))
  const end = samples.length - toSamples(win.dropMs)
  const start = Math.max(0, end - toSamples(win.keepMs))
  if (end <= start) return null
  const tail = samples.subarray ? samples.subarray(start, end) : samples.slice(start, end)
  const ratio = srcRate / IDENTITY_RATE
  const n = Math.floor(tail.length / ratio)
  if (n <= 0) return null
  const out = new Uint8Array(44 + n * 2)
  const view = new DataView(out.buffer)
  const ascii = (at, s) => { for (let i = 0; i < s.length; i++) out[at + i] = s.charCodeAt(i) }
  ascii(0, 'RIFF')
  view.setUint32(4, 36 + n * 2, true)
  ascii(8, 'WAVE')
  ascii(12, 'fmt ')
  view.setUint32(16, 16, true)            // fmt chunk size
  view.setUint16(20, 1, true)             // PCM
  view.setUint16(22, 1, true)             // mono
  view.setUint32(24, IDENTITY_RATE, true)
  view.setUint32(28, IDENTITY_RATE * 2, true)
  view.setUint16(32, 2, true)             // block align
  view.setUint16(34, 16, true)            // bits per sample
  ascii(36, 'data')
  view.setUint32(40, n * 2, true)
  for (let i = 0; i < n; i++) {
    const s = Math.max(-1, Math.min(1, tail[Math.floor(i * ratio)] || 0))
    view.setInt16(44 + i * 2, s < 0 ? s * 0x8000 : s * 0x7fff, true)
  }
  return out
}

// The batch upload's form: the recording and its length as always, plus
// the turn id and the identity copy when there is one. A copy without a
// turn id can't be matched to the message it becomes, so neither goes
// alone, and the server checks nothing for that turn.
export function batchSttForm(blob, speechMs, turnId, copy) {
  const fd = new FormData()
  fd.append('file', blob, 'utterance.webm')
  fd.append('duration_ms', String(Math.round(speechMs)))
  if (turnId && copy) {
    fd.append('turn_id', turnId)
    fd.append('pcm', copy, 'turn.wav')
  }
  return fd
}
