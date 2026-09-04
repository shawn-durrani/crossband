// Owner-discard of a captured voice turn (#106) - pure, node --test'd.
//
// The rule this module owns: the confirm step must say EXACTLY what a
// discard can and cannot undo, because live capture reaching the chat is a
// privacy event and vague copy would overpromise.

// Only the owner's own VOICE turns carry the affordance: a typed message
// was deliberate, and other speakers' turns are not yours to discard.
export function canDiscard(msg) {
  return Boolean(msg && msg.speaker === 'user' && msg.voice_turn_id
                 && !msg.streaming)
}

// The honest confirm copy. `hasLaterReplies`: a model already answered
// after this turn this session, so its content was read regardless.
export function discardWarnings({ hasLaterReplies }) {
  const out = ['Removes this turn from the chat and from everything the '
               + 'models see from now on.']
  if (hasLaterReplies) {
    out.push('Models already read it this conversation - replies that '
             + 'exist stay.')
  }
  out.push('If a copy already reached memory it stays there (memory is '
           + 'append-only); this stops any future copy.')
  return out
}

// #111: turn the discard response's memory ref into membro's erase deep
// link. `origin` is the browser-reachable origin membro itself reports
// (contract 1.4 `browser_origin`), so the link works from a phone on the
// tailnet. Without it (an older membro) fall back to the host the browser
// is already on with membro's fixed port. Ref segments encoded. Null when
// there is nothing to link (not ingested, or an older server without the
// ref).
export function eraseLink(ref, hostname, origin = '') {
  if (!ref || !ref.source_app || !ref.conversation || !ref.message) return null
  const seg = (s) => encodeURIComponent(String(s))
  const path = `#erase=${seg(ref.source_app)}/${seg(ref.conversation)}/${seg(ref.message)}`
  const base = typeof origin === 'string' ? origin.replace(/\/+$/, '') : ''
  if (base) return `${base}/${path}`
  if (!hostname) return null
  return `http://${hostname}:8901/${path}`
}
