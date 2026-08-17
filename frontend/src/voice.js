import { captureConstraints, captureProfileName } from './captureProfile.js'
import { speculativeStep } from './speculative.js'
import { playbackFailureMessage } from './voiceErrors.js'
import { couldBePass } from './voiceView.js'
import { gateEvent, gateRoundDone } from './voiceGate.js'
import { realtimeCommitAction, recoveryPlan, shouldReopenAfterClose } from './voiceRecovery.js'
import { HARD_MAX_TURN_MS, MAX_TURN_TOTAL_MS, shouldForceEndpoint,
         sttCommitTimeoutMs } from './turnPolicy.js'
import { shouldForceRoundDone } from './roundGuard.js'
import { newLedger, onCommit, onFinal, onSalvage, resetLedger } from './commitLedger.js'
import { VoiceTrace } from './voiceTrace.js'

// Hands-free voice session: open mic with VAD auto-send (no push-to-talk),
// streamed TTS playback per participant voice, and barge-in - speak over the
// models to cut them off. Voice detection requires sustained low-frequency
// (speech-shaped) energy, so keyboard clicks and other transients are ignored.

const DEFAULT_SILENCE_MS = 2000 // default pause that ends your turn in auto mode (user-tunable)
const MIN_SPEECH_MS = 500       // ignore anything shorter than this
const LEVEL = 0.013             // RMS floor while listening
const INTERRUPT_LEVEL = 0.03    // stricter floor while AI audio is playing
// Auto-calibration ceilings. The legacy app used the fixed floors above and
// never mis-heard; calibration may only harden them a LITTLE against a noisy
// room - an unbounded floor (the old 0.08 cap ≈ 6× base) read soft speech as
// silence: turns cut off mid-sentence and quiet speakers never opened one.
const LEVEL_CEIL = 0.035
const INTERRUPT_CEIL = 0.06
const CONFIRM_MS = 280          // sustained voicing needed to count as speech
const INTERRUPT_CONFIRM_MS = 500 // sustained voicing needed to barge in
const RECORDER_ROTATE_MS = 9000 // bound the pre-speech audio we keep

function b64ToBytes(b64) {
  const bin = atob(b64)
  const bytes = new Uint8Array(bin.length)
  for (let i = 0; i < bin.length; i++) bytes[i] = bin.charCodeAt(i)
  return bytes
}

function wsBase() {
  return `${location.protocol === 'https:' ? 'wss' : 'ws'}://${location.host}`
}

// Plays one reply's audio. Streams via MediaSource when the browser supports
// mp3 MSE; otherwise buffers the chunks and plays a single blob at the end.
class StreamPlayer {
  // `sink` is a single shared, pre-unlocked <audio> element. iOS Safari unlocks
  // autoplay PER ELEMENT, so every reply must reuse the one element that was
  // unlocked during the start-button gesture - a fresh Audio() per reply would
  // be locked again and silently blocked.
  constructor(sink) {
    this.audio = sink || new Audio()
    this.chunks = []
    this.ended = false
    this.useMse = typeof MediaSource !== 'undefined' && MediaSource.isTypeSupported('audio/mpeg')
    if (this.useMse) this.mediaSource = new MediaSource()
    // DO NOT touch this.audio here. The sink is shared by every reply in the
    // round, and players are constructed while the previous reply is still
    // playing - assigning audio.src now would hijack the element mid-speech
    // (speaker 2 goes silent, its play() never resolves, the chain jams and
    // interrupt dies with it). The sink becomes ours only inside play().
  }

  _attach() {
    this.audio.src = URL.createObjectURL(this.mediaSource)
    this.mediaSource.addEventListener('sourceopen', () => {
      this.sb = this.mediaSource.addSourceBuffer('audio/mpeg')
      this.sb.addEventListener('updateend', () => this._pump())
      this._pump()
    })
  }

  push(b64) {
    this.chunks.push(b64ToBytes(b64))
    if (this.useMse) this._pump()
  }

  end() {
    this.ended = true
    if (this.useMse) this._pump()
  }

  _pump() {
    if (!this.sb || this.sb.updating) return
    if (this.chunks.length) {
      try { this.sb.appendBuffer(this.chunks.shift()) } catch { /* aborted */ }
    } else if (this.ended && this.mediaSource.readyState === 'open') {
      try { this.mediaSource.endOfStream() } catch { /* already closed */ }
    }
  }

