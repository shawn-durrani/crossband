import { useEffect, useRef } from 'react'
import { api, streamSSE } from '../api'
import {
  mergeMessagesById, highestId, hydrateCursor, nextBackoffDelay, INITIAL_BACKOFF_MS,
  shouldHydrateActiveChat, shouldVoiceAttach, shouldDeferEvent,
  voiceAttachEligible, queuePendingEvent, drainPendingQueue,
} from '../eventStream'
import { mergeGuestJob } from '../guestJobs'

// The global live-events connection, lifted out of App.jsx.
//
// ONE persistent SSE stream per tab, not per chat - see backend/events.py's
// module docstring for the full design. Everything the connection owns lives
// here: the watermark, the reconnect token, the abort controller, the queue of
// events deferred while a round was streaming, and the belt-and-suspenders
// poll. None of it is referenced anywhere else in the app, which is what made
// this a clean seam.
//
// **Why callbacks go through a ref.** The stream is opened once, in a mount
// effect, so everything it closes over is frozen at first render. Today that
// happens to be safe because the handlers only touch refs and setState
// functions, both stable - but `onVoiceAttach` is an ordinary function
// re-created every render, and freezing it would be a real bug waiting for
// someone to add state to it. `cb.current` is refreshed on every render, so the
// long-lived stream always calls today's handlers. Same hazard App.jsx already
// works around with `voiceActiveRef` and `addCaptionRef`.
export function useEventStream({
  messages, activeChatIdRef, streamingRef, voiceActiveRef,
  refreshState, onGuestJob, onMessages, onUnread, onVoiceAttach, onError,
  onRoomEvent,
}) {
  const watermarkRef = useRef(0)      // highest message id this tab has seen, ANY chat
  const messagesRef = useRef([])      // live mirror of `messages`, for the handler's closure
  const pendingLiveEvents = useRef([])
  const tokenRef = useRef(0)          // a newer connect attempt (or unmount) supersedes an older retry loop
  const ctrlRef = useRef(null)

  const cb = useRef(null)
  cb.current = { refreshState, onGuestJob, onMessages, onUnread, onVoiceAttach,
                 onError, onRoomEvent }

  useEffect(() => { messagesRef.current = messages }, [messages])

  // Act on a single new_message event: voice-attach and/or hydrate the open
  // chat, or flag another chat unread. Separate from the dispatcher below so
  // the deferred queue can replay events through the exact same logic once the
  // streaming suppression has lifted.
  function hydrateLiveEvent(ev) {
    // Voice mode: a NEW message on the open chat while we're idle means a round
    // we didn't start is generating (a Claude Code hand-back). Attach to it so
    // it's actually spoken, not delivered as a silent text-only message. A
    // message_update (room-mode labels retro-attached, #28) is type-gated out:
    // it re-renders an existing turn, there is no round behind it to attach to.
    if (voiceAttachEligible(ev.type)
        && shouldVoiceAttach(ev.chat_id, activeChatIdRef.current,
                             { streaming: streamingRef.current, voiceActive: voiceActiveRef.current })) {
      cb.current.onVoiceAttach(ev.chat_id)
    }
    if (shouldHydrateActiveChat(ev.chat_id, activeChatIdRef.current, streamingRef.current)) {
      // Anchored on the EVENT's id, not just the local high-water mark:
      // a deferred notice's id sits below the round replies that persisted
      // after it, and "after my highest" could never reach back down to it.
      api.messagesAfter(ev.chat_id, hydrateCursor(ev.id, messagesRef.current))
        .then((d) => {
          // Cross-chat write-guard (the same invariant as streamGuard.js):
          // re-check after the async fetch, since the user may have switched
          // chats while this was in flight.
          if (ev.chat_id !== activeChatIdRef.current) return
          if (d.messages.length) cb.current.onMessages((m) => mergeMessagesById(m, d.messages))
        })
        .catch(() => {}) // transient - the next live event or the fallback poll retries
    } else if (ev.type === 'new_message' && ev.chat_id !== activeChatIdRef.current) {
      // Only a NEW message dirty-marks another chat. A label update on a chat
      // you're not looking at isn't new activity - its labels are simply there
      // when that chat next opens (the fetch reads current rows).
      cb.current.onUnread((s) => (s.has(ev.chat_id) ? s : new Set(s).add(ev.chat_id)))
    }
  }

  // One event off the wire: bump the watermark, then either hydrate the active
  // chat's content (an incremental fetch - never the whole transcript) or just
  // flag another chat as having unseen activity.
  function handleLiveEvent(ev) {
    if (ev.type === 'guest_job') {
      // Guest job status rides the same global stream. Only the open
      // chat drives the chip; other chats' guest activity is out of view (the
      // durable status is re-seeded from the snapshot when that chat opens).
      if (ev.chat_id === activeChatIdRef.current) {
        cb.current.onGuestJob((j) => mergeGuestJob(j, ev))
      }
      return
    }
    if (ev.type === 'room_roster' || ev.type === 'room_flag') {
      // Room-mode roster/flag change (#28 phase 2): the event is content-free
      // (ids + kind), so the open chat refetches its roster snapshot. Never
      // deferred - roster state is not round content, and refetching during a
      // streaming round is safe and idempotent.
      if (ev.chat_id === activeChatIdRef.current) {
        cb.current.onRoomEvent?.(ev.chat_id)
      }
      return
    }
    if (ev.type !== 'new_message' && ev.type !== 'message_update') return
    // Only a new message advances the replay watermark: a message_update
    // carries an OLD id (room-mode labels landing on an existing turn), and
    // its catch-up story is the row itself, not the since-cursor.
    if (ev.type === 'new_message') {
      watermarkRef.current = Math.max(watermarkRef.current, ev.id)
    }
    // A message for the OPEN chat while a round is streaming is suppressed by
    // both paths below (the round's own SSE stream is authoritative while it
    // runs). But the watermark just advanced past this id, so no reconnect will
    // replay it: queue it and drain when streaming ends, else it's lost until a
    // full reload. Other-chat events fall through and still flag unread as
    // before.
    if (shouldDeferEvent(ev.chat_id, activeChatIdRef.current, streamingRef.current)) {
      pendingLiveEvents.current = queuePendingEvent(pendingLiveEvents.current, ev)
      return
    }
    hydrateLiveEvent(ev)
  }

  // Reconnects with escalating backoff and always resumes from the watermark,
  // so a drop (network blip, laptop sleep, or the deploy restart that prompted
  // this stream in the first place) never re-delivers or loses anything: the
  // server's own DB-query catch-up on every (re)connect is what guarantees
  // that, not anything remembered here.
  async function startEventStream() {
    const token = ++tokenRef.current
    let delay = INITIAL_BACKOFF_MS
    while (token === tokenRef.current) {
      const ctrl = new AbortController()
      ctrlRef.current = ctrl
      try {
        await streamSSE(`/api/events/stream?since=${watermarkRef.current}`,
                        null, handleLiveEvent, ctrl.signal, 'GET')
        if (token !== tokenRef.current) return // superseded while connected
        delay = INITIAL_BACKOFF_MS // clean end (shouldn't normally happen) - reset backoff
      } catch (e) {
        if (e.name === 'AbortError') return // unmounted, or a newer connect attempt took over
      }
      if (token !== tokenRef.current) return
      await new Promise((r) => setTimeout(r, delay))
      delay = nextBackoffDelay(delay)
    }
  }

  useEffect(() => {
    let cancelled = false
    // Seed the watermark from the server's current high-water mark BEFORE
    // opening the persistent stream: connecting with since=0 would
    // replay every message ever created as a burst of "new" events, which the
    // UI would misread as unread activity in every chat with history. Only
    // once we know where "now" is do we start listening for what comes after.
    cb.current.refreshState().then((s) => {
      if (cancelled) return
      watermarkRef.current = s.latest_message_id || 0
      startEventStream()
    }).catch((e) => cb.current.onError(`Failed to load: ${e.message}`))
    return () => {
      cancelled = true
      tokenRef.current++ // supersede any in-flight reconnect backoff
      ctrlRef.current?.abort()
    }
  }, [])

  // Belt-and-suspenders fallback: if a proxy/tunnel ever silently breaks
  // SSE without erroring, this ~30s, visibility-gated poll for the OPEN chat
  // still surfaces out-of-band notices: incremental (messages newer than the
  // last one we have), never a full transcript refetch, and the same merge/dedup
  // as the live path so a message can't double-append regardless of which path
  // delivered it first.
  useEffect(() => {
    const tick = async () => {
      if (document.visibilityState !== 'visible') return
      const chatId = activeChatIdRef.current
      if (!chatId || streamingRef.current) return
      try {
        const d = await api.messagesAfter(chatId, highestId(messagesRef.current))
        if (chatId !== activeChatIdRef.current) return
        if (d.messages.length) cb.current.onMessages((m) => mergeMessagesById(m, d.messages))
      } catch { /* transient - try again next tick */ }
    }
    const t = setInterval(tick, 30000)
    return () => clearInterval(t)
  }, [])

  // Drain events that arrived while a round was streaming. App calls this the
  // instant streaming goes false, from runStream's finally, where
  // streamingRef is already false by then, so replaying each event through
  // hydrateLiveEvent now hydrates (and voice-attaches) exactly what was
  // suppressed. mergeMessagesById makes re-hydration idempotent, so a message
  // the round's own stream already rendered can't double-append.
  function drainPendingLiveEvents() {
    if (!pendingLiveEvents.current.length) return
    const { events, queue } = drainPendingQueue(pendingLiveEvents.current)
    pendingLiveEvents.current = queue
    for (const ev of events) hydrateLiveEvent(ev)
  }

  return { drainPendingLiveEvents }
}
