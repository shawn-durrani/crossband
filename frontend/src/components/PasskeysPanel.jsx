import { useEffect, useState } from 'react'
import { Fingerprint, Loader2 } from 'lucide-react'
import { api } from '../api'
import {
  ceremonyErrorCopy, decodeCreationOptions, serialiseAttestation,
} from '../webauthnCodec'

/* Passkey management (#25 slice 2), in the Integrations console because that
   is the app's operational surface. Enrol the origin you are currently on;
   the lock screen then offers the passkey first there. A passkey is bound to
   its exact web address (localhost and the tailnet name enrol separately, an
   IP address can hold none), and removing one can never lock you out - the
   password always remains as the fallback. */

export default function PasskeysPanel() {
  const [rows, setRows] = useState(null)
  const [msg, setMsg] = useState('')
  const [busy, setBusy] = useState(false)

  async function load() {
    try {
      setRows((await api.webauthnCredentials()).credentials)
    } catch (ex) {
      setRows([])
      setMsg(`Couldn't load passkeys: ${ex.message}`)
    }
  }
  useEffect(() => { load() }, [])

  async function enrol() {
    setBusy(true); setMsg('')
    try {
      if (!window.PublicKeyCredential) throw new Error('this browser has no passkey support')
      const o = await api.webauthnRegisterOptions()
      const cred = await navigator.credentials.create(
        { publicKey: decodeCreationOptions(o.publicKey) })
      await api.webauthnRegister({ cid: o.cid, credential: serialiseAttestation(cred) })
      setMsg('Passkey enrolled - the lock screen here now offers it first.')
      load()
    } catch (ex) {
      setMsg(ceremonyErrorCopy(ex, 'register'))
    } finally {
      setBusy(false)
    }
  }

  async function remove(id) {
    if (!confirm('Remove this passkey? It will stop unlocking crossband; your password still works.')) return
    try {
      await api.webauthnRemove(id)
      load()
    } catch (ex) {
      setMsg(`Couldn't remove passkey: ${ex.message}`)
    }
  }

  return (
    <section className="space-y-3">
      <div>
        <h2 className="text-[11px] font-semibold uppercase tracking-[0.06em] text-ink-dim">
          Passkeys
        </h2>
        <p className="text-xs text-ink-faint mt-0.5">
          Unlock with Touch ID or Face ID instead of typing the password. A passkey only works
          on the exact web address it was made on, so enrol one here and another wherever else
          you open crossband (say, over your tailnet). The password stays as the fallback, so
          this can never lock you out.
        </p>
      </div>

      <div className="bg-panel border border-edge2 rounded-xl p-4 space-y-3">
        {rows === null ? (
          <div className="flex items-center gap-2 text-sm text-ink-faint">
            <Loader2 size={14} className="animate-spin" /> Loading passkeys…
          </div>
        ) : rows.length === 0 ? (
          <p className="text-sm text-ink-mid">
            No passkeys yet - enrol one to unlock with a fingerprint instead of the password.
          </p>
        ) : (
          <ul className="space-y-2">
            {rows.map((c) => (
              <li key={c.id} className="flex items-center justify-between gap-3 text-sm">
                <span className="flex items-center gap-2 min-w-0">
                  <Fingerprint size={14} className="shrink-0 text-ink-dim" />
                  <span className="truncate">{c.origin}</span>
                </span>
                <span className="flex items-center gap-3 shrink-0 text-xs text-ink-faint">
                  {(c.created_at || '').slice(0, 10)}
                  <span>{c.backed_up ? 'synced (e.g. iCloud)' : 'this device only'}</span>
                  <button className="text-red-400 hover:underline underline-offset-2"
                          onClick={() => remove(c.id)}>
                    remove
                  </button>
                </span>
              </li>
            ))}
          </ul>
        )}

        <div className="flex items-center gap-3">
          <button onClick={enrol} disabled={busy}
                  className="rounded-lg px-3 py-1.5 text-sm font-medium bg-ink-hi text-app
                             hover:opacity-90 disabled:opacity-50">
            Enrol a passkey in this browser
          </button>
          {msg && <span className="text-xs text-ink-mid" role="status">{msg}</span>}
        </div>
      </div>
    </section>
  )
}
