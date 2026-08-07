import { useEffect, useRef, useState } from 'react'
import { Paperclip, Square, FileText, X, AlertTriangle, Captions } from 'lucide-react'
import { api } from '../api'

const LONG_PASTE_CHARS = 6000
const YT_RE = /(?:youtube\.com\/(?:watch\?(?:[^\s]*&)?v=|shorts\/|embed\/|live\/)|youtu\.be\/)[A-Za-z0-9_-]{11}/

export default function Composer({ onSend, disabled, streaming, queuedCount = 0, onStop, chatParticipants = [], draft, slashCommands = [] }) {
  const [text, setText] = useState('')
  const [attachments, setAttachments] = useState([])
  const [uploading, setUploading] = useState(false)
  const [uploadError, setUploadError] = useState(null)
  const [dragOver, setDragOver] = useState(false)
  const fileInput = useRef(null)
  const textarea = useRef(null)

  const growTextarea = (el) => {
    el.style.height = 'auto'
    el.style.height = `${Math.min(el.scrollHeight, window.innerHeight * 0.4)}px`
  }

  // Empty-state example chips prefill the composer without sending
  useEffect(() => {
    if (draft?.text == null) return
    setText(draft.text)
    const el = textarea.current
    if (el) {
      el.focus()
      requestAnimationFrame(() => growTextarea(el))
    }
  }, [draft])

  const totalBytes = attachments.reduce((s, a) => s + a.size, 0)

  async function addFiles(files) {
    setUploadError(null)
    setUploading(true)
    try {
      for (const file of files) {
        const att = await api.uploadAttachment(file)
        setAttachments((prev) => [...prev, att])
      }
    } catch (e) {
      setUploadError(e.message)
    } finally {
      setUploading(false)
    }
  }

  const ytUrl = (text.match(YT_RE) || [])[0]

  // Pull the FULL transcript of a YouTube link and attach it as a document - bypasses the
  // model tool's length cap and the tool-output replay-trim, so the whole thing reaches the models.
  async function grabYoutube(url) {
    setUploadError(null)
    setUploading(true)
    try {
      const r = await api.youtubeTranscript(url)
      setAttachments((prev) => [...prev, r.attachment])
    } catch (e) {
      setUploadError(`YouTube transcript: ${e.message}`)
    } finally {
      setUploading(false)
    }
  }

  useEffect(() => {
    const prevent = (e) => {
      e.preventDefault()
      if (e.dataTransfer?.types?.includes('Files')) setDragOver(true)
    }
    const leave = (e) => {
      if (!e.relatedTarget) setDragOver(false) // left the window
    }
    const drop = (e) => {
      e.preventDefault()
      setDragOver(false)
      const files = [...(e.dataTransfer?.files || [])]
      if (files.length) addFiles(files)
    }
    window.addEventListener('dragover', prevent)
    window.addEventListener('dragleave', leave)
    window.addEventListener('drop', drop)
    return () => {
      window.removeEventListener('dragover', prevent)
      window.removeEventListener('dragleave', leave)
      window.removeEventListener('drop', drop)
    }
  }, [])

  function handlePaste(e) {
    const files = [...(e.clipboardData?.files || [])]
    if (files.length) {
      e.preventDefault()
      addFiles(files)
      return
    }
    const pasted = e.clipboardData?.getData('text') || ''
    if (pasted.length > LONG_PASTE_CHARS) {
      e.preventDefault()
      const blob = new Blob([pasted], { type: 'text/plain' })
      addFiles([new File([blob], `pasted-${Date.now()}.txt`, { type: 'text/plain' })])
    }
  }

  function submit() {
    const trimmed = text.trim()
    if ((!trimmed && attachments.length === 0) || disabled || uploading) return
    onSend(trimmed, attachments)
    setText('')
    setAttachments([])
    if (textarea.current) textarea.current.style.height = 'auto'
  }

  function insertMention(name) {
    setText((t) => (t.trim() ? `${t.trimEnd()} @${name} ` : `@${name} `))
    textarea.current?.focus()
  }

  // Predictive slash commands: a message starting with "/" is a note to
  // tooling (no model replies). The suggestions come from config; Crossband
  // only renders them, it assigns no meaning to any command.
  const slashMatches = text.startsWith('/')
    ? slashCommands.filter((c) =>
        c.insert.toLowerCase().startsWith(text.trimEnd().toLowerCase()))
    : []

  return (
    <div className={`border-t border-edge bg-app px-3 sm:px-4 py-3 ${dragOver ? 'composer-drag' : ''}`}>
      {uploadError && (
        <div className="flex items-center gap-1.5 text-xs mb-2" style={{ color: 'var(--t-err)' }}><AlertTriangle size={13} /> {uploadError}</div>
      )}
      {attachments.length > 0 && (
        <div className="flex flex-wrap items-center gap-2 mb-2">
          {attachments.map((a) => (
            a.mime.startsWith('image/') ? (
              <span key={a.id} className="relative inline-block">
                <img
                  src={`/api/attachments/${a.id}/file`}
                  alt={a.filename}
                  title={a.filename}
                  className="h-14 w-14 rounded-[6px] object-cover border border-edge2"
                />
                <button
                  className="absolute -top-1.5 -right-1.5 bg-panel2 border border-edge2 rounded-full p-0.5 text-ink-dim hover:text-err"
                  aria-label={`Remove ${a.filename}`}
                  onClick={() => setAttachments((prev) => prev.filter((x) => x.id !== a.id))}
                >
                  <X size={11} />
                </button>
              </span>
            ) : (
            <span
              key={a.id}
              className="inline-flex items-center gap-1.5 bg-panel2 border border-edge2 rounded-lg px-2 py-1 text-xs"
            >
              <FileText size={13} className="text-ink-dim" /> {a.filename}
              <button
                className="text-ink-dim hover:text-err"
                aria-label={`Remove ${a.filename}`}
                onClick={() => setAttachments((prev) => prev.filter((x) => x.id !== a.id))}
              >
                <X size={12} />
              </button>
            </span>
            )
          ))}
          {totalBytes > 1024 * 1024 && (
            <span className="text-xs text-amber-400/80 self-center">
              Large attachments are sent to both models each turn - token costs add up.
            </span>
          )}
        </div>
      )}
      {slashMatches.length > 0 && (
        <div className="flex flex-wrap items-center gap-2 mb-2 text-xs" aria-label="Slash command suggestions">
          <span className="text-ink-dim">to tooling, not the AIs:</span>
          {slashMatches.map((c) => (
            <button
              key={c.insert}
              className="bg-panel2 border border-edge2 rounded-lg px-2 py-1 font-mono text-ink-mid hover:border-edge3 hover:text-ink"
              title={c.hint || c.label || c.insert}
              onClick={() => {
                setText(c.insert)
                textarea.current?.focus()
              }}
            >
              {c.label || c.insert}
            </button>
          ))}
        </div>
      )}
      <div className="flex items-end gap-2">
        <button
          className="text-ink-mid hover:text-ink inline-flex items-center justify-center h-10 w-9 shrink-0"
          title="Attach files"
          aria-label="Attach files"
          onClick={() => fileInput.current?.click()}
        >
          <Paperclip size={18} />
        </button>
        <input
          ref={fileInput}
          type="file"
          multiple
          className="hidden"
          onChange={(e) => {
            addFiles([...e.target.files])
            e.target.value = ''
          }}
        />
        {ytUrl && (
          <button
            className="text-ink-mid hover:text-err inline-flex items-center justify-center h-10 w-9 shrink-0 disabled:opacity-40"
            title="Grab the full YouTube transcript and attach it as a document"
            aria-label="Attach the full YouTube transcript as a document"
            onClick={() => grabYoutube(ytUrl)}
            disabled={uploading}
          >
            <Captions size={18} />
          </button>
        )}
        {/* 16px on mobile: iOS Safari auto-zooms into any focused input under
            16px, which shoved the composer + Send button off the right edge on
            focus. sm+ keeps the 15px design body size. Do NOT "fix" this
            via maximum-scale - that would kill pinch-zoom (a11y floor). */}
        <textarea
          ref={textarea}
          value={text}
          rows={1}
          placeholder={
            !chatParticipants.length
              ? 'Add a participant to this chat first…'
              : streaming
                // Also phone-short: the long form wrapped and clipped.
                // The queued pill above the composer explains the mechanics.
                ? 'Queue a follow-up…'
                : chatParticipants.length > 1
                  // Short enough to never wrap on a phone (the long form
                  // clipped itself mid-word at 375px). The @mention trick is
                  // taught by the chips right below the composer.
                  ? 'Message the room…'
                  : `Message ${chatParticipants[0].name}…`
          }
          className="flex-1 min-w-0 resize-none bg-panel border border-edge2 rounded-xl px-4 py-2.5 text-[16px] sm:text-[15px] leading-relaxed focus:outline-none focus:border-edge3 max-h-[40vh]"
          onChange={(e) => {
            setText(e.target.value)
            growTextarea(e.target)
          }}
          onPaste={handlePaste}
          onKeyDown={(e) => {
            if (e.key === 'Enter' && !e.shiftKey) {
              e.preventDefault()
              submit()
            }
          }}
        />
        {streaming ? (
          // Mid-round: the composer stays live so you can line up a follow-up.
          // Queueing batches your text into one turn; Stop still cuts the round
          // short. Both are reachable, and the stable Stop never moves.
          <div className="flex items-end gap-2 shrink-0">
            <button
              className="bg-btn text-btn-ink font-semibold rounded-xl px-4 py-2.5 text-sm disabled:opacity-40 hover:bg-btn-hover"
              title="Queue this message - it combines with any others and sends as one turn when the round finishes"
              disabled={disabled || uploading || (!text.trim() && attachments.length === 0)}
              onClick={submit}
            >
              {uploading ? 'Uploading…' : queuedCount > 0 ? `Queue +${queuedCount}` : 'Queue'}
            </button>
            <button
              className="inline-flex items-center gap-1.5 border border-rose-700 text-rose-400 hover:text-rose-300 hover:border-rose-500 font-semibold rounded-xl px-4 py-2.5 text-sm"
              title="Stop them mid-reply - the partial stays in the transcript, marked as cut off"
              onClick={onStop}
            >
              <Square size={13} /> Stop
            </button>
          </div>
        ) : (
          <button
            className="bg-btn text-btn-ink font-semibold rounded-xl px-4 py-2.5 text-sm disabled:opacity-40 hover:bg-btn-hover"
            disabled={disabled || uploading || (!text.trim() && attachments.length === 0)}
            onClick={submit}
          >
            {uploading ? 'Uploading…' : 'Send'}
          </button>
        )}
      </div>
      <div className="flex flex-wrap gap-2 mt-2 text-xs text-ink-dim">
        {chatParticipants.map((p) => (
          <button
            key={p.id}
            className="hover:brightness-125"
            style={{ color: p.color }}
            onClick={() => insertMention(p.slug)}
          >
            @{p.name}
          </button>
        ))}
        <span className="ml-auto hidden sm:inline">Enter to send · Shift+Enter for newline · paste or drop files</span>
      </div>
    </div>
  )
}
