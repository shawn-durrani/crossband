// The header's row of links to the owner's other apps - pure, node --test'd.
//
// GET /api/app-links names each sibling app that answered on this machine,
// with two addresses: `origin`, where the app says a browser can open it
// (its tailnet address when it's served there), and `local`, its loopback
// address. Which one works depends on where THIS page was opened:
//
// - on the Mac itself (127.0.0.1 or localhost), every sibling opens at its
//   loopback address, and a page on `localhost` keeps that name so a
//   passkey made there still offers itself;
// - anywhere else (the tailnet, from a phone), only a sibling that reports
//   a non-loopback address can open, so the rest are left out.
//
// The current app sits in the row too, marked and not linked. Names sort
// alphabetically, so every app's row reads the same.

const LOOPBACK = new Set(['localhost', '127.0.0.1', '::1', '[::1]'])

export function isLoopbackHost(hostname) {
  const h = String(hostname || '').toLowerCase()
  return LOOPBACK.has(h) || h.startsWith('127.')
}

function parse(url) {
  try {
    const u = new URL(url)
    return u.protocol === 'http:' || u.protocol === 'https:' ? u : null
  } catch {
    return null
  }
}

// One sibling's href for a page at `pageHost`, or '' when it can't open there.
export function siblingHref(pageHost, sibling) {
  if (isLoopbackHost(pageHost)) {
    const u = parse(sibling?.local)
    if (!u || !isLoopbackHost(u.hostname)) return ''
    if (String(pageHost).toLowerCase() === 'localhost') u.hostname = 'localhost'
    return u.origin + '/'
  }
  const u = parse(sibling?.origin)
  if (!u || isLoopbackHost(u.hostname)) return ''
  return u.origin + '/'
}

// The row for a page at `pageHost`: [{ name, href, current }], sorted by
// name, or [] when no sibling can open from here (a row of one says nothing).
export function linkRow(pageHost, current, apps) {
  const links = []
  for (const s of apps || []) {
    if (!s?.name || s.name === current) continue
    const href = siblingHref(pageHost, s)
    if (href) links.push({ name: s.name, href, current: false })
  }
  if (!links.length) return []
  links.push({ name: current, href: '', current: true })
  return links.sort((a, b) => a.name.localeCompare(b.name))
}
