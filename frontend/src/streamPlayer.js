import { heldReplyPlays, keepsAudio } from './replyCut.js'

function b64ToBytes(b64) {
  const bin = atob(b64)
  const bytes = new Uint8Array(bin.length)
  for (let i = 0; i < bin.length; i++) bytes[i] = bin.charCodeAt(i)
  return bytes
}

// Plays one reply's audio. Streams via MediaSource when the browser supports
// mp3 MSE; otherwise buffers the chunks and plays a single blob at the end.
// iPhone Safari has no MediaSource, so it always takes the second path.
//
// stop() cuts the reply off on either path, whenever it lands: before the
// reply's turn to play, while the held path waits for the stream to end,
// or mid-playback. A stopped player drops any audio still arriving and
// never plays (replyCut.js holds the rules).
export class StreamPlayer {
  // `sink` is a single shared, pre-unlocked <audio> element. iOS Safari unlocks
  // autoplay PER ELEMENT, so every reply must reuse the one element that was
  // unlocked during the start-button gesture - a fresh Audio() per reply would
  // be locked again and silently blocked.
  constructor(sink) {
    this.audio = sink || new Audio()
    this.chunks = []
    this.ended = false
    this.stopped = false
    this.useMse = typeof MediaSource !== 'undefined' && MediaSource.isTypeSupported('audio/mpeg')
    if (this.useMse) this.mediaSource = new MediaSource()
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
    this.audio.src = URL.createObjectURL(this.mediaSource)
    this.mediaSource.addEventListener('sourceopen', () => {
      if (this.stopped) return
      this.sb = this.mediaSource.addSourceBuffer('audio/mpeg')
      this.sb.addEventListener('updateend', () => this._pump())
      this._pump()
    })
  }

  push(b64) {
    if (!keepsAudio(this)) return // late audio for a cut-off reply
    this.chunks.push(b64ToBytes(b64))
    if (this.useMse) this._pump()
  }

  end() {
    this.ended = true
    this._settle()
    if (this.useMse) this._pump()
  }

  // Cut this reply off: whatever it holds is discarded, audio still on its
  // way is dropped, and a play() in progress or still waiting returns now.
  // `onStop` lets the owner close the reply's speech socket.
  stop() {
    if (this.stopped) return
    this.stopped = true
    this.chunks = []
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
    if (this.stopped || !this.sb || this.sb.updating) return
    if (this.chunks.length) {
      try { this.sb.appendBuffer(this.chunks.shift()) } catch { /* aborted */ }
    } else if (this.ended && this.mediaSource.readyState === 'open') {
      try { this.mediaSource.endOfStream() } catch { /* already closed */ }
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
    try { this.audio.pause() } catch { /* */ }
    if (this.useMse) {
      this._attach() // our turn: chunks buffered so far pump in on sourceopen
    } else {
      await this._settled
      if (!heldReplyPlays(this)) return
      this.audio.src = URL.createObjectURL(new Blob(this.chunks, { type: 'audio/mpeg' }))
    }
    const ourSrc = this.audio.src
    await new Promise((resolve) => {
      let done = false
      let watch = null
      const finish = () => { if (!done) { done = true; clearInterval(watch); resolve() } }
      // Fire onStart exactly once, when audio becomes AUDIBLE (the 'playing'
      // event, which follows buffering/decode) on OUR source: this is the true
      // playback-start the end-to-end latency trace needs, distinct from the
      // earlier play() invocation. Never load-bearing for playback itself.
      if (onStart) {
        this.audio.onplaying = () => {
          if (this.audio.src !== ourSrc) return
          this.audio.onplaying = null
          try { onStart() } catch { /* diagnostics must never disturb playback */ }
        }
      }
      this.audio.onended = finish
      // Only a REAL error on OUR source ends the turn - a stray event from
      // the src swap must not silently skip this speaker.
      this.audio.onerror = () => {
        if (this.audio.src === ourSrc && this.audio.error) finish()
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
        if (this.ended && t === lastT && (++stalls >= 2)) { finish(); return }
        if (t !== lastT) stalls = 0
        lastT = t
      }, 2000)
      this.audio.play().catch((err) => {
        onPlayFailed?.(err)  // every rejection speaks, not just autoplay blocks
        finish()
      })
    })
  }
}
