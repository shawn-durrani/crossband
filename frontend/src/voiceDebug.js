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
// browser until the owner taps "save voice diagnostics", or until the app
// itself sees a voice stall and saves the same bundle automatically
// (autoDump below, rate-limited).

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
// caller shows the outcome. `trigger` names what asked for the save:
// 'manual' for the button, or the stall kind for an automatic save.
export async function dump(chatId, trigger = 'manual') {
  try {
    const r = await fetch('/api/voice/debug-dump', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ chat_id: chatId || null, trigger,
                             entries: snapshot() }),
    })
    if (!r.ok) return { ok: false }
    return await r.json()
  } catch {
    return { ok: false }
  }
}

// ---- Automatic saves (#304) ----------------------------------------------
//
// The next stall captures itself: when the voice client sees a hand-off or
// a round that never finished (a stall beacon kind in voice.js), it saves
// the same bundle the button does, with nobody pressing anything.
// The rate limit is what stops a flapping connection from turning one bad
// minute into a folder of near-identical files: at most one automatic save
// every AUTO_SAVE_MIN_GAP_MS, and at most AUTO_SAVE_MAX_PER_PAGE per page
// load. Ten minutes is long enough that one stall episode (a stranded
// barge-in, then the round guard, then the hand-off clock, all within about
// a minute) saves once, and short enough that a separate stall later in the
// same session still gets its own file. Only successful saves count, since
// a failed POST writes nothing. The server keeps its own floor too.
export const AUTO_SAVE_MIN_GAP_MS = 10 * 60 * 1000
export const AUTO_SAVE_MAX_PER_PAGE = 3

// Pure decision: `savedAt` holds the times of this page's successful
// automatic saves, oldest first.
export function autoSaveDecision(savedAt, now) {
  const saves = savedAt || []
  if (saves.length >= AUTO_SAVE_MAX_PER_PAGE) {
    return { allowed: false, reason: 'page_limit' }
  }
  const last = saves[saves.length - 1]
  if (last !== undefined && Number(now) - last < AUTO_SAVE_MIN_GAP_MS) {
    return { allowed: false, reason: 'too_soon' }
  }
  return { allowed: true, reason: null }
}

const autoSaves = []
let autoInFlight = false

// Save the ring automatically for a stall of kind `trigger`. Never throws.
// A skipped save still leaves a ring entry, so a later manual save shows
// every stall the rate limit held back.
export async function autoDump(chatId, trigger, now = nowMs()) {
  const d = autoInFlight ? { allowed: false, reason: 'in_flight' }
    : autoSaveDecision(autoSaves, now)
  if (!d.allowed) {
    record('diag:autoSkipped', { trigger, reason: d.reason })
    return { ok: false, skipped: d.reason }
  }
  autoInFlight = true
  record('diag:autoSave', { trigger })
  try {
    const r = await dump(chatId, trigger)
    if (r && r.ok) autoSaves.push(now)
    else record('diag:autoFailed', { trigger, reason: (r && r.reason) || null })
    return r
  } finally {
    autoInFlight = false
  }
}

// The one quiet line the owner sees after an automatic save: that it
// happened, that no speech is in it, and where the file is.
export function autoSaveNotice(where) {
  return `Voice looked stuck, so the app saved diagnostics (no speech) to ${where}`
}

// The same line's hover explainer, for the surfaces that can show one.
export const AUTO_SAVE_EXPLAINER = 'A voice turn didn\'t reach the models in '
  + 'time, so the app saved the same short technical log the "save voice '
  + 'diagnostics" button saves: states, timings and error messages. It never '
  + 'includes anything that was said. Mention the file name in the bug report.'

export function resetAutoSavesForTest() {
  autoSaves.length = 0
  autoInFlight = false
}
