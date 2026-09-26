// The pass on screen and in the voice (#98, #456, #460). A seat that
// replies with a bare [pass] stays silent, and the app hides the turn: the
// server saves nothing and the round stream sends `passed` so the streamed
// bubble goes. Two gaps let a pass show anyway. While a reply streams, its
// first characters ("[", "[pa") drew as text until `passed` arrived. And a
// barge-in aborts the reader, so a `passed` still on the wire never lands:
// the bubble kept "[pass" as text, and a seat cut off before writing
// anything kept an empty bubble (its name, nothing under it). Copy chat
// printed both.
//
// #460: seats also wrote a remark and then the token ("Nothing to add.
// [pass]"), and the whole thing was saved, shown and spoken. The server
// now reads a reply that ends with [pass] by what comes before it
// (backend/passes.py is_pass): a short remark that says the seat has
// nothing to add or is staying quiet makes it a pass, and anything else is
// a real reply kept without the token. The rule here is the same rule,
// pinned through tests/fixtures/backend_contract.json.
//
// Pure module, node --test: Message.jsx, ThreadView.jsx, useRoundStream.js,
// voice.js and App.jsx's Copy chat only apply what these return.

// The backend's token (backend/passes.py PASS_TOKEN), pinned through
// tests/fixtures/backend_contract.json.
export const PASS_TOKEN = '[pass]'

// backend/passes.py QUIET_PHRASE_PATTERN, character for character: what a
// quiet remark says, matched against one normalised clause.
export const QUIET_PHRASE_PATTERN = String.raw`\b(?:`
  + String.raw`nothing (?:(?:new|more|further|else|much|useful|really) )*`
  + String.raw`(?:to (?:add|say|contribute)|from me|from my end|on my end|`
  + String.raw`on my side|here)`
  + String.raw`|nothing(?: (?:new|more|further|else))?$`
  + String.raw`|(?:don't|do not|didn't|haven't|have not|not) (?:have |got |see )?`
  + String.raw`(?:anything|much|a lot)(?: (?:new|more|further|else))? `
  + String.raw`to (?:add|say|contribute)`
  + String.raw`|no (?:(?:further|more|new) )?(?:input|comments?|thoughts|additions)`
  + String.raw`|(?:stay|stays|staying|stayed|keep|keeps|keeping|remain|remaining|`
  + String.raw`be|being|go|going) (?:quiet|quietly|silent)`
  + String.raw`|(?:quiet|silent|listening|eavesdropping|lurking) mode`
  + String.raw`|(?:hold|holds|holding|held) (?:back|off|fire)`
  + String.raw`|(?:sit|sits|sitting) (?:this|that) (?:one )?out`
  + String.raw`|stay(?:ing)? out of (?:it|this|the way)`
  + String.raw`|passing`
  + String.raw`|pass(?=$| for now| on this| this (?:one|time|round|turn))`
  + String.raw`|listening(?=$| in| quietly| for now| along)`
  + String.raw`|eavesdropping|lurking|standing by`
  + String.raw`|(?:leave|let) (?:you|you two|you both|you all|the two of you|`
  + String.raw`y'all) (?:to it|carry on|chat|talk|continue|get on with it)`
  + String.raw`|(?:not|wasn't|was not|isn't|is not) `
  + String.raw`(?:(?:a question|one) (?:for|to)|aimed at|addressed to|`
  + String.raw`meant for|directed at) (?:me|us)`
  + String.raw`|(?:not|wasn't|isn't|nobody|no one|nobody's|no one's) `
  + String.raw`(?:asking|addressing|asked|addressed) (?:me|us)`
  + String.raw`|no question (?:for|aimed at|to) (?:me|us)`
  + String.raw`)\b`
// backend/passes.py CLAUSE_BREAK_PATTERN, QUIET_MAX_CHARS, QUIET_CONTRAST
// and AFTER_TOKEN.
export const CLAUSE_BREAK_PATTERN = String.raw`[.,;:!\u2026\n]+|\s[-\u2013\u2014]\s|[\u2013\u2014]`
export const QUIET_MAX_CHARS = 100
export const QUIET_CONTRAST = new Set(['but', 'though', 'although', 'however', 'except'])
export const AFTER_TOKEN = '.!*_~`"\')\u2026'

const QUIET_PHRASE = new RegExp(QUIET_PHRASE_PATTERN)
const CLAUSE_BREAK = new RegExp(CLAUSE_BREAK_PATTERN)

function norm(text) {
  return (text || '').toLowerCase().replace(/[\u2018\u2019]/g, "'")
}

