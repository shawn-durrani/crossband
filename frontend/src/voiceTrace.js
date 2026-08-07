// Per-turn voice latency trace: the client half.
//
// The browser is the only vantage point that sees a voice turn end to end: it
// detects end-of-speech, drives the STT/TTS sockets, and starts audible
// playback. This module records timestamped MARKS along that timeline for one
// turn, then turns them into content-free stage DURATIONS that get POSTed to
// /api/voice/trace for durable, aggregatable diagnostics.
//
// PRIVACY: this module never touches transcript or reply text. It records only
// clock readings, participant slugs, and provider/model/voice labels. The
// backend re-enforces that floor on ingest (backend/voice_trace.sanitize).
//
// Pure and injectable (no DOM, no globals) so it runs under `node --test`:
// `now` returns milliseconds, `post` ships a payload. VoiceController wires the
// real `performance.now()` and a `fetch` POST; tests pass fakes.

const STAGE_ORDER = [
  'end_of_speech_to_final',
  'final_to_first_token',
  'first_token_to_first_audio',
  'playback_queue_wait',
  'first_audio_to_playback',
  'end_to_end_first_audio',
]

export class VoiceTrace {
  constructor({ now, post, getChatId } = {}) {
    this._now = now || (() => Date.now())
    this._post = post || (() => {})
    this._getChatId = getChatId || (() => null)
    this.turn = null
  }

  // Start a fresh turn at end-of-speech. `this.turn` moves to the new turn, but
  // a prior turn still awaiting its DEFERRED flush is not lost: the controller
  // captured that turn's handle (see current()/flush(turn)) and will flush it
  // when its playback drains. Turns don't overlap in the controller (one round
  // at a time), so the new turn's marks and the old turn's trailing marks never
  // land on the same object.
  // `speechEndAgoMs` backdates the speech_end mark: a VAD only learns the
  // utterance ended after its silence window has already elapsed, so "now" is
  // ~silenceMs after the user actually stopped talking. It is an ELAPSED
  // amount, not a timestamp - the VAD's clock and this trace's clock may
  // differ (Date.now vs performance.now), and deltas are safe across both.
  begin(turnId, speechEndAgoMs = 0) {
    this.turn = {
      turnId: turnId || `t${this._now().toString(36)}${Math.random().toString(36).slice(2, 8)}`,
      chatId: this._getChatId(),
      marks: {},
      // slug -> { provider, model, tts_provider, first_delta, first_audio,
      //           play_invoked, playback }. speakerOrder preserves the order
      //           speakers first appeared (≈ play order) so we can tell the
      //           first reply (no queue ahead of it) from the 2nd+ (which queue).
      speakers: {},
      speakerOrder: [],
      flushed: false,
    }
    this.mark('speech_end', this._now() - Math.max(0, speechEndAgoMs || 0))
    return this.turn.turnId
  }

  active() { return !!this.turn && !this.turn.flushed }

  // The turn currently being recorded (or null). Callers capture this handle so
  // a DEFERRED flush targets exactly this turn even if a new turn begins in the
  // gap between round-done and playback draining - see flush(turn).
  current() { return this.turn }

  // Turn-level timestamp. First write wins for a given name (we care about the
  // FIRST token / FIRST audio, not the last).
  mark(name, at) {
    if (!this.turn) return
    if (this.turn.marks[name] === undefined) this.turn.marks[name] = at ?? this._now()
  }

  // Per-speaker timestamp + optional provider/model/voice tags. First write
  // wins per (slug, name). The FIRST time a slug is seen it joins speakerOrder,
  // so build() can distinguish the lead reply from queued followers.
  speakerMark(slug, name, meta, at) {
    if (!this.turn || !slug) return
    let s = this.turn.speakers[slug]
    if (!s) { s = this.turn.speakers[slug] = {}; this.turn.speakerOrder.push(slug) }
    if (meta) {
      if (meta.provider && !s.provider) s.provider = meta.provider
      if (meta.model && !s.model) s.model = meta.model
      if (meta.tts_provider && !s.tts_provider) s.tts_provider = meta.tts_provider
    }
    if (s[name] === undefined) s[name] = at ?? this._now()
  }

