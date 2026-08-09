// Remembered voices on the Models page (#28 phase 2).
//
// The privacy surface for room mode: every person whose voice the app has
// stored is listed here with what is stored (clips, seconds) and whether it
// is enough to identify them - and each has a Forget button that DELETES the
// audio from disk. All wording and derivations live in ../roomState.js
// (pure, unit-tested); this file is markup and wiring only.
import { useEffect, useState } from 'react'
import { AudioLines, ChevronDown, ChevronRight, Pencil, Trash2 } from 'lucide-react'

import { api } from '../api.js'
import { cleanPreferredName, FORGET_EXPLAINER, personSummary,
         sufficiencyProgress } from '../roomState.js'

export default function RememberedVoices() {
  const [open, setOpen] = useState(false)
  const [people, setPeople] = useState(null)     // null = not loaded yet
  const [sufficientSecs, setSufficientSecs] = useState(6)
  const [error, setError] = useState(null)
  const [confirming, setConfirming] = useState(null)  // person_id pending confirm
  const [editing, setEditing] = useState(null)        // person_id being renamed
  const [draft, setDraft] = useState('')
  // A rename that hit another person's name (#28: naming is law): the
  // backend refused and returned the conflict, and the merge affordance
  // asks before folding two people together. {personId, name, conflict}.
  const [mergeOffer, setMergeOffer] = useState(null)

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

  async function rename(personId) {
    const name = cleanPreferredName(draft)
    if (!name) return
    setEditing(null)
    setDraft('')
    try {
      const r = await api.renameVoice(personId, name)
      if (r && r.ok === false && r.conflict) {
        // The name belongs to someone else: offer the merge instead of
        // silently renaming two people onto one another.
        setMergeOffer({ personId, name, conflict: r.conflict })
        return
      }
      await load()
    } catch (e) {
      setError(`Could not rename: ${e.message}`)
    }
  }

  async function merge() {
    const offer = mergeOffer
    setMergeOffer(null)
    if (!offer) return
    try {
      await api.mergeVoice(offer.personId, offer.conflict.person_id, offer.name)
      await load()
    } catch (e) {
      setError(`Could not merge: ${e.message}`)
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
          {mergeOffer && (
            <div className="text-xs border border-sky-800 bg-sky-950/40 rounded-lg px-3 py-2 space-y-1.5">
              <div className="text-sky-200">
                {mergeOffer.name} already belongs to{' '}
                {mergeOffer.conflict.display_name}. Merge them into one
                person? Their stored voices combine (the best clips are
                kept), and both names will mean the same person.
              </div>
              <div className="flex items-center gap-2">
                <button
                  className="text-sky-300 hover:text-sky-100 border border-sky-800 rounded px-2 py-1"
                  onClick={merge}
                >
                  Merge them
                </button>
                <button
                  className="text-ink-dim hover:text-ink px-1"
                  onClick={() => setMergeOffer(null)}
                >
                  keep separate
                </button>
              </div>
            </div>
          )}
          {people !== null && people.length === 0 && !error && (
            <p className="text-xs text-ink-faint">
              Nobody yet. When someone is introduced in a voice chat ("say hi
              to Alex"), their voice is learned here so later sessions
              recognise them without another introduction.
            </p>
          )}
          {(people || []).map((p) => {
            const s = personSummary(p, sufficientSecs)
            // Learning progress (#28 phase 4): a still-learning voice shows
            // how far toward the bar it is, so patience is an informed
            // choice. Derivation lives in roomState.js (pure, node --test).
            const prog = sufficiencyProgress(p, sufficientSecs)
            return (
              <div key={p.person_id}
                   className="flex items-start gap-2 border border-edge2 rounded-lg px-3 py-2">
                <div className="flex-1 min-w-0">
                  {editing === p.person_id ? (
                    <div className="flex items-center gap-1.5">
                      <input
                        className="bg-transparent border border-edge rounded px-2 py-1 text-sm text-ink w-40"
                        value={draft}
                        maxLength={40}
                        autoFocus
                        placeholder="Preferred name"
                        onChange={(e) => setDraft(e.target.value)}
                        onKeyDown={(e) => { if (e.key === 'Enter') rename(p.person_id) }}
                      />
                      <button
                        className="text-xs text-ink-dim hover:text-ink border border-edge rounded px-2 py-1 disabled:opacity-40"
                        disabled={!cleanPreferredName(draft)}
                        onClick={() => rename(p.person_id)}
                      >
                        Save
                      </button>
                      <button
                        className="text-xs text-ink-dim hover:text-ink px-1"
                        onClick={() => { setEditing(null); setDraft('') }}
                      >
                        cancel
                      </button>
                    </div>
                  ) : (
                    <div className="text-sm text-ink flex items-baseline gap-2">
                      {s.name}
                      <button
                        className="text-ink-faint hover:text-ink"
                        title="Set the preferred spelling of this person's name - it fixes the roster, memory records and future transcripts"
                        onClick={() => { setEditing(p.person_id); setDraft(s.name) }}
                      >
                        <Pencil size={11} />
                      </button>
                      {s.alias && <span className="text-[11px] text-ink-faint">{s.alias}</span>}
                      <span className="text-[11px] text-ink-faint">{s.detail}</span>
                    </div>
                  )}
                  <div className="text-xs text-ink-dim mt-0.5">{s.status}</div>
                  {prog && !prog.done && (
                    <div
                      className="mt-1 h-1 w-40 rounded-full bg-panel2 overflow-hidden"
                      role="progressbar"
                      aria-valuemin={0}
                      aria-valuemax={100}
                      aria-valuenow={Math.round(prog.fraction * 100)}
                      aria-label={`Voice learning progress: ${prog.label}`}
                      title={prog.label}
                    >
                      <div
                        className="h-full rounded-full bg-sky-700"
                        style={{ width: `${Math.round(prog.fraction * 100)}%` }}
                      />
                    </div>
                  )}
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
