// #69: the compact voice surface while a full page (models menu,
// connections, spend) is open. Voice keeps running - capture and playback
// continue - and this strip keeps the session's controls in reach: mute
// (#67), end, and the way back to the call. Rendered at every width: on
// desktop the in-thread dock unmounts with the chat view, so this is the
// only voice UI while a page is up.
import { Mic, MicOff, X } from 'lucide-react'

export default function VoiceStrip({ voiceState, muted, onToggleMute, onEnd, onBack }) {
  const speaking = voiceState === 'speaking'
  return (
    <div
      className="fixed bottom-3 right-3 z-40 flex items-center gap-2 rounded-full border border-edge2 bg-panel px-3 py-2 shadow-lg"
      role="status"
      aria-label="Voice session running"
    >
      <span
        className={`inline-block w-2 h-2 rounded-full ${
          muted ? 'bg-amber-400' : speaking ? 'bg-sky-400' : 'bg-emerald-400'
        }`}
        aria-hidden="true"
      />
      <span className="text-xs text-ink-dim">
        {muted ? 'Voice muted' : 'Voice live'}
      </span>
      <button
        title={muted ? 'Unmute - start listening again' : 'Mute - stop listening (models keep talking)'}
        aria-label={muted ? 'Unmute microphone' : 'Mute microphone'}
        aria-pressed={muted}
        className={`inline-flex items-center rounded-full px-2 py-1 border ${
          muted ? 'border-amber-400/60 text-amber-300' : 'border-edge2 text-ink-dim hover:text-ink'
        }`}
        onClick={onToggleMute}
      >
        {muted ? <MicOff size={13} /> : <Mic size={13} />}
      </button>
      <button
        title="End voice session"
        aria-label="End voice session"
        className="inline-flex items-center rounded-full px-2 py-1 border border-edge2 text-red-400 hover:text-red-300"
        onClick={onEnd}
      >
        <X size={13} />
      </button>
      <button
        className="text-xs text-ink-dim hover:text-ink underline"
        onClick={onBack}
      >
        Back to call
      </button>
    </div>
  )
}
