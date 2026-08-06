// Per-chat outbound text batching with pre-ingestion cancel.
//
// THE INVARIANT: the `queued → in-flight` transition is a SINGLE ATOMIC FLIP.
// A cancel issued in the same tick as the worker's flip must resolve
// deterministically — either cancel wins and NOTHING sends, or the send commits
// and cancel no-ops. There is never a partial send. (Same bug-class as the
// cross-chat write-guard: one boolean decides, and it's checked in one place.)
//
// This is text-only. Voice interrupt/barge-in semantics live in voice.js and are
// deliberately untouched.
//
// A batch is a plain immutable value moving through explicit states:
//   empty → queued → in-flight → accepted
//   queued → cancelled   (only before the flip)
//   in-flight → failed   (retryable; the network never took it)
// Every transition returns a NEW batch object; callers swap it into their store.
// Kept dependency-free so the atomic-flip invariant is unit-testable without a
// React/DOM harness (run: node --test frontend/src/textQueue.test.js).

export const BATCH_STATES = ['empty', 'queued', 'in-flight', 'accepted', 'cancelled', 'failed']

// A fresh, empty batch. `attachmentIds` ride along with the text fragments so a
// message queued mid-round keeps its attachments when the batch finally drains.
export function createBatch() {
  return { status: 'empty', fragments: [], attachmentIds: [] }
}

// True once the batch has been handed toward the backend — past this point it is
// immutable and normal chat semantics take over (cancel becomes a no-op).
export function isCommitted(batch) {
  return batch.status === 'in-flight' || batch.status === 'accepted'
}

// A batch the user can still act on: it holds unsent text and hasn't flipped.
export function isPending(batch) {
  return batch.status === 'queued'
}

// Number of user fragments waiting in the batch (for the "N queued" pill).
export function pendingCount(batch) {
  return isCommitted(batch) ? 0 : batch.fragments.length
}

// Append one typed message. Refused once the batch is committed — a fragment
// added after the flip would be a partial send, which the invariant forbids.
export function addFragment(batch, text, ts, attachmentIds = []) {
  if (isCommitted(batch)) return batch
  const trimmed = (text || '').trim()
  if (!trimmed && attachmentIds.length === 0) return batch
  return {
    status: 'queued',
    fragments: [...batch.fragments, { text: trimmed, ts: ts ?? Date.now() }],
    attachmentIds: [...batch.attachmentIds, ...attachmentIds],
  }
}

// Cancel the pending batch. Allowed ONLY before the flip; on a committed batch
// this is a clean no-op (returns the same batch untouched), which is exactly the
// "cancel after ingestion cleanly no-ops" acceptance criterion.
export function cancelBatch(batch) {
  if (isCommitted(batch)) return batch
  return createBatch()
}

// THE ATOMIC FLIP. Returns { batch, committed }. Only a `queued` batch commits;
// a batch that was cancelled (or never had fragments) yields committed:false and
// the caller sends nothing. Because JS runs this to completion on one thread,
// whichever of cancelBatch/flipBatch ran first in a shared tick has already
// rewritten `status`, so the loser sees a non-queued batch and does nothing —
// never a partial send.
export function flipBatch(batch) {
  if (batch.status !== 'queued') return { batch, committed: false }
  return { batch: { ...batch, status: 'in-flight' }, committed: true }
}

// The single user turn the model sees: fragments concatenated in order. The
// original fragments + timestamps stay on the batch object as metadata for
// replay/debug even though the model only ever sees this joined string.
export function combineFragments(batch) {
  return batch.fragments.map((f) => f.text).filter(Boolean).join('\n\n')
}
