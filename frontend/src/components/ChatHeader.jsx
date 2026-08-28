import { useEffect, useRef, useState } from 'react'
import { Menu, Headphones, MoreHorizontal, Check, Copy, MicOff, Brain, Globe,
  SquareTerminal, AlertTriangle, Wrench } from 'lucide-react'
import { withAlpha } from '../speakers'
import { chatCostFigures, chatCostNote, COST_TONE } from '../messageCost'
import { contextGauge, usageSummary, capabilityRows } from '../headerView'
import { headline } from '../spendView'

// The chat's header, both widths (extracted from App.jsx).
//
// There are two headers, not one responsive one: on a phone the controls
// collapse into a ⋯ menu with room for words, on a desktop they sit out in the
// open as chips. They render the SAME controls, which is why they belong in one
// file: while they lived in App.jsx a change to any chip meant finding and
// editing it in two places several hundred lines apart, and one cost-labelling
// change had to touch three of them at once.
//
// Presentational: every piece of state it needs arrives as a prop and every
// action leaves as a callback. The one exception is `overflowOpen`, which is
// the menu's own open/closed state and is of no interest to anything outside
// this file.

// A control chip. Every one of them states what it does AND what it means to
// turn it on, because a room where the AIs can suddenly read your repo should
// never be one toggle you didn't understand.
function Chip({ on, title, onClick, tone, children }) {
  return (
    <button
      title={title}
      className={`text-xs inline-flex items-center gap-1.5 rounded-full px-2.5 py-1 border ${
        on ? tone : 'border-edge2 text-ink-dim hover:text-ink-mid'
      }`}
      onClick={onClick}
    >
      {children}
    </button>
  )
}

