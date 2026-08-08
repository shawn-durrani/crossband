import { useCallback, useEffect, useState } from 'react'
import { api, setUnauthorizedHandler } from './api'
import LockScreen from './components/LockScreen'
import { gateView } from './lockState'

/* The gate around the whole app (#25). App.jsx stays untouched: this wrapper
   probes /api/auth/session before mounting it, swaps in the LockScreen while
   locked, and re-locks the moment any API call comes back 401 (logout in
   another tab, session expiry, a restart). On an unenrolled install the app
   runs exactly as before, with a slim banner offering to set the password. */

export default function AuthGate({ children }) {
  const [session, setSession] = useState(null)
  const [showSetup, setShowSetup] = useState(false)
  const [setupDismissed, setSetupDismissed] = useState(
    () => sessionStorage.getItem('gate_nudge_dismissed') === '1')

  const probe = useCallback(async () => {
    try {
      setSession(await api.authSession())
    } catch {
      // Backend down or restarting (a deploy): retry until it answers, so the
      // gate never wrongly settles on a stale view.
      setTimeout(probe, 2000)
    }
  }, [])

  useEffect(() => { probe() }, [probe])
  useEffect(() => {
    setUnauthorizedHandler(() =>
      setSession((s) => (s && s.enrolled ? { ...s, authenticated: false } : s)))
    return () => setUnauthorizedHandler(null)
  }, [])

  if (gateView(session) === 'probing') {
    return <div className="fixed inset-0 bg-app" aria-busy="true" />
  }

  if (gateView(session, !!window.PublicKeyCredential) === 'open') {
    return (
      <>
        {!session.enrolled && !setupDismissed && (
          <div className="fixed top-0 inset-x-0 z-40 bg-panel border-b border-edge2
                          px-4 py-2 text-sm text-ink-mid flex items-center gap-3">
            <span>No unlock password is set - anything on this Mac can use this app.</span>
            <button className="underline underline-offset-2 hover:text-ink-hi"
                    onClick={() => setShowSetup(true)}>
              Set one now
            </button>
            <button className="ml-auto hover:text-ink-hi" aria-label="Dismiss"
                    onClick={() => { setSetupDismissed(true); sessionStorage.setItem('gate_nudge_dismissed', '1') }}>
              ✕
            </button>
          </div>
        )}
        {showSetup && (
          <LockScreen session={session} mode="setup"
                      onUnlocked={() => { setShowSetup(false); probe() }}
                      onDismiss={() => setShowSetup(false)} />
        )}
        {children}
      </>
    )
  }

  return <LockScreen session={session} onUnlocked={probe} />
}
