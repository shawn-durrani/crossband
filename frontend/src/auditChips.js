// Attribution-audit chips (#211): render copy for a message row's
// `audit_flags` column. A finding means one thing only: the quoted claim has
// no word-for-word match in that speaker's messages in the window the model
// could see. Paraphrase, or an older message folded into the rolling
// summary, produces the same flag for a perfectly true quote - so the copy
// reads as a prompt to check, never a verdict. All decision logic lives here
// (pure, node --test); Message.jsx only renders what this returns.

const MAX_CLAIM = 60

export function auditChips(auditFlagsJson) {
  let flags
  try {
    flags = JSON.parse(auditFlagsJson || '[]')
  } catch {
    return []
  }
  if (!Array.isArray(flags)) return []
  const out = []
  for (const f of flags) {
    if (!f || f.kind !== 'attribution' || !f.who || !f.claim) continue
    const claim = String(f.claim)
    const shown = claim.length > MAX_CLAIM
      ? `${claim.slice(0, MAX_CLAIM - 1)}…`
      : claim
    out.push({
      label: `“${shown}” – not found in ${f.who}'s messages here`,
      title:
        `This reply quotes ${f.who} saying something with no word-for-word ` +
        `match in ${f.who}'s messages in the visible window. A paraphrase, ` +
        'or an older message folded into the summary, looks exactly the ' +
        'same - treat this as a prompt to check, not proof of a misquote.',
    })
  }
  return out
}
