// Transient caption feed for the mobile voice-call overlay.
//
// Each finished reply (or the user's transcribed utterance) becomes a caption
// that flashes up, drifts gently upward over the HOLD, then fades out and is
// removed. Only the last KEEP are ever on screen. This is UI-only decoration
// layered on top of the real voice pipeline - it never touches voice.js or the
// SSE stream; App feeds it from the same events it already handles.

import { useCallback, useEffect, useRef, useState } from 'react'

export const CAPTION_HOLD_MS = 10000 // how long a caption lives before fading (mockup: 10s)
export const CAPTION_KEEP = 2        // how many captions stay on screen (mockup: last 2)
export const HISTORY_KEEP = 200      // faded captions stay recallable (swipe up on the call screen)

// Pure helper: given the current captions and a new one, return the next list -
// trimmed to `keep` (oldest dropped). Extracted so the trimming rule is unit
// testable without React or timers.
export function pushCaption(captions, caption, keep = CAPTION_KEEP) {
  const next = [...captions, caption]
  return next.length > keep ? next.slice(next.length - keep) : next
}

let seq = 0
const nextId = () => `cap-${Date.now()}-${seq++}`

// Manages the caption array + per-caption life timers. Returns the live list
// (each item gains `leaving: true` shortly before removal, so the view can play
// the fade-out) and an `add({ speaker, name, color, text, you })` pusher.
export function useCaptions({ hold = CAPTION_HOLD_MS, keep = CAPTION_KEEP, enabled = true } = {}) {
  const [captions, setCaptions] = useState([])
  const [history, setHistory] = useState([]) // everything said this session, oldest→newest
  const timers = useRef(new Map()) // id -> [holdTimer, removeTimer]

  const clearTimers = useCallback((id) => {
    const t = timers.current.get(id)
    if (t) { t.forEach(clearTimeout); timers.current.delete(id) }
  }, [])

  const add = useCallback((caption) => {
    if (!enabled) return
    const text = (caption.text || '').trim()
    if (!text) return
    const id = nextId()
    setCaptions((cur) => pushCaption(cur, { ...caption, text, id, leaving: false }, keep))
    setHistory((cur) => pushCaption(cur, { ...caption, text, id: `h-${id}` }, HISTORY_KEEP))
    // Mark leaving (triggers the fade-out animation), then remove after it plays.
    const holdTimer = setTimeout(() => {
      setCaptions((cur) => cur.map((c) => (c.id === id ? { ...c, leaving: true } : c)))
    }, hold)
    const removeTimer = setTimeout(() => {
      setCaptions((cur) => cur.filter((c) => c.id !== id))
      timers.current.delete(id)
    }, hold + 950) // 950ms = the capOut animation duration
    timers.current.set(id, [holdTimer, removeTimer])
  }, [enabled, hold, keep])

  // When the feed is disabled (session ended), drop everything and its timers.
  useEffect(() => {
    if (enabled) return
    timers.current.forEach((t) => t.forEach(clearTimeout))
    timers.current.clear()
    setCaptions([])
    setHistory([])
  }, [enabled])

  // Unmount cleanup.
  useEffect(() => () => {
    timers.current.forEach((t) => t.forEach(clearTimeout))
    timers.current.clear()
  }, [])

  return { captions, history, add }
}
