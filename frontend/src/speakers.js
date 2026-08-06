const USER_COLOR = '#a1a1aa'

export function participantInfo(slug, participants) {
  if (slug === 'user') {
    return { label: 'You', color: USER_COLOR, isUser: true }
  }
  if (slug.startsWith('ext:')) {
    // external event producers (calendar, CI, …): badge by source
    return { label: slug.slice(4), color: '#5b9dd9', isUser: false }
  }
  if (slug === 'system') {
    // tooling notices (deploy status etc.) — muted, clearly non-participant
    return { label: 'System', color: '#8b8fa3', isUser: false }
  }
  if (slug === 'claude-code') {
    // the summoned guest specialist — not a roster participant
    return { label: 'Claude Code', color: '#c15f3c', isUser: false }
  }
  const p = (participants || []).find((x) => x.slug === slug)
  return {
    label: p?.name || slug,
    color: p?.color || '#a1a1aa',
    isUser: false,
  }
}

// '#rrggbb' + alpha byte, for tinted bubbles
export function withAlpha(hex, alphaByte) {
  return /^#[0-9a-fA-F]{6}$/.test(hex) ? hex + alphaByte : hex
}
