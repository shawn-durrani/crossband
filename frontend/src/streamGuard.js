// Cross-chat write-guard.
//
// THE INVARIANT: an SSE event must never write into the message state of a chat
// it did not originate from. Crossband is multi-chat, and a round keeps running
// server-side after you navigate away (detached rounds), so its reader can still
// dispatch already-parsed events in the microtask AFTER we abort the stream.
// Aborting the reader is hygiene; THIS comparison is the actual seatbelt.
//
// Every stream captures the chat_id it was opened for (runStream's `chatId`).
// Before applying any event to React state we compare that captured id against
// the currently-active chat. If they don't match, the event belongs to another
// room and is dropped. Kept as a tiny pure function so the invariant is unit-
// testable without a React/DOM harness.
export function eventBelongsToActiveChat(streamChatId, activeChatId) {
  return streamChatId != null && streamChatId === activeChatId
}
