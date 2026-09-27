import { heldReplyPlays, keepsAudio } from './replyCut.js'
import { HELD, MANAGED, SOURCE_OPEN_MS, evictionPlan, fallsBack, feedAfter, newFeed,
         playbackPath, pumpStep, rangesOf, starvedPlan } from './streamFeed.js'

function b64ToBytes(b64) {
  const bin = atob(b64)
  const bytes = new Uint8Array(bin.length)
  for (let i = 0; i < bin.length; i++) bytes[i] = bin.charCodeAt(i)
  return bytes
}

// What a streamed play() returns when its source failed before a word was
// heard, so play() carries on down the held path.
const FELL_BACK = 'fell-back'

// Plays one reply's audio, on one of three paths (streamFeed.js decides
// which): MediaSource on desktop browsers, ManagedMediaSource on an
// iPhone with iOS 17.1 or later, and otherwise the held path, which keeps
// the chunks and plays a single blob at the end. The two streaming paths
// append each chunk as it arrives and start playing early. The managed
// one also honours the browser's `endstreaming` and `startstreaming`,
// steps over audio the browser evicted, and falls back to the held path
// if its source fails before a word is heard.
//
// stop() cuts the reply off on every path, whenever it lands: before the
// reply's turn to play, while the held path waits for the stream to end,
// or mid-playback. A stopped player drops any audio still arriving and
// never plays (replyCut.js holds the rules).
export class StreamPlayer {
  // `sink` is a single shared, pre-unlocked <audio> element. iOS Safari unlocks
  // autoplay PER ELEMENT, so every reply must reuse the one element that was
  // unlocked during the start-button gesture - a fresh Audio() per reply would
  // be locked again and silently blocked. `env` is the page's global scope,
  // where the player looks for MediaSource and ManagedMediaSource.
  constructor(sink, env = globalThis) {
    this.audio = sink || new Audio()
    this.chunks = []
    this.ended = false
    this.stopped = false
    this.path = playbackPath(env)
    if (this.path !== HELD) {
      const Source = this.path === MANAGED ? env.ManagedMediaSource : env.MediaSource
      this.mediaSource = new Source()
    }
    // The managed source's own wish to be fed or left alone (streamFeed.js).
    this.feed = newFeed()
    // The managed path keeps a copy of every chunk until the reply is
    // heard, so a source that fails before then can still play it whole.
    this._kept = this.path === MANAGED ? [] : null
    // The held path's wait: settles when the stream ends or the reply is
    // cut off, whichever comes first. The wait used to poll `ended` alone,
    // so a cut reply stayed "playing" until its stream ended.
    this._settled = new Promise((resolve) => { this._settle = resolve })
    // DO NOT touch this.audio here. The sink is shared by every reply in the
    // round, and players are constructed while the previous reply is still
    // playing - assigning audio.src now would hijack the element mid-speech
    // (speaker 2 goes silent, its play() never resolves, the chain jams and
    // interrupt dies with it). The sink becomes ours only inside play().
  }

  _attach() {
    const source = this.mediaSource
    if (this.path === MANAGED) {
      // Safari opens a managed source only once remote playback (AirPlay)
      // is off on the element. Set before the source is attached, so it
      // opens straight away.
      try {
        if (!this.audio.disableRemotePlayback) this.audio.disableRemotePlayback = true
      } catch { /* */ }
      source.addEventListener('startstreaming', () => this._feedEvent('startstreaming'))
      source.addEventListener('endstreaming', () => this._feedEvent('endstreaming'))
    }
    this.audio.src = URL.createObjectURL(source)
    source.addEventListener('sourceopen', () => {
      // A source that ended opens again if anything touches its buffer, and
      // it already has the one buffer it needs.
      if (this.stopped || this.path === HELD || this.sb) return
      this._opened = true
      try {
        this.sb = source.addSourceBuffer('audio/mpeg')
      } catch {
        this._fail?.('no-buffer')
        return
      }
      this.sb.addEventListener('updateend', () => this._pump())
      if (this.path === MANAGED) {
        this.sb.addEventListener('bufferedchange', (e) => this._bufferedChange(e))
      }
      this._pump()
    })
  }

