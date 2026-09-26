// The app-wide ElevenLabs voice model (#480), on the Models page beside the
// per-seat voice and volume. All meaning lives in ../ttsModels.js (pure,
// unit-tested) and the backend's model rules; this file is markup and wiring.
import { useState } from 'react'
import { AlertTriangle, AudioLines, Check } from 'lucide-react'

import { api } from '../api.js'
import {
  appChoiceRows, choiceNote, lockedNote, refusedNote, sourceNote,
} from '../ttsModels.js'

export default function VoiceModelPanel({ data, onChanged }) {
  const [saving, setSaving] = useState(false)
  const [saved, setSaved] = useState(false)
  const [failed, setFailed] = useState(null)
  if (!data) return null
  const locked = lockedNote(data)
  const refused = refusedNote(data)

  async function choose(value) {
    setSaving(true); setFailed(null); setSaved(false)
    try {
      onChanged(await api.setVoiceModel(value))
      setSaved(true)
      setTimeout(() => setSaved(false), 1500)
    } catch (e) {
      setFailed(e.message)
    } finally {
      setSaving(false)
    }
  }

  return (
    <div className="border border-edge rounded-xl px-3 py-2.5 space-y-1.5">
      <label className="block" htmlFor="voice-model-app">
        <span className="text-sm text-ink-mid flex items-center gap-1.5">
          <AudioLines size={14} className="text-ink-dim" />
          Voice model (ElevenLabs)
          <span className="text-ink-faint">(how every seat sounds, unless a seat picks its own)</span>
          {saved && <span className="ml-auto inline-flex items-center gap-1 text-xs text-link"><Check size={12} /> saved</span>}
        </span>
        <select
          id="voice-model-app"
          className="mt-1 w-full bg-app border border-edge2 rounded-lg px-3 py-2 text-sm focus:outline-none focus:border-edge3 disabled:opacity-50"
          value={data.setting}
          disabled={saving || !!locked}
          aria-describedby="voice-model-app-note"
          onChange={(e) => choose(e.target.value)}
        >
          {appChoiceRows(data).map((r) => (
            <option key={r.value} value={r.value} disabled={r.disabled}>{r.label}</option>
          ))}
        </select>
      </label>
      <p id="voice-model-app-note" className="text-xs text-ink-faint leading-relaxed">
        {choiceNote(data, data.setting)} {sourceNote(data)}
      </p>
      {locked && <p className="text-xs text-amber-500">{locked}</p>}
      {refused && (
        <p className="text-xs text-amber-500 flex items-start gap-1">
          <AlertTriangle size={12} className="shrink-0 translate-y-0.5" /> {refused}
        </p>
      )}
      {failed && (
        <p role="alert" className="text-xs text-red-400 flex items-start gap-1">
          <AlertTriangle size={12} className="shrink-0 translate-y-0.5" /> {failed}
        </p>
      )}
    </div>
  )
}
