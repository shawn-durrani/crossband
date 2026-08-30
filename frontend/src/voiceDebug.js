// #304 evidence capture: a bounded in-memory ring of the voice/round
// control-flow diagnostics, so the next stall can be reported without a
// tethered browser console. The [voice] and [round] console logs already
// exist and are content-free by their own contracts - this module just
// KEEPS the last few hundred of them, adds the red error text+stack the
// issue asks for (window errors, unhandled rejections, round errors, the
// banner), and posts the lot to the server on the owner's explicit tap.
//
// Privacy floor, stated once: transcript text never enters this ring.
// The [voice]/[round] sources log ids, states and milliseconds only;
// the error entries carry error text and stack traces, which is exactly
// what the issue's evidence list wants and is app/system text, not
// speech. Every entry is size-capped here AND re-capped server-side
// (backend/routers/voice.py sanitize_debug_entries). Nothing leaves the
// browser until the owner taps "save voice diagnostics".

const MAX_ENTRIES = 400
const MAX_DATA_CHARS = 300
const MAX_ERROR_CHARS = 1500

const ring = []
let installed = false

function nowMs() {
  return typeof performance !== 'undefined' && performance.now
    ? performance.now() : Date.now()
}

function push(tag, data) {
  ring.push({ t: Math.round(nowMs() * 10) / 10, tag: String(tag).slice(0, 64), data })
  if (ring.length > MAX_ENTRIES) ring.splice(0, ring.length - MAX_ENTRIES)
}

// One control-flow event, same shape the console lines carry. `data` is
// serialised and capped here so a mistake upstream can never grow the ring.
export function record(tag, data) {
  let s = null
  if (data !== undefined && data !== null && data !== '') {
    try {
      s = JSON.stringify(data).slice(0, MAX_DATA_CHARS)
    } catch {
      s = String(data).slice(0, MAX_DATA_CHARS)
    }
  }
  push(tag, s)
}

// A red error: the text and stack the issue's evidence list asks for.
// `kind` names the surface it appeared on (window, unhandledrejection,
// round, banner).
export function recordError(kind, message, stack) {
  let s
  try {
    s = JSON.stringify({
      message: String(message || '').slice(0, MAX_ERROR_CHARS),
      stack: stack ? String(stack).slice(0, MAX_ERROR_CHARS) : undefined,
    })
  } catch {
    s = String(message || '').slice(0, MAX_ERROR_CHARS)
  }
  push(`error:${kind}`, s)
}

// Catch what nothing else does: uncaught exceptions and unhandled promise
// rejections. Installed once at app start; idempotent; never throws.
export function installGlobalCapture(target) {
  const t = target || (typeof window !== 'undefined' ? window : null)
  if (installed || !t || typeof t.addEventListener !== 'function') return
  installed = true
  t.addEventListener('error', (ev) => {
    recordError('window', ev?.message || ev?.error?.message,
      ev?.error?.stack)
  })
  t.addEventListener('unhandledrejection', (ev) => {
    const r = ev?.reason
    recordError('unhandledrejection',
      (r && typeof r === 'object' ? r.message : r) || 'unhandled rejection',
      r && typeof r === 'object' ? r.stack : undefined)
  })
}

export function snapshot() {
  return ring.map((e) => ({ ...e }))
}

// Test hooks: the ring is module state, so tests need a reset.
export function clear() {
  ring.length = 0
}

export function resetInstalledForTest() {
  installed = false
}

// The one-tap dump: POST the ring to the server, which writes it to one
// file beside its own correlated state (captures, identity history,
// label flow, latency summary). Best-effort like every diagnostic; the
// caller shows the outcome.
export async function dump(chatId) {
  try {
    const r = await fetch('/api/voice/debug-dump', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ chat_id: chatId || null, entries: snapshot() }),
    })
    if (!r.ok) return { ok: false }
    return await r.json()
  } catch {
    return { ok: false }
  }
}
