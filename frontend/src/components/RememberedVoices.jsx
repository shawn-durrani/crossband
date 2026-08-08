// Remembered voices on the Models page (#28 phase 2).
//
// The privacy surface for room mode: every person whose voice the app has
// stored is listed here with what is stored (clips, seconds) and whether it
// is enough to identify them - and each has a Forget button that DELETES the
// audio from disk. All wording and derivations live in ../roomState.js
// (pure, unit-tested); this file is markup and wiring only.
import { useEffect, useState } from 'react'
import { AudioLines, ChevronDown, ChevronRight, Trash2 } from 'lucide-react'

import { api } from '../api.js'
import { FORGET_EXPLAINER, personSummary } from '../roomState.js'

export default function RememberedVoices() {
  const [open, setOpen] = useState(false)
  const [people, setPeople] = useState(null)     // null = not loaded yet
  const [sufficientSecs, setSufficientSecs] = useState(6)
  const [error, setError] = useState(null)
  const [confirming, setConfirming] = useState(null)  // person_id pending confirm

  async function load() {
    try {
      const d = await api.voicePeople()
      setPeople(d.people || [])
      setSufficientSecs(d.sufficient_seconds || 6)
      setError(null)
    } catch (e) {
      setError(`Could not load remembered voices: ${e.message}`)
    }
  }

  useEffect(() => { if (open && people === null) load() }, [open])

  async function forget(personId) {
    setConfirming(null)
    try {
      await api.forgetVoice(personId)
      await load()
    } catch (e) {
      setError(`Could not forget: ${e.message}`)
    }
  }

  return (
    <div className="border border-edge rounded-lg">
      <button
        className="w-full flex items-center gap-2 px-3 py-2.5 text-sm text-ink-mid hover:text-ink"
        aria-expanded={open}
        onClick={() => setOpen((o) => !o)}
      >
        {open ? <ChevronDown size={14} /> : <ChevronRight size={14} />}
        <AudioLines size={14} className="text-ink-dim" />
        Remembered voices
        <span className="text-ink-faint text-xs">
          room mode's people - review or forget stored voice audio
        </span>
      </button>
      {open && (
        <div className="px-3 pb-3 space-y-2">
          {error && <div className="text-xs text-red-400">{error}</div>}
          {people !== null && people.length === 0 && !error && (
            <p className="text-xs text-ink-faint">
              Nobody yet. When someone is introduced in a voice chat ("say hi
              to Alex"), their voice is learned here so later sessions
              recognise them without another introduction.
            </p>
          )}
          {(people || []).map((p) => {
            const s = personSummary(p, sufficientSecs)
            return (
              <div key={p.person_id}
                   className="flex items-start gap-2 border border-edge2 rounded-lg px-3 py-2">
                <div className="flex-1 min-w-0">
                  <div className="text-sm text-ink flex items-baseline gap-2">
                    {s.name}
                    <span className="text-[11px] text-ink-faint">{s.detail}</span>
                  </div>
                  <div className="text-xs text-ink-dim mt-0.5">{s.status}</div>
                </div>
                {confirming === p.person_id ? (
                  <span className="shrink-0 flex items-center gap-1.5 text-xs">
                    <button
                      className="text-red-400 hover:text-red-300 border border-red-900 rounded px-2 py-1"
                      onClick={() => forget(p.person_id)}
                    >
                      Delete their audio
                    </button>
                    <button
                      className="text-ink-dim hover:text-ink px-1"
                      onClick={() => setConfirming(null)}
                    >
                      keep
                    </button>
                  </span>
                ) : (
                  <button
                    className="shrink-0 inline-flex items-center gap-1 text-xs text-ink-dim hover:text-red-400"
                    title={FORGET_EXPLAINER}
                    onClick={() => setConfirming(p.person_id)}
                  >
                    <Trash2 size={13} /> Forget
                  </button>
                )}
              </div>
            )
          })}
        </div>
      )}
    </div>
  )
}
