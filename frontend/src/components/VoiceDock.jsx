import { Hand, Timer, Square, X, CornerDownLeft, SlidersHorizontal, ChevronDown, Users } from 'lucide-react'

// The in-session voice controls, docked bottom-right of the thread.
//
// Everything here is session-only: it matters while a voice conversation is
// live and nowhere else, which is why it lives in the dock rather than the
// header. The header owns "start talking"; this owns everything after that.
//
// GEOMETRY RULE, and the reason the markup is shaped this way: **the orb is the
// anchor.** An earlier column layout let the status text move the orb -
// sideways when the label changed, upward when it wrapped - so the one element
// your eye tracks during a call was the least stable thing on screen. The
// row-reverse + flex-end arrangement in `.voice-dock` pins the orb bottom-right
// and lets text grow leftward and wrap upward instead. Anything added here must
// keep that: in a status cluster, variable-length text never displaces the
// stable element.
export default function VoiceDock({
  voiceState, pttMode, silenceSecs, voiceRate, dockOpen, roomMode,
  rosterText, rosterHint, onRoomModeOff,
  onPttModeChange, onSilenceSecsChange, onVoiceRateChange, onDockOpenChange,
  onRoomModeChange, onFinalizeNow, onInterrupt, onStop,
}) {
  if (voiceState === 'off') return null
  return (
    <div className="absolute bottom-3 right-3 sm:right-5 voice-dock">
      {/* The roster chip (#28 phase 2): who the app is telling apart right
          now - which doubles as the transparency cue that multi-voice
          processing is on. The × is the durable override off. */}
      {rosterText && (
        <div
          className="inline-flex items-center gap-1.5 text-xs rounded-full px-2.5 py-1 border border-sky-800 text-sky-200 bg-sky-950/50"
          title={rosterHint}
        >
          <Users size={13} />
          <span className="max-w-64 truncate">{rosterText}</span>
          {onRoomModeOff && (
            <button
              className="text-sky-400 hover:text-sky-100"
              title="Switch room mode off for this chat (turns stop being attributed by voice)"
              aria-label="Switch room mode off"
              onClick={onRoomModeOff}
            >
              <X size={12} />
            </button>
          )}
        </div>
      )}
      {/* Collapsible - the capsule tucks into a small sliders button, because
          during a call the orb and its status matter and the knobs mostly
          don't. */}
      {!dockOpen && (
        <button
          className="voice-controls-toggle"
          title="Show voice controls (mode, pause, speed, stop)"
          aria-label="Show voice controls"
          aria-expanded="false"
          onClick={() => onDockOpenChange(true)}
        >
          <SlidersHorizontal size={15} />
        </button>
      )}
      {dockOpen && (
        <div className="voice-controls">
          <button
            title={pttMode
              ? "Manual mode: pauses won't send - press ⏎ Send to end your turn"
              : 'Auto mode: a pause ends your turn. Click to switch to manual (push-to-talk)'}
            className={`text-xs inline-flex items-center gap-1.5 rounded-full px-2.5 py-1 border ${
              pttMode
                ? 'border-amber-700 text-amber-300 bg-amber-950/40'
                : 'border-edge2 text-ink-dim hover:text-ink-mid'
            }`}
            onClick={() => onPttModeChange(!pttMode)}
          >
            {pttMode ? <><Hand size={13} /> manual</> : <><Timer size={13} /> auto</>}
          </button>
          {/* Room mode (#28 phase 1): session-only, default off. The copy
              states the cost plainly - a second transcription pass runs, so
              voice minutes roughly double while it is on. */}
          <button
            title={roomMode
              ? 'Room mode is on: turns can carry a Voice label when another voice is heard. Telling voices apart uses a second transcription pass, so voice minutes roughly double while it is on. Click to switch it off.'
              : 'Room mode: label turns when more than one person is talking. Telling voices apart uses a second transcription pass, so voice minutes roughly double while it is on.'}
            aria-pressed={!!roomMode}
            className={`text-xs inline-flex items-center gap-1.5 rounded-full px-2.5 py-1 border ${
              roomMode
                ? 'border-sky-700 text-sky-300 bg-sky-950/40'
                : 'border-edge2 text-ink-dim hover:text-ink-mid'
            }`}
            onClick={() => onRoomModeChange(!roomMode)}
          >
            <Users size={13} /> room
          </button>
          {!pttMode && (
            <span
              className="inline-flex items-center gap-1.5 text-xs text-ink-dim"
              title="How long a pause ends your turn in auto mode - raise it if the models cut in while you're still thinking."
            >
              <span className="text-ink-faint">pause</span>
              <input
                type="range" min="0.8" max="3.5" step="0.1" value={silenceSecs}
                onChange={(e) => onSilenceSecsChange(Number(e.target.value))}
                className="w-20 accent-amber-500 cursor-pointer"
              />
              <span className="tabular-nums">{silenceSecs.toFixed(1)}s</span>
            </span>
          )}
          <span
            className="inline-flex items-center gap-1.5 text-xs text-ink-dim"
            title="How fast the models speak - playback speed, pitch preserved. Applies mid-speech."
          >
            <span className="text-ink-faint">speed</span>
            <input
              type="range" min="0.9" max="1.6" step="0.05" value={voiceRate}
              onChange={(e) => onVoiceRateChange(Number(e.target.value))}
              className="w-16 accent-sky-500 cursor-pointer"
            />
            <span className="tabular-nums">{voiceRate.toFixed(2)}×</span>
          </span>
          {pttMode && voiceState === 'listening' && (
            <button
              title="Send your turn now"
              className="text-xs inline-flex items-center gap-1.5 rounded-full px-2.5 py-1 border border-emerald-700 text-emerald-300 hover:text-emerald-200"
              onClick={onFinalizeNow}
            >
              <CornerDownLeft size={13} /> send
            </button>
          )}
          {voiceState === 'speaking' && (
            <button
              title="Stop the current speech"
              aria-label="Stop the current speech"
              className="text-xs inline-flex items-center rounded-full px-2 py-1.5 border border-edge2 text-ink-dim hover:text-ink"
              onClick={onInterrupt}
            >
              <Square size={12} />
            </button>
          )}
          <button
            title="End voice session"
            aria-label="End voice session"
            className="text-xs inline-flex items-center rounded-full px-2 py-1.5 border border-edge2 text-ink-dim hover:text-ink"
            onClick={onStop}
          >
            <X size={14} />
          </button>
          <button
            title="Hide voice controls"
            aria-label="Hide voice controls"
            aria-expanded="true"
            className="text-ink-faint hover:text-ink-mid shrink-0"
            onClick={() => onDockOpenChange(false)}
          >
            <ChevronDown size={14} />
          </button>
        </div>
      )}
      {/* The anchor. role="status" so a screen reader hears the state change
          that the orb conveys visually. */}
      <div className="voice-orb-row" role="status">
        <div className={`voice-orb ${voiceState}`} aria-hidden="true" />
        <span className="voice-status">
          {voiceState === 'listening' && (pttMode ? 'Your turn - press Send' : 'Listening')}
          {voiceState === 'transcribing' && 'Thinking…'}
          {voiceState === 'speaking' && 'Speaking - talk to interrupt'}
        </span>
      </div>
    </div>
  )
}
