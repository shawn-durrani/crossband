import { useEffect, useRef, useState } from 'react'
import { ChevronDown, Mic, MicOff, Users, X } from 'lucide-react'
import { orbStateFor, statusFor, showInterruptHint, displayPartial } from '../voiceView'

// Full-screen mobile voice "call" overlay. Rendered by App only while a voice
// session is live (voiceState !== 'off'). Mobile-only: the wrapper is `sm:hidden`
// so desktop keeps its in-thread .voice-dock untouched.
//
// This is a VIEW over state App already owns - it renders the orb (driven by
// voiceState), the roster, and the transient captions the useCaptions hook
// produces from the SSE event flow. It does not touch the voice pipeline; the
// actions it takes are stopVoice() (End), the mic mute toggle, and per-agent
// sit-out via onToggleParticipant (the same roster toggle as the desktop
// header chips - an agent tapped out skips rounds from the next turn).
export default function MobileVoiceCall({ voiceState, held = 0, participants, roster = [],
                                          activeIds = [], onToggleParticipant, captions,
                                          captionHistory = [], speakingSlug, onEnd, voice,
                                          partial = null, banner = null, onDismissBanner,
                                          roomMode = false, onRoomModeChange }) {
  const [muted, setMuted] = useState(false)
  // Transcript browsing: faded captions aren't gone - swipe up (or tap the
  // hint) to scroll back through everything said this session.
  const [browsing, setBrowsing] = useState(false)
  const touchY = useRef(null)
  const historyRef = useRef(null)
  useEffect(() => {
    // Opening the transcript starts at the newest line.
    if (browsing && historyRef.current)
      historyRef.current.scrollTop = historyRef.current.scrollHeight
  }, [browsing])
  const onTouchStart = (e) => { touchY.current = e.touches?.[0]?.clientY ?? null }
  const onTouchMove = (e) => {
    if (touchY.current == null) return
    if (touchY.current - (e.touches?.[0]?.clientY ?? touchY.current) > 30) {
      setBrowsing(true)
      touchY.current = null
    }
  }

  // Orb state ← voiceState + muted, via the pure rules in voiceView.js:
  // a muted session must never claim it can hear you, but a speaking model
  // keeps the speaking visual, because who's talking still matters.
  const orbState = orbStateFor(voiceState, muted)

  // While speaking, tint the orb to the current speaker's participant color.
  const speaker = speakingSlug && participants.find((p) => p.slug === speakingSlug)
  const orbColor = voiceState === 'speaking' && speaker?.color ? speaker.color : undefined

  // Status line priority (voiceView.statusFor): held messages first (you were
  // heard, nothing is lost), then muted (the mic is off and you should know),
  // then who's speaking / thinking / listening.
  const status = statusFor({ held, muted, voiceState, speakerName: speaker?.name })
  const livePartial = displayPartial(partial)

  const toggleMute = () => {
    // Real mute: voice.setMuted() disables the mic track at the source (the
    // track emits silence, so nothing is detected, recorded, or streamed) and
    // freezes the VAD. The models keep talking; the app stops listening.
    setMuted((m) => !m)
    voice?.setMuted?.(!muted)
  }

  return (
    <div className="mvc sm:hidden" role="dialog" aria-label="Voice call" aria-live="polite">
      {/* Voice errors must be visible ON the call screen: this overlay covers
          the app's main banner, which made every playback failure silent. */}
      {banner && (
        <div className="bg-red-950/85 border-b border-red-900 text-red-200 text-sm px-4 py-2 flex items-center gap-2"
             role="alert">
          <span className="flex-1">⚠ {banner}</span>
          <button aria-label="Dismiss" onClick={onDismissBanner}>✕</button>
        </div>
      )}
      {/* Top row: tappable agent chips (tap = sit out / bring back), private chip */}
      <div className="mvc-top">
        <div className="mvc-who" role="group" aria-label="Agents - tap one to mute or unmute them">
          {roster.map((p) => {
            const active = activeIds.includes(p.id)
            const lastActive = active && activeIds.length === 1
            return (
              <button
                key={p.id}
                type="button"
                className={`mvc-agent${active ? '' : ' out'}`}
                aria-pressed={!active}
                aria-label={active
                  ? (lastActive ? `${p.name} is the last agent - can't mute` : `Mute ${p.name} (sits out from the next turn)`)
                  : `Unmute ${p.name}`}
                title={active
                  ? (lastActive ? `${p.name} is the last agent - a chat needs one` : `Tap to mute ${p.name} - sits out from the next turn`)
                  : `Tap to bring ${p.name} back in`}
                disabled={lastActive}
                onClick={() => onToggleParticipant?.(p.id)}
                style={active ? { '--agent-color': p.color } : undefined}
              >
                <i style={{ background: active ? p.color : 'transparent', borderColor: p.color }} />
                <span>{p.name}</span>
                {!active && <span className="mvc-agent-tag">muted</span>}
              </button>
            )
          })}
        </div>
        <div className="flex items-center gap-2">
          {/* Room mode (#28 phase 1) - the phone-on-the-table case. Same
              session toggle as the desktop dock, same plain cost note. */}
          <button
            type="button"
            aria-pressed={!!roomMode}
            aria-label={roomMode ? 'Switch room mode off' : 'Switch room mode on'}
            title={roomMode
              ? 'Room mode is on: turns can carry a Voice label when another voice is heard. Telling voices apart uses a second transcription pass, so voice minutes roughly double while it is on. Tap to switch it off.'
              : 'Room mode: label turns when more than one person is talking. Telling voices apart uses a second transcription pass, so voice minutes roughly double while it is on.'}
            className={`text-xs inline-flex items-center gap-1.5 rounded-full px-2.5 py-1 border ${
              roomMode
                ? 'border-sky-700 text-sky-300 bg-sky-950/40'
                : 'border-edge2 text-ink-dim'
            }`}
            onClick={() => onRoomModeChange?.(!roomMode)}
          >
            <Users size={13} /> room{roomMode ? ' on' : ''}
          </button>
          <span className="mvc-lock">🔒 private</span>
        </div>
      </div>

      {/* Caption zone - transient flash / drift / fade; swipe up to browse
          everything said this session (captions fade, they don't vanish). */}
      <div className="mvc-stage" onTouchStart={onTouchStart} onTouchMove={onTouchMove}>
        {browsing ? (
          <>
            <div ref={historyRef} className="mvc-history" role="log"
                 aria-label="Everything said this session">
              {captionHistory.length === 0 && (
                <div className="mvc-history-empty">Nothing said yet this session.</div>
              )}
              {captionHistory.map((c) => (
                <div key={c.id} className={`mvc-hline${c.you ? ' you' : ''}`}>
                  <div className="mvc-cap-name">
                    {!c.you && <i style={{ background: c.color }} />}
                    {c.you ? 'You' : c.name}
                  </div>
                  <div className="mvc-hline-text">{c.text}</div>
                </div>
              ))}
            </div>
            <button type="button" className="mvc-live-pill" onClick={() => setBrowsing(false)}>
              <ChevronDown size={15} /> Back to live
            </button>
          </>
        ) : (
          <div className="mvc-captions">
            {captions.map((c) => (
              <div key={c.id} className={`mvc-cap${c.you ? ' you' : ''}${c.leaving ? ' out' : ''}`}>
                {!c.you && (
                  <div className="mvc-cap-name">
                    <i style={{ background: c.color }} />
                    {c.name}
                  </div>
                )}
                <div className="mvc-cap-text">{c.you ? `You: ${c.text}` : c.text}</div>
              </div>
            ))}
            {/* Your words, live, while you're still saying them: the realtime
                STT stream's partials, faint until the transcript is final and
                the normal "You:" caption replaces this. */}
            {livePartial && (
              <div className="mvc-cap you partial" aria-live="off">
                <div className="mvc-cap-text">You: {livePartial}</div>
              </div>
            )}
          </div>
        )}
      </div>

      {/* Bottom cluster in normal flow: status → orb → hint → controls, so
          nothing overlaps however large the orb scales. */}
      <div className="mvc-bottom">
        <div className="mvc-status">{status}</div>
        <div className="mvc-orb-wrap">
          <div
            className={`voice-orb ${orbState}`}
            aria-hidden="true"
            style={orbColor ? { '--orb-tint': orbColor } : undefined}
          />
        </div>
        {/* Barge-in is allowed and worth advertising, but only when a voice
            interrupt could actually work (never while muted). */}
        {showInterruptHint(voiceState, muted) && (
          <div className="mvc-subhint">talk any time to interrupt</div>
        )}
        <button type="button" className="mvc-hint" onClick={() => setBrowsing((b) => !b)}>
          {browsing ? 'Tap here to return to live captions' : 'Just talk - swipe up for the transcript'}
        </button>

        {/* Mute is THE control in a car - it's both "stop listening" and, when
            you've just spoken, "done talking, send it" - so it gets the big
            thumb-sized pill. End is deliberately smaller and set apart: a
            hang-up should never be a fat-finger away from mute. */}
        <div className="mvc-controls">
          <button
            type="button"
            className={`mvc-mute${muted ? ' muted' : ''}`}
            aria-pressed={muted}
            aria-label={muted ? 'Unmute microphone' : 'Mute microphone (sends what you just said)'}
            onClick={toggleMute}
          >
            {muted ? <MicOff size={24} /> : <Mic size={24} />}
            <span>{muted ? 'Muted - tap to unmute' : 'Mute'}</span>
          </button>
          <button
            type="button"
            className="mvc-cbtn end"
            aria-label="End voice session"
            title="End voice session"
            onClick={onEnd}
          >
            <X size={24} />
          </button>
        </div>
      </div>
    </div>
  )
}
