// What the chat header derives before it renders anything.
//
// Pulled out of App.jsx during the header extraction so the arithmetic is
// unit-tested rather than re-derived inline - and so it exists in exactly one
// place. It did not: the header ring and the bar above the composer both
// computed "how full is the context" from the same budget and then disagreed
// about it, the ring turning red at 85% in hex literals while the bar turned
// red at 90% in CSS variables. Two indicators of one quantity, contradicting
// each other on screen. One function now answers the question.

// The budget the gauge is a fraction OF. Not a hard limit - nothing refuses to
// send at 100% - it is the point past which answers measurably get vaguer, so
// the number is advice, not enforcement. Named once; it was written twice.
export const CONTEXT_BUDGET = 150000

// Escalating bands. `level` is the meaning; callers pick their own presentation
// from it, which is how the ring (inline colour) and the composer bar (CSS
// custom properties) can look different without disagreeing about the state.
const LEVELS = [
  { at: 85, level: 'critical', tone: '#ef4444', cssTone: 'var(--t-err)' },
  { at: 60, level: 'high', tone: '#f97316', cssTone: 'var(--t-warn)' },
  { at: 32, level: 'elevated', tone: '#f59e0b', cssTone: 'var(--t-warn)' },
  { at: 0, level: 'comfortable', tone: '#10b981', cssTone: 'var(--t-accent)' },
]

// Compact token count: 1.2k rather than 1200. Kept here because both the ring
// and its tooltip need the same rounding.
export function fmtTokens(n) {
  return n >= 1000 ? `${(n / 1000).toFixed(0)}k` : `${n}`
}

// The gauge for one chat's context breakdown, or null when the server hasn't
// reported one yet (an empty chat, or a chat still loading).
//
// `ctx` is activeChat.context: { total, history, research, chat_summary,
// memory, overhead }.
export function contextGauge(ctx) {
  if (!ctx || typeof ctx.total !== 'number') return null
  const total = ctx.total
  const pct = Math.min(100, (total / CONTEXT_BUDGET) * 100)
  const band = LEVELS.find((l) => pct >= l.at) || LEVELS[LEVELS.length - 1]
  // Megabytes re-sent per turn. A token count alone under-sells a chat
  // whose real weight is re-uploaded attachments rather than conversation
  // history; that chat feels slow for a reason the token gauge never shows.
  const uploadMb = Math.round((ctx.upload_bytes || 0) / 1e5) / 10
  return {
    uploadMb,
    total,
    pct,
    level: band.level,
    tone: band.tone,
    cssTone: band.cssTone,
    label: `~${fmtTokens(total)}`,
    // Says what the number IS and why it matters, not just how big it is -
    // a percentage with no consequence attached is a number nobody acts on.
    title:
      `What every reply re-reads: ~${fmtTokens(total)} tokens (~${Math.round(pct)}% of a sensible budget)\n`
      + `· conversation history ~${fmtTokens(ctx.history || 0)}\n`
      + (ctx.attachments
        ? `· attachments ~${fmtTokens(ctx.attachments)}`
          + (ctx.image_count ? ` (${ctx.image_count} image${ctx.image_count > 1 ? 's' : ''})` : '')
          + '\n'
        : '')
      + `· research logs ~${fmtTokens(ctx.research || 0)}\n`
      + `· summaries & memory ~${fmtTokens((ctx.chat_summary || 0) + (ctx.memory || 0))}\n`
      + `· instructions & tools ~${fmtTokens(ctx.overhead || 0)}\n`
      + (uploadMb >= 1
        ? `\nRe-uploaded EVERY turn, to each participant: ~${uploadMb} MB. `
          + 'This is sent again with every message and grows as you add files - '
          + 'usually the real reason a chat starts feeling slow.\n'
        : '')
      + '\nBig contexts dilute attention - answers get vaguer as this grows. '
      + 'A fresh chat stays sharp, and memory carries over.',
  }
}

// The chat's token/voice totals line, as data. The dollar figures come from
// messageCost.chatCostFigures - this only decides what else appears beside
// them, and whether the cluster is worth showing at all.
export function usageSummary(chatTotal, voice) {
  const tokens = chatTotal?.tokens || 0
  const chars = voice?.tts_chars || 0
  if (!tokens && !chars) return null
  return {
    tokens: `Σ ${fmtCount(tokens)} tok`,
    voice: chars > 0
      ? ` · voice ${fmtCount(chars)} ch`
        + (voice.stt_seconds > 0 ? ` / ${Math.round(voice.stt_seconds)}s` : '')
        + (voice.cost > 0 ? ` · ~$${voice.cost.toFixed(3)}` : '')
      : '',
  }
}

// One decimal for the running totals (12.4k / 3.1M), where the gauge rounds
// to whole thousands. Deliberately different: a total is a measurement and the
// gauge is an indicator. The M tier exists because a multi-million-token chat
// used to render as a five-digit "k" figure, which is easy to misread by a
// factor of a thousand.
export function fmtCount(n) {
  if (n >= 1_000_000) return `${(n / 1_000_000).toFixed(1)}M`
  return n >= 1000 ? `${(n / 1000).toFixed(1)}k` : `${n}`
}

// The header's capability menu: the four per-chat modes as data, so the
// popover renders from one list and the on/off truth stays in the chat row.
// Each entry keeps the SAME plain-English explainer the standalone chips
// carried - collapsing the chrome must not lose the explanations, which are
// the product's voice. `code` only exists when the machine offers it.
export function capabilityRows(chat, cfg) {
  if (!chat) return []
  const rows = [
    {
      key: 'voice', label: 'Voice mode', on: !!chat.voice_mode,
      hint: chat.voice_mode
        ? 'ON - replies are short and spoken-style'
        : 'OFF - turn on for short, spoken-style replies',
    },
    {
      key: 'memory', label: 'Memory', on: !!chat.memory_enabled,
      hint: chat.memory_enabled
        ? 'ON - your shared memory summary is included for all participants'
        : 'OFF - participants know nothing about you in this chat',
    },
    {
      key: 'web', label: 'Web research', on: !!chat.web_enabled,
      hint: chat.web_enabled
        ? `ON - search: ${cfg?.search?.tavily || cfg?.search?.brave ? [cfg.search.tavily && 'Tavily', cfg.search.brave && 'Brave'].filter(Boolean).join(' + ') : 'no search key (page/Reddit fetch only)'}`
        : 'OFF - turn on to let models search and fetch pages',
    },
  ]
  if (cfg?.code?.available || cfg?.github?.available) {
    rows.push({
      key: 'code', label: 'Code', on: !!chat.code_enabled,
      hint: chat.code_enabled
        ? `ON - the AIs can ${[cfg.code?.available && (cfg.code?.writes ? 'summon Claude Code (investigate, or implement + open PRs - never merge)' : 'summon Claude Code (read-only)'), cfg.github?.available && 'read & file GitHub issues'].filter(Boolean).join(' and ')}`
        : 'OFF - turn on to let the AIs work with your repos',
    })
  }
  return rows
}
