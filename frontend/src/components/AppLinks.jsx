import { useEffect, useState } from 'react'
import { api } from '../api'
import { linkRow } from '../appLinks'

// A thin row above every page linking the owner's other apps: membro,
// spendglass and threadfold, each only when it can open from where this
// page was opened (appLinks.js has the rule). Absent when none can.
// Links open a new tab, so leaving never ends a live voice call here.
export default function AppLinks() {
  const [apps, setApps] = useState([])
  useEffect(() => {
    let live = true
    api.appLinks()
      .then((r) => { if (live) setApps(r?.apps || []) })
      .catch(() => {})  // no row is the whole fallback
    return () => { live = false }
  }, [])
  const row = linkRow(window.location.hostname, 'crossband', apps)
  if (!row.length) return null
  return (
    <nav
      aria-label="Your apps"
      className="shrink-0 border-b border-edge px-3 sm:px-5 flex items-center gap-1 overflow-x-auto text-xs"
    >
      {row.map((l) => (l.current ? (
        <span key={l.name} aria-current="page" className="px-1.5 py-1.5 font-semibold text-ink">
          {l.name}
        </span>
      ) : (
        <a
          key={l.name}
          href={l.href}
          target="_blank"
          rel="noopener noreferrer"
          title={`Open ${l.name} in a new tab`}
          className="px-1.5 py-1.5 rounded text-ink-dim hover:text-ink hover:underline underline-offset-2"
        >
          {l.name}
        </a>
      )))}
    </nav>
  )
}
