// Pure-function tests for the global live-events stream.
// Run: node --test frontend/src/eventStream.test.js
import { test } from 'node:test'
import assert from 'node:assert/strict'
import {
  mergeMessagesById, highestId, hydrateCursor, nextBackoffDelay, INITIAL_BACKOFF_MS,
  shouldHydrateActiveChat, shouldVoiceAttach,
  shouldDeferEvent, queuePendingEvent, drainPendingQueue,
} from './eventStream.js'

test('merge appends fresh rows in id order', () => {
  const current = [{ id: 1, content: 'a' }, { id: 2, content: 'b' }]
  const fresh = [{ id: 3, content: 'c' }]
  assert.deepEqual(mergeMessagesById(current, fresh).map((m) => m.id), [1, 2, 3])
})

test('merge de-duplicates an id that arrives twice', () => {
  // Same message delivered via both the round's own SSE stream AND the
  // global events stream's hydration fetch — must never double-append.
  const current = [{ id: 1, content: 'a' }]
  const fresh = [{ id: 1, content: 'a' }, { id: 2, content: 'b' }]
  const merged = mergeMessagesById(current, fresh)
  assert.deepEqual(merged.map((m) => m.id), [1, 2])
})

test('merge with no fresh rows returns the same list unchanged', () => {
  const current = [{ id: 1 }]
  assert.equal(mergeMessagesById(current, []), current)
  assert.equal(mergeMessagesById(current, null), current)
})

test('merge sorts even if fresh rows arrive out of order', () => {
  const current = [{ id: 5 }]
  const fresh = [{ id: 7 }, { id: 6 }]
  assert.deepEqual(mergeMessagesById(current, fresh).map((m) => m.id), [5, 6, 7])
})

test('highestId finds the max, 0 on empty', () => {
  assert.equal(highestId([]), 0)
  assert.equal(highestId([{ id: 3 }, { id: 9 }, { id: 1 }]), 9)
})

test('hydrateCursor reaches BELOW the high-water mark for a deferred event', () => {
  // The three-week intermittent: a deploy notice inserted mid-round gets id
  // 122; the round's replies persist AFTER it as 123+. By drain time the local
  // list holds [.., 121, 123], so "after my highest" (123) can never fetch 122.
  // Anchoring on the event's own id must reach back down.
  const local = [{ id: 120 }, { id: 121 }, { id: 123 }]
  const cursor = hydrateCursor(122, local)
  assert.ok(cursor < 122, 'cursor must sit below the deferred event id')
  assert.equal(cursor, 121)
})

test('hydrateCursor stays incremental in the normal case', () => {
  // An event above the high-water mark keeps the old behaviour exactly:
  // fetch after what we have, not after ev.id-1 (there may be OTHER unseen
  // messages between the two).
  assert.equal(hydrateCursor(130, [{ id: 120 }, { id: 123 }]), 123)
  // Empty chat: everything.
  assert.equal(hydrateCursor(7, []), 0)
})

test('hydrateCursor falls back to highestId without a numeric event id', () => {
  assert.equal(hydrateCursor(undefined, [{ id: 4 }, { id: 9 }]), 9)
  assert.equal(hydrateCursor('live-x', [{ id: 9 }]), 9)
  // and never trusts a string placeholder id in the list either
  assert.equal(hydrateCursor(undefined, [{ id: 9 }, { id: 'live-a' }]), 9)
})

test('highestId ignores string placeholder ids', () => {
  // The post-abort shape: real persisted rows plus the streaming placeholder
  // whose speaker_end never came, so its synthetic `live-…` id is still in the
  // list. One string id used to NaN the whole cursor — every later fetch
  // became `?after=NaN`, a silent 422, and the open chat froze out of live
  // updates until reopened. The cursor must come from numeric ids alone.
  const afterAbort = [
    { id: 41 }, { id: 42 },
    { id: 'live-claude-1234567890-ab1cd', streaming: false },
  ]
  assert.equal(highestId(afterAbort), 42)
  assert.ok(Number.isFinite(highestId(afterAbort)), 'cursor must be usable')
  // A list that is ONLY a placeholder (brand-new chat, first reply aborted
  // pre-persist) is an empty-chat cursor, not NaN.
  assert.equal(highestId([{ id: 'live-gpt-1-x' }]), 0)
})

test('backoff escalates by 1.5s per attempt, capped at 8s', () => {
  let d = INITIAL_BACKOFF_MS
  assert.equal(d, 2000)
  d = nextBackoffDelay(d); assert.equal(d, 3500)
  d = nextBackoffDelay(d); assert.equal(d, 5000)
  d = nextBackoffDelay(d); assert.equal(d, 6500)
  d = nextBackoffDelay(d); assert.equal(d, 8000)
  d = nextBackoffDelay(d); assert.equal(d, 8000) // capped, never exceeds 8s
})

test('active-chat hydration only when the event matches AND no round is streaming', () => {
  assert.equal(shouldHydrateActiveChat(5, 5, false), true)
  assert.equal(shouldHydrateActiveChat(5, 7, false), false) // different chat → dirty-mark, not fetch
  assert.equal(shouldHydrateActiveChat(5, 5, true), false)  // round's own stream owns delivery
  assert.equal(shouldHydrateActiveChat(null, 5, false), false)
})