export default function ChatHeader({
  activeChat, activeTitle, activeProject, participants, cfg, memory,
  running, chatTotal, copiedChat, voiceState,
  onOpenDrawer, onCopyChat, onStartVoice, onEndVoice, onToggleParticipant,
  onToggleVoice, onToggleWeb, onToggleMemory, onToggleCode,
}) {
  const [overflowOpen, setOverflowOpen] = useState(false)
  const [toolsOpen, setToolsOpen] = useState(false)
  const [usageOpen, setUsageOpen] = useState(false)
  const overflowRef = useRef(null)
  const toolsRef = useRef(null)
  const usageRef = useRef(null)

  // Close any open header surface (⋯ menu, Tools popover, usage popover) on
  // outside click or Esc. One effect for all three: their refs, state and
  // dismissal are one thing.
  useEffect(() => {
    if (!overflowOpen && !toolsOpen && !usageOpen) return
    const closeAll = () => { setOverflowOpen(false); setToolsOpen(false); setUsageOpen(false) }
    const onKey = (e) => { if (e.key === 'Escape') closeAll() }
    const onDown = (e) => {
      for (const [ref, close] of [[overflowRef, setOverflowOpen], [toolsRef, setToolsOpen], [usageRef, setUsageOpen]]) {
        if (ref.current && !ref.current.contains(e.target)) close(false)
      }
    }
    document.addEventListener('keydown', onKey)
    document.addEventListener('mousedown', onDown)
    return () => {
      document.removeEventListener('keydown', onKey)
      document.removeEventListener('mousedown', onDown)
    }
  }, [overflowOpen, toolsOpen, usageOpen])

  const gauge = contextGauge(activeChat?.context)
  const usage = usageSummary(chatTotal, activeChat?.voice)
  // The server sends the raw three-bucket row; headline() names which bucket
  // is "billed". Exactly what ExportModal does with the same shape, so the
  // two surfaces cannot disagree about a dollar again.
  const named = chatTotal ? headline(chatTotal) : null
  const costFigures = named ? chatCostFigures(named) : []
  const costNote = named ? chatCostNote(named) : null

  // ---- Shared control fragments (desktop header ⇄ mobile overflow) ----

  const runningBadge = running ? (
    <span
      className="running-badge shrink-0"
      role="status"
      title="A task is running in this chat - a model or Claude Code is working. It keeps going if you switch away."
    >
      <span className="running-spinner" aria-hidden="true" />
      running
    </span>
  ) : null

  const participantToggles = () =>
    participants.filter((p) => p.enabled).map((p) => {
      const inChat = (activeChat.participant_ids || []).includes(p.id)
      return (
        <button
          key={p.id}
          title={`${p.name} (${p.model}) - click to ${inChat ? 'remove from' : 'add to'} this chat`}
          className={`text-xs rounded-full px-2.5 py-1 border transition-opacity ${
            inChat ? '' : 'opacity-35 border-edge2 text-ink-mid'
          }`}
          style={
            inChat
              ? { backgroundColor: withAlpha(p.color, '1f'), borderColor: withAlpha(p.color, '66'), color: p.color }
              : undefined
          }
          onClick={() => onToggleParticipant(p.id)}
        >
          {p.name}
        </button>
      )
    })

  // #108: this chip shapes REPLY STYLE only and must never look like a
  // microphone control - the mic icon and the word "voice" made it read as
  // the kill switch, and clicking it left capture running. The real end-
  // voice control renders separately below, only while a session is live.
  const voiceModeButton = () => (
    <Chip
      on={activeChat.voice_mode}
      tone="bg-sky-950/60 border-sky-700 text-sky-300"
      title={activeChat.voice_mode
        ? 'Concise replies ON - replies are short and spoken-style. This does not touch the microphone.'
        : 'Concise replies OFF - click for short, spoken-style replies. This does not touch the microphone.'}
      onClick={onToggleVoice}
    >
      concise replies
    </Chip>
  )

  const memoryButton = () => (
    <Chip
      on={activeChat.memory_enabled}
      tone="bg-purple-950/60 border-purple-700 text-purple-300"
      title={activeChat.memory_enabled
        ? 'Memory ON - your shared memory summary is included for all participants (browse it via the 🧠 chip)'
        : 'Memory OFF - participants know nothing about you in this chat'}
      onClick={onToggleMemory}
    >
      <Brain size={13} /> memory
    </Chip>
  )

  const webButton = () => (
    <Chip
      on={activeChat.web_enabled}
      tone="bg-indigo-950/60 border-indigo-700 text-indigo-300"
      title={activeChat.web_enabled
        ? `Web research ON - search: ${cfg?.search?.tavily || cfg?.search?.brave ? [cfg.search.tavily && 'Tavily', cfg.search.brave && 'Brave'].filter(Boolean).join(' + ') : 'no search key (page/Reddit fetch only)'}`
        : 'Web research OFF - click to let models search and fetch pages'}
      onClick={onToggleWeb}
    >
      <Globe size={13} /> web
    </Chip>
  )

  // Dev-mode chip (Claude Code guest + GitHub issue tools) - shown when the
  // server says either capability is usable on this machine.
  const codeButton = () => (cfg?.code?.available || cfg?.github?.available) && (
    <>
      <Chip
        on={activeChat.code_enabled}
        tone="bg-orange-950/60 border-orange-700 text-orange-300"
        title={activeChat.code_enabled
          ? `Code ON - the AIs can ${[cfg.code?.available && (cfg.code?.writes ? 'summon Claude Code (investigate, or implement + open PRs - never merge)' : 'summon Claude Code (read-only)'), cfg.github?.available && 'read & file GitHub issues'].filter(Boolean).join(' and ')}. Repos: ${[...new Set([...(cfg.code?.repos || []), ...(cfg.github?.repos || [])])].join(', ')}`
          : 'Code OFF - click to let the AIs work with your repos: summon Claude Code and read/file GitHub issues'}
        onClick={onToggleCode}
      >
        <SquareTerminal size={13} /> code
      </Chip>
      {/* Billing-drift indicator: visible whenever Claude Code guests would
          bill the metered API key instead of a configured subscription token,
          so it is caught at a glance rather than on the invoice. */}
      {cfg?.code?.available && cfg?.code?.use_api_key && (
        <span
          title="Claude Code guests are billing your ANTHROPIC_API_KEY (metered). Guest turns bill that key unless a subscription token is configured. To switch: run `claude setup-token`, set CLAUDE_CODE_OAUTH_TOKEN, then turn code_use_api_key off."
          className="text-xs inline-flex items-center gap-1 rounded-full px-2 py-1 border bg-amber-950/60 border-amber-700 text-amber-300"
        >
          <AlertTriangle size={12} /> API-billed
        </span>
      )}
    </>
  )

  // 🧠 status chip: memory lives in a companion service - link out when it's
  // up, hint at how to enable it when it isn't.
  const memoryChip = memory?.available && memory.url ? (
    <a
      href={memory.url}
      target="_blank"
      rel="noreferrer"
      title="Shared memory is on - open the memory service UI"
      className="text-xs inline-flex items-center rounded-full px-2 py-1 border bg-purple-950/40 border-purple-800 text-purple-300 hover:border-purple-600"
    >
      🧠
    </a>
  ) : (
    <span
      title="Memory service not detected - run Membro to enable shared memory"
      className="text-xs inline-flex items-center rounded-full px-2 py-1 border border-edge2 text-ink-faint opacity-60 cursor-help"
    >
      🧠
    </span>
  )

  const totalsTitle = 'Chat totals: model tokens, cost, and voice usage (TTS characters / STT seconds). '
    + 'Billed spend is what you actually pay; usage covered by a subscription is counted apart and never added to it. '
    + 'Rates editable in config.json'

  const totalsText = usage && (
    <>
      {usage.tokens}
      {costFigures.map((f) => (
        <span key={f.tone}> · <span className={COST_TONE[f.tone]}>{f.text}</span></span>
      ))}
      {usage.voice}
    </>
  )

  // The context ring. Rendered in BOTH headers: it was desktop-only, so on a
  // phone the one number that would have explained a chat going slow was absent
  // entirely. At-a-glance has to work without opening a menu, on whichever
  // device the chat is happening on.
  const contextRing = gauge && (() => {
    const R = 7, CIRC = 2 * Math.PI * R
    return (
      <span className="flex items-center gap-1 text-xs font-medium tabular-nums" style={{ color: gauge.tone }} title={gauge.title}>
        <svg width="18" height="18" viewBox="0 0 18 18" className="-rotate-90 shrink-0">
          <circle cx="9" cy="9" r={R} fill="none" stroke={gauge.tone} strokeOpacity="0.22" strokeWidth="2.5" />
          <circle
            cx="9" cy="9" r={R} fill="none" stroke={gauge.tone} strokeWidth="2.5" strokeLinecap="round"
            strokeDasharray={CIRC} strokeDashoffset={CIRC * (1 - gauge.pct / 100)}
            style={{ transition: 'stroke-dashoffset 0.4s ease, stroke 0.4s ease' }}
          />
        </svg>
        {gauge.label}
      </span>
    )
  })()

  // ---- Desktop popovers: the four mode toggles fold into one Tools button
  // whose dots show what's on; the audit numbers live behind the ring.
  // Set-and-forget per-chat choices don't need permanent chrome, but every
  // explainer the chips carried moves INTO the popover, not away.
  const toggleFor = { voice: onToggleVoice, memory: onToggleMemory, web: onToggleWeb, code: onToggleCode }
  const rows = capabilityRows(activeChat, cfg)
  const DOT = { voice: '#7dd3fc', memory: '#d8b4fe', web: '#a5b4fc', code: '#fdba74' }

  const toolsButton = (
    <div className="relative" ref={toolsRef}>
      <button
        className="text-xs inline-flex items-center gap-1.5 rounded-full px-2.5 py-1 border border-edge2 text-ink-dim hover:text-ink-mid"
        aria-haspopup="menu"
        aria-expanded={toolsOpen}
        title="What this chat's models can do - voice style, memory, web research, code. Click to view or change."
        onClick={() => { setToolsOpen((v) => !v); setUsageOpen(false) }}
      >
        <Wrench size={13} /> Tools
        <span className="inline-flex items-center gap-[3px]" aria-hidden="true">
          {rows.filter((r) => r.on).map((r) => (
            <span key={r.key} className="h-1.5 w-1.5 rounded-full" style={{ backgroundColor: DOT[r.key] }} />
          ))}
        </span>
      </button>
      {toolsOpen && (
        <div
          role="menu"
          className="absolute right-0 top-8 z-30 w-72 bg-panel border border-edge2 rounded-xl p-2 space-y-0.5"
          style={{ boxShadow: 'var(--shadow-pop)' }}
        >
          {rows.map((r) => (
            <button
              key={r.key}
              role="menuitemcheckbox"
              aria-checked={r.on}
              className="w-full flex items-start gap-2.5 text-left rounded-lg px-2.5 py-2 hover:bg-panel2"
              onClick={() => toggleFor[r.key]()}
            >
              <span className="mt-0.5 h-2 w-2 rounded-full shrink-0" style={{ backgroundColor: r.on ? DOT[r.key] : 'var(--t-edge3)' }} aria-hidden="true" />
              <span className="flex-1 min-w-0">
                <span className={`block text-xs font-medium ${r.on ? 'text-ink' : 'text-ink-mid'}`}>{r.label}</span>
                <span className="block text-[11px] text-ink-dim leading-snug">{r.hint}</span>
              </span>
              <span className={`mt-0.5 text-[10px] uppercase tracking-wide shrink-0 ${r.on ? 'text-ink-mid' : 'text-ink-faint'}`}>{r.on ? 'on' : 'off'}</span>
            </button>
          ))}
          <div className="flex items-center gap-2 border-t border-edge mt-1 pt-1.5 px-2.5 pb-1 text-[11px] text-ink-dim">
            {memory?.available && memory.url ? (
              <a href={memory.url} target="_blank" rel="noreferrer" className="hover:text-ink-mid" title="Open the memory service UI">
                🧠 Shared memory service connected - open
              </a>
            ) : (
              <span className="cursor-help" title="Run Membro to enable shared memory">🧠 Memory service not detected</span>
            )}
          </div>
        </div>
      )}
    </div>
  )

  const usageButton = gauge && (
    <div className="relative" ref={usageRef}>
      <button
        className="inline-flex"
        aria-haspopup="dialog"
        aria-expanded={usageOpen}
        title="Context and cost for this chat - click for the numbers"
        onClick={() => { setUsageOpen((v) => !v); setToolsOpen(false) }}
      >
        {contextRing}
      </button>
      {usageOpen && (
        <div
          role="dialog"
          aria-label="This chat's context and cost"
          className="absolute left-0 top-8 z-30 w-80 bg-panel border border-edge2 rounded-xl p-3 space-y-2 text-xs"
          style={{ boxShadow: 'var(--shadow-pop)' }}
        >
          <div className="text-ink whitespace-pre-line leading-relaxed">{gauge.title}</div>
          {usage && (
            <div className="border-t border-edge pt-2 tabular-nums text-ink-mid">
              {usage.tokens}
              {costFigures.map((f) => (
                <span key={f.tone}> · <span className={COST_TONE[f.tone]}>{f.text}</span></span>
              ))}
              {usage.voice}
            </div>
          )}
          {costNote && <div className="text-[11px] text-ink-faint leading-snug">{costNote}</div>}
        </div>
      )}
    </div>
  )

  return (
    <>
      {/* ---- Mobile header (<sm): hamburger · title · talk · ⋯ ---- */}
      <header className="sm:hidden border-b border-edge px-3 py-2 flex items-center gap-2">
        <button
          title="Menu - chats & projects"
          aria-label="Open chats & projects"
          className="text-ink-dim hover:text-ink p-2 -ml-1 shrink-0"
          onClick={onOpenDrawer}
        >
          <Menu size={20} />
        </button>
        <h1 className="font-semibold truncate min-w-0">{activeTitle}</h1>
        {runningBadge}
        <span className="flex-1" />
        {contextRing}
        {cfg?.voice_enabled && voiceState === 'off' && (
          <button
            title="Start a hands-free voice conversation (Scribe + ElevenLabs voices)"
            aria-label="Start voice conversation"
            className="shrink-0 inline-flex items-center justify-center rounded-full h-10 w-10 border border-edge2 text-ink-dim hover:text-ink-mid"
            onClick={onStartVoice}
          >
            <Headphones size={18} />
          </button>
        )}
        {/* #108: while the mic is live, the kill switch is ALWAYS here -
            never only inside the dock. Ending voice here stops capture:
            tracks, socket, context, timers, the lot. */}
        {voiceState !== 'off' && (
          <button
            title="End the voice session - stops the microphone completely"
            aria-label="End voice session and stop the microphone"
            className="shrink-0 inline-flex items-center gap-1.5 rounded-full px-3 h-10 border border-red-800 bg-red-950/60 text-red-300 hover:text-red-200 text-xs font-medium"
            onClick={onEndVoice}
          >
            <MicOff size={14} /> End voice
          </button>
        )}
        <div className="relative shrink-0" ref={overflowRef}>
          <button
            title="More"
            aria-label="More options"
            aria-haspopup="menu"
            aria-expanded={overflowOpen}
            className="inline-flex items-center justify-center rounded-full h-10 w-10 text-ink-dim hover:text-ink border border-transparent hover:border-edge2"
            onClick={() => setOverflowOpen((v) => !v)}
          >
            <MoreHorizontal size={20} />
          </button>
          {overflowOpen && (
            <div
              role="menu"
              className="overflow-menu absolute right-0 top-11 z-30 w-64 max-w-[calc(100vw-24px)] p-3 space-y-3"
            >
              {activeProject && (
                <div className="text-xs text-ink-mid inline-flex items-center gap-1.5">
                  📁 {activeProject.name}
                </div>
              )}
              {/* Context + cost, as text rows */}
              {(gauge || usage) && (
                <div className="space-y-1 pb-1 border-b border-edge">
                  {gauge && (
                    <div className="flex items-center justify-between text-xs" title={gauge.title}>
                      <span className="text-ink-dim">Context</span>
                      <span className="tabular-nums font-medium" style={{ color: gauge.tone }}>
                        {gauge.label} · {Math.round(gauge.pct)}%
                      </span>
                    </div>
                  )}
                  {usage && (
                    <div className="flex items-center justify-between text-xs" title={totalsTitle}>
                      <span className="text-ink-dim">Usage</span>
                      <span className="tabular-nums text-ink-mid text-right">{totalsText}</span>
                    </div>
                  )}
                  {/* Why two dollar figures, in words, on screen, not in
                      a tooltip a phone can't hover. */}
                  {costNote && (
                    <p className="text-[11px] text-ink-faint leading-snug">{costNote}</p>
                  )}
                </div>
              )}
              {/* Participant toggles */}
              <div>
                <p className="text-[11px] font-semibold uppercase tracking-[0.05em] text-ink-dim mb-1.5">Participants</p>
                <div className="flex flex-wrap gap-1.5">{participantToggles()}</div>
              </div>
              {/* Chat toggles */}
              <div className="flex flex-wrap gap-1.5">
                {memoryButton()}
                {webButton()}
                {codeButton()}
                {voiceModeButton()}
              </div>
              {/* Actions */}
              <div className="flex items-center gap-1.5 pt-1 border-t border-edge">
                <button
                  title="Copy the whole chat to the clipboard (markdown)"
                  aria-label="Copy the whole chat to the clipboard as markdown"
                  className="inline-flex items-center gap-1.5 text-xs text-ink-mid hover:text-ink rounded-lg px-2.5 py-2"
                  onClick={() => { onCopyChat(); setOverflowOpen(false) }}
                >
                  {copiedChat ? <Check size={14} /> : <Copy size={14} />} Copy chat
                </button>
                {memoryChip}
              </div>
            </div>
          )}
        </div>
      </header>

      {/* ---- Desktop header (sm+): full, session settings live in the dock ---- */}
      <header className="hidden sm:flex border-b border-edge px-5 py-3 items-center gap-3 flex-wrap">
        <h1 className="font-semibold truncate">{activeTitle}</h1>
        {runningBadge}
        <button
          title="Copy the whole chat to the clipboard (markdown)"
          aria-label="Copy the whole chat to the clipboard as markdown"
          className="text-ink-dim hover:text-ink p-1 shrink-0"
          onClick={onCopyChat}
        >
          {copiedChat ? <Check size={15} /> : <Copy size={15} />}
        </button>
        {activeProject && (
          <span className="text-xs bg-panel2 border border-edge2 rounded-full px-2.5 py-0.5 text-ink-mid">
            📁 {activeProject.name}
          </span>
        )}
        {usageButton}
        <div className="ml-auto flex items-center gap-1.5">
          {participantToggles()}
          {toolsButton}
          {/* The API-billed drift warning stays OUT of the popover: a
              billing surprise must be visible at a glance, not one click deep. */}
          {cfg?.code?.available && cfg?.code?.use_api_key && (
            <span
              title="Claude Code guests are billing your ANTHROPIC_API_KEY (metered). Guest turns bill that key unless a subscription token is configured. To switch: run `claude setup-token`, set CLAUDE_CODE_OAUTH_TOKEN, then turn code_use_api_key off."
              className="text-xs inline-flex items-center gap-1 rounded-full px-2 py-1 border bg-amber-950/60 border-amber-700 text-amber-300"
            >
              <AlertTriangle size={12} /> API-billed
            </span>
          )}
          {/* talk is the one true ACTION in this cluster, so it stops dressing
              like a toggle. In-session controls live in the voice dock. */}
          {cfg?.voice_enabled && voiceState === 'off' && (
            <button
              title="Start a hands-free voice conversation (Scribe + ElevenLabs voices)"
              className="text-xs inline-flex items-center gap-1.5 rounded-full px-3 py-1 border border-edge3 bg-panel2 text-ink hover:bg-panel"
              onClick={onStartVoice}
            >
              <Headphones size={13} /> talk
            </button>
          )}
        </div>
      </header>
    </>
  )
}
