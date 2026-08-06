// Per-chat "task running" state.
//
// A chat is "running" when it has a round/agent generating in the background:
// model inference, tool calls, or a Claude Code turn. Crossband is multi-chat and
// rounds are DETACHED (they keep going after you navigate away), so this can't be
// a single active-view flag — a background chat must be able to advertise that
// it's busy while you're looking at another one.
//
// Two sources feed the running set, merged here:
//   1. The backend's `running_chat_ids` (source of truth; survives navigation
//      and clears reliably when a detached round actually finishes).
//   2. An optimistic local id for the chat whose round WE just started, so the
//      indicator lights up immediately instead of waiting for the next poll.
//
// Kept as tiny pure functions so the merge + membership rules are unit-testable
// without a React/DOM harness (run: node --test frontend/src/runningState.test.js).

// Build the running set from the server list plus an optional optimistic id.
// Ids are kept as-is (chat ids are numbers app-wide); membership is strict, the
// same no-loose-coercion discipline as the cross-chat write-guard.
export function computeRunningChats(serverIds, optimisticId = null) {
  const set = new Set(Array.isArray(serverIds) ? serverIds : [])
  if (optimisticId != null) set.add(optimisticId)
  return set
}

// Is this chat currently running a task? Safe on a null/absent set.
export function isChatRunning(runningChats, chatId) {
  return chatId != null && !!runningChats && runningChats.has(chatId)
}

// Should we keep polling /api/state? Only while something is still running —
// this is what lets a background chat's indicator clear without the user
// touching it, and stops the polling entirely once everything is idle.
export function shouldPollRunning(runningChats) {
  return !!runningChats && runningChats.size > 0
}