test('voice-attach fires only for the open chat, in voice, while idle', () => {
  // A hand-back round we didn't start → attach and speak it.
  assert.equal(shouldVoiceAttach(5, 5, { streaming: false, voiceActive: true }), true)
  // Text mode: no audio pipeline to feed — normal hydration handles it.
  assert.equal(shouldVoiceAttach(5, 5, { streaming: false, voiceActive: false }), false)
  // Already tailing a round: it already feeds voice; don't double-attach.
  assert.equal(shouldVoiceAttach(5, 5, { streaming: true, voiceActive: true }), false)
  // A different chat's activity never pulls audio into the open one.
  assert.equal(shouldVoiceAttach(5, 7, { streaming: false, voiceActive: true }), false)
  assert.equal(shouldVoiceAttach(null, 5, { streaming: false, voiceActive: true }), false)
})

// --- Deferred hydration queue ---------------------------------------------
// The bug, which came back after a first fix: a new_message for the open chat
// arriving mid-round was suppressed (hydrate/voice both carry !streaming)
// AFTER the watermark advanced past it, so nothing ever replayed it and it
// vanished until a full reload.

test('an event for the open chat while streaming must be deferred, not acted on now', () => {
  // Exactly the suppressed case: open chat + a round is streaming.
  assert.equal(shouldDeferEvent(5, 5, true), true)
  // Not streaming → hydrate immediately, nothing to defer.
  assert.equal(shouldDeferEvent(5, 5, false), false)
  // Another chat streaming/elsewhere → it just flags unread, never deferred.
  assert.equal(shouldDeferEvent(5, 7, true), false)
  assert.equal(shouldDeferEvent(null, 5, true), false)
})

test('the defer condition is the exact complement of active-chat hydration', () => {
  // If we deferred it, hydration would have been suppressed — and vice versa.
  // This is the invariant that guarantees a deferred event is never also acted
  // on now (no double-render) and a hydrated one is never also queued (no loss).
  for (const streaming of [true, false]) {
    const deferred = shouldDeferEvent(5, 5, streaming)
    const hydrated = shouldHydrateActiveChat(5, 5, streaming)
    assert.equal(deferred && hydrated, false)
    assert.equal(deferred || hydrated, true) // open-chat event: one path always owns it
  }
})

test('queue stashes an event and de-duplicates a reconnect re-delivery', () => {
  let q = []
  q = queuePendingEvent(q, { type: 'new_message', chat_id: 111, id: 301 })
  q = queuePendingEvent(q, { type: 'new_message', chat_id: 111, id: 302 })
  q = queuePendingEvent(q, { type: 'new_message', chat_id: 111, id: 301 }) // SSE reconnect replays it
  assert.deepEqual(q.map((e) => e.id), [301, 302])
})

test('queue ignores an event with no id and never mutates its input', () => {
  const q0 = [{ id: 1 }]
  assert.equal(queuePendingEvent(q0, { chat_id: 111 }), q0) // no id → unchanged
  assert.equal(queuePendingEvent(q0, null), q0)
  const q1 = queuePendingEvent(q0, { id: 2 })
  assert.deepEqual(q0.map((e) => e.id), [1]) // original untouched (pure)
  assert.deepEqual(q1.map((e) => e.id), [1, 2])
})

test('drain returns queued events once, in id order, and empties the queue', () => {
  // The acceptance criterion: events queued mid-round drain exactly once when
  // streaming ends, in emission order, and the cursor holds nothing stale.
  const q = [
    { chat_id: 111, id: 303 },
    { chat_id: 111, id: 301 },
    { chat_id: 111, id: 302 },
  ]
  const { events, queue } = drainPendingQueue(q)
  assert.deepEqual(events.map((e) => e.id), [301, 302, 303])
  assert.deepEqual(queue, []) // drained once — a second drain yields nothing
  assert.deepEqual(drainPendingQueue(queue).events, [])
})

test('end-to-end: an event deferred mid-round is drained exactly once after it ends', () => {
  // Simulates handleLiveEvent → runStream-finally drain without a DOM.
  let queue = []
  const hydrated = []
  const onEvent = (ev, streaming) => {
    if (shouldDeferEvent(ev.chat_id, 111, streaming)) {
      queue = queuePendingEvent(queue, ev)
    } else if (shouldHydrateActiveChat(ev.chat_id, 111, streaming)) {
      hydrated.push(ev.id)
    }
  }
  // A deploy notice arrives while a round streams → deferred, not hydrated yet.
  onEvent({ chat_id: 111, id: 301 }, /* streaming */ true)
  assert.deepEqual(hydrated, [])
  assert.deepEqual(queue.map((e) => e.id), [301])
  // Round ends → drain replays it through the now-unsuppressed hydrate path.
  const drained = drainPendingQueue(queue)
  queue = drained.queue
  for (const ev of drained.events) onEvent(ev, /* streaming */ false)
  assert.deepEqual(hydrated, [301]) // rendered exactly once, no reload needed
  assert.deepEqual(queue, [])
})
