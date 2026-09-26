// The render smoke test's entry (bundled by scripts/render-smoke.mjs, run
// under node --test conditions). Renders the REAL message list - ThreadView
// with Message rows - against realistic fixture messages via react-dom's
// server renderer: any render-time crash (a free identifier, a null deref,
// a bad prop shape) throws here instead of blanking every chat in
// production, which is exactly what happened on 2026-08-14. Effects don't
// run under renderToStaticMarkup; this guards the render path, not
// behaviour - the pure-module suites own behaviour.
import { renderToStaticMarkup } from 'react-dom/server'
import ThreadView from './components/ThreadView'
import VoiceReadiness from './components/VoiceReadiness'

const PARTICIPANTS = [
  { slug: 'claude', name: 'Claude', color: '#7aa2f7', enabled: true, model: 'claude-opus-4-8' },
  { slug: 'gpt', name: 'GPT', color: '#34d399', enabled: true, model: 'gpt-5.1' },
  { slug: 'quiet', name: 'Quietseat', color: '#f59e0b', enabled: true, model: 'quiet-1' },
]

// The shapes a real chat holds: typed user turns, model replies (one with
// usage), a voice turn (discardable - the row that crashed), a system
// notice, and a streaming in-flight reply. Then the #456 shapes: a seat
// whose every turn is silent (a pass, one cut off mid-pass, one cut off
// before it wrote anything), which must draw no bubble at all, and a
// streaming reply that could still be a pass, which draws the thinking
// dots and never the brackets. Then the #460 shapes: a quiet remark
// before [pass], which is a pass and draws nothing, and a real reply with
// [pass] stuck on the end, which draws its words without the token.
const MESSAGES = [
  { id: 1, speaker: 'user', content: 'hello both of you', created_at: 1, attachments: [] },
  { id: 2, speaker: 'claude', content: 'A reply with **markdown**.', created_at: 2,
    usage_json: '{"input_tokens": 10, "output_tokens": 5, "model": "claude-opus-4-8"}',
    attachments: [] },
  { id: 3, speaker: 'user', content: 'a captured voice turn', created_at: 3,
    voice_turn_id: 'vt-1', attachments: [] },
  { id: 4, speaker: 'system', content: '[12:00] deploy notice', created_at: 4, attachments: [] },
  { id: 5, speaker: 'gpt', content: 'Still streaming…', created_at: 5,
    streaming: true, attachments: [] },
  { id: 'live-quiet-1', speaker: 'quiet', content: '[pass]', created_at: 6, attachments: [] },
  { id: 'live-quiet-2', speaker: 'quiet', content: '[pass', created_at: 7, attachments: [] },
  { id: 'live-quiet-3', speaker: 'quiet', content: '', created_at: 8, attachments: [] },
  { id: 'live-claude-9', speaker: 'claude', content: '[pa', created_at: 9,
    streaming: true, attachments: [] },
  { id: 10, speaker: 'quiet', content: 'Nothing to add from me.  [pass]', created_at: 10,
    attachments: [] },
  { id: 11, speaker: 'gpt', content: 'Oil it after sanding.  [pass]', created_at: 11,
    attachments: [] },
]

export function renderSmoke() {
  const html = renderToStaticMarkup(
    <ThreadView
      messages={MESSAGES}
      participants={PARTICIPANTS}
      chatParticipants={PARTICIPANTS}
      examplePrompts={[]}
      mismatchFlags={{}}
      roomRoster={[]}
      voicePeople={[]}
      onReassign={() => {}}
      onDiscard={() => {}}
      streaming={false}
      roundProgress={null}
      canContinue={false}
      contRounds={1}
      atBottom
      newCount={0}
      scrollRef={{ current: null }}
      onScroll={() => {}}
      onJumpToBottom={() => {}}
      onContinue={() => {}}
      onContRoundsChange={() => {}}
      onPickPrompt={() => {}}
    />,
  )
  for (const needle of ['hello both of you', 'a captured voice turn', 'Claude is thinking',
                        'Oil it after sanding.']) {
    if (!html.includes(needle)) {
      throw new Error(`render smoke: expected ${JSON.stringify(needle)} in the markup`)
    }
  }
  // #456: a passed seat turn draws no bubble, and a pass never draws as text.
  // #460: nor does a quiet remark in front of it.
  for (const needle of ['Quietseat', '[pa', 'Nothing to add']) {
    if (html.includes(needle)) {
      throw new Error(`render smoke: ${JSON.stringify(needle)} must not be in the markup`)
    }
  }
  // #482 stage 2: the Voices page's readiness line, for a ready voice, one
  // short of pieces, and the test switched off (which draws nothing).
  const summary = { state: 'ready', min_pieces: 20, share: 0.95, piece_seconds: 2 }
  const readiness = renderToStaticMarkup(
    <div>
      <VoiceReadiness summary={summary} readiness={{
        ready: true, reason: 'ready', pieces: 40, share_right: 1,
        named_as_other: 0, days: 2, speech_seconds: 52 }} />
      <VoiceReadiness summary={summary} readiness={{
        ready: false, reason: 'too_few_pieces', pieces: 12, share_right: 1,
        named_as_other: 0, days: 2, speech_seconds: 14 }} />
      <VoiceReadiness summary={{ state: 'off' }} readiness={null} />
    </div>,
  )
  for (const needle of ['Ready', '52s of speech', 'Needs more speech: 12 of 20 pieces']) {
    if (!readiness.includes(needle)) {
      throw new Error(`render smoke: expected ${JSON.stringify(needle)} in the readiness markup`)
    }
  }
  return html.length + readiness.length
}
