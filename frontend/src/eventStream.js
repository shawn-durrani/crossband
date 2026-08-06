// Pure helpers for the global live-events stream, kept dependency-free and
// React-free so the actual merge/backoff/dedup logic is unit-testable without
// a DOM harness (same discipline as runningState.js and streamGuard.js).
// App.jsx wires these into one mount-once effect that opens
// GET /api/events/stream and reconnects on drop.

// Merge freshly-fetched rows into the current message list: de-duplicated by
// id, ordered by id ascending. Used both for the live-events hydration path
// (messagesAfter) and the visibility-gated fallback poll — the exact same
// merge either way, so a message can never be appended twice regardless of
// which path delivered it first.
export function mergeMessagesById(current, fresh) {
  if (!fresh || !fresh.length) return current
  const byId = new Map(current.map((m) => [m.id, m]))
  for (const m of fresh) byId.set(m.id, m)
  return [...byId.values()].sort((a, b) => a.id - b.id)
}

// The highest message id across a list — the local per-chat watermark used
// as the `after` cursor for the next incremental fetch. 0 for an empty chat
// (matches the backend's `after=0` meaning "everything").
//
// NUMERIC ids only. A streaming placeholder carries a synthetic string
// id (`live-<speaker>-…`) until speaker_end swaps in the persisted row — and
// an ABORTED round means that swap never happens, so the string id stays in
// the list. `Math.max(n, 'live-…')` is NaN, and one NaN here poisoned every
// later fetch into `?after=NaN`: a silent 422, swallowed by both callers, that
// froze the open chat out of live updates until it was reopened. A string id
// is a local placeholder by construction, never a server watermark, so it has
// no business in this cursor.
export function highestId(messages) {
  return messages.reduce(
    (max, m) => (typeof m.id === 'number' ? Math.max(max, m.id) : max), 0)
}

// The `after` cursor for hydrating ONE known event (the id-ordering race).
// `highestId` alone is wrong for a deferred event: a deploy notice inserted
// mid-round gets its id BEFORE the round's replies persist, so by the time the
// post-round drain runs, the local list already holds ids above the notice's;
// "fetch after my highest" can never reach back down to it. That was the last
// leg of the live-events work: notices arrived, deferred correctly, then the
// drain's fetch skipped them forever (deploy notices land at round start, so a
// reply almost always overtakes them, which is why it looked intermittent for
// three weeks).
//
// The event carries its own id, so anchor on it: fetch after `min(eventId - 1,
// highestId)`. The min keeps the normal case incremental, and when the event
// sits BELOW the local high-water mark the refetched overlap is re-merged by
// id (idempotent), so nothing double-renders. Falls back to plain `highestId`
// for an event with no numeric id.
export function hydrateCursor(eventId, messages) {
  const high = highestId(messages)
  return typeof eventId === 'number' && eventId > 0
    ? Math.min(eventId - 1, high)
    : high
}

// Escalating reconnect backoff — the same shape as reattachRound's (App.jsx):
// start at 2s, +1.5s per attempt, capped at 8s. A pure function so the
// escalation/cap logic is testable without faking timers around a real
// network call.
export function nextBackoffDelay(prevDelayMs) {
  return Math.min(8000, prevDelayMs + 1500)
}

export const INITIAL_BACKOFF_MS = 2000

// Should the live-events connection even try fetching content for this
// event? Only when it's for the chat currently open AND we're not mid-round
// for it (the round's own SSE stream already delivers that content; fetching
// again here would just be redundant, not wrong, but the round stream is the
// authoritative live path while it's running).
export function shouldHydrateActiveChat(eventChatId, activeChatId, streaming) {
  return eventChatId != null && eventChatId === activeChatId && !streaming
}

// Should a live message for the open chat make the VOICE pipeline attach to the
// round producing it? Only when: it's the open chat, voice is on, and we're not
// already tailing a round (the round we started already feeds voice directly).
// This is what makes a Claude Code hand-back (a detached round no client
// started) audible: the client attaches to its SSE buffer and speaks it,
// instead of the narration arriving as a silent, text-only message. The caller
// still guards against re-attaching a round it already tailed (round id set).
export function shouldVoiceAttach(eventChatId, activeChatId, { streaming, voiceActive }) {
  return eventChatId != null && eventChatId === activeChatId
    && voiceActive === true && !streaming
}

// --- Deferred hydration queue ---------------------------------------------
// A live `new_message` for the OPEN chat that arrives WHILE a round is
// streaming is suppressed by shouldHydrateActiveChat AND shouldVoiceAttach
// (both carry `!streaming`) — the round's own SSE stream is the authoritative
// live path while it runs. The trap: the global watermark has ALREADY advanced
// past that id, so no SSE reconnect (`?since=<watermark>`) will ever replay it.
// Dropping it makes an out-of-band notice (e.g. deploy narration) permanently
// invisible until a full reload. We deliberately do NOT hold the watermark back
// (a single transient fetch failure would then replay every message after it —
// a broad replay storm). Instead we stash the suppressed events here and drain
// them the instant `streaming` goes false, hydrating them then.

// Is this the exact case both hydrate/voice paths suppress — the open chat
// while a round streams? Then it must be deferred, not dropped. Other-chat
// events (which just flag unread) and idle-time events are unaffected.
export function shouldDeferEvent(eventChatId, activeChatId, streaming) {
  return eventChatId != null && eventChatId === activeChatId && streaming === true
}

// Append an event to the pending queue, de-duplicated by id (an SSE reconnect
// can re-deliver the same frame). Returns a new array; the input is untouched.
export function queuePendingEvent(queue, event) {
  if (!event || event.id == null) return queue
  if (queue.some((e) => e.id === event.id)) return queue
  return [...queue, event]
}

// Drain the queue: return the stashed events to replay (ascending by id, so
// they hydrate in the order they were emitted) and the emptied queue. Pure —
// the caller re-runs each event through the normal hydrate/voice logic, which
// is now unsuppressed because streaming has gone false.
export function drainPendingQueue(queue) {
  const events = [...queue].sort((a, b) => (a.id || 0) - (b.id || 0))
  return { events, queue: [] }
}