  push(b64) {
    if (!keepsAudio(this)) return // late audio for a cut-off reply
    const bytes = b64ToBytes(b64)
    this.chunks.push(bytes)
    this._kept?.push(bytes)
    if (this.path !== HELD) this._pump()
  }

  end() {
    this.ended = true
    this._settle()
    if (this.path !== HELD) this._pump()
  }

  // Cut this reply off: whatever it holds is discarded, audio still on its
  // way is dropped, and a play() in progress or still waiting returns now.
  // `onStop` lets the owner close the reply's speech socket.
  stop() {
    if (this.stopped) return
    this.stopped = true
    this.chunks = []
    this._kept = null
    this._settle()
    this.stopNow?.()
    try { this.onStop?.() } catch { /* closing a socket must never block the cut */ }
  }

  // #460: the reply was a pass and nothing was sent to TTS. The play chain
  // skips an abandoned player, so it never claims the shared sink.
  abandon() {
    this.abandoned = true
    this.stop()
  }

  _pump() {
    const step = pumpStep({
      stopped: this.stopped || this.path === HELD,
      ready: !!this.sb && !this.sb.updating,
      queued: this.chunks.length,
      held: this.feed.held,
      ended: this.ended,
      open: this.mediaSource?.readyState === 'open',
    })
    if (step === 'append') {
      try { this.sb.appendBuffer(this.chunks.shift()) } catch { /* aborted */ }
    } else if (step === 'end') {
      try { this.mediaSource.endOfStream() } catch { /* already closed */ }
    }
  }

  // The managed source asked to stop or start being fed.
  _feedEvent(event) {
    if (this.stopped || this.path !== MANAGED) return
    const before = this.feed.held
    this.feed = feedAfter(this.feed, event)
    if (this.feed.held !== before) {
      this.log?.(this.feed.held ? 'tts:hold' : 'tts:resume', { event, queued: this.chunks.length })
    }
    this._pump()
  }

  // Whether the shared element still plays this reply's source.
  _ours() {
    return !!this._src && this.audio.src === this._src
  }

  _buffered() {
    try { return rangesOf(this.sb?.buffered) } catch { return [] } // detached
  }

  _seek(t) {
    try { this.audio.currentTime = t } catch { /* */ }
  }

  // The browser evicted some of this reply's audio (managed path only).
  _bufferedChange(e) {
    if (this.stopped || this.path !== MANAGED || !this._ours()) return
    const removed = rangesOf(e?.removedRanges)
    if (!removed.length) return
    const plan = evictionPlan({
      removed, buffered: this._buffered(), currentTime: this.audio.currentTime,
    })
    if (!plan.unheard) return // already heard
    this.log?.('tts:evicted', { at: this.audio.currentTime, seekTo: plan.seekTo })
    if (plan.seekTo !== null) this._seek(plan.seekTo)
  }

  // The element ran out of audio at the playhead (managed path only).
  _starved() {
    if (this.stopped || this.path !== MANAGED || !this._ours()) return
    const plan = starvedPlan({
      held: this.feed.held, buffered: this._buffered(), currentTime: this.audio.currentTime,
    })
    if (plan.resume) this._feedEvent('starved')
    if (plan.seekTo !== null) {
      this.log?.('tts:gap', { at: this.audio.currentTime, seekTo: plan.seekTo })
      this._seek(plan.seekTo)
    }
  }

