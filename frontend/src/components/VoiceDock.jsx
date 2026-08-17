import { useState } from 'react'
import { Check, ChevronDown, CornerDownLeft, Hand, Mic, MicOff, Settings2, Square, Timer, Users, X } from 'lucide-react'
import { AMBIENT_EXPLAINER, CHIP_CONFIRMED, CHIP_LEARNING } from '../roomState'

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
//
// ONE CONTAINER, TWO TIERS (#28, the dock refinement). This used to be four
// floating layers - health strip, roster chip, controls capsule, orb row -
// each sized by its own content, with an unbounded prose status line that
// overflowed the tray as soon as a second voice existed. Now a single
// fixed-width panel holds:
//   tier 1, status:   the room-mode INDICATOR chip, then per-person chips
//                     that WRAP, plus the live pulse, small and
//                     right-aligned;
//   tier 2, controls: the per-turn controls inline (mic mode, send, stop,
//                     end, collapse);
//   behind a settings disclosure: the set-once sliders (pause, speed), the
//                     room-mode escape hatches, and the standing readouts
//                     (matcher, remembered voices, close-voice warnings).
// Nothing was removed - the sliders and readouts are one click away, and
// the panel's width is fixed so no amount of text can shove the orb.
//
// THE ROOM BUTTON BECAME AN INDICATOR (#28): arming is fully automatic now
// (ambient arm on unknown or remembered voices, spoken commands,
// introductions), so a toggle in the tray misled users into thinking they
// had to press it. Room state is SHOWN here - "room on · N" / "listening" /
// "solo" - and never operated from the tray: the chip has no click handler.
// The two manual doors survive, demoted into the settings disclosure below:
// "switch on now" and "switch off for this chat". They are the degraded
// path (matcher unavailable = no automatic arming - a pinned behaviour),
// the day-one enrolment door, and the control for anyone who cannot speak
// a command.
//
// Every derivation the chips need - which state, the label text, the order,
// the "+N" - lives in roomState.js / voiceHealth.js with node --test cover.
// This file only maps a state to a colour and renders.
const CHIP_CLASS = {
  [CHIP_CONFIRMED]: 'border-emerald-800/80 text-emerald-200/90',
  [CHIP_LEARNING]: 'border-dashed border-amber-700/70 text-amber-200/90',
}
const CHIP_FALLBACK = 'border-edge2 text-ink-dim'
// The indicator's colour per mode state (modeReadout in voiceHealth.js is
// the one source of the state and the copy): an armed room reads active,
// the two off states stay quiet.
const MODE_CLASS = {
  on: 'border-sky-700 text-sky-300 bg-sky-950/40',
  listening: 'border-edge2 text-ink-dim',
  solo: 'border-edge2 text-ink-dim',
}

