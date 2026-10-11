// How far into a reply you've heard, from the word timings ElevenLabs sends
// with its audio (#634). Pure, per the house rule: voice.js acts only on
// what these return.
//
// Timings come in batches of a few words, each timed from 0 at the start
// of its batch. Laid end to end, they fall a little behind the audio,
// about a second over ten seconds of speech, because a batch's timings
// leave out the pause after its last word. Once a reply's audio is all
// in, its length stretches the timeline to fit: the stream is MP3 at
// 128 kbps, so 16,000 bytes make a second. The accent tag the app puts in
// front of each piece of text comes back with timings of its own, and is
// dropped here.

export const MP3_BYTES_PER_SECOND = 16000

// A phrase that began longer ago than this is too far to go back to, so a
// resume backs up to the start of the word instead.
export const MAX_REWIND_S = 3

export function newTimeline() {
  return { text: '', starts: [], mids: [], end: 0, inTag: false }
}

// One batch of timings, {chars, start, dur} in ms, added after the last.
// Returns a new timeline; the one passed in is left alone.
export function addTimings(tl, batch) {
  const chars = batch?.chars
  if (!Array.isArray(chars) || !chars.length) return tl
  const start = batch.start || []
  const dur = batch.dur || []
  const offset = tl.end
  let { text, inTag } = tl
  const starts = tl.starts.slice()
  const mids = tl.mids.slice()
  let last = 0
  for (let i = 0; i < chars.length; i++) {
    const c = String(chars[i] ?? '')
    const s = Number(start[i]) || 0
    const d = Number(dur[i]) || 0
    last = Math.max(last, s + d)
    if (c === '[') { inTag = true; continue }
    if (inTag) {
      if (c === ']') inTag = false
      continue
    }
    text += c
    starts.push(offset + s)
    mids.push(offset + s + d / 2)
  }
  return { text, starts, mids, end: offset + last, inTag }
}

// How much to stretch the timeline to fit the audio: only once all of it
// is in, and only a believable amount.
function stretch(tl, audioSeconds) {
  if (!(audioSeconds > 0) || !(tl.end > 0)) return 1
  const k = (audioSeconds * 1000) / tl.end
  return k >= 1 && k <= 1.5 ? k : 1
}

// How many characters of `times` fall at or before `ms`.
function countUpTo(times, ms) {
  let lo = 0
  let hi = times.length
  while (lo < hi) {
    const mid = (lo + hi) >> 1
    if (times[mid] <= ms) lo = mid + 1
    else hi = mid
  }
  return lo
}

// How many characters of the reply's spoken text had been heard by
// `seconds` into its audio: those whose middle had played. Pass
// `audioSeconds` once all the reply's audio is in.
export function heardChars(tl, seconds, { audioSeconds = 0 } = {}) {
  return countUpTo(tl.mids, (seconds * 1000) / stretch(tl, audioSeconds))
}

// Where to carry on from after a pause that began `seconds` into the
// reply: the start of the phrase that was playing, the text after the
// last comma or full stop, or the start of the word when that phrase began
// more than MAX_REWIND_S earlier. In seconds of audio, never later than
// `seconds`. Null when the timeline has nothing there.
export function phraseStart(tl, seconds, { audioSeconds = 0 } = {}) {
  const k = stretch(tl, audioSeconds)
  const ms = (seconds * 1000) / k
  const n = countUpTo(tl.starts, ms)
  if (!n) return null
  const t = tl.text
  const startOf = (i) => (tl.starts[i] * k) / 1000
  let word = n - 1
  while (word > 0 && !/\s/.test(t[word - 1])) word--
  while (word < n - 1 && /\s/.test(t[word])) word++
  let phrase = word
  while (phrase > 0 && !(/\s/.test(t[phrase - 1]) && /[.,;:!?…]/.test(t[phrase - 2] || '.'))) {
    phrase--
  }
  while (phrase < word && /\s/.test(t[phrase])) phrase++
  const at = seconds - startOf(phrase) <= MAX_REWIND_S ? startOf(phrase) : startOf(word)
  return Math.min(at, seconds)
}

// How many bytes of audio a base64 chunk holds.
export function base64Bytes(b64) {
  const s = String(b64 || '')
  const pad = s.endsWith('==') ? 2 : s.endsWith('=') ? 1 : 0
  return Math.max(0, (s.length * 3) / 4 - pad)
}

// How many words of a reply's spoken text the first `chars` characters
// hold, and the last few of them, for the server to find where in the
// saved reply the cut fell (#634). Counted the way the server counts:
// letters and digits, with an apostrophe inside allowed.
export const TAIL_WORDS = 6

export function heardWords(tl, chars) {
  const text = String(tl?.text || '')
  const n = Math.max(0, chars || 0)
  let heard = text.slice(0, n)
  // A word cut off halfway wasn't heard.
  if (/[\p{L}\p{N}]$/u.test(heard) && /^[\p{L}\p{N}'’]/u.test(text.slice(n))) {
    heard = heard.replace(/[\p{L}\p{N}'’]+$/u, '')
  }
  const words = heard.match(/[\p{L}\p{N}]+(?:['’][\p{L}\p{N}]+)*/gu) || []
  return { words: words.length, tail: words.slice(-TAIL_WORDS) }
}