// Lowercased words, straight apostrophes, quotes trimmed off the ends.
// backend/passes.py _words is the same rule.
export function wordsOf(text) {
  return (norm(text).match(/[a-z0-9']+/g) || [])
    .map((w) => w.replace(/^'+|'+$/g, ''))
    .filter(Boolean)
}

function clausesOf(text) {
  return norm(text).split(CLAUSE_BREAK).map((c) => wordsOf(c).join(' '))
}

// A question, a number or a turn ("but") anywhere: the seat had something
// to say.
function barred(text, words) {
  return text.includes('?') || /[0-9]/.test(text) || words.some((w) => QUIET_CONTRAST.has(w))
}

// A short remark that says the seat has nothing to add or is staying
// quiet, or no words at all. backend/passes.py is_quiet_remark.
export function isQuietRemark(text) {
  const t = text || ''
  const words = wordsOf(t)
  if (words.length === 0) return true
  if (t.trim().length > QUIET_MAX_CHARS || barred(t, words)) return false
  return clausesOf(t).some((c) => c && QUIET_PHRASE.test(c))
}

// { before, tail }: the text with a trailing pass token taken off,
// whitespace, case and trailing punctuation aside. With `partial`, a
// trailing start of the token ("[", "[p" ... "[pass") counts too.
// backend/passes.py _split_tail.
export function splitPassTail(text, partial = false) {
  const s = (text || '').trimEnd()
  const low = s.toLowerCase()
  let end = low.length
  while (end > 0 && (AFTER_TOKEN + ' \t\n').includes(low[end - 1])) end--
  if (low.slice(0, end).endsWith(PASS_TOKEN)) {
    return { before: s.slice(0, end - PASS_TOKEN.length), tail: true }
  }
  if (partial) {
    for (let n = PASS_TOKEN.length - 1; n > 0; n--) {
      if (low.endsWith(PASS_TOKEN.slice(0, n))) return { before: s.slice(0, -n), tail: true }
    }
  }
  return { before: s, tail: false }
}

// Text that is a pass, or was on its way to one when it stopped: the pass
// token or any start of it ("[", "[p" ... "[pass]"), alone or after a
// quiet remark ("Nothing to add. [pa"). backend/passes.py is_cut_pass is
// the same rule.
export function isPassShaped(text) {
  if (!(text || '').trim()) return false
  const { before, tail } = splitPassTail(text, true)
  return tail && isQuietRemark(before)
}

// A real reply with the pass token (or a start of it) taken off its end.
// The server saves the reply without it; this covers the stream and the
// rows saved before the server did. backend/passes.py strip_pass.
export function stripPassTail(text) {
  const { before, tail } = splitPassTail(text, true)
  return tail && before.trim() ? before.trimEnd() : (text || '')
}

// The words a streaming reply may be made of while the screen keeps
// showing the thinking dots: the ones quiet remarks are built from. A
// reply shows from its first other word, so a real reply streams as it
// always has. A quiet remark with other words in it ("you two carry on
// with the plan, passing") shows while it streams and goes when `passed`
// arrives.
export const QUIET_WORDS = new Set([
  'a', 'add', 'addressed', 'again', 'all', 'along', 'am', 'an', 'and', 'anyone',
  'anything', 'are', 'as', 'ask', 'asked', 'at', 'back', 'be', 'being', 'both',
  'by', 'chat', 'contribute', "didn't", 'do', "don't", 'eavesdropping', 'else',
  'end', 'everyone', 'for', 'from', 'further', 'go', 'going', 'got', 'have',
  "haven't", 'here', 'hold', 'holding', 'i', "i'd", "i'll", "i'm", "i've", 'in',
  'input', 'is', 'it', 'just', 'keep', 'keeping', 'let', 'listening', 'lurking',
  'me', 'mode', 'more', 'much', 'my', 'new', 'no', 'not', 'nothing', 'now', 'of',
  'off', 'ok', 'okay', 'on', 'one', 'out', 'pass', 'passing', 'per', 'quiet',
  'quietly', 'remain', 'request', 'requested', 'right', 'room', 'say', 'side',
  'silent', 'sit', 'sitting', 'so', 'someone', 'standing', 'stay', 'staying',
  'still', 'talk', 'that', 'the', 'this', 'thoughts', 'time', 'to', 'turn', 'two',
  'understood', 'unless', 'until', 'up', 'us', 'we', 'while', 'will', 'you', 'your',
])

// A streaming reply the screen and the voice both hold back: pass-shaped
// already, or every word so far one of QUIET_WORDS, the last one perhaps
// still half written. The voice opens no speech for it, so a bare pass or
// a remark of quiet words never makes a sound.
export function couldBePass(text) {
  const t = text || ''
  const { before, tail } = splitPassTail(t, true)
  if (tail) return isQuietRemark(before)
  const words = wordsOf(t)
  // Streaming splits anywhere, so an unfinished last word only has to be
  // the start of a quiet word.
  const open = /[a-z0-9'\u2018\u2019]$/i.test(t)
  return words.every((w, i) => QUIET_WORDS.has(w) || (open && i === words.length - 1
    && [...QUIET_WORDS].some((q) => q.startsWith(w))))
}

// Could this streaming reply still end as a pass under the server's rule?
// True while it is no longer than a quiet remark can be and holds no
// question, number or turn, or when it already reads as a quiet remark
// and a start of the token. The voice holds a reply's text while this is
// true. That costs no time to first audio: QUIET_MAX_CHARS is under the
// TTS first chunk, and TTS makes no audio until it holds a first chunk or
// is flushed (voicePassGate.test.js measures it against today's path).
export function couldStillBePass(text) {
  const t = text || ''
  const { before, tail } = splitPassTail(t, true)
  if (tail && isQuietRemark(before)) return true
  if (t.trim().length > QUIET_MAX_CHARS) return false
  if (t.includes('?') || /[0-9]/.test(t)) return false
  const words = wordsOf(t)
  const complete = /[a-z0-9'\u2018\u2019]$/i.test(t) ? words.slice(0, -1) : words
  return !complete.some((w) => QUIET_CONTRAST.has(w))
}

// The text a message shows. A seat's pass shows as nothing, so a streaming
// pass keeps the thinking dots instead of drawing brackets or a quiet
// remark, and a real reply never shows a trailing [pass]. The user's own
// turns always show as typed.
export function shownText(msg) {
  const content = msg?.content || ''
  if (!msg || msg.speaker === 'user') return content
  if (msg.streaming ? couldBePass(content) : isPassShaped(content)) return ''
  return stripPassTail(content)
}

// A seat turn with nothing to show: not streaming, no error, no tool use,
// no attachment, and no text once a pass is hidden. It draws no bubble.
// A streaming turn always draws (its thinking dots), and so does an error.
export function isSilentSeatTurn(msg) {
  if (!msg || msg.speaker === 'user' || msg.streaming || msg.error) return false
  if (msg.tool_events?.length || msg.attachments?.length) return false
  return shownText(msg).trim() === ''
}

// The rows the transcript draws.
export function visibleMessages(messages) {
  return (messages || []).filter((m) => !isSilentSeatTurn(m))
}

// Copy chat's markdown. A seat turn with no text to show is skipped,
// since it holds no words and the chat never kept it. The user's turns
// stay, with "(no text)" for one that was only an attachment.
export function chatTranscript(title, messages, labelOf) {
  const lines = [`# ${title}`, '']
  for (const m of messages || []) {
    const text = shownText(m).trim()
    if (m.speaker !== 'user' && !text) continue
    lines.push(`**${labelOf(m.speaker)}:** ${text || '(no text)'}`, '')
  }
  return lines.join('\n').trim() + '\n'
}

// What one speaker's reply sends to TTS (#460), one gate per speaker per
// turn, ahead of the written channel's filter. feed(chunk) returns the text
// now safe to speak. While the reply could still end as a pass
// (couldStillBePass) it holds everything. After that it passes text on,
// drops a complete token wherever one lands, and holds a tail that could
// still become one ("[", "[pa") until the next chunk shows what it was.
// flush() at the end of the reply returns what is still held, unless the
// reply was a pass, and drops a half-written token.
export class PassSpeechGate {
  constructor() {
    this.all = ''
    this.released = false
    this.hold = ''
  }

  feed(chunk) {
    const c = chunk || ''
    this.all += c
    if (!this.released) {
      if (couldStillBePass(this.all)) return ''
      this.released = true
      return this._tokens(this.all)
    }
    return this._tokens(c)
  }

  _tokens(chunk) {
    let buf = this.hold + chunk
    let low = buf.toLowerCase()
    for (let i = low.indexOf(PASS_TOKEN); i !== -1; i = low.indexOf(PASS_TOKEN)) {
      buf = buf.slice(0, i) + buf.slice(i + PASS_TOKEN.length)
      low = buf.toLowerCase()
    }
    let keep = 0
    for (let k = Math.min(low.length, PASS_TOKEN.length - 1); k > 0; k--) {
      if (PASS_TOKEN.startsWith(low.slice(low.length - k))) { keep = k; break }
    }
    this.hold = buf.slice(buf.length - keep)
    return buf.slice(0, buf.length - keep)
  }

  flush() {
    let out = ''
    if (!this.released) {
      this.released = true
      if (!isPassShaped(this.all)) out = this._tokens(stripPassTail(this.all))
    }
    this.hold = ''
    return out
  }
}
