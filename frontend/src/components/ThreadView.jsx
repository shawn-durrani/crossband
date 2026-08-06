import { MessagesSquare, RotateCw, ArrowDown } from 'lucide-react'
import Message from './Message'

// The conversation itself: the scrolling transcript, what an empty chat says,
// the round-progress line, the continue control, and the jump-to-latest pill
// (extracted from App.jsx).
//
// The voice dock is positioned against this component's `relative` wrapper, so
// it arrives as `children` rather than being rendered here — it belongs to a
// live voice session, not to the transcript, and keeping it out means this file
// has nothing to say about voice.
//
// `scrollRef` and `onScroll` stay owned by App: the scroll position drives
// at-bottom tracking, the unread count and the auto-scroll-on-new-message
// effect, all of which are app state rather than presentation.
export default function ThreadView({
  messages, participants, chatParticipants, examplePrompts,
  streaming, roundProgress, canContinue, contRounds,
  atBottom, newCount, scrollRef,
  onScroll, onJumpToBottom, onContinue, onContRoundsChange, onPickPrompt,
  children,
}) {
  const showJump = !atBottom && (newCount > 0 || streaming)
  return (
    <div className="relative flex-1 min-h-0">
      <div ref={scrollRef} onScroll={onScroll} className="h-full overflow-y-auto px-3 sm:px-6 py-6">
        <div className="mx-auto w-full max-w-[768px]" aria-live="polite">
          {messages.length === 0 && (
            <div className="mx-auto max-w-md text-center mt-14 space-y-6">
              {/* An empty chat has to teach the one thing that makes this app
                  different — everyone answers — before you type anything. */}
              <div className="space-y-2">
                <MessagesSquare size={32} className="mx-auto text-ink-faint" strokeWidth={1.5} />
                <p className="text-ink-mid">One conversation, every model in the room.</p>
                <p className="text-sm text-ink-faint">
                  Everyone replies to each message. @mention someone to address them alone.
                </p>
              </div>
              {chatParticipants.length > 0 && (
                <div className="flex flex-wrap justify-center gap-2">
                  {chatParticipants.map((p) => (
                    <span
                      key={p.id}
                      className="inline-flex items-center gap-1.5 text-[13px] text-ink-mid border border-edge rounded-full px-3 py-1"
                    >
                      <span className="h-2 w-2 rounded-full" style={{ backgroundColor: p.color }} />
                      {p.name}
                      <span className="text-[11px] text-ink-faint">{p.model}</span>
                    </span>
                  ))}
                </div>
              )}
              <div className="space-y-2">
                <p className="text-[11px] font-semibold uppercase tracking-[0.05em] text-ink-dim">
                  Try one
                </p>
                {examplePrompts.map((t) => (
                  <button
                    key={t}
                    className="block w-full text-left text-sm text-ink-mid hover:text-ink border border-edge2 hover:border-edge3 rounded-[10px] px-3 py-2 transition-colors"
                    onClick={() => onPickPrompt(t)}
                  >
                    {t}
                  </button>
                ))}
              </div>
              <div className="text-xs text-ink-faint flex flex-wrap justify-center gap-x-4 gap-y-2">
                <span><kbd className="kbd">Enter</kbd> send</span>
                <span><kbd className="kbd">Shift</kbd>+<kbd className="kbd">Enter</kbd> newline</span>
                <span><kbd className="kbd">@name</kbd> address one model</span>
                <span>drag &amp; drop / paste to attach</span>
              </div>
            </div>
          )}
          {messages.map((m, i) => (
            <Message key={m.id} msg={m} prev={messages[i - 1]} participants={participants} />
          ))}
          {streaming && roundProgress && (
            <div className="flex justify-center pt-6">
              <span className="text-xs text-ink-dim">
                Round {roundProgress.n} of {roundProgress.total} — collaborating without you (Stop to step in)
              </span>
            </div>
          )}
          {canContinue && (
            <div className="flex justify-center items-center gap-2 pt-6">
              <button
                className="text-xs inline-flex items-center gap-1.5 border border-edge2 rounded-full px-4 py-1.5 text-ink-mid hover:text-ink hover:border-edge3"
                onClick={onContinue}
              >
                <RotateCw size={13} /> Let them continue{contRounds > 1 ? ` ×${contRounds}` : ''}
              </button>
              <select
                className="text-xs bg-app text-ink-dim border border-edge rounded-full px-2 py-1.5 cursor-pointer hover:border-edge2"
                title="How many rounds they talk before handing back to you"
                value={contRounds}
                onChange={(e) => onContRoundsChange(Number(e.target.value))}
              >
                {[1, 2, 3, 5, 10].map((n) => (
                  <option key={n} value={n}>{n} round{n > 1 ? 's' : ''}</option>
                ))}
              </select>
            </div>
          )}
        </div>
      </div>
      {/* Only offered when you'd actually miss something: scrolled up AND
          either new messages arrived or a round is still producing them. */}
      {showJump && (
        <div className="absolute bottom-3 left-1/2 -translate-x-1/2">
          <button
            className="inline-flex items-center gap-1.5 text-xs text-ink-mid hover:text-ink bg-panel border border-edge rounded-full px-3 py-1.5"
            style={{ boxShadow: 'var(--shadow-pop)' }}
            onClick={onJumpToBottom}
          >
            <ArrowDown size={12} /> {newCount > 0 ? `${newCount} new` : 'Jump to latest'}
          </button>
        </div>
      )}
      {children}
    </div>
  )
}