  async play(onPlayFailed, onStart) {
    // Cut off before its turn came: it never claims the sink.
    if (this.stopped) return
    // Deterministic teardown of the previous reply's state on the shared
    // element BEFORE claiming it - stale ended/error events from the last
    // src must not leak into our turn and resolve it unplayed (the residual
    // race the legacy per-reply-element design was immune to).
    this.audio.onended = null
    this.audio.onerror = null
    this.audio.onplaying = null
    this.audio.onwaiting = null
    try { this.audio.pause() } catch { /* */ }
    if (this.path !== HELD) {
      this._attach() // our turn: chunks buffered so far pump in on sourceopen
      if (await this._playSource(onPlayFailed, onStart) !== FELL_BACK) return
    }
    await this._settled
    if (!heldReplyPlays(this)) return
    this.audio.src = URL.createObjectURL(new Blob(this.chunks, { type: 'audio/mpeg' }))
    await this._playSource(onPlayFailed, onStart)
  }

  // Play whatever source the element now holds, until it ends, fails, or
  // the reply is cut off.
  _playSource(onPlayFailed, onStart) {
    const ourSrc = this.audio.src
    this._src = ourSrc
    return new Promise((resolve) => {
      let done = false
      let fellBack = false
      let watch = null
      let opening = null
      const finish = (outcome) => {
        if (done) return
        done = true
        clearInterval(watch)
        clearTimeout(opening)
        this._fail = null
        resolve(outcome)
      }
      // The managed source failed before a word was heard: carry on down
      // the held path with every chunk so far (streamFeed.js fallsBack).
      const failed = (cause, errorName) => {
        if (done || !fallsBack({ path: this.path, started: this._started, cause, errorName })) {
          return false
        }
        fellBack = true
        this.log?.('tts:fallback', { cause, errorName: errorName || null })
        this.path = HELD
        this.chunks = this._kept || []
        this._kept = null
        if (this.audio.src === ourSrc) { try { this.audio.pause() } catch { /* */ } }
        finish(FELL_BACK)
        return true
      }
      this._fail = failed
      // Fire onStart exactly once, when audio becomes AUDIBLE (the 'playing'
      // event, which follows buffering/decode) on OUR source: this is the true
      // playback-start the end-to-end latency trace needs, distinct from the
      // earlier play() invocation. On the streaming paths that's as soon as
      // the first chunks decode, before the stream ends.
      this.audio.onplaying = () => {
        if (this.audio.src !== ourSrc) return
        this.audio.onplaying = null
        this._started = true
        this._kept = null // heard now, so it never falls back
        try { onStart?.() } catch { /* diagnostics must never disturb playback */ }
      }
      this.audio.onended = () => finish()
      // Only a REAL error on OUR source ends the turn - a stray event from
      // the src swap must not silently skip this speaker.
      this.audio.onerror = () => {
        if (this.audio.src === ourSrc && this.audio.error && !failed('element-error')) finish()
      }
      if (this.path === MANAGED) {
        this.audio.onwaiting = () => this._starved()
        if (!this._opened) {
          opening = setTimeout(() => { if (!this._opened) failed('no-open') }, SOURCE_OPEN_MS)
        }
      }
      // Pause only our own source: once this reply is done the sink may
      // already belong to the next one.
      this.stopNow = () => {
        if (this.audio.src === ourSrc) { try { this.audio.pause() } catch { /* */ } }
        finish()
      }
      // Watchdog: a wedged element (swallowed error, ended never firing) must
      // not jam the chain and kill interrupt with it. Stream done + no
      // playback progress across two checks = give up the turn.
      let lastT = -1, stalls = 0
      watch = setInterval(() => {
        if (this.audio.src !== ourSrc) { finish(); return }
        const t = this.audio.currentTime
        if (this.ended && t === lastT && (++stalls >= 2)) {
          if (!failed('stalled')) finish()
          return
        }
        if (t !== lastT) stalls = 0
        lastT = t
      }, 2000)
      this.audio.play().catch((err) => {
        // Our own fallback paused this source, which rejects its play().
        if (fellBack) return
        if (failed('play-rejected', err?.name)) return
        onPlayFailed?.(err)  // every rejection speaks, not just autoplay blocks
        finish()
      })
    })
  }
}
