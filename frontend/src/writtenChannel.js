// The written-deliverable channel (#80).
//
// A participant gets exactly one message per round, and voice mode pushes
// replies to be short - so long-form deliverables (a ranked list, a table)
// kept being deferred to a "next message" that structurally cannot exist.
// The channel ends that: a reply may put the token [written] on a line, and
// everything from the token on is transcript-only. The voice client speaks
// the summary before the token and feeds nothing after it to TTS.
//
// Pure module, no React and no sockets, so the streaming edge cases are
// unit-tested directly. The token literal is pinned on both sides: the
// backend's prompt (backend/providers.py WRITTEN_TOKEN) tells models to use
// exactly this string.

export const WRITTEN_TOKEN = '[written]'

// How much of the buffer tail could still become the token if more text
// arrives. Streaming deltas split anywhere ("[writ" + "ten]"), so the filter
// holds a possible prefix back instead of speaking half a marker.
function holdbackLen(s) {
  const max = Math.min(s.length, WRITTEN_TOKEN.length - 1)
  for (let k = max; k > 0; k--) {
    if (WRITTEN_TOKEN.startsWith(s.slice(s.length - k))) return k
  }
  return 0
}

// Streaming filter, one per speaker per turn. feed(chunk) returns the text
// now safe to speak; once the token has streamed past, everything else
// returns ''. flush() releases a held tail that never became the token -
// call it at speaker end so trailing words are not lost.
export class WrittenFilter {
  constructor() {
    this.buf = ''
    this.done = false
  }

  feed(chunk) {
    if (this.done) return ''
    this.buf += chunk || ''
    const i = this.buf.indexOf(WRITTEN_TOKEN)
    if (i !== -1) {
      this.done = true
      const out = this.buf.slice(0, i)
      this.buf = ''
      return out
    }
    const hold = holdbackLen(this.buf)
    const out = this.buf.slice(0, this.buf.length - hold)
    this.buf = this.buf.slice(this.buf.length - hold)
    return out
  }

  flush() {
    if (this.done) return ''
    const out = this.buf
    this.buf = ''
    return out
  }
}

// Transcript rendering: the raw token line reads as noise, so the first
// token becomes a labelled divider. Everything below it renders as normal
// markdown - that is the point of the channel.
export function renderWritten(text) {
  if (!text || !text.includes(WRITTEN_TOKEN)) return text
  return text.replace(WRITTEN_TOKEN,
    '\n\n---\n*Written deliverable - in the transcript, not spoken:*\n')
}
