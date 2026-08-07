// Pure-function tests for the client-side voice latency trace.
// Run: node --test frontend/src/voiceTrace.test.js
//
// Guardrails pinned: marks compute the right stage DURATIONS; EVERY speaker's
// TTS/playback timings are emitted (not just the lead); multi-agent queue wait
// is computed from ready→sink-handoff; a partial turn only emits what it has;
// backwards clocks are skipped; the payload is content-free; flush ships once.
import { test } from 'node:test'
import assert from 'node:assert/strict'
import { VoiceTrace } from './voiceTrace.js'

// A controllable clock so timings are deterministic.
function fakeClock(start = 0) {
  let t = start
  return { now: () => t, set: (v) => { t = v }, adv: (d) => { t += d } }
}

const META = { provider: 'openai', model: 'gpt-5.1', tts_provider: 'elevenlabs' }

test('a full single-speaker turn computes every stage duration', () => {
  const clk = fakeClock()
  let posted = null
  const tr = new VoiceTrace({ now: clk.now, post: (p) => { posted = p }, getChatId: () => 3 })

  tr.begin('turn-1')                     // speech_end @ 0
  clk.set(500); tr.mark('transcript_final')
  clk.set(1400); tr.mark('first_token')
  tr.speakerMark('gpt', 'first_delta', META)
  clk.set(1650); tr.speakerMark('gpt', 'first_audio')
  clk.set(1660); tr.speakerMark('gpt', 'play_invoked')  // sink free immediately (lead)
  clk.set(1720); tr.speakerMark('gpt', 'playback')      // audible

  const payload = tr.flush()
  assert.equal(payload.turn_id, 'turn-1')
  assert.equal(payload.chat_id, 3)
  const byStage = Object.fromEntries(payload.stages.map((s) => [s.stage, s.ms]))
  assert.equal(byStage.end_of_speech_to_final, 500)
  assert.equal(byStage.final_to_first_token, 900)       // 1400 - 500 (no phantom dispatch stage)
  assert.equal(byStage.first_token_to_first_audio, 250) // 1650 - 1400
  assert.equal(byStage.first_audio_to_playback, 70)     // 1720 - 1650 (audible)
  assert.equal(byStage.end_to_end_first_audio, 1720)    // speech_end → audible
  // the removed/renamed stages are gone
  assert.ok(!('final_to_dispatch' in byStage))
  assert.ok(!('dispatch_to_first_token' in byStage))
  // the lead speaker never gets a queue_wait (nobody ahead of it)
  assert.ok(!('playback_queue_wait' in byStage))
  // tags: model on the generation stage, voice provider on the tts stage
  assert.equal(payload.stages.find((s) => s.stage === 'final_to_first_token').model, 'gpt-5.1')
  assert.equal(payload.stages.find((s) => s.stage === 'first_token_to_first_audio').tts_provider, 'elevenlabs')
  assert.deepEqual(posted, payload)
})

test('begin() backdates speech_end by the elapsed silence window', () => {
  // The VAD only fires after its silence window has fully elapsed, so at
  // begin() time the user stopped talking speechEndAgoMs AGO. Every stage
  // anchored on speech_end must include that gap or the trace under-reports.
  const clk = fakeClock(2000)
  const tr = new VoiceTrace({ now: clk.now })
  tr.begin('t', 1500)                    // user actually stopped at 500
  clk.set(2400); tr.mark('transcript_final')
  tr.speakerMark('gpt', 'first_delta', META)
  clk.set(2600); tr.speakerMark('gpt', 'first_audio')
  clk.set(2610); tr.speakerMark('gpt', 'play_invoked')
  clk.set(2700); tr.speakerMark('gpt', 'playback')
  const byStage = Object.fromEntries(tr.flush().stages.map((s) => [s.stage, s.ms]))
  assert.equal(byStage.end_of_speech_to_final, 1900)   // 2400 - 500, not 2400 - 2000
  assert.equal(byStage.end_to_end_first_audio, 2200)   // 2700 - 500
  // a negative/absent backdate never pushes speech_end into the future
  const tr2 = new VoiceTrace({ now: clk.now })
  tr2.begin('t2', -50)
  assert.equal(tr2.current().marks.speech_end, clk.now())
})

test('EVERY speaker gets its own TTS/playback samples, tagged with its slug', () => {
  const clk = fakeClock()
  const tr = new VoiceTrace({ now: clk.now })
  tr.begin('t')
  clk.set(400); tr.mark('transcript_final')
  clk.set(1000); tr.mark('first_token')

  // speaker 1 (gpt) plays first
  tr.speakerMark('gpt', 'first_delta', META)
  clk.set(1200); tr.speakerMark('gpt', 'first_audio')
  clk.set(1210); tr.speakerMark('gpt', 'play_invoked')
  clk.set(1250); tr.speakerMark('gpt', 'playback')
  // speaker 2 (claude) streams while gpt is still talking
  clk.set(1300); tr.speakerMark('claude', 'first_delta', { provider: 'anthropic', model: 'claude-opus-4-8', tts_provider: 'elevenlabs' })
  clk.set(1500); tr.speakerMark('claude', 'first_audio')  // ready early…
  clk.set(4000); tr.speakerMark('claude', 'play_invoked') // …but waits for gpt to finish
  clk.set(4050); tr.speakerMark('claude', 'playback')

  const stages = tr.build().stages
  // two speakers → two first_token_to_first_audio + two first_audio_to_playback
  const tta = stages.filter((s) => s.stage === 'first_token_to_first_audio')
  assert.equal(tta.length, 2)
  assert.deepEqual(tta.map((s) => s.speaker).sort(), ['claude', 'gpt'])
  assert.equal(stages.filter((s) => s.stage === 'first_audio_to_playback').length, 2)
})

