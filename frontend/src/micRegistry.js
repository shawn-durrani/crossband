// #134: every live microphone, visible from every surface - the pure
// rules behind the banner. The field failure this closes: two capture
// sessions ran at once, the owner ended the one in front of them, and
// the orphan kept hearing the room with nothing anywhere showing it.

export function foreignCaptures(captures, ownSid) {
  return (captures || []).filter((c) => c.sid !== ownSid)
}

// The banner's verdict: null (nothing to say), or {level, text}.
// 'double' outranks 'elsewhere': a second mic in THIS chat means every
// utterance is heard twice - that is both a privacy problem and the
// doubled-turn mess in the transcript.
export function captureBanner(captures, ownSid, activeChatId) {
  const foreign = foreignCaptures(captures, ownSid)
  if (!foreign.length) return null
  const here = foreign.filter((c) => String(c.chat_id) === String(activeChatId))
  const when = (c) => new Date(c.started_at * 1000)
    .toLocaleTimeString([], { hour: '2-digit', minute: '2-digit' })
  if (here.length && ownSid) {
    return { level: 'double', sessions: foreign,
             text: `Two microphones are live in this chat (the other since ${when(here[0])}) - every utterance is heard twice. End the other one.` }
  }
  return { level: 'elsewhere', sessions: foreign,
           text: foreign.length === 1
             ? `A microphone is live in another window (since ${when(foreign[0])}).`
             : `${foreign.length} microphones are live in other windows.` }
}