  async play(onPlayFailed, onStart) {
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
      while (!this.ended) await new Promise((r) => setTimeout(r, 100))
      if (!this.chunks.length) return
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
      this.stopNow = () => { try { this.audio.pause() } catch { /* */ } finish() }
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

// ~60ms of silence - played within a user gesture to unlock <audio> autoplay
// for the session (browsers block audio until the user interacts with the page).
const SILENT_WAV = 'data:audio/wav;base64,UklGRuQDAABXQVZFZm10IBAAAAABAAEAQB8AAIA+AAACABAAZGF0YcADAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA='

export default class VoiceController {
  constructor({ getChatId, getParticipants, sendText, onState, onError, onInterruptRound, onPartial, onSttFallback, onLevel, onSpeaker, onHeld }) {
    this.getChatId = getChatId
    this.getParticipants = getParticipants
    this.sendText = sendText
    this.onState = onState
    this.onSpeaker = onSpeaker  // which participant's AUDIO is currently playing (or null)
    this.onError = onError
    this.onLevel = onLevel
    this.onInterruptRound = onInterruptRound
    this.onPartial = onPartial            // live preview of the in-progress transcript (optional)
    this.onSttFallback = onSttFallback     // realtime gave up → UI should reflect batch mode
    // One shared audio sink, unlocked once in the start gesture and reused by
    // every reply - required for iOS Safari's per-element autoplay policy.
    this.sink = new Audio()
    this.sink.setAttribute('playsinline', '')
    this.playbackRate = 1  // session speech speed (dock slider); pitch preserved
    this.active = false
    this.players = new Map()
    this.sockets = new Map()
    this.playChain = Promise.resolve()
    this.playing = 0
    this.roundActive = false
    this.dropQueue = false
    // Round liveness clock for the wedged-gate escape hatch (roundGuard.js):
    // stamped on EVERY incoming round event, read by the VAD's gated branch.
    this._lastRoundEventAt = 0
    this.manualMode = false // push-to-talk: pauses don't send; user ends the turn
    this.silenceMs = DEFAULT_SILENCE_MS // auto mode: how long a pause before the turn ends
    this.voicedHistory = [] // {t, voiced}
    // Auto-calibrated VAD floors: seeded from the constants, then raised above
    // the room's ambient noise (EMA of non-voiced frames) - a TV in the
    // background no longer trips turn detection. No user knob.
    this.levelFloor = LEVEL
    this.interruptFloor = INTERRUPT_LEVEL
    this._ambient = 0
    // --- realtime STT (opt-in, parallel to the batch /stt POST) ---
    this.sttRealtime = true   // realtime by default; batch is the automatic fallback
    // Room mode (#28 phase 1): per-session, default OFF (App resets it on
    // every voice start). ON tells the server relay to tee utterance audio
    // into a parallel diarization pass - a second transcription of the same
    // audio, which is why the toggle's copy warns that voice minutes roughly
    // double. Entirely server-side work: nothing about capture, VAD, commit
    // timing or playback changes here whichever way this flag points.
    this.roomMode = false
    this.sttWs = null
    this.sttProc = null
    this.sttStreaming = false // true while inside a user utterance (sending PCM)
    this._utterFrames = 0     // PCM frames actually SENT this utterance
    this._pcmPre = []         // small pre-onset PCM ring so the start isn't clipped
    // #85/#104: per-commit only-one-wins accounting (replaces a per-
    // instance drop boolean the next turn's commit could reset mid-race),
    // plus the buffered text of a capped-but-continuing logical turn.
    this._ledger = newLedger()
    this._turnBuffer = []
    this._logicalStart = 0    // when the LOGICAL turn began (survives caps)
    this.muted = false        // hard mute: mic track disabled, nothing detected/sent
    // Hold-and-retry: utterances whose /stt POST failed on NETWORK (a dead
    // zone) wait here as audio blobs and retry until the tunnel returns -
    // nothing you said is ever silently dropped.
    this.onHeld = onHeld
    this.heldUtterances = []
    this._heldTimer = null
    // Per-turn latency trace: records content-free stage timings across
    // this turn's timeline and POSTs them for durable diagnostics. Best-effort
    // and fully isolated - it can never disturb capture, barge-in, or playback.
    this._trace = new VoiceTrace({
      now: () => (typeof performance !== 'undefined' && performance.now
        ? performance.now() : Date.now()),
      post: (payload) => this._postTrace(payload),
      getChatId: () => this.getChatId(),
    })
  }

  // Diagnostics-only, content-free: timestamped console logging for the
  // turn-handoff investigation (issue: voice turn-handoff stuck in
  // listening / false "2 microphones live"). Never disturbs capture,
  // barge-in, or playback - console.debug only, no network, no state
  // read-back. Safe to leave on by default; the payload is small and
  // never includes transcript text.
  _vlog(tag, data) {
    const t = (typeof performance !== 'undefined' && performance.now
      ? performance.now() : Date.now()).toFixed(1)
    console.debug(`[voice] ${t}ms ${tag}`, data || '')
  }

  // Provider/model/voice labels for a speaker, for per-model trace segmentation.
  _traceMeta(slug) {
    const p = this.getParticipants?.().find((x) => x.slug === slug)
    return { provider: p?.provider || '', model: p?.model || '', tts_provider: 'elevenlabs' }
  }

  _postTrace(payload) {
    try {
      fetch('/api/voice/trace', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify(payload),
        keepalive: true,
      }).catch(() => {})
    } catch { /* diagnostics are never load-bearing */ }
  }

  // Real mute: disable the mic track at the source (a disabled track emits
  // silence, so nothing is detected, recorded, or streamed) and freeze the VAD.
  // The models keep talking; the app just stops listening until you unmute.
  setMuted(on) {
    this.muted = !!on
    if (this.muted) {
      if (this.speechStart) {
        // Muting right after speaking is phone-call muscle memory for "my
        // turn is done" - finalize and SEND the captured words before the
        // mic goes dark. (Road-tested the hard way: tapping mute inside the
        // 2s silence window silently killed the utterance. Words spoken are
        // never discarded; _finalizeUtterance still drops sub-500ms blips.)
        this.finalizeNow()
      } else {
        // nothing captured - just clear pre-onset state so unmute starts
        // clean. (No drop flag needed: a final for a commit the ledger
        // never saw simply does not send.)
        this.voicedHistory = []
      }
      this.onLevel?.(0)
    }
    this.stream?.getAudioTracks().forEach((t) => { t.enabled = !this.muted })
    return this.muted
  }

  setManualMode(on) { this.manualMode = !!on }

  // Room mode toggle: remember the choice for (re)opened STT sockets and tell
  // a live one via a control frame (no audio key, so the server treats it as
  // ours alone and sends nothing upstream). Works mid-session both ways.
  // The capture experiment (#28 phase 4) rides the same edge: room mode
  // re-asks the live mic track for the room profile (noise suppression and
  // auto gain off, so the quieter second voice stops being "cleaned" away),
  // solo mode re-asks for the tuned solo profile. Best-effort by design - a
  // browser that refuses applyConstraints keeps the profile it has, and
  // capture never breaks over it.
  setRoomMode(on) {
    on = !!on
    if (on === this.roomMode) return
    this.roomMode = on
    this.stream?.getAudioTracks().forEach((t) => {
      try { t.applyConstraints?.(captureConstraints(on))?.catch(() => {}) } catch { /* */ }
    })
    if (this.sttWs && this.sttWs.readyState === WebSocket.OPEN) {
      this._sttSend({ room_mode: on, capture_profile: captureProfileName(on) })
    }
  }
  setSilenceMs(ms) { this.silenceMs = Math.max(400, Math.min(6000, ms || DEFAULT_SILENCE_MS)) }

  setPlaybackRate(r) {
    this.playbackRate = Math.max(0.8, Math.min(2, r || 1))
    // apply live so a mid-speech change is heard immediately
    this.sink.defaultPlaybackRate = this.playbackRate
    this.sink.playbackRate = this.playbackRate
  }

  // Realtime STT: stream PCM to Scribe v2 Realtime over a persistent socket instead
  // of recording an utterance and POSTing it. Opt-in; falls back to the batch path on
  // any failure so a hiccup never breaks a live session.
  setSttRealtime(on) {
    on = !!on
    if (on === this.sttRealtime) return
    this.sttRealtime = on
    if (!this.active) return  // otherwise applied on next start()
    if (on) this._openSttStream()
    else this._closeSttStream()
  }

  _openSttStream() {
    if (this.sttWs || !this.audioCtx || !this.micSource) return
    const ws = new WebSocket(`${wsBase()}/api/voice/stt-stream`)
    ws.onopen = () => {
      try {
        ws.send(JSON.stringify({ chat_id: this.getChatId(), sample_rate: 16000,
                                 room_mode: this.roomMode,
                                 // Which mic profile this session captures
                                 // with (#28 phase 4) - the relay logs it
                                 // content-free for field comparisons.
                                 capture_profile: captureProfileName(this.roomMode) }))
      } catch { /* */ }
    }
    ws.onmessage = (e) => {
      let msg; try { msg = JSON.parse(e.data) } catch { return }
      if (msg.session) {
        // #134: our capture-session id, so the every-surface mic banner
        // can tell this session apart from an orphan's. Log the sid
        // hand-off so a reconnect that leaves the OLD sid registered
        // server-side (Bug B: false "2 microphones live") can be
        // correlated against backend/routers/voice.py's sid lifetime log.
        this._vlog('stt:sid', { previousSid: this.captureSid || null, sid: msg.session })
        this.captureSid = msg.session
        return
      }
      if (msg.partial !== undefined) {
        this.onPartial?.(msg.partial)
      } else if (msg.final !== undefined) {
        clearTimeout(this._sttCommitTimer)
        const text = (msg.final || '').trim()
        // #85: the relay stamps each final with its commit's turn id; the
        // ledger decides whether this final still owns its commit. A final
        // whose commit was already salvaged - or that names no commit we
        // know - drops here, which is exactly the doubled-turn case.
        const win = onFinal(this._ledger, msg.turn_id)
        if (win && text) {
          this._deliverTranscript(text, win.turnId, win.dispatch)
        }
        // The turn is transcribed; if its round is already generating, say
        // so. 'listening' here rendered the whole generation wait as
        // "Listening…", inviting speech the gated mic then discarded.
        if (this.active && this.state === 'transcribing') {
          this._state(this.roundActive ? 'working' : 'listening')
        }
      } else if (msg.error) {
        this.onError?.(`Realtime STT: ${msg.error}`)
        this._fallbackToBatch(`relay error: ${msg.error}`)
      }
    }
    ws.onerror = () => this._fallbackToBatch('websocket error')
    ws.onclose = (e) => {
      this._vlog('stt:close', { sid: this.captureSid || null, code: e?.code, reason: e?.reason,
                                ourClose: !!this._sttClosing })
      this.sttWs = null
      if (e && e.code === 4001) {
        // #134: the owner ended this capture from another surface. This
        // is a DELIBERATE stop, not a drop: full teardown - microphone
        // tracks stopped - and never a reopen.
        this.stop()
        this.onError?.('Voice was ended from another window.')
        return
      }
      // A socket we did NOT close is a dropped connection - a backgrounded tab
      // is the common cause. Left alone, the ScriptProcessor keeps running and
      // feeding a socket that no longer exists: the mic works, the context
      // runs, and transcripts simply never arrive. Errors are not
      // handled here - those fall back to the batch recorder, which is the
      // right answer for a failing service rather than a sleeping tab.
      if (shouldReopenAfterClose({
        active: this.active, sttRealtime: this.sttRealtime,
        sttClosing: this._sttClosing,
      })) {
        if (this.sttProc) { try { this.sttProc.disconnect() } catch { /* */ } this.sttProc = null }
        // Only reopen while visible - a hidden tab would just lose it again,
        // and _recover() reopens on return anyway.
        if (typeof document === 'undefined' || !document.hidden) {
          clearTimeout(this._sttReopenTimer)
          this._sttReopenTimer = setTimeout(() => this._openSttStream(), 500)
        }
      }
    }
    this.sttWs = ws
    this._attachSttProcessor()
  }

  // Tap the same mic source the VAD uses; the node's output is silent (never
  // written). Split out so a dead processor can be rebuilt in place.
  _attachSttProcessor() {
    const proc = this.audioCtx.createScriptProcessor(4096, 1, 1)
    proc.onaudioprocess = (ev) => {
      if (!this.sttRealtime || this.muted) return
      const chunk = this._pcm16Base64(ev.inputBuffer.getChannelData(0), this.audioCtx.sampleRate)
      if (!chunk) return
      if (this.sttStreaming) {
        this._sttSend({ audio: chunk })
        this._utterFrames++  // proof the capture graph is alive
      } else {
        this._pcmPre.push(chunk)
        if (this._pcmPre.length > 8) this._pcmPre.shift()  // ~0.5s pre-roll cap
      }
    }
    try { this.micSource.connect(proc); proc.connect(this.audioCtx.destination) } catch { /* */ }
    this.sttProc = proc
  }

  // The in-foreground cousin of the return-to-foreground recovery: iOS can
  // kill the ScriptProcessor across a playback route change while the analyser
  // (and so the VAD) keeps reading, so the turn opens and commits but zero
  // audio flows. Resume a suspended context, then reattach a fresh processor
  // to the same mic source.
  _rebuildSttProcessor() {
    if (this.audioCtx?.state === 'suspended') this.audioCtx.resume?.().catch(() => { /* */ })
    if (this.sttProc) { try { this.sttProc.disconnect() } catch { /* */ } this.sttProc = null }
    if (this.sttWs && this.audioCtx && this.micSource) this._attachSttProcessor()
  }

  _closeSttStream() {
    this.sttStreaming = false
    this._pcmPre = []
    resetLedger(this._ledger)
    clearTimeout(this._sttCommitTimer)
    clearTimeout(this._sttReopenTimer)
    if (this.sttProc) { try { this.sttProc.disconnect() } catch { /* */ } this.sttProc = null }
    if (this.sttWs) {
      // Mark the close as OURS across the async onclose, so the reopen path
      // can tell "we ended this" from "the connection dropped".
      this._sttClosing = true
      try { this.sttWs.send(JSON.stringify({ done: true })) } catch { /* */ }
      try { this.sttWs.close() } catch { /* */ }
      this.sttWs = null
      setTimeout(() => { this._sttClosing = false }, 0)
    }
  }

  // A realtime failure must never break a live session - drop to the batch
  // recorder. #21: the cause travels with the fallback - the banner used to
  // say only that realtime was unavailable, leaving nothing to debug with.
  _fallbackToBatch(cause = 'unknown') {
    if (!this.sttRealtime) return
    this.sttRealtime = false
    this.sttFallbackCause = cause
    console.warn('[voice] realtime STT fell back to batch:', cause)
    this._closeSttStream()
    if (this.active && this.state === 'transcribing') {
      this._state(this.roundActive ? 'working' : 'listening')
    }
    this.onSttFallback?.(cause)
  }

  _sttSend(obj) {
    const ws = this.sttWs
    if (ws && ws.readyState === WebSocket.OPEN) {
      try { ws.send(JSON.stringify({ sample_rate: 16000, ...obj })) } catch { /* */ }
    }
  }

  // Begin streaming this utterance - flush the short pre-onset buffer first so the
  // ~280ms the VAD took to confirm speech isn't clipped.
  _sttStartStreaming() {
    // A fresh utterance starts its frame count at zero even when streaming
    // was already on (the barge-in interjection path); the count answers
    // "did THIS utterance's audio flow", so pre-roll is deliberately not
    // counted.
    this._utterFrames = 0
    this._specFired = false  // a fresh utterance re-arms the silence hint
    if (!this.sttRealtime || !this.sttWs || this.sttStreaming) return
    this.sttStreaming = true
    for (const c of this._pcmPre) this._sttSend({ audio: c })
    this._pcmPre = []
  }

  // Downsample Float32 mic audio to 16 kHz, encode PCM16 mono, base64.
  _pcm16Base64(input, srcRate) {
    const ratio = srcRate / 16000
    const outLen = Math.floor(input.length / ratio)
    if (outLen <= 0) return null
    const pcm = new Int16Array(outLen)
    for (let i = 0; i < outLen; i++) {
      const s = Math.max(-1, Math.min(1, input[Math.floor(i * ratio)] || 0))
      pcm[i] = s < 0 ? s * 0x8000 : s * 0x7fff
    }
    const bytes = new Uint8Array(pcm.buffer)
    let bin = ''
    for (let i = 0; i < bytes.length; i++) bin += String.fromCharCode(bytes[i])
    return btoa(bin)
  }

  // Push-to-talk: end the current turn now, regardless of pauses. No-op if the
  // user hasn't actually said anything yet (avoids sending silence).
  finalizeNow() {
    if (this.speechStart) {
      this.lastVoice = Date.now() // count trailing speech toward the turn
      this._finalizeUtterance()
    }
  }

  _state(s) {
    if (s !== this.state) this._vlog('state', { from: this.state, to: s })
    this.state = s
    this.onState?.(s)
  }

  async start() {
    this._vlog('session:start', { roomMode: this.roomMode, staleRoundActive: this.roundActive })
    // A fresh session starts with a clean gate. The controller outlives
    // stop()/start() (App builds it once), so a gate wedged true by a dead
    // round in the PREVIOUS session would pin this one to barge-in-only
    // from its first tick - End voice + start again used to inherit the
    // wedge, and only a page reload cleared it.
    this.roundActive = false
    this.dropQueue = false
    this._lastRoundEventAt = 0
    // Unlock audio while we still hold the start-button gesture, and on any
    // later click - so a session that auto-resumes on reload (no gesture) isn't
    // left permanently muted with no way to recover. Makes the "click anywhere"
    // banner actually do something.
    this._primeAudio()
    this._unlockHandler = () => { this._primeAudio(); this._resumeCtx() }
    window.addEventListener('pointerdown', this._unlockHandler)
    // Capture profile (#28 phase 4): solo sessions ask for exactly what they
    // always did; a session starting IN room mode asks with noise
    // suppression and auto gain off, so single-voice tuning cannot muffle
    // the second speaker. The decision table lives in captureProfile.js.
    this.stream = await navigator.mediaDevices.getUserMedia({
      audio: captureConstraints(this.roomMode),
    })
    this.audioCtx = new AudioContext()
    // Resume the context we just built. _primeAudio ran BEFORE it existed (it
    // has to - the sink unlock needs the start gesture), so without this the
    // new context keeps whatever state it was born in. On mobile that is
    // 'suspended', and a suspended context means no VAD, no STT capture and no
    // playback for the whole session.
    this._resumeCtx()
    const src = this.audioCtx.createMediaStreamSource(this.stream)
    this.micSource = src
    this.analyser = this.audioCtx.createAnalyser()
    this.analyser.fftSize = 2048
    src.connect(this.analyser)
    this.active = true
    this._startRecorder()
    if (this.sttRealtime) this._openSttStream()
    // Repair the session when the tab comes back: a backgrounded tab gets its
    // AudioContext suspended and can lose its websockets, and nothing else
    // notices.
    this._visibilityHandler = () => this._recover()
    document.addEventListener('visibilitychange', this._visibilityHandler)
    window.addEventListener('pageshow', this._visibilityHandler)
    this._vadLoop()
    this._state('listening')
  }

  _primeAudio() {
    if (this._audioUnlocked) return
    try {
      // Unlock the SHARED sink element (not a throwaway) so every later reply,
      // which reuses this same element, inherits the unlock on iOS.
      this.sink.src = SILENT_WAV
      this.sink.play().then(() => {
        this._audioUnlocked = true
        if (this._unlockHandler) {
          window.removeEventListener('pointerdown', this._unlockHandler)
          this._unlockHandler = null
        }
      }).catch(() => { /* not granted yet - a later real click retries */ })
    } catch { /* */ }
  }

  // Resume a suspended AudioContext. Safe to call at any time: a running
  // context is a no-op and a closed one rejects into the catch (recovering a
  // closed context is a restart, not a resume).
  _resumeCtx() {
    if (this.audioCtx?.state === 'suspended') {
      this.audioCtx.resume?.().catch(() => {})
    }
  }

  // Foreground/bfcache return: repair whatever the browser took away while we
  // weren't on screen. What needs repairing is decided by voiceRecovery.js so
  // the rule is unit-tested rather than inferred from browser behaviour.
  _recover() {
    const plan = recoveryPlan({
      active: this.active,
      visible: typeof document === 'undefined' || !document.hidden,
      ctxState: this.audioCtx?.state,
      sttRealtime: this.sttRealtime,
      sttOpen: !!this.sttWs,
      sttClosing: this._sttClosing,
    })
    if (plan.resumeContext) this._resumeCtx()
    if (plan.reopenStt) {
      // Drop the orphaned processor first: it is still wired to the mic and
      // still firing into a socket that no longer exists.
      if (this.sttProc) { try { this.sttProc.disconnect() } catch { /* */ } this.sttProc = null }
      this._openSttStream()
    }
  }

  stop() {
    this._vlog('session:stop', { state: this.state, roundActive: this.roundActive, captureSid: this.captureSid })
    this.active = false
    if (this._unlockHandler) {
      window.removeEventListener('pointerdown', this._unlockHandler)
      this._unlockHandler = null
    }
    if (this._visibilityHandler) {
      document.removeEventListener('visibilitychange', this._visibilityHandler)
      window.removeEventListener('pageshow', this._visibilityHandler)
      this._visibilityHandler = null
    }
    // The unlock belongs to the context and sink of THIS session. Leaving it
    // latched made the next start() skip priming entirely and run on a context
    // nothing ever resumed: the reason toggling voice off and on never
    // recovered dictation.
    this._audioUnlocked = false
    clearTimeout(this._heldTimer)
    this._heldTimer = null
    if (this.heldUtterances.length) {
      this.onError?.(`${this.heldUtterances.length} held message(s) discarded - the call ended before the connection returned.`)
    }
    this.heldUtterances = []
    this.onHeld?.(0)
    try { this.recorder?.state !== 'inactive' && this.recorder?.stop() } catch { /* */ }
    this._closeSttStream()
    this.stream?.getTracks().forEach((t) => t.stop())
    this.audioCtx?.close().catch(() => {})
    // Drop the references too: a closed context can never be resumed, and a
    // stale one here would make _resumeCtx and _openSttStream look satisfiable.
    this.audioCtx = null
    this.micSource = null
    this.analyser = null
    for (const s of this.sockets.values()) try { s.ws.close() } catch { /* */ }
    this.sockets.clear()
    for (const p of this.players.values()) p.stopNow?.()
    this.players.clear()
    this._state('off')
  }

  // Cut the models off: silence audio, drop queued speech, abort generation.
  // Triggered by sustained user speech during playback, or the ◼ button.
  interrupt() {
    this.dropQueue = true
    for (const p of this.players.values()) p.stopNow?.()
    this.onInterruptRound?.()
  }

  // ---- microphone / VAD ----

  _startRecorder() {
    if (!this.stream) return
    // Stop the previous recorder first. Otherwise a rotation during silence
    // leaves the old recorder running, and its late dataavailable chunks land
    // in the new recChunks array - interleaving two streams into one webm with
    // a misplaced/missing header. ElevenLabs then rejects it as invalid_audio
    // (surfaced as a 502 on /stt) and the utterance is silently lost.
    if (this.recorder && this.recorder.state !== 'inactive') {
      this.recorder.ondataavailable = null
      try { this.recorder.stop() } catch { /* */ }
    }
    this.recChunks = []
    const mime = MediaRecorder.isTypeSupported('audio/webm;codecs=opus')
      ? 'audio/webm;codecs=opus'
      : (MediaRecorder.isTypeSupported('audio/mp4') ? 'audio/mp4' : '')
    this.recorder = new MediaRecorder(this.stream, mime ? { mimeType: mime } : undefined)
    this.recorder.ondataavailable = (e) => { if (e.data.size) this.recChunks.push(e.data) }
    this.recorder.start(250)
    this.recStarted = Date.now()
    this.speechStart = 0
    this.lastVoice = 0
  }

  // A frame counts as voiced only if it is loud enough AND speech-shaped:
  // energy concentrated below ~1.2kHz rather than the broadband/high-frequency
  // signature of keyboard clicks and taps.
  _frameVoiced(timeBuf, freqBuf) {
    this.analyser.getFloatTimeDomainData(timeBuf)
    let sum = 0
    for (let i = 0; i < timeBuf.length; i++) sum += timeBuf[i] * timeBuf[i]
    const rms = Math.sqrt(sum / timeBuf.length)
    this.lastRms = rms // live level for the UI's mic-reactive indicator
    let floor = this.playing > 0 ? this.interruptFloor : this.levelFloor
    // Hysteresis: OPENING a turn needs the full floor, but a turn already in
    // progress holds at 70% - trailing-off sentence ends must not read as the
    // 2s silence that cuts the speaker off mid-thought.
    if (this.speechStart && this.playing === 0) floor *= 0.7
    if (rms < floor) return false
    this.analyser.getByteFrequencyData(freqBuf)
    const binHz = (this.audioCtx.sampleRate / 2) / freqBuf.length
    const idx = (hz) => Math.min(freqBuf.length - 1, Math.round(hz / binHz))
    let low = 0
    for (let i = idx(80); i <= idx(1200); i++) low += freqBuf[i]
    let high = 0
    for (let i = idx(3500); i <= idx(10000); i++) high += freqBuf[i]
    const lowAvg = low / (idx(1200) - idx(80) + 1)
    const highAvg = high / (idx(10000) - idx(3500) + 1)
    return lowAvg > highAvg * 1.6
  }

  // sustained voicing: ≥60% of frames voiced across the last `needMs`
  _confirmedSpeech(now, needMs) {
    const windowStart = now - needMs
    const frames = this.voicedHistory.filter((f) => f.t >= windowStart)
    if (frames.length < 6 || frames[0].t > windowStart + 80) return false
    const voiced = frames.filter((f) => f.voiced).length
    return voiced / frames.length >= 0.6
  }

  _vadLoop() {
    const timeBuf = new Float32Array(this.analyser.fftSize)
    const freqBuf = new Uint8Array(this.analyser.frequencyBinCount)
    const tick = () => {
      if (!this.active) return
      requestAnimationFrame(tick)
      if (this.muted) { this.onLevel?.(0); return }  // hard mute: don't listen
      if (this.state === 'transcribing') {
        this.onLevel?.(0)
        return
      }
      const now = Date.now()
      const voiced = this._frameVoiced(timeBuf, freqBuf)
      // ~20Hz live mic level to the UI (0..1), so "Listening" visibly reacts
      if (!this._lastLevelAt || now - this._lastLevelAt > 50) {
        this._lastLevelAt = now
        this.onLevel?.(Math.min(1, (this.lastRms || 0) / 0.1))
      }
      // auto-calibration: non-voiced frames feed an ambient-noise EMA; the
      // floors ride above it (3× ambient, clamped) so a noisy room (TV on)
      // stops tripping turn detection while a quiet room keeps full sensitivity.
      // ONLY while the room is actually quiet: during playback the "ambient"
      // the mic hears is the models' own audio bleeding past echo cancellation
      // - learning from it ratchets interruptFloor up until human speech can't
      // cross it and barge-in dies (the legacy app's fixed floor never drifted).
      if (!voiced && this.playing === 0 && !this.roundActive) {
        const r = this.lastRms || 0
        // adapt DOWN fast, UP slowly: a floor inflated by a passing noise must
        // recover in about a second, not linger and swallow the next turn
        const w = this._ambient && r < this._ambient ? 0.10 : 0.02
        this._ambient = this._ambient ? this._ambient * (1 - w) + r * w : r
        this.levelFloor = Math.min(LEVEL_CEIL, Math.max(LEVEL, this._ambient * 3))
        this.interruptFloor = Math.min(INTERRUPT_CEIL,
                                       Math.max(INTERRUPT_LEVEL, this.levelFloor * 2.3))
      }
      this.voicedHistory.push({ t: now, voiced })
      while (this.voicedHistory.length && this.voicedHistory[0].t < now - 1600) {
        this.voicedHistory.shift()
      }

      // Diagnostics-only: which side of this gate the loop is on, logged only
      // on the edge (not every tick, or this would spam at ~60Hz). This is
      // the exact fork the turn-handoff investigation is watching - a
      // `roundActive` that never flips back to false pins the loop on the
      // 'gated' (barge-in-only) side forever, which is Bug A's mechanism.
      const gated = this.playing > 0 || this.roundActive
      if (gated !== this._lastVadGated) {
        this._lastVadGated = gated
        this._vlog('vad:branch', { gated, playing: this.playing, roundActive: this.roundActive })
      }

      if (gated) {
        // Wedged-gate escape hatch: a round silent for the roundGuard bound,
        // with nothing playing, is treated as gone. A half-open stream never
        // errors, so its onRoundDone() never runs and nothing else can clear
        // the gate - the session stays pinned to barge-in-only forever.
        // Force the round-done path (gate cleared, trace flushed, state
        // honest) and let normal listening resume next tick.
        if (shouldForceRoundDone({ roundActive: this.roundActive, playing: this.playing,
                                   idleMs: now - (this._lastRoundEventAt || now) })) {
          const idleMs = Math.round(now - this._lastRoundEventAt)
          console.warn(`[voice] round guard: no round events for ${idleMs}ms `
            + 'with nothing playing - forcing round-done')
          this._vlog('gate:forceRoundDone', { idleMs })
          this.onRoundDone()
          return
        }
        // models are talking/generating - listen only for a barge-in
        if (this._confirmedSpeech(now, INTERRUPT_CONFIRM_MS)) {
          this.speechStart = now - INTERRUPT_CONFIRM_MS
          if (!this._turnBuffer.length) this._logicalStart = this.speechStart
          this.lastVoice = now
          this._sttStartStreaming()
          this._vlog('vad:bargeIn', { playing: this.playing, roundActive: this.roundActive })
          this.interrupt() // capture continues; finalize sends the interjection
        } else if (!this.speechStart && now - this.recStarted > RECORDER_ROTATE_MS) {
          this._startRecorder() // bound stale audio while they monologue
        }
        return
      }

      if (!this.speechStart) {
        // Manual mode opens the turn on the first voiced frame so nothing is
        // clipped; auto mode waits for sustained speech to avoid false starts.
        if (this.manualMode ? voiced : this._confirmedSpeech(now, CONFIRM_MS)) {
          this.speechStart = this.manualMode ? now : now - CONFIRM_MS
          // A fresh logical turn starts here - unless capped segments are
          // already buffered, in which case this is the same turn resuming
          // and the logical clock keeps its original start (#104).
          if (!this._turnBuffer.length) this._logicalStart = this.speechStart
          this.lastVoice = now
          this._sttStartStreaming()
        } else if (now - this.recStarted > RECORDER_ROTATE_MS) {
          this._startRecorder()
        }
      } else {
        if (voiced) this.lastVoice = now
        // Speculative identity (#28 PR-B): at silence-start, send a
        // content-free hint frame so the server can run the LOCAL identity
        // check on the buffered utterance while the silence window is still
        // counting down. The rule lives in speculative.js (pure,
        // unit-tested); the frame carries no audio and the relay forwards
        // nothing upstream for it.
        const spec = speculativeStep(this._specFired, {
          inUtterance: true,
          streaming: this.sttStreaming && !!this.sttWs,
          voiced,
          silenceMs: now - this.lastVoice,
        })
        this._specFired = spec.fired
        if (spec.fire) this._sttSend({ speculative: true })
        // Auto mode sends after a pause; manual mode waits for finalizeNow().
        if (!this.manualMode && now - this.lastVoice > this.silenceMs) {
          this._vlog('vad:finalize', { cause: 'gap', silenceMs: now - this.lastVoice })
          this._finalizeUtterance('gap')
        } else if (!this.manualMode &&
                   shouldForceEndpoint({ turnMs: now - this.speechStart, voiced })) {
          // #60: sustained background noise (road/wind/fan) can keep frames
          // reading voiced, or keep RMS above the calibrated floor, so the
          // silence gap above never opens and the turn would listen forever.
          // Bounded fallback - but since #104 a cap ends only the SEGMENT:
          // the audio commits, the text buffers, and the logical turn keeps
          // going until a real gap. The total bound is the #60 guarantee's
          // new home: past it, even a zero-gap noise wall sends.
          const total = now - (this._logicalStart || this.speechStart)
          const cause = total >= MAX_TURN_TOTAL_MS ? 'gap' : 'cap'
          this._vlog('vad:finalize', { cause, forced: true, turnMs: now - this.speechStart, total })
          this._finalizeUtterance(cause)
        }
      }
    }
    requestAnimationFrame(tick)
  }

  async _finalizeUtterance(cause = 'gap') {
    const speechMs = this.lastVoice - this.speechStart
    this._vlog('finalizeUtterance', { cause, speechMs, roundActive: this.roundActive, playing: this.playing })
    // #104: a cap ends the SEGMENT, not the turn - text buffers and the
    // round waits for a real gap. 'gap' (or the total bound, mapped to gap
    // by the caller) is the only real end.
    const continuation = cause === 'cap'
    // The user actually stopped talking at lastVoice; in auto mode this
    // method only runs after silenceMs more of quiet (manual mode stamps
    // lastVoice at the tap, so the gap is ~0). Backdate speech_end by that
    // gap or every turn reads ~silenceMs faster than perceived.
    const endedAgoMs = Math.max(0, Date.now() - this.lastVoice)
    this.speechStart = 0
    this.lastVoice = 0
    // End-of-speech: open a fresh latency trace (speech_end backdated above).
    // The turn id it mints correlates EVERYTHING about this turn: the trace
    // stages, the /send that persists the message, and (via the commit frame
    // below) the diarization pass that labels who spoke it.
    const turnId = this._trace.begin(undefined, endedAgoMs)
    if (this.sttRealtime && this.sttWs) {
      // Realtime path: close the utterance with a commit; the transcript comes
      // back asynchronously on the socket (onmessage -> sendText).
      this.sttStreaming = false
      if (realtimeCommitAction({ framesSent: this._utterFrames, speechMs,
                                 minSpeechMs: MIN_SPEECH_MS }) === 'salvage-rebuild') {
        // The VAD heard a real utterance but the processor sent ZERO frames,
        // so the capture graph is dead (iOS kills it across playback route
        // changes). Committing would transcribe nothing and lose the
        // question: salvage from the parallel batch recorder, and rebuild
        // the mic graph so the NEXT turn streams again.
        console.warn(`voice: 0 frames sent for a ${Math.round(speechMs)}ms utterance - salvaging via batch recorder, rebuilding capture`)
        this._rebuildSttProcessor()
        this._state('transcribing')
        await this._salvageUtterance(speechMs)
        return
      }
      const tooShort = speechMs < MIN_SPEECH_MS
      // turn_id rides the commit frame (#28 phase 3) so the server's
      // diarization pass can label the EXACT message this utterance becomes
      // (the /send carries the same id). A dropped short utterance still
      // commits with its id but never enters the ledger, so its final finds
      // no commit to win and attaches nowhere - the structural version of
      // the old drop flag.
      this._sttSend({ commit: true, turn_id: turnId })
      if (!tooShort) {
        onCommit(this._ledger, turnId, continuation ? 'buffer' : 'send')
        // A continuation commit is mid-speech: capture keeps flowing, so
        // the state stays 'listening' and the user sees nothing change.
        if (!continuation) this._state('transcribing')
        clearTimeout(this._sttCommitTimer)
        // #104: patience scales with the audio just committed - a capped
        // 20s segment finalizes slower than a 2s remark, and punting a
        // HEALTHY long finalization to the 60s batch path was half the
        // stall. The ledger makes the race safe in both directions.
        this._sttCommitTimer = setTimeout(() => {
          const dispatch = onSalvage(this._ledger, turnId)
          if (dispatch && this.active) {
            // Realtime never answered in time. The batch recorder ran the
            // whole time - salvage its copy; the ledger has already
            // consumed the commit, so a late realtime final drops.
            this._salvageUtterance(speechMs, turnId, dispatch)
          }
        }, sttCommitTimeoutMs(speechMs))
      } else if (cause === 'gap' && this._turnBuffer.length) {
        // The turn ended on a too-short tail with buffered segments behind
        // it: flush what the monologue already said.
        this._deliverTranscript('', turnId, 'send')
      }
      return
    }
    const rec = this.recorder
    const chunks = this.recChunks
    await new Promise((res) => { rec.onstop = res; try { rec.stop() } catch { res() } })
    this._startRecorder()
    if (speechMs < MIN_SPEECH_MS) {
      if (cause === 'gap' && this._turnBuffer.length) {
        this._deliverTranscript('', turnId, 'send')
      }
      return
    }
    if (!continuation) this._state('transcribing')
    const blob = new Blob(chunks, { type: rec.mimeType || 'audio/webm' })
    await this._transcribeOrHold(blob, speechMs,
                                 { turnId, dispatch: continuation ? 'buffer' : 'send' })
    if (this.active && this.state === 'transcribing') {
      this._state(this.roundActive ? 'working' : 'listening')
    }
  }

  // The single delivery door (#85/#104): every winning transcript - realtime
  // final, salvage, batch, held retry - lands here with its dispatch. A
  // 'buffer' dispatch accumulates a capped segment's text; a 'send' joins
  // everything buffered with this text and dispatches ONE user turn.
  _deliverTranscript(text, turnId, dispatch) {
    if (dispatch === 'buffer') {
      if (text) this._turnBuffer.push(text)
      return
    }
    const parts = [...this._turnBuffer, text].filter(Boolean)
    this._turnBuffer = []
    this._logicalStart = 0
    if (!parts.length) return
    this._trace.mark('transcript_final')
    this.sendText(parts.join(' '), turnId || this._trace.current()?.turnId)
  }

  // POST one utterance to /stt; on a NETWORK failure hold the audio and retry
  // until the tunnel returns. HTTP errors (bad audio) surface and drop -
  // retrying identical bytes can't fix those.
  async _transcribeOrHold(blob, speechMs, { turnId = null, dispatch = 'send' } = {}) {
    try {
      const fd = new FormData()
      fd.append('file', blob, 'utterance.webm')
      fd.append('duration_ms', String(Math.round(speechMs)))
      const res = await fetch(`/api/chats/${this.getChatId()}/stt`, { method: 'POST', body: fd })
      const data = await res.json().catch(() => ({}))
      if (res.ok && data.text) {
        this._deliverTranscript(data.text, turnId, dispatch)
      } else if (!res.ok) this.onError?.(`Transcription failed (${data.detail || res.status})`)
      return true
    } catch {
      this.heldUtterances.push({ blob, speechMs })
      this.onHeld?.(this.heldUtterances.length)
      this._startHeldRetry()
      return false
    }
  }

  async _salvageUtterance(speechMs, turnId = null, dispatch = 'send') {
    const rec = this.recorder
    const chunks = this.recChunks
    await new Promise((res) => { rec.onstop = res; try { rec.stop() } catch { res() } })
    this._startRecorder()
    const blob = new Blob(chunks, { type: rec.mimeType || 'audio/webm' })
    await this._transcribeOrHold(blob, speechMs, { turnId, dispatch })
    if (this.active && this.state === 'transcribing') {
      this._state(this.roundActive ? 'working' : 'listening')
    }
  }

  _startHeldRetry() {
    if (this._heldTimer) return
    const tick = async () => {
      this._heldTimer = null
      const item = this.heldUtterances[0]
      if (!item) { this.onHeld?.(0); return }
      try {
        const fd = new FormData()
        fd.append('file', item.blob, 'utterance.webm')
        fd.append('duration_ms', String(Math.round(item.speechMs)))
        const res = await fetch(`/api/chats/${this.getChatId()}/stt`, { method: 'POST', body: fd })
        const data = await res.json().catch(() => ({}))
        // Reached the server: this item is done either way (HTTP errors
        // won't improve by resending the same bytes).
        this.heldUtterances.shift()
        this.onHeld?.(this.heldUtterances.length)
        if (res.ok && data.text) this._deliverTranscript(data.text, null, 'send')
        else if (!res.ok) this.onError?.(`Held message failed to transcribe (${data.detail || res.status})`)
        this._heldTimer = setTimeout(tick, this.heldUtterances.length ? 500 : 0)
      } catch {
        this._heldTimer = setTimeout(tick, 4000) // still offline - keep holding
      }
    }
    this._heldTimer = setTimeout(tick, 3000)
  }

  // ---- TTS playback (driven by the chat's SSE events) ----

  onEvent(ev) {
    if (!this.active) return
    // Round/drop gating is a pure state machine (voiceGate.js) so the reset
    // rules, including "a barge-in must never leak past its own round", are
    // unit-tested. Every event advances it; only the ones below also have a
    // playback side effect.
    const before = { roundActive: this.roundActive, dropQueue: this.dropQueue }
    const gate = gateEvent(before, ev.type)
    this.roundActive = gate.roundActive
    this.dropQueue = gate.dropQueue
    if (gate.roundActive !== before.roundActive || gate.dropQueue !== before.dropQueue) {
      this._vlog('gate:onEvent', { evType: ev.type, before, after: gate })
    }
    // Round liveness: EVERY event feeds the wedge guard's idle clock -
    // work_status/tool_activity included, so a live tool round never trips it.
    this._lastRoundEventAt = Date.now()
    // A round generating with nothing audible is 'working', not 'listening':
    // the screen must not invite speech the gated mic would then discard.
    if (this.roundActive && this.playing === 0 && this.state === 'listening') {
      this._state('working')
    }
    if (ev.type === 'speaker_start') {
      // Defer opening TTS until the reply has real content, so a benched model's
      // ellipsis-only "…" reply is never voiced (ElevenLabs otherwise breathes it).
      this._pendingSpeaker = { slug: ev.speaker, text: '', started: false }
    } else if (ev.type === 'delta') {
      // First streamed token of the round, and of this speaker: the model +
      // orchestration wait resolves here.
      this._trace.mark('first_token')
      this._trace.speakerMark(ev.speaker, 'first_delta', this._traceMeta(ev.speaker))
      this._feedSpeaker(ev.speaker, ev.text)
    } else if (ev.type === 'passed') {
      // #98: the seat passed - nothing is spoken. TTS never opened for a
      // bare pass (couldBePass held it); just drop the pending speaker.
      const p = this._pendingSpeaker
      if (p && p.slug === ev.speaker) this._pendingSpeaker = null
    } else if (ev.type === 'speaker_end' || ev.type === 'error') {
      const p = this._pendingSpeaker
      if (p && p.slug === ev.speaker && !p.started) {
        this._pendingSpeaker = null // ellipsis/empty reply - never opened, nothing to flush
      } else {
        this.sockets.get(ev.speaker)?.send({ flush: true, done: true })
      }
    }
    // 'work_status' deliberately has NO playback branch here: it's a
    // structured liveness signal for the text/UI chip only. Speaking it
    // (a deterministic phrase, or an optional model-supplied voice_handoff
    // racing it) is a separate product decision, not yet made - see
    // docs/DECISIONS.md. gateEvent() above still advances the pure state
    // machine for it (a harmless no-op, same as tool_activity/guest_job).
  }

  _feedSpeaker(slug, text) {
    const p = this._pendingSpeaker
    if (p && p.slug === slug && !p.started) {
      p.text += text
      // #98: also hold while the text could still be a bare [pass] - a
      // passed turn is never spoken, and the 'passed' event clears it.
      if (/[^.…\s]/.test(p.text) && !couldBePass(p.text)) { // real content arrived - start speaking now
        p.started = true
        this._pendingSpeaker = null
        this._beginSpeaker(slug)
        this.sockets.get(slug)?.send({ text: p.text })
      }
      return
    }
    this.sockets.get(slug)?.send({ text })
  }

  onRoundDone() {
    const before = { roundActive: this.roundActive, dropQueue: this.dropQueue }
    const gate = gateRoundDone()
    this.roundActive = gate.roundActive
    this.dropQueue = gate.dropQueue
    this._vlog('gate:onRoundDone', { before, after: gate })
    // The TEXT/round stream has ended - but TTS and the sequential per-speaker
    // playChain keep running AFTER this, so the later speakers' play_invoked/
    // playback/queue-wait marks haven't happened yet. Flushing here
    // would POST the trace before they exist and lose them for every 2+ speaker
    // round. Instead, defer the flush until the playChain has fully drained for
    // THIS turn. We capture the turn now and branch a flush off the current
    // chain tail WITHOUT extending it (playback must never wait on diagnostics).
    // A captured handle (not this._trace's live turn) means a new turn beginning
    // in the round-done→drained gap can't hijack or lose this flush. Barge-in
    // still closes it out: dropQueue makes each remaining playChain link resolve
    // immediately, so the chain drains fast and the trace flushes, never hangs.
    const turn = this._trace.current()
    this.playChain.then(() => this._trace.flush(turn),
                        () => this._trace.flush(turn))
    // Force the resume rather than going through _maybeResume: a round where
    // nobody speaks aloud (e.g. a benched model's unvoiced "…" reply) never
    // passes through 'speaking', so the state is still 'transcribing' here -
    // and _maybeResume refuses to leave that state. The round is over by
    // definition now; anything still buffered plays via the playChain check.
    if (this.active && this.playing === 0) this._state('listening')
  }

  _beginSpeaker(slug) {
    if (this.dropQueue) return // user already cut this round off
    const participant = this.getParticipants().find((p) => p.slug === slug)
    const player = new StreamPlayer(this.sink)
    this.players.set(slug, player)
    const ws = new WebSocket(`${wsBase()}/api/voice/tts`)
    // Deltas start arriving before the websocket handshake completes - buffer
    // everything until open, or the start of every reply gets silently dropped.
    const pending = []
    let open = false
    let lastSend = Date.now()
    let finished = false
    const send = (obj) => {
      const s = JSON.stringify(obj)
      if (obj.done) finished = true
      if (open && ws.readyState === WebSocket.OPEN) ws.send(s)
      else if (!open) pending.push(s)
      lastSend = Date.now()
    }
    // ElevenLabs drops the stream after 20s without input - keep it alive while
    // the model thinks or runs tools mid-reply.
    const keepalive = setInterval(() => {
      if (!finished && open && ws.readyState === WebSocket.OPEN && Date.now() - lastSend > 12000) {
        ws.send(JSON.stringify({ text: ' ' }))
        lastSend = Date.now()
      }
    }, 4000)
    ws.onopen = () => {
      ws.send(JSON.stringify({
        chat_id: this.getChatId(),
        voice_id: participant?.voice_id || '',
      }))
      open = true
      for (const s of pending) ws.send(s)
      pending.length = 0
    }
    ws.onmessage = (e) => {
      const msg = JSON.parse(e.data)
      if (msg.audio) {
        this._trace.speakerMark(slug, 'first_audio', this._traceMeta(slug))
        player.push(msg.audio)
      }
      if (msg.error) this.onError?.(`Voice synthesis (${participant?.name || slug}): ${msg.error}`)
      if (msg.final || msg.error) {
        player.end()
        try { ws.close() } catch { /* */ }
      }
    }
    ws.onclose = () => { clearInterval(keepalive); player.end() }
    ws.onerror = () => { clearInterval(keepalive); player.end() }

    this.playing += 1
    this._state('speaking')
    this.playChain = this.playChain
      .then(() => {
        if (this.dropQueue) return undefined
        // The shared sink is now handed to this speaker - its queued audio can
        // begin. This is play INVOKED, not yet audible: the gap between its
        // first_audio (ready) and here is time it spent queued behind the prior
        // speaker (the trace's playback_queue_wait stage).
        this._trace.speakerMark(slug, 'play_invoked', this._traceMeta(slug))
        // This participant's audio is now the one actually playing - tint the orb
        // to them for the FULL duration of their speech (not just their text).
        this.onSpeaker?.(slug)
        // Per-agent volume (voice_gain 0.2–1.0 normalizes loud voices) and the
        // session playback rate, applied as this speaker claims the sink.
        // defaultPlaybackRate too: loading a new src resets playbackRate to it.
        this.sink.volume = Math.min(1, Math.max(0.2, participant?.voice_gain ?? 1))
        this.sink.defaultPlaybackRate = this.playbackRate || 1
        this.sink.playbackRate = this.playbackRate || 1
        return player.play(
          (err) => this.onError?.(playbackFailureMessage(err, {
            mobile: /iPhone|iPad|iPod|Android/i.test(navigator.userAgent || ''),
          })),
          // AUDIBLE start (the element's 'playing' event) - the true end of the
          // end-to-end latency, not the earlier play() invocation above.
          () => this._trace.speakerMark(slug, 'playback', this._traceMeta(slug)),
        )
      })
      .then(() => {
        this.playing -= 1
        this.players.delete(slug)
        this.sockets.delete(slug)
        if (this.playing === 0) this.onSpeaker?.(null) // nobody speaking now
        this._maybeResume()
      })
    this.sockets.set(slug, { ws, send })
  }

  _maybeResume() {
    if (!this.active) return
    if (this.playing === 0 && !this.roundActive) {
      if (this.state !== 'transcribing') this._state('listening')
    } else if (this.playing === 0 && this.roundActive && this.state === 'speaking') {
      // Between speakers: the round is still generating, nothing is audible.
      this._state('working')
    }
  }
}