test('playback_queue_wait captures the follower waiting behind the lead', () => {
  const clk = fakeClock()
  const tr = new VoiceTrace({ now: clk.now })
  tr.begin('t')
  clk.set(400); tr.mark('transcript_final')
  clk.set(1000); tr.mark('first_token')
  // lead
  tr.speakerMark('gpt', 'first_delta', META)
  clk.set(1200); tr.speakerMark('gpt', 'first_audio')
  clk.set(1205); tr.speakerMark('gpt', 'play_invoked')
  clk.set(1250); tr.speakerMark('gpt', 'playback')
  // follower: audio ready @1500, but sink not handed over until @4000
  clk.set(1300); tr.speakerMark('claude', 'first_delta', META)
  clk.set(1500); tr.speakerMark('claude', 'first_audio')
  clk.set(4000); tr.speakerMark('claude', 'play_invoked')
  clk.set(4050); tr.speakerMark('claude', 'playback')

  const qw = tr.build().stages.filter((s) => s.stage === 'playback_queue_wait')
  assert.equal(qw.length, 1)               // only the follower
  assert.equal(qw[0].speaker, 'claude')
  assert.equal(qw[0].ms, 2500)             // 4000 - 1500: time queued behind gpt
})

test('a follower that was itself the laggard reports no queue wait', () => {
  const clk = fakeClock()
  const tr = new VoiceTrace({ now: clk.now })
  tr.begin('t')
  clk.set(400); tr.mark('transcript_final')
  clk.set(1000); tr.mark('first_token')
  tr.speakerMark('gpt', 'first_delta', META)
  clk.set(1200); tr.speakerMark('gpt', 'first_audio')
  clk.set(1205); tr.speakerMark('gpt', 'play_invoked')
  clk.set(1250); tr.speakerMark('gpt', 'playback')
  // follower's sink turn came up @3000, but its audio wasn't ready until @3200
  clk.set(3000); tr.speakerMark('claude', 'play_invoked')
  clk.set(3200); tr.speakerMark('claude', 'first_delta', META)
  clk.set(3200); tr.speakerMark('claude', 'first_audio')  // ready AFTER play_invoked
  clk.set(3250); tr.speakerMark('claude', 'playback')
  // first_audio(3200) → play_invoked(3000) is negative → skipped
  assert.equal(tr.build().stages.filter((s) => s.stage === 'playback_queue_wait').length, 0)
})

test('end_to_end uses the AUDIBLE playback mark, not play invocation', () => {
  const clk = fakeClock()
  const tr = new VoiceTrace({ now: clk.now })
  tr.begin('t')
  clk.set(400); tr.mark('transcript_final')
  clk.set(1000); tr.mark('first_token')
  tr.speakerMark('gpt', 'first_delta', META)
  clk.set(1200); tr.speakerMark('gpt', 'first_audio')
  clk.set(1210); tr.speakerMark('gpt', 'play_invoked')  // NOT the headline endpoint
  clk.set(1600); tr.speakerMark('gpt', 'playback')      // audible - this one counts
  const e2e = tr.build().stages.find((s) => s.stage === 'end_to_end_first_audio')
  assert.equal(e2e.ms, 1600)  // uses playback (1600), not play_invoked (1210)
})

test('a partial turn only emits the stages it has', () => {
  const clk = fakeClock()
  const tr = new VoiceTrace({ now: clk.now })
  tr.begin('t')
  clk.set(300); tr.mark('transcript_final')
  // no first_token/audio - round never produced audible speech
  const stages = tr.build().stages.map((s) => s.stage)
  assert.deepEqual(stages, ['end_of_speech_to_final'])
})

test('a backwards clock skips the stage rather than reporting a negative', () => {
  const clk = fakeClock(1000)
  const tr = new VoiceTrace({ now: clk.now })
  tr.begin('t')                          // speech_end @ 1000
  clk.set(400); tr.mark('transcript_final')  // earlier than speech_end
  assert.equal(tr.build().stages.length, 0)
})

test('first write wins for a mark (we want the FIRST token, not the last)', () => {
  const clk = fakeClock()
  const tr = new VoiceTrace({ now: clk.now })
  tr.begin('t')
  clk.set(100); tr.mark('transcript_final')
  clk.set(200); tr.mark('first_token')
  clk.set(999); tr.mark('first_token')   // ignored
  assert.equal(tr.build().stages.find((s) => s.stage === 'final_to_first_token').ms, 100)
})

