import { memo, useEffect, useRef, useState } from 'react'
import ReactMarkdown from 'react-markdown'
import remarkGfm from 'remark-gfm'
import { Copy, Check, FileText, Search, Globe, MessagesSquare, Wrench, AlertTriangle,
  ArrowUp, ArrowDown, Loader2, ChevronRight } from 'lucide-react'
import { participantInfo } from '../speakers'
import { classifyUsage, costLabel, COST_TONE } from '../messageCost'
import { chipData, chipSuffix, chipTitle, crosstalkNote, crosstalkSegments } from '../voiceChips'
import { flagCopy, reassignOptions } from '../roomState'

// Same-speaker messages closer than this group into one visual run
// (header suppressed, tight 4px rhythm - Discord cozy mode).
const GROUP_WINDOW_S = 300

function CopyButton({ text, className = '' }) {
  const [copied, setCopied] = useState(false)
  return (
    <button
      className={`inline-flex items-center gap-1 text-xs text-ink-dim hover:text-ink transition-opacity ${className}`}
      title="Copy"
      aria-label="Copy message"
      onClick={() => {
        navigator.clipboard.writeText(text)
        setCopied(true)
        setTimeout(() => setCopied(false), 1200)
      }}
    >
      {copied ? <Check size={13} /> : <Copy size={13} />}
    </button>
  )
}

function Pre({ children, ...props }) {
  // children is the <code> element; pull its raw text for the copy button
  const codeText = (() => {
    try {
      const child = Array.isArray(children) ? children[0] : children
      return typeof child?.props?.children === 'string'
        ? child.props.children
        : String(child?.props?.children ?? '')
    } catch {
      return ''
    }
  })()
  return (
    <div className="relative group/code">
      <pre {...props}>{children}</pre>
      <CopyButton
        text={codeText}
        className="absolute top-2 right-2 opacity-0 group-hover/code:opacity-100 bg-panel2 rounded px-1.5 py-1"
      />
    </div>
  )
}

function AttachmentChip({ att }) {
  const isImage = att.mime?.startsWith('image/')
  const url = `/api/attachments/${att.id}/file`
  if (isImage) {
    return (
      <a href={url} target="_blank" rel="noreferrer" className="block">
        <img
          src={url}
          alt={att.filename}
          className="max-h-40 max-w-60 rounded-lg object-cover"
        />
      </a>
    )
  }
  return (
    <a
      href={url}
      target="_blank"
      rel="noreferrer"
      className="inline-flex items-center gap-1.5 bg-panel2 rounded-lg px-2.5 py-1.5 text-xs text-ink-mid hover:text-ink transition-colors"
    >
      <FileText size={13} className="text-ink-dim" />
      <span className="max-w-48 truncate">{att.filename}</span>
      <span className="text-ink-faint">{(att.size / 1024).toFixed(0)} KB</span>
    </a>
  )
}

const TOOL_ICONS = { web_search: Search, fetch_page: Globe, fetch_reddit_thread: MessagesSquare }

// One-line chip: [icon] tool name (mono) · arg summary · status. Click expands.
function ToolChip({ ev, running }) {
  let summary = ''
  try {
    const input = JSON.parse(ev.input_json || '{}')
    summary = input.query || input.url || ''
  } catch { /* keep tool name only */ }
  const Icon = TOOL_ICONS[ev.tool] || Wrench
  return (
    <details className="tool-chip border border-edge rounded-lg text-xs max-w-full min-w-0">
      <summary className="cursor-pointer flex items-center gap-1.5 h-7 px-2.5 text-ink-mid hover:text-ink select-none">
        <Icon size={12} className="text-ink-dim shrink-0" />
        <span className="font-mono">{ev.tool.replace(/_/g, ' ')}</span>
        {summary ? <span className="text-ink-faint truncate">· {summary.slice(0, 80)}</span> : null}
        <span className="ml-auto shrink-0 inline-flex items-center" aria-label={running ? 'running' : 'done'}>
          {running
            ? <Loader2 size={12} className="animate-spin text-ink-dim" />
            : <Check size={12} style={{ color: 'var(--t-ok)' }} />}
        </span>
      </summary>
      <pre className="px-3 pb-2.5 pt-1 whitespace-pre-wrap break-words text-ink-mid max-h-64 overflow-y-auto border-t border-edge">
        {ev.output_text}
      </pre>
    </details>
  )
}