export default function VoiceDock({
  voiceState, pttMode, silenceSecs, voiceRate, dockOpen, roomMode,
  rosterText, rosterHint, onRoomModeOff, health,
  onPttModeChange, onSilenceSecsChange, onVoiceRateChange, onDockOpenChange,
  onRoomModeChange, onFinalizeNow, onInterrupt, onStop,
  muted = false, onToggleMute,
}) {
  const [settingsOpen, setSettingsOpen] = useState(false)
  if (voiceState === 'off') return null
  const chips = health && health.chips
  const mode = health && health.mode
  const hasStatus = !!(health && (mode || (chips && chips.shown.length)
    || health.pulse || health.matcher.degraded))
  return (
    <div className="absolute bottom-3 right-3 sm:right-5 voice-dock">
      <div className="voice-panel">
        {/* ---- Tier 1: status. Chips wrap; the pulse stays put. ---- */}
        {hasStatus && (
          <div className="voice-tier-status" role="status" aria-label="Voice identity">
            <div
              className="voice-chips"
              title={rosterHint || undefined}
              aria-label={rosterText || 'Voices this session can name'}
            >
              {/* The room-mode indicator (#28: the room button became an
                  indicator). Passive by design - NO click handler: arming
                  is automatic, and the manual doors live in the settings
                  disclosure. State and copy come from modeReadout
                  (voiceHealth.js), the one source of the mode string. */}
              {mode && (
                <span
                  className={`voice-chip ${MODE_CLASS[mode.state] || CHIP_FALLBACK}`}
                  title={mode.title}
                >
                  <Users size={11} aria-hidden="true" />
                  <span>{mode.label}</span>
                </span>
              )}
              {chips && chips.shown.map((chip) => (
                <span
                  key={chip.key}
                  className={`voice-chip ${CHIP_CLASS[chip.state] || CHIP_FALLBACK}`}
                  title={chip.title}
                >
                  {/* The owner asked for a tick BESIDE the name, not a
                      different name - so the label never changes shape. */}
                  {chip.state === CHIP_CONFIRMED && <Check size={11} aria-hidden="true" />}
                  <span>{chip.label}</span>
                </span>
              ))}
              {chips && chips.more && (
                <span className={`voice-chip ${CHIP_FALLBACK}`} title={chips.moreTitle}>
                  <span>{chips.more}</span>
                </span>
              )}
              {/* A degraded matcher is the one standing readout that earns a
                  place in tier 1: it changes what the app can do this turn. */}
              {health.matcher.degraded && (
                <span
                  className="voice-chip border-dashed border-amber-700/70 text-amber-200/90"
                  title={health.matcher.title}
                >
                  <span>{health.matcher.label}</span>
                </span>
              )}
            </div>
            {health.pulse && (
              <span className="voice-pulse" title={health.pulse.title}>
                {health.pulse.label}
              </span>
            )}
          </div>
        )}
        {/* ---- Tier 2: controls. Collapsed, the capsule tucks into one small
            button, because during a call the orb and its status matter and
            the knobs mostly don't. ---- */}
        {!dockOpen && (
          <div className="voice-tier-controls">
            <button
              className="voice-controls-toggle"
              title="Show voice controls (mode, settings, stop)"
              aria-label="Show voice controls"
              aria-expanded="false"
              onClick={() => onDockOpenChange(true)}
            >
              <Settings2 size={15} />
            </button>
          </div>
        )}
        {dockOpen && (
          <div className="voice-tier-controls">
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
            {/* The room toggle that used to sit here became the tier-1
                indicator (#28): arming is automatic, so the tray shows room
                state and never operates it. The manual doors moved into the
                settings disclosure below. */}
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
            {/* The set-once knobs live behind this: pause and speed are
                chosen once a session, not fiddled with per turn. */}
            <button
              title="Voice settings: pause length, speaking speed, and what the voice matcher is doing"
              aria-label="Voice settings"
              aria-expanded={settingsOpen}
              className={`text-xs inline-flex items-center rounded-full px-2 py-1.5 border ${
                settingsOpen
                  ? 'border-edge2 text-ink bg-panel2'
                  : 'border-edge2 text-ink-dim hover:text-ink'
              }`}
              onClick={() => setSettingsOpen((o) => !o)}
            >
              <Settings2 size={13} />
            </button>
            {/* #67: the one unmistakable "stop listening" control at
                desktop width - real mute (mic track disabled at the source,
                VAD frozen; the models keep talking). Amber when muted so
                the state reads at a glance. */}
            <button
              title={muted ? 'Unmute - start listening again' : 'Mute - stop listening (models keep talking)'}
              aria-label={muted ? 'Unmute microphone' : 'Mute microphone'}
              aria-pressed={muted}
              className={`text-xs inline-flex items-center gap-1 rounded-full px-2 py-1.5 border ${
                muted ? 'border-amber-400/60 text-amber-300 bg-amber-400/10'
                      : 'border-edge2 text-ink-dim hover:text-ink'
              }`}
              onClick={onToggleMute}
            >
              {muted ? <MicOff size={14} /> : <Mic size={14} />}
              {muted && <span>muted</span>}
            </button>
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
              className="text-ink-faint hover:text-ink-mid shrink-0 ml-auto"
              onClick={() => onDockOpenChange(false)}
            >
              <ChevronDown size={14} />
            </button>
          </div>
        )}
        {/* ---- The settings disclosure: set-once knobs and the standing
            readouts that used to crowd the status line. ---- */}
        {dockOpen && settingsOpen && (
          <div className="voice-settings">
            {!pttMode && (
              <span
                className="inline-flex items-center gap-1.5 text-xs text-ink-dim"
                title="How long a pause ends your turn in auto mode - raise it if the models cut in while you're still thinking."
              >
                <span className="text-ink-faint w-12">pause</span>
                <input
                  type="range" min="0.8" max="3.5" step="0.1" value={silenceSecs}
                  onChange={(e) => onSilenceSecsChange(Number(e.target.value))}
                  className="flex-1 accent-amber-500 cursor-pointer"
                />
                <span className="tabular-nums">{silenceSecs.toFixed(1)}s</span>
              </span>
            )}
            <span
              className="inline-flex items-center gap-1.5 text-xs text-ink-dim"
              title="How fast the models speak - playback speed, pitch preserved. Applies mid-speech."
            >
              <span className="text-ink-faint w-12">speed</span>
              <input
                type="range" min="0.9" max="1.6" step="0.05" value={voiceRate}
                onChange={(e) => onVoiceRateChange(Number(e.target.value))}
                className="flex-1 accent-sky-500 cursor-pointer"
              />
              <span className="tabular-nums">{voiceRate.toFixed(2)}×</span>
            </span>
            {/* The room-mode escape hatches (#28: the room button became an
                indicator). Settings-tier on purpose: arming is automatic,
                so these are for the degraded path (matcher unavailable = no
                automatic arming - a pinned behaviour), day-one enrolment,
                and anyone who cannot speak a command. Same durable plumbing
                as ever: "switch off for this chat" is the solo-mode disarm
                the tray's × used to do, "switch on now" is the manual arm. */}
            <span className="inline-flex items-center flex-wrap gap-1.5 text-xs text-ink-dim">
              <span className="text-ink-faint w-12" title={AMBIENT_EXPLAINER}>room</span>
              <button
                className="rounded-full px-2.5 py-1 border border-edge2 text-ink-dim hover:text-ink disabled:opacity-40"
                title="Switch the room on for this chat now - the manual door for a first session, or while voice recognition is unavailable."
                disabled={!!roomMode}
                onClick={() => onRoomModeChange(true)}
              >
                switch on now
              </button>
              <button
                className="rounded-full px-2.5 py-1 border border-edge2 text-ink-dim hover:text-ink disabled:opacity-40"
                title='Switch the room off for this chat and keep it off - the same durable disarm as saying "solo mode".'
                disabled={!!(mode && mode.state === 'solo')}
                onClick={onRoomModeOff}
              >
                switch off for this chat
              </button>
            </span>
            {health && (
              <div className="text-[11px] text-ink-faint flex flex-col gap-0.5">
                <span
                  title={health.matcher.title}
                  className={health.matcher.degraded ? 'text-amber-300/90' : ''}
                >
                  {health.matcher.label}
                </span>
                {health.voices.length > 0 && (
                  <span title="Remembered voices, and how much clear speech has been learned for each">
                    {health.voices.map((v) => (v.done ? `${v.name} ✓` : `${v.name} ${v.label}`)).join(', ')}
                  </span>
                )}
                {(health.learning || []).map((line) => (
                  <span key={`learn-${line.name}`} title={line.title}>
                    {line.label}
                  </span>
                ))}
                {(health.close || []).map((line) => (
                  <span
                    key={line}
                    className="text-amber-300/90"
                    title="These voices' stored banks sit close together, so the matcher demands a wider winning margin before naming either - fewer names, never wrong ones."
                  >
                    {line}
                  </span>
                ))}
              </div>
            )}
          </div>
        )}
      </div>
      {/* The anchor. role="status" so a screen reader hears the state change
          that the orb conveys visually. */}
      <div className="voice-orb-row" role="status">
        {/* 'working' (round generating, nothing audible yet) shares the
            transcribing visual - both are "busy on your turn". */}
        <div className={`voice-orb ${voiceState === 'working' ? 'transcribing' : voiceState}`} aria-hidden="true" />
        <span className="voice-status">
          {voiceState === 'listening' && (pttMode ? 'Your turn - press Send' : 'Listening')}
          {(voiceState === 'transcribing' || voiceState === 'working') && 'Thinking…'}
          {voiceState === 'speaking' && 'Speaking - talk to interrupt'}
        </span>
      </div>
    </div>
  )
}