test('payload never carries transcript/text fields', () => {
  const clk = fakeClock()
  const tr = new VoiceTrace({ now: clk.now })
  tr.begin('t')
  clk.set(500); tr.mark('transcript_final')
  const payload = tr.build()
  const keys = new Set(payload.stages.flatMap((s) => Object.keys(s)))
  for (const k of keys) assert.ok(!['text', 'transcript', 'content'].includes(k))
})

test('flush is idempotent - ships at most once', () => {
  const clk = fakeClock()
  let calls = 0
  const tr = new VoiceTrace({ now: clk.now, post: () => { calls++ } })
  tr.begin('t')
  clk.set(500); tr.mark('transcript_final')
  tr.flush()
  tr.flush()
  assert.equal(calls, 1)
})

test('flush with no usable stages posts nothing', () => {
  const clk = fakeClock()
  let calls = 0
  const tr = new VoiceTrace({ now: clk.now, post: () => { calls++ } })
  tr.begin('t')                          // only speech_end, no second mark
  tr.flush()
  assert.equal(calls, 0)
})

// --- deferred flush: the round/text stream ends long before the sequential
// per-speaker playChain drains, so later speakers' marks land AFTER round-done.
// The controller captures the turn at round-done and flushes only once playback
// drains; these tests pin that the captured-turn flush picks up those late marks
// and can't be hijacked by a following turn.

test('marks recorded AFTER round-done but before flush ARE captured (2 speakers)', () => {
  const clk = fakeClock()
  let posted = null
  const tr = new VoiceTrace({ now: clk.now, post: (p) => { posted = p } })
  tr.begin('turn-A')
  clk.set(400); tr.mark('transcript_final')
  clk.set(1000); tr.mark('first_token')
  // lead speaker plays; its marks arrive before round-done
  tr.speakerMark('gpt', 'first_delta', META)
  clk.set(1200); tr.speakerMark('gpt', 'first_audio')
  clk.set(1210); tr.speakerMark('gpt', 'play_invoked')
  clk.set(1250); tr.speakerMark('gpt', 'playback')

  // ROUND-DONE fires here (text stream ended). Controller captures the turn but
  // does NOT flush yet - gpt is still talking and claude hasn't played.
  const captured = tr.current()

  // claude's first token/audio arrived during the round; its play_invoked/
  // playback/queue-wait only happen now, as the playChain reaches it - AFTER
  // round-done. Under the old code (flush in onRoundDone) these were lost.
  tr.speakerMark('claude', 'first_delta', { provider: 'anthropic', model: 'claude-opus-4-8', tts_provider: 'elevenlabs' })
  clk.set(1500); tr.speakerMark('claude', 'first_audio')
  clk.set(4000); tr.speakerMark('claude', 'play_invoked')  // waited behind gpt
  clk.set(4050); tr.speakerMark('claude', 'playback')

  // playChain finally drains → deferred flush of the captured turn
  const payload = tr.flush(captured)
  const qw = payload.stages.filter((s) => s.stage === 'playback_queue_wait')
  assert.equal(qw.length, 1)
  assert.equal(qw[0].speaker, 'claude')
  assert.equal(qw[0].ms, 2500)             // 4000 - 1500, recorded post-round-done
  // both speakers' TTS/playback samples present
  assert.equal(payload.stages.filter((s) => s.stage === 'first_audio_to_playback').length, 2)
  assert.deepEqual(posted, payload)
})

test('a new turn beginning before the deferred flush cannot hijack it', () => {
  const clk = fakeClock()
  const posts = []
  const tr = new VoiceTrace({ now: clk.now, post: (p) => posts.push(p) })
  // turn A
  tr.begin('turn-A')
  clk.set(400); tr.mark('transcript_final')
  clk.set(1000); tr.mark('first_token')
  tr.speakerMark('gpt', 'first_delta', META)
  clk.set(1200); tr.speakerMark('gpt', 'first_audio')
  clk.set(1210); tr.speakerMark('gpt', 'play_invoked')
  clk.set(1250); tr.speakerMark('gpt', 'playback')
  const capturedA = tr.current()

  // a NEW turn B begins (e.g. user barged in) BEFORE A's playback drained/flushed
  tr.begin('turn-B')
  clk.set(2000); tr.mark('transcript_final')

  // now A's deferred flush finally runs - it must flush A, not B
  const payload = tr.flush(capturedA)
  assert.equal(payload.turn_id, 'turn-A')
  assert.equal(posts.length, 1)
  assert.equal(posts[0].turn_id, 'turn-A')
  // B is untouched and still flushable on its own
  assert.equal(tr.current().turnId, 'turn-B')
  assert.equal(tr.flush(tr.current()).turn_id, 'turn-B')
  assert.equal(posts.length, 2)
})

test('deferred flush of a captured turn is idempotent', () => {
  const clk = fakeClock()
  let calls = 0
  const tr = new VoiceTrace({ now: clk.now, post: () => { calls++ } })
  tr.begin('t')
  clk.set(500); tr.mark('transcript_final')
  const captured = tr.current()
  tr.flush(captured)
  tr.flush(captured)   // playChain reject+resolve handlers could both fire
  assert.equal(calls, 1)
})