function ToolChips({ events, streaming }) {
  const chips = events.map((t, i) => (
    <ToolChip key={t.id} ev={t} running={streaming && i === events.length - 1 && !t.output_text} />
  ))
  if (events.length < 3) {
    return <div className="flex flex-col gap-1 mb-1.5 w-full min-w-0 max-w-[68ch]">{chips}</div>
  }
  // Consecutive tool calls collapse into one group chip
  return (
    <details className="tool-group mb-1.5 w-full min-w-0 max-w-[68ch]">
      <summary className="cursor-pointer inline-flex items-center gap-1.5 h-7 px-2.5 border border-edge rounded-lg text-xs text-ink-mid hover:text-ink select-none">
        <ChevronRight size={12} className="tool-chev text-ink-dim" />
        Ran {events.length} tools
        {streaming && !events[events.length - 1]?.output_text && (
          <Loader2 size={12} className="animate-spin text-ink-dim" />
        )}
      </summary>
      <div className="flex flex-col gap-1 mt-1">{chips}</div>
    </details>
  )
}

function formatStamp(ts) {
  // ts is a unix epoch (seconds). Absolute time in the thread ("14:32");
  // prefix the date once a message is from another day.
  const d = new Date(ts * 1000)
  const time = d.toLocaleTimeString([], { hour: 'numeric', minute: '2-digit' })
  const sameDay = d.toDateString() === new Date().toDateString()
  return sameDay ? time : `${d.toLocaleDateString([], { month: 'short', day: 'numeric' })} · ${time}`
}

function Stamp({ ts, className = '' }) {
  if (!ts) return null
  return (
    <span
      className={`text-[11px] text-ink-faint tabular-nums transition-opacity ${className}`}
      title={new Date(ts * 1000).toLocaleString()}
    >
      {formatStamp(ts)}
    </span>
  )
}

// Wrap plain-text words in fading spans - only used while streaming; the
// completed message re-renders without them (FlowToken pattern, opacity only).
// `cursor`/`seen`: every delta re-parses the markdown, and a completing inline
// element (**bold**, a link) restructures the tree and REMOUNTS spans for text
// already on screen - replaying their fade reads as flashing. So each word
// tracks its running character offset, and only offsets beyond what the
// previous render already showed get the animation; old words render plain.
function fadeWords(children, cursor, seen) {
  let k = 0
  const walk = (node) => {
    if (typeof node === 'string') {
      return node.split(/(\s+)/).map((w) => {
        const start = cursor.n
        cursor.n += w.length
        if (w === '' || /^\s+$/.test(w)) return w
        return (
          <span key={`w${k++}`} className={start >= seen ? 'stream-word' : undefined}>
            {w}
          </span>
        )
      })
    }
    if (Array.isArray(node)) return node.map(walk)
    return node
  }
  return walk(children)
}

// Tokens in / tokens out / what the turn cost. The cost is never a bare number:
// a turn that rode a subscription cost no incremental cash, and says so in
// words right next to the figure. The qualifier is exactly as visible as the
// amount it qualifies, and readable with no colour at all.
function UsageFooter({ usageJson, speaker }) {
  const entry = classifyUsage(usageJson, speaker)
  if (!entry) return null
  const label = costLabel(entry)
  const fmt = (n) => (n >= 1000 ? `${(n / 1000).toFixed(1)}k` : n)
  return (
    <div
      className="flex items-center justify-end gap-1.5 text-[11px] text-ink-faint mt-1 max-w-[68ch] opacity-0 group-hover:opacity-100 transition-opacity"
      title="input tokens (incl. cached) / output tokens"
    >
      <ArrowUp size={10} />{fmt(entry.input)}
      <ArrowDown size={10} />{fmt(entry.output)}
      {label && <span className={COST_TONE[label.tone]} title={label.title}>· {label.text}</span>}
    </div>
  )
}

