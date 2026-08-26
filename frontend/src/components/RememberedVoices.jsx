// Remembered voices on the Models page (#28 phase 2).
//
// The privacy surface for room mode: every person whose voice the app has
// stored is listed here with what is stored (clips, seconds) and whether it
// is enough to identify them - and each has a Forget button that DELETES the
// audio from disk. All wording and derivations live in ../roomState.js
// (pure, unit-tested); this file is markup and wiring only.
import { useEffect, useRef, useState } from 'react'
import { AudioLines, ChevronDown, ChevronRight, Ear, Pause, Pencil, Play,
         Trash2 } from 'lucide-react'

import { api } from '../api.js'
import { auditionNotice } from '../roomState.js'
import { cleanPreferredName, FORGET_EXPLAINER, personSummary,
         sufficiencyProgress } from '../roomState.js'
import { clipRow, DELETE_CLIP_EXPLAINER, moveTargets } from '../voiceClips.js'

export default function RememberedVoices() {
  const [open, setOpen] = useState(false)
  const [people, setPeople] = useState(null)     // null = not loaded yet
  const [sufficientSecs, setSufficientSecs] = useState(6)
  const [minShortClips, setMinShortClips] = useState(0)
  const [error, setError] = useState(null)
  const [confirming, setConfirming] = useState(null)  // person_id pending confirm
  const [editing, setEditing] = useState(null)        // person_id being renamed
  const [draft, setDraft] = useState('')
  // A rename that hit another person's name (#28: naming is law): the
  // backend refused and returned the conflict, and the merge affordance
  // asks before folding two people together. {personId, name, conflict}.
  const [mergeOffer, setMergeOffer] = useState(null)
  // Clip audition (#68): which person's clips are open, their rows, which
  // file is playing, and which is pending delete confirmation.
  const [clipsFor, setClipsFor] = useState(null)     // person_id or null
  const [clips, setClips] = useState([])
  const [playing, setPlaying] = useState(null)       // file token or null
  const [clipConfirm, setClipConfirm] = useState(null)
  const audioRef = useRef(null)
  // Reassignment tools (#90): the new-person input, and which clip's
  // move-target select is open.
  const [newPerson, setNewPerson] = useState(null)   // null closed, '' open
  const [movingFile, setMovingFile] = useState(null)

  async function createPerson() {
    const name = cleanPreferredName(newPerson || '')
    if (!name) return
    try {
      await api.createVoicePerson(name)
      setNewPerson(null)
      await load()
    } catch (e) {
      setError(`Could not create: ${e.message}`)
    }
  }

  async function confirmAudition(personId) {
    try {
      await api.confirmAudition(personId)
      await load()
    } catch (e) {
      setError(`Could not confirm: ${e.message}`)
    }
  }

  async function addAlias(personId) {
    const name = cleanPreferredName(draft)
    if (!name) return
    setEditing(null)
    setDraft('')
    try {
      await api.addVoiceAlias(personId, name)
      await load()
    } catch (e) {
      setError(`Could not add the spelling: ${e.message}`)
    }
  }

  async function moveClip(personId, file, to) {
    setMovingFile(null)
    if (!to) return
    if (playing === file) stopAudio()
    try {
      await api.moveVoiceClip(personId, file, to)
      const d = await api.voiceClips(personId)
      setClips(d.clips || [])
      await load()                       // both banks' counts moved
    } catch (e) {
      setError(`Could not move clip: ${e.message}`)
    }
  }

  function stopAudio() {
    if (audioRef.current) {
      audioRef.current.pause()
      audioRef.current = null
    }
    setPlaying(null)
  }

  async function toggleClips(personId) {
    stopAudio()
    setClipConfirm(null)
    if (clipsFor === personId) { setClipsFor(null); setClips([]); return }
    try {
      const d = await api.voiceClips(personId)
      setClips(d.clips || [])
      setClipsFor(personId)
    } catch (e) {
      setError(`Could not load clips: ${e.message}`)
    }
  }

  function playClip(personId, file) {
    if (playing === file) { stopAudio(); return }
    stopAudio()
    // A plain same-origin <audio> fetch: the session cookie rides along,
    // and playback never touches anchor state (pinned by a backend test).
    const a = new Audio(api.voiceClipAudioUrl(personId, file))
    a.onended = () => setPlaying((f) => (f === file ? null : f))
    a.onerror = () => {
      setError('Could not play that clip.')
      setPlaying((f) => (f === file ? null : f))
    }
    audioRef.current = a
    setPlaying(file)
    a.play().catch(() => setPlaying(null))
  }

  async function deleteClip(personId, file) {
    setClipConfirm(null)
    if (playing === file) stopAudio()
    try {
      await api.deleteVoiceClip(personId, file)
      const d = await api.voiceClips(personId)
      setClips(d.clips || [])
      await load()                       // counts and sufficiency moved
    } catch (e) {
      setError(`Could not delete clip: ${e.message}`)
    }
  }

  async function load() {
    try {
      const d = await api.voicePeople()
      setPeople(d.people || [])
      setSufficientSecs(d.sufficient_seconds || 6)
      setMinShortClips(d.min_short_clips || 0)
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
          {/* #90: a person can exist before any voice - the destination for
              clips that were banked under the wrong name. */}
          {newPerson === null ? (
            <button
              className="text-[11px] text-ink-dim hover:text-ink border border-edge rounded px-2 py-1"
              onClick={() => setNewPerson('')}
            >
              + New person
            </button>
          ) : (
            <div className="flex items-center gap-1.5">
              <input
                className="bg-transparent border border-edge rounded px-2 py-1 text-sm text-ink w-40"
                value={newPerson}
                maxLength={40}
                autoFocus
                placeholder="Their name"
                onChange={(e) => setNewPerson(e.target.value)}
                onKeyDown={(e) => { if (e.key === 'Enter') createPerson() }}
              />
              <button
                className="text-xs text-ink-dim hover:text-ink border border-edge rounded px-2 py-1 disabled:opacity-40"
                disabled={!cleanPreferredName(newPerson)}
                onClick={createPerson}
              >
                Create
              </button>
              <button
                className="text-xs text-ink-dim hover:text-ink px-1"
                onClick={() => setNewPerson(null)}
              >
                cancel
              </button>
            </div>
          )}
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
            const s = personSummary(p, sufficientSecs, minShortClips)
            // Learning progress (#28 phase 4): a still-learning voice shows
            // how far toward the bar it is, so patience is an informed
            // choice. Derivation lives in roomState.js (pure, node --test).
            const prog = sufficiencyProgress(p, sufficientSecs, minShortClips)
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
                        className="text-xs text-ink-dim hover:text-ink border border-edge rounded px-2 py-1 disabled:opacity-40"
                        title="Keep the current name and record this as another spelling of it - a misspelling worth keeping, or how it's pronounced. Transcripts and introductions under either form resolve to the same person."
                        disabled={!cleanPreferredName(draft)}
                        onClick={() => addAlias(p.person_id)}
                      >
                        Add as spelling
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
                  {/* The hygiene guard's surfacing (#28 PR-B): clips set
                      aside because they matched another voice better. */}
                  {s.setAside && (
                    <div className="text-[11px] text-amber-300/90 mt-0.5">{s.setAside}</div>
                  )}
                  {/* The owner's ear (#83): a bank that crossed sufficiency
                      without an introduction or correction is the phantom
                      shape - ask before (or while) it names anyone. */}
                  {auditionNotice(p) && (
                    <div className="text-[11px] text-amber-300/90 mt-0.5 flex items-center gap-2 flex-wrap">
                      <span>{auditionNotice(p)}</span>
                      <button
                        className="border border-edge rounded px-1.5 py-0.5 text-ink-dim hover:text-ink"
                        title="I listened to the clips and this voice is who the name says"
                        onClick={() => confirmAudition(p.person_id)}
                      >
                        Yes, this is {p.preferred_name || p.name}
                      </button>
                    </div>
                  )}
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
                  {/* Clip audition (#68): hear exactly what this voice was
                      built from, and delete a recording that is wrong. */}
                  <button
                    className="mt-1 inline-flex items-center gap-1 text-[11px] text-ink-dim hover:text-ink"
                    aria-expanded={clipsFor === p.person_id}
                    onClick={() => toggleClips(p.person_id)}
                    title="Listen to the stored recordings this voice was learnt from"
                  >
                    <Ear size={11} />
                    {clipsFor === p.person_id ? 'hide clips' : 'listen to clips'}
                  </button>
                  {clipsFor === p.person_id && (
                    <div className="mt-1.5 space-y-1">
                      {clips.length === 0 && (
                        <div className="text-[11px] text-ink-faint">
                          No recordings stored - this person is known but
                          their voice is unlearnt.
                        </div>
                      )}
                      {clips.map((c) => {
                        const row = clipRow(c)
                        return (
                          <div key={row.file}
                               className="flex items-center gap-2 text-[11px] text-ink-dim">
                            <button
                              className="text-ink-mid hover:text-ink"
                              aria-label={playing === row.file
                                ? 'Pause this recording' : 'Play this recording'}
                              onClick={() => playClip(p.person_id, row.file)}
                            >
                              {playing === row.file
                                ? <Pause size={12} /> : <Play size={12} />}
                            </button>
                            <span className="tabular-nums">{row.duration}</span>
                            <span>{row.source}</span>
                            {row.needsEar && (
                              <span className="text-amber-300/90"
                                    title="Never voice-verified: banked because they were the only unlearnt person in the room. Worth a listen.">
                                listen closely
                              </span>
                            )}
                            {row.quarantined && (
                              <span className="text-amber-300/90"
                                    title={row.quarantineTitle}>
                                {row.quarantineChip}
                              </span>
                            )}
                            <span className="text-ink-faint">{row.when}</span>
                            {/* #90: refile under the person it belongs to */}
                            {movingFile === row.file ? (
                              <select
                                className="bg-panel2 border border-edge rounded px-1 py-0.5 text-[11px] text-ink"
                                autoFocus
                                defaultValue=""
                                aria-label="Move this recording to another person"
                                onBlur={() => setMovingFile(null)}
                                onChange={(e) => moveClip(p.person_id, row.file, e.target.value)}
                              >
                                <option value="" disabled>this is actually…</option>
                                {moveTargets(people, p.person_id).map((t) => (
                                  <option key={t.person_id} value={t.person_id}>
                                    {t.name}
                                  </option>
                                ))}
                              </select>
                            ) : (
                              <button
                                className="text-ink-faint hover:text-ink"
                                title="This recording is someone else's voice - move it to the right person. Nothing is deleted; both voices re-learn from what they hold."
                                onClick={() => setMovingFile(row.file)}
                              >
                                move
                              </button>
                            )}
                            {clipConfirm === row.file ? (
                              <span className="inline-flex items-center gap-1.5">
                                <button
                                  className="text-red-400 hover:text-red-300 border border-red-900 rounded px-1.5 py-0.5"
                                  onClick={() => deleteClip(p.person_id, row.file)}
                                >
                                  Delete recording
                                </button>
                                <button
                                  className="text-ink-dim hover:text-ink"
                                  onClick={() => setClipConfirm(null)}
                                >
                                  keep
                                </button>
                              </span>
                            ) : (
                              <button
                                className="text-ink-faint hover:text-red-400"
                                title={DELETE_CLIP_EXPLAINER}
                                aria-label="Delete this recording"
                                onClick={() => setClipConfirm(row.file)}
                              >
                                <Trash2 size={11} />
                              </button>
                            )}
                          </div>
                        )
                      })}
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