  // Compute the content-free stage durations from the marks recorded so far.
  // Only emits a stage when both endpoints exist and the delta is non-negative
  // (a clock that went backwards, or a stage that never happened, is skipped
  // rather than reported as a bogus 0/negative).
  //
  // Per-speaker stages are emitted for EVERY speaker that produced marks, each
  // tagged with its OWN slug/model, so a 3-model round yields three TTS/
  // playback samples, not one. That is what lets the summary answer the
  // multi-agent-serialisation question instead of silently dropping followers.
  build(turn) {
    const t = turn || this.turn
    if (!t) return null
    const m = t.marks
    const stages = []
    const push = (stage, a, b, tags) => {
      if (typeof a !== 'number' || typeof b !== 'number') return
      const ms = b - a
      if (ms < 0) return
      stages.push({ stage, ms, ...(tags || {}) })
    }
    const firstSlug = t.speakerOrder[0]
    const first = firstSlug ? t.speakers[firstSlug] : null
    const genTags = first
      ? { provider: first.provider || '', model: first.model || '', speaker: firstSlug }
      : {}

    // Capture + dispatch/model. There is no separate final_to_dispatch stage:
    // on the client, handing the transcript to the round (sendText → POST /send)
    // is SYNCHRONOUS with finalisation, so that gap is always ~0 and would only
    // ever be measured server-side. final_to_first_token therefore honestly
    // bundles client dispatch + network + group-turn orchestration + model TTFT.
    push('end_of_speech_to_final', m.speech_end, m.transcript_final)
    push('final_to_first_token', m.transcript_final, m.first_token, genTags)

    // Per-speaker TTS synthesis + playback, for every speaker (not just the lead).
    t.speakerOrder.forEach((slug, i) => {
      const s = t.speakers[slug]
      const tags = { provider: s.provider || '', model: s.model || '',
                     tts_provider: s.tts_provider || '', speaker: slug }
      push('first_token_to_first_audio', s.first_delta, s.first_audio, tags)
      push('first_audio_to_playback', s.first_audio, s.playback, tags)
      // Serialisation cost: this speaker's audio was READY at first_audio, but
      // the shared sink wasn't handed to it (play_invoked) until the prior
      // speaker finished. Only the 2nd+ reply can queue - the lead has nobody
      // ahead of it. A follower whose audio arrived AFTER its turn came up (it
      // was itself the laggard) yields a negative delta and is skipped, which
      // correctly reads as "no queue wait".
      if (i > 0) push('playback_queue_wait', s.first_audio, s.play_invoked, tags)
    })

    // Headline: end-of-speech → first AUDIBLE word of the first reply.
    if (first) {
      push('end_to_end_first_audio', m.speech_end, first.playback,
        { provider: first.provider || '', model: first.model || '',
          tts_provider: first.tts_provider || '', speaker: firstSlug })
    }

    // stable, human-friendly order for logs/tests (ties keep insertion order,
    // which is speaker order - Array.prototype.sort is stable)
    stages.sort((x, y) => STAGE_ORDER.indexOf(x.stage) - STAGE_ORDER.indexOf(y.stage))
    return { turn_id: t.turnId, chat_id: t.chatId, stages }
  }

  // Ship a turn's trace (best-effort) and close it. Never throws - a diagnostics
  // POST must never be able to disturb a live voice session.
  //
  // `turn` defaults to the current turn, but the controller passes a CAPTURED
  // handle so the flush can be DEFERRED until playback has fully drained: the
  // text/round stream ends well before the sequential per-speaker playChain
  // finishes, so the later speakers' play_invoked/playback/queue-wait marks are
  // recorded AFTER round-done. Targeting the captured turn (not `this.turn`)
  // means a new turn beginning in that gap can neither hijack this flush nor
  // lose these trailing marks. Idempotent per turn (turn.flushed).
  flush(turn) {
    const t = turn || this.turn
    if (!t || t.flushed) return null
    const payload = this.build(t)
    t.flushed = true
    if (payload && payload.stages.length) {
      try { this._post(payload) } catch { /* diagnostics are never load-bearing */ }
    }
    return payload
  }
}

export { STAGE_ORDER }
