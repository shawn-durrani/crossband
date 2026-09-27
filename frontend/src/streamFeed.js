// How one reply's audio reaches the shared audio element. Pure, per the
// house rule: streamPlayer.js acts only on what these return.
//
// A reply's speech arrives as MP3 chunks from the TTS relay. There are
// three ways to play it:
//   'mse'      MediaSource (desktop Chrome, Safari on a Mac): each chunk
//              is appended as it arrives and playback starts early.
//   'managed'  ManagedMediaSource (Safari on an iPhone, iOS 17.1 and
//              later): the same, except the browser can ask the page to
//              pause feeding it, and it can evict audio it holds.
//   'held'     neither: the player keeps every chunk until the speech
//              stream ends, then plays them as one clip.
// Until this module, an iPhone always took the held path, so every reply
// waited for its whole speech stream before a word was heard (3 to 37
// seconds of first_audio_to_playback in the owner's traces).
//
// What the managed source asks of the page, from the W3C Media Source
// Extensions draft and WebKit's ManagedMediaSource.cpp:
//   - It opens only once remote playback is off on the element
//     (`disableRemotePlayback = true`) or an AirPlay alternative exists.
//     Without that, `sourceopen` never fires.
//   - `streaming` starts false. The browser sets it true and fires
//     `startstreaming` when it wants more data, and sets it false and
//     fires `endstreaming` when it holds enough (WebKit: 30 seconds ahead
//     of the playhead, asking again below 10). Both are hints, and an
//     append made while `streaming` is false isn't refused.
//   - Its buffers can drop audio at any time, and each drop fires
//     `bufferedchange` on the ManagedSourceBuffer with the `removedRanges`.

export const MSE = 'mse'
export const MANAGED = 'managed'
export const HELD = 'held'

// How long a streaming source gets to open before the phone gives up on
// it and plays the reply whole. `sourceopen` lands within milliseconds of
// the element taking the source when it works.
export const SOURCE_OPEN_MS = 3000

function takesMp3(Source) {
  if (typeof Source !== 'function') return false
  try { return !!Source.isTypeSupported?.('audio/mpeg') } catch { return false }
}

// Which way this browser plays a reply. `env` is the page's global scope.
// MediaSource comes first where it takes MP3, which keeps desktop Chrome
// and Safari on a Mac on the path they already had.
export function playbackPath(env = globalThis) {
  if (takesMp3(env?.MediaSource)) return MSE
  if (takesMp3(env?.ManagedMediaSource)) return MANAGED
  return HELD
}

// ---- feeding a managed source ----
//
// The player holds new chunks after an `endstreaming` and appends them
// again on `startstreaming`. It doesn't hold before the first
// `endstreaming`: `streaming` starts false before the browser has said
// anything, and waiting for a first `startstreaming` would wait on an
// event the browser may only send once audio is appended.
// `starved` is the audio element's `waiting` event: it ran out of audio
// at the playhead. If that happens while chunks are held, the browser
// plainly wants them, so they go in even without a `startstreaming`.

export function newFeed() {
  return { held: false }
}

export function feedAfter(feed, event) {
  if (event === 'endstreaming') return { held: true }
  if (event === 'startstreaming' || event === 'starved') return { held: false }
  return feed
}

// One turn of the pump that moves queued chunks into the source buffer.
//   append: append the next queued chunk
//   end:    the stream is over and everything is in, so end the source
//   wait:   nothing to do until the next chunk, update or event
// `ready`: a source buffer exists and isn't mid-update. `open`: the
// source's readyState is 'open'.
export function pumpStep({ stopped, ready, queued, held, ended, open }) {
  if (stopped || !ready) return 'wait'
  if (queued > 0) return held ? 'wait' : 'append'
  if (ended && open) return 'end'
  return 'wait'
}

// ---- audio the browser dropped ----
//
// A TimeRanges object (or anything with length, start(i) and end(i)) as
// plain [start, end] pairs.
export function rangesOf(timeRanges) {
  const out = []
  const n = timeRanges?.length || 0
  for (let i = 0; i < n; i++) out.push([timeRanges.start(i), timeRanges.end(i)])
  return out
}

// Slack for float timestamps, in seconds.
const EPS = 0.01

// Where the playhead should jump when it sits in a gap and audio waits
// further on: the start of the next buffered range. Null when the
// playhead is inside buffered audio, or when nothing lies ahead.
export function gapSkip(buffered, currentTime) {
  const t = currentTime || 0
  if (buffered.some(([s, e]) => t >= s - EPS && t < e - EPS)) return null
  const ahead = buffered.filter(([s]) => s > t).map(([s]) => s)
  return ahead.length ? Math.min(...ahead) : null
}

// The browser evicted audio from a managed source buffer.
//   unheard: some of what it dropped was still to be played, and that
//            stretch of the reply is lost. The player doesn't put it
//            back: MP3 carries no timestamps into a source buffer, so a
//            chunk appended again lands at the end, not in the gap.
//   seekTo:  the playhead now sits in a gap with audio further on, so it
//            jumps there. Null when playback can carry on as it is.
// Audio dropped behind the playhead has been heard, and nothing changes.
export function evictionPlan({ removed, buffered, currentTime }) {
  const t = currentTime || 0
  const unheard = removed.some(([, e]) => e > t + EPS)
  return { unheard, seekTo: unheard ? gapSkip(buffered, t) : null }
}

// The audio element ran out of audio at the playhead (`waiting`).
//   resume: chunks were held after an `endstreaming`, so append them now
//   seekTo: the playhead sits in a gap left by an eviction, so jump it
export function starvedPlan({ held, buffered, currentTime }) {
  return { resume: !!held, seekTo: gapSkip(buffered, currentTime) }
}

// ---- when the managed source doesn't work ----
//
// Nobody has run the managed path on a real iPhone yet. If the source
// never opens, won't take MP3, the element fails, or the stream ends
// with the element stuck and silent, all before a word is heard, the
// reply falls back to the held path and plays whole once its
// stream ends, the way every iPhone reply played before. Once any of the
// reply has been heard it never starts again from the top. A blocked
// autoplay (NotAllowedError) isn't the source's fault and would block the
// clip too, so it's reported as it always was. The desktop MediaSource
// path never falls back, so it behaves as it did.
//   cause: 'no-open' | 'no-buffer' | 'element-error' | 'stalled' | 'play-rejected'
export function fallsBack({ path, started, cause, errorName }) {
  if (path !== MANAGED || started) return false
  if (cause === 'play-rejected') return errorName !== 'NotAllowedError'
  return true
}
