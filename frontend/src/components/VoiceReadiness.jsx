// One person's readiness line on the Voices page (#482 stage 2): "Ready",
// or what the voice still needs. The words live in ../voiceReadiness.js
// (pure, node-tested); this file is markup only.
import { readinessLine } from '../voiceReadiness.js'

export default function VoiceReadiness({ readiness, summary }) {
  const line = readinessLine(readiness, summary)
  if (!line) return null
  return (
    <div className="text-[11px] mt-0.5" title={line.title}>
      <span className={line.ready ? 'text-emerald-300' : 'text-ink-dim'}>
        {line.text}
      </span>
      {line.detail && <span className="text-ink-faint"> · {line.detail}</span>}
    </div>
  )
}
