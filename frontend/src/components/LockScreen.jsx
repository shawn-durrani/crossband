import { useState } from 'react'
import { api } from '../api'
import { setupProblem } from '../lockState'
import {
  ceremonyErrorCopy, decodeRequestOptions, serialiseAssertion,
} from '../webauthnCodec'

/* The lock screen (#25): passkey-first unlock once one is enrolled for this
   origin, password one click behind it, first-run enrolment, and
   recovery-secret reset. Rendered by AuthGate INSTEAD of the app (locked),
   or over it as an overlay (enrolment nudge on an unenrolled install).
   Deliberately minimal: nothing here fetches anything but the auth surface,
   and nothing sensitive is ever rendered into it. */

const inputCls = 'mt-1 w-full bg-app border border-edge2 rounded-lg px-3 py-2 ' +
  'text-sm focus:outline-none focus:border-edge3'
const buttonCls = 'w-full rounded-lg px-3 py-2 text-sm font-medium ' +
  'bg-ink-hi text-app hover:opacity-90 disabled:opacity-50'
const linkCls = 'text-sm text-ink-mid hover:text-ink-hi underline-offset-2 hover:underline'

export default function LockScreen({ session, mode: initialMode, onUnlocked, onDismiss }) {
  // modes: 'passkey' | 'login' | 'setup' | 'reset' - setup and reset share
  // the form, differing only in endpoint and copy (both recovery-gated
  // server-side). Passkey leads only when this origin has one AND the
  // browser can do WebAuthn.
  const [mode, setMode] = useState(initialMode || (
    !session?.enrolled ? 'setup'
      : session?.passkey && window.PublicKeyCredential ? 'passkey' : 'login'))
  const [password, setPassword] = useState('')
  const [recovery, setRecovery] = useState('')
  const [confirm, setConfirm] = useState('')
  const [err, setErr] = useState('')
  const [busy, setBusy] = useState(false)

  async function submitLogin(e) {
    e.preventDefault()
    setBusy(true); setErr('')
    try {
      await api.authLogin(password)
      onUnlocked()
    } catch (ex) {
      setErr(ex.message === 'wrong password' ? 'Wrong password.' : ex.message)
      setBusy(false)
    }
  }

  async function passkeyUnlock() {
    setBusy(true); setErr('')
    try {
      const o = await api.webauthnLoginOptions()
      const cred = await navigator.credentials.get(
        { publicKey: decodeRequestOptions(o.publicKey) })
      await api.webauthnLogin({ cid: o.cid, credential: serialiseAssertion(cred) })
      onUnlocked()
    } catch (ex) {
      setErr(ceremonyErrorCopy(ex, 'login'))
      setMode('login')
      setBusy(false)
    }
  }

  async function submitSetup(e) {
    e.preventDefault()
    const problem = setupProblem({ recovery, password, confirm })
    if (problem) { setErr(problem); return }
    setBusy(true); setErr('')
    try {
      await (mode === 'reset'
        ? api.authReset({ recovery_secret: recovery, password, confirm })
        : api.authSetup({ recovery_secret: recovery, password, confirm }))
      onUnlocked()
    } catch (ex) {
      setErr(ex.message)
      setBusy(false)
    }
  }

  const setupish = mode === 'setup' || mode === 'reset'
  return (
    <div className="fixed inset-0 bg-app flex items-center justify-center z-50 p-6">
      <div className="bg-panel border border-edge2 rounded-2xl w-full max-w-sm p-6 space-y-4">
        <div>
          <h1 className="text-xl font-semibold">crossband</h1>
          <p className="text-sm text-ink-mid mt-1">
            {mode === 'passkey' && 'Locked. Unlock with your passkey.'}
            {mode === 'login' && 'Locked. Your conversations live behind this password.'}
            {mode === 'setup' && 'Set the password that will protect this app. Prove it’s you with the recovery secret from .env (CROSSBAND_RECOVERY_SECRET) or the server’s startup output.'}
            {mode === 'reset' && 'Reset your password with the recovery secret. Every signed-in browser is signed out.'}
          </p>
        </div>

        {mode === 'passkey' ? (
          <div className="space-y-3">
            <button onClick={passkeyUnlock} disabled={busy} autoFocus className={buttonCls}>
              Unlock with passkey
            </button>
            <button className={linkCls} onClick={() => { setMode('login'); setErr('') }}>
              Use your password instead
            </button>
          </div>
        ) : setupish ? (
          <form onSubmit={submitSetup} className="space-y-3">
            <label className="block">
              <span className="text-sm text-ink-mid">Recovery secret</span>
              <input type="password" autoComplete="off" autoFocus value={recovery}
                     onChange={(e) => setRecovery(e.target.value)} className={inputCls} />
            </label>
            <input type="text" name="username" value="owner" readOnly aria-hidden="true"
                   autoComplete="username" tabIndex={-1}
                   style={{ position: 'absolute', left: '-9999px' }} />
            <label className="block">
              <span className="text-sm text-ink-mid">New password</span>
              <input type="password" autoComplete="new-password" value={password}
                     onChange={(e) => setPassword(e.target.value)} className={inputCls} />
            </label>
            <label className="block">
              <span className="text-sm text-ink-mid">Confirm password</span>
              <input type="password" autoComplete="new-password" value={confirm}
                     onChange={(e) => setConfirm(e.target.value)} className={inputCls} />
            </label>
            <button type="submit" disabled={busy} className={buttonCls}>
              {mode === 'reset' ? 'Reset password & unlock' : 'Set password & unlock'}
            </button>
          </form>
        ) : (
          <form onSubmit={submitLogin} className="space-y-3">
            <input type="text" name="username" value="owner" readOnly aria-hidden="true"
                   autoComplete="username" tabIndex={-1}
                   style={{ position: 'absolute', left: '-9999px' }} />
            <label className="block">
              <span className="text-sm text-ink-mid">Password</span>
              <input type="password" autoComplete="current-password" autoFocus value={password}
                     onChange={(e) => setPassword(e.target.value)} className={inputCls} />
            </label>
            <button type="submit" disabled={busy} className={buttonCls}>Unlock</button>
          </form>
        )}

        {err && <p className="text-sm text-red-400" role="alert">{err}</p>}

        {/* #87: the honest passkey state. "Never enrolled" and "enrolled at
            a different address" both used to render as a silent absence of
            the passkey button, which reads as broken. Say which it is. */}
        {mode === 'login' && session?.enrolled && !session?.passkey && (
          <p className="text-xs text-ink-faint" role="note">
            {(session?.passkey_elsewhere || []).length > 0
              ? `A passkey exists for ${session.passkey_elsewhere.join(' and ')} - open the app there to use it, or sign in here and enrol one for this address in Settings → Passkeys.`
              : 'No passkey is enrolled yet. Sign in with your password, then add one in Settings → Passkeys to unlock with Touch ID or Face ID.'}
          </p>
        )}

        <div className="flex items-center justify-between">
          {mode === 'login' && (
            <button className={linkCls} onClick={() => { setMode('reset'); setErr('') }}>
              Forgot? Reset with the recovery secret
            </button>
          )}
          {mode === 'reset' && (
            <button className={linkCls} onClick={() => { setMode('login'); setErr('') }}>
              Back to password
            </button>
          )}
          {onDismiss && (
            <button className={linkCls} onClick={onDismiss}>Not now</button>
          )}
        </div>

        <p className="text-xs text-ink-low">
          This unlocks only this browser, via a private, expiring session.
        </p>
      </div>
    </div>
  )
}
