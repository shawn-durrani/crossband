// The app's own short line in front of a reply that waits on a search of
// the saved chats (backend/history_prefetch.py, membro#136). The round
// sends it as the seat's first delta, with SPEAK_NOW_FLAG set. The screen
// shows it like any delta. The voice speaks it straight away and flushes
// it: TTS makes no audio until it holds a first chunk or is flushed, and
// the pass gate holds a short reply that could still be a pass, so the
// line would otherwise wait for the very reply it's covering for. It's
// the app's words, never the model's, so it can't be a pass.
//
// Pure module, node --test: voice.js only applies what this returns. The
// flag is pinned through tests/fixtures/backend_contract.json.

export const SPEAK_NOW_FLAG = 'speak_now'

// True for a round event the voice speaks at once, flushed.
export function speaksNow(ev) {
  return ev?.type === 'delta' && ev[SPEAK_NOW_FLAG] === true
    && typeof ev.text === 'string' && ev.text.trim() !== ''
}