function Message({ msg, prev, participants, mismatchFlag, roomRoster,
                   voicePeople, onReassign, onDiscard, hasLaterReplies }) {
  const info = participantInfo(msg.speaker, participants)
  const isUser = info.isUser
  const [discardConfirm, setDiscardConfirm] = useState(null)
  // Tap-to-correct menu on a labelled user turn (#28 phase 2).
  const [chipMenuOpen, setChipMenuOpen] = useState(false)
  // How many characters of this message the PREVIOUS render already showed -
  // fadeWords animates only beyond this, so re-parses can't re-flash old text.
  const seenChars = useRef(0)
  useEffect(() => {
    seenChars.current = msg.streaming ? (msg.content || '').length : 0
  })
  // Model that actually produced this message (recorded in usage at send time);
  // fall back to the participant's currently configured model for older messages.
  const model = (() => {
    if (isUser) return null
    try {
      const recorded = JSON.parse(msg.usage_json || '{}').model
      if (recorded) return recorded
    } catch { /* fall through */ }
    return participants?.find((p) => p.slug === msg.speaker)?.model || null
  })()

  // Grouping rhythm: 4px within a speaker run, 20px on speaker change,
  // 32px before a user turn (the strongest boundary).
  const grouped =
    !!prev &&
    prev.speaker === msg.speaker &&
    !prev.error &&
    (msg.created_at || 0) - (prev.created_at || 0) < GROUP_WINDOW_S
  const gapClass = !prev ? '' : grouped ? 'mt-1' : isUser ? 'mt-8' : 'mt-5'

  // Buffer incomplete markdown: an unclosed code fence gets a synthetic close
  // so a half-streamed block still renders as code, never as runaway text.
  const fenceOpen = msg.streaming && ((msg.content?.match(/```/g) || []).length % 2 === 1)
  const displayContent = fenceOpen ? `${msg.content}\n\`\`\`` : msg.content

  const mdComponents = { pre: Pre }
  if (msg.streaming) {
    const cursor = { n: 0 } // shared across p/li so offsets run through the whole message
    const seen = seenChars.current
    mdComponents.p = ({ node, children, ...props }) => (
      <p {...props}>{fadeWords(children, cursor, seen)}</p>
    )
    mdComponents.li = ({ node, children, ...props }) => (
      <li {...props}>{fadeWords(children, cursor, seen)}</li>
    )
  }

  /* ---- Sat-out turn: a bare "…" is the passing convention (the system
     prompt tells models to reply with only an ellipsis when they're sitting
     out or have nothing new) - render it as a quiet marker, not a bubble. */
  if (!isUser && !msg.streaming && (msg.content || '').trim() === '…') {
    return (
      <div className={`flex items-center gap-2 text-xs text-ink-dim ${gapClass}`}
           title={`${info.name} passed this turn`}>
        <i aria-hidden="true" className="inline-block w-1.5 h-1.5 rounded-full"
           style={{ background: info.color, opacity: 0.5 }} />
        <span>{info.name} sat this one out</span>
      </div>
    )
  }

  /* ---- User turn: compact right-aligned bubble --------------------------- */
  if (isUser) {
    // Room-mode voice labels (#28), attached a second or two after the turn
    // by the parallel diarization pass. ALL decision logic lives in
    // voiceChips.js / roomState.js (pure, node --test); this only renders
    // what they return. Tapping a chip opens the correction menu.
    const voiceChips = chipData(msg, voicePeople)
    const reassignNames = onReassign
      ? reassignOptions(roomRoster, voicePeople, voiceChips.map((c) => c.label))
      : []
    // Crosstalk (#28 phase 4): the plain-English marker, and the best-effort
    // per-voice split when one was salvageable. Decision logic in
    // voiceChips.js; this only renders what it returns.
    const ctNote = crosstalkNote(msg)
    const ctSegments = crosstalkSegments(msg, voicePeople)
    return (
      <div className={`group flex items-center justify-end gap-2 ${gapClass}`}>
        {/* #106: a captured voice turn is discardable by its owner - the
            affordance, the honest confirm copy (voiceDiscard.js, pure) and
            the deletion all speak plainly about what cannot be undone. */}
        {canDiscard(msg) && onDiscard && (
          discardConfirm === msg.id ? (
            <span className="flex items-center gap-1.5 text-[11px]">
              <span className="text-ink-faint max-w-[16rem] text-right leading-tight">
                {discardWarnings({ hasLaterReplies }).join(' ')}
              </span>
              <button
                className="text-red-400 hover:text-red-300 border border-red-900 rounded px-1.5 py-0.5 shrink-0"
                onClick={() => onDiscard(msg.id)}
              >
                Discard turn
              </button>
              <button className="text-ink-dim hover:text-ink shrink-0"
                      onClick={() => setDiscardConfirm(null)}>keep</button>
            </span>
          ) : (
            <button
              className="opacity-0 group-hover:opacity-100 text-ink-faint hover:text-red-400"
              title="Discard this captured voice turn - removes it from the chat and from future model context"
              aria-label="Discard this voice turn"
              onClick={() => setDiscardConfirm(msg.id)}
            >
              <Trash2 size={13} />
            </button>
          )
        )}
        {msg.content && (
          <CopyButton text={msg.content} className="opacity-0 group-hover:opacity-100" />
        )}
        <Stamp ts={msg.created_at} className="opacity-0 group-hover:opacity-100" />
        <div className="flex flex-col items-end gap-1.5 max-w-[75%] min-w-0">
          {voiceChips.length > 0 && (
            <div className="relative flex flex-wrap justify-end gap-1">
              {voiceChips.map((chip) => (
                <button
                  key={chip.label}
                  type="button"
                  title={chipTitle(chip)}
                  aria-label={`Spoken by ${chip.display || chip.label}${chip.owner ? ' (voice confirmed)' : ''}${chip.learning ? ' (learning this voice)' : ''}${chip.uncertain && !chip.learning ? ' (uncertain)' : ''}${reassignNames.length ? ' - tap to correct' : ''}`}
                  className={`text-[11px] px-1.5 py-0.5 rounded-full border bg-panel2 ${
                    chip.uncertain
                      ? 'border-dashed border-amber-700/70 text-amber-200/90'
                      : 'border-edge2 text-ink-dim'
                  } ${reassignNames.length ? 'hover:text-ink cursor-pointer' : 'cursor-default'}`}
                  onClick={() => reassignNames.length && setChipMenuOpen((o) => !o)}
                >
                  {/* The tick (#28 PR-C), the cold-start "· learning" tag
                      (#28) and the bare "?" are all decided in
                      voiceChips.chipSuffix - never in this JSX. */}
                  {chip.display || chip.label}{chipSuffix(chip)}
                </button>
              ))}
              {chipMenuOpen && reassignNames.length > 0 && (
                <div className="absolute top-full right-0 mt-1 z-10 min-w-32 bg-panel border border-edge rounded-lg p-1 flex flex-col"
                     style={{ boxShadow: 'var(--shadow-pop)' }}>
                  <span className="text-[10px] px-2 py-0.5 text-ink-faint">Who spoke this?</span>
                  {reassignNames.map((o) => (
                    <button
                      key={o.name}
                      type="button"
                      className="text-left text-xs px-2 py-1 rounded text-ink-mid hover:text-ink hover:bg-panel2"
                      onClick={() => { setChipMenuOpen(false); onReassign?.(msg.id, o.name) }}
                    >
                      {o.display}
                    </button>
                  ))}
                  <button
                    type="button"
                    className="text-left text-[11px] px-2 py-1 rounded text-ink-faint hover:text-ink-mid"
                    onClick={() => setChipMenuOpen(false)}
                  >
                    cancel
                  </button>
                </div>
              )}
            </div>
          )}
          {ctNote && (
            <div className="text-[11px] text-amber-200/80 text-right max-w-[46ch]">
              {ctNote}
            </div>
          )}
          {ctSegments.length > 0 && (
            <div className="text-[11px] text-ink-dim text-right max-w-[46ch]"
                 title="Best-effort split by voice from the second listen - not verbatim certainty.">
              {ctSegments.map((s, i) => (
                <span key={i}>
                  {i > 0 ? ' / ' : ''}
                  <span className="font-medium">{s.display || s.label}{s.uncertain ? '?' : ''}:</span>
                  {' '}&ldquo;{s.text}&rdquo;
                </span>
              ))}
            </div>
          )}
          {mismatchFlag && (
            <div className="text-[11px] text-amber-300/90 text-right max-w-[46ch]">
              {flagCopy(mismatchFlag)}
            </div>
          )}
          {msg.attachments?.length > 0 && (
            <div className="flex flex-wrap justify-end gap-2">
              {msg.attachments.map((a) => (
                <AttachmentChip key={a.id} att={a} />
              ))}
            </div>
          )}
          {msg.content && (
            <div className="user-bubble md-body break-words min-w-0">
              <ReactMarkdown remarkPlugins={[remarkGfm]} components={{ pre: Pre }}>
                {msg.content}
              </ReactMarkdown>
            </div>
          )}
        </div>
      </div>
    )
  }

  /* ---- AI turn: full-width prose block with a colored left spine ---------- */
  return (
    <div
      className={`group relative ${gapClass}`}
      style={{ borderLeft: `2px solid ${info.color}`, paddingLeft: '12px' }}
      aria-busy={msg.streaming || undefined}
    >
      {!grouped ? (
        <div className="flex items-baseline gap-2 mb-1 min-w-0">
          <span
            className="h-2 w-2 rounded-full self-center shrink-0"
            style={{ backgroundColor: info.color }}
          />
          <span className="text-[13px] font-semibold" style={{ color: info.color }}>
            {info.label}
          </span>
          {model && <span className="text-xs text-ink-dim truncate">{model}</span>}
          <Stamp ts={msg.created_at} className="opacity-0 group-hover:opacity-100" />
          {!msg.streaming && msg.content && (
            <CopyButton text={msg.content} className="opacity-0 group-hover:opacity-100" />
          )}
        </div>
      ) : (
        // grouped run: header suppressed; timestamp + copy surface on hover
        <div className="absolute right-0 top-0 flex items-center gap-2 opacity-0 group-hover:opacity-100 transition-opacity bg-app/80 rounded px-1">
          <Stamp ts={msg.created_at} />
          {!msg.streaming && msg.content && <CopyButton text={msg.content} />}
        </div>
      )}
      {msg.streaming && !msg.content && (
        msg.workStatus ? (
          // A trusted activity label (never the model's own words, never
          // persisted) beats a generic "thinking" placeholder while a tool
          // batch is in flight: same live spot, disappears the instant real
          // content/tool results arrive (useRoundStream.js clears it then).
          <span className="stream-status my-1 inline-flex items-center gap-1.5 text-xs text-ink-dim" role="status">
            <Loader2 size={12} className="animate-spin" />
            {msg.workStatus.label}
          </span>
        ) : (
          <span className="stream-dots my-1" role="status" aria-label={`${info.label} is thinking`}>
            <i /><i /><i />
          </span>
        )
      )}
      {msg.tool_events?.length > 0 && (
        <ToolChips events={msg.tool_events} streaming={msg.streaming} />
      )}
      {msg.attachments?.length > 0 && (
        <div className="flex flex-wrap gap-2 mb-1.5">
          {msg.attachments.map((a) => (
            <AttachmentChip key={a.id} att={a} />
          ))}
        </div>
      )}
      {msg.content && (
        <div className={`md-body break-words ${msg.streaming ? 'md-streaming' : ''}`}>
          <ReactMarkdown remarkPlugins={[remarkGfm]} components={mdComponents}>
            {displayContent}
          </ReactMarkdown>
        </div>
      )}
      {msg.error && (
        <div className="flex items-center gap-1.5 text-xs mt-1" style={{ color: 'var(--t-err)' }}>
          <AlertTriangle size={13} /> {msg.error}
        </div>
      )}
      {!msg.streaming && <UsageFooter usageJson={msg.usage_json} speaker={msg.speaker} />}
    </div>
  )
}

// --- Memoization (phase 1) --------------------------------------------------
// Chat switching loads the full history and App.jsx re-renders EVERY message on
// each state update (a streaming delta, a tool_activity append, the round-end
// pass at App.jsx that stamps `streaming:false` onto all messages). Without
// memoization each such update re-runs ReactMarkdown/remarkGfm over every old
// message - the dominant cost on big chats. React.memo lets unchanged messages
// skip that work. The comparator compares by VALUE (not object identity, since
// App.jsx builds fresh arrays/objects every update) across exactly the fields
// this component reads: skip the re-render only when all of them are unchanged.

function sameAttachments(a = [], b = []) {
  if (a === b) return true
  if (a.length !== b.length) return false
  for (let i = 0; i < a.length; i++) if (a[i].id !== b[i].id) return false
  return true
}

function sameToolEvents(a = [], b = []) {
  if (a === b) return true
  if (a.length !== b.length) return false
  for (let i = 0; i < a.length; i++) {
    // output_text grows/fills during a streaming tool run; input_json can too.
    if (a[i].id !== b[i].id) return false
    if (a[i].output_text !== b[i].output_text) return false
    if (a[i].input_json !== b[i].input_json) return false
  }
  return true
}

function propsEqual(prev, next) {
  // participantInfo/model lookup depend on the roster; it's a stable reference
  // across message-only updates, so identity comparison is correct and cheap.
  if (prev.participants !== next.participants) return false

  // Room-mode props (#28 phase 2). roomRoster/voicePeople are state
  // references that only change when the room snapshot refetches - identity
  // comparison is right. The mismatch flag compares by id + resolution.
  // onReassign is deliberately NOT compared: App re-creates the handler each
  // render, and it reads only live refs - comparing it would defeat the memo.
  if (prev.roomRoster !== next.roomRoster) return false
  if (prev.voicePeople !== next.voicePeople) return false
  if ((prev.mismatchFlag?.id ?? null) !== (next.mismatchFlag?.id ?? null)
      || (prev.mismatchFlag?.resolved_at ?? null) !== (next.mismatchFlag?.resolved_at ?? null)) {
    return false
  }

  const a = prev.msg, b = next.msg
  if (
    a.id !== b.id ||
    a.content !== b.content ||
    !!a.streaming !== !!b.streaming ||  // undefined vs false must compare equal
    a.speaker !== b.speaker ||
    a.created_at !== b.created_at ||
    a.error !== b.error ||
    a.usage_json !== b.usage_json ||
    (a.voice_labels || '') !== (b.voice_labels || '') ||  // room-mode labels land AFTER insert
    (a.workStatus?.label || '') !== (b.workStatus?.label || '') ||
    !sameAttachments(a.attachments, b.attachments) ||
    !sameToolEvents(a.tool_events, b.tool_events)
  ) return false

  // Grouping (GROUP_WINDOW_S) reads only these fields off the previous message.
  const pa = prev.prev, pb = next.prev
  if (
    pa?.speaker !== pb?.speaker ||
    !!pa?.error !== !!pb?.error ||
    (pa?.created_at || 0) !== (pb?.created_at || 0)
  ) return false

  return true
}

export default memo(Message, propsEqual)
