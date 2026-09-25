// The pass on screen (#98, #456). A seat that replies with a bare [pass]
// stays silent, and the app hides the turn: the server saves nothing and
// the round stream sends `passed` so the streamed bubble goes. Two gaps
// let a pass show anyway. While a reply streams, its first characters
// ("[", "[pa") drew as text until `passed` arrived. And a barge-in aborts
// the reader, so a `passed` still on the wire never lands: the bubble kept
// "[pass" as text, and a seat cut off before writing anything kept an
// empty bubble (its name, nothing under it). Copy chat printed both.
//
// Pure module, node --test: Message.jsx, ThreadView.jsx, useRoundStream.js
// and App.jsx's Copy chat only apply what these return.

// The backend's token (backend/passes.py PASS_TOKEN), pinned through
// tests/fixtures/backend_contract.json.
export const PASS_TOKEN = '[pass]'

// Text that is the pass token or the start of it, whitespace and case
// aside: "[", "[p" ... "[pass]". backend/passes.py is_cut_pass is the same
// rule. A streaming reply that matches could still become a pass, and a
// finished or cut-off one that matches was one.
export function isPassShaped(text) {
  const t = (text || '').trim().toLowerCase()
  return t.length > 0 && PASS_TOKEN.startsWith(t)
}

// The text a message shows. A seat's pass-shaped text shows as nothing,
// so a streaming pass keeps the thinking dots instead of drawing brackets.
// The user's own turns always show as typed.
export function shownText(msg) {
  const content = msg?.content || ''
  if (!msg || msg.speaker === 'user') return content
  return isPassShaped(content) ? '' : content
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
