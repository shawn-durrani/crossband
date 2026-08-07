// Header identity: the "chat is a self-contained room" invariant.
//
// The chat header shows a title plus the running badge. Those, the sidebar
// selection, and the rendered transcript must all describe the SAME chat_id.
// The trap: `activeChat` (the fully-loaded chat object the header reads its
// title from) is fetched asynchronously after a switch, while `activeChatId`
// (what the sidebar highlight and running badge key off) flips synchronously.
// In the gap - or when two rapid switches let getChat responses land out of
// order - `activeChat` can lag behind `activeChatId`, so the header title
// describes a different chat than the selection/badge/body. That's the mobile
// title/header desync.
//
// Resolving the header's chat strictly by id fixes it: prefer the freshly
// loaded `activeChat` only when it actually matches `activeChatId`, otherwise
// fall back to the sidebar's copy of that same id (already correct and present
// app-wide). The header can then never show a title from a stale chat.
//
// Kept as a tiny pure function so the id-matching rule is unit-testable without
// a React/DOM harness (run: node --test frontend/src/headerState.test.js).

// Return the chat object the header should describe, or null when nothing is
// active. Membership is strict by id, the same no-loose-coercion discipline as
// the cross-chat write-guard (chat ids are numbers app-wide).
export function resolveHeaderChat(activeChat, activeChatId, chats) {
  if (activeChatId == null) return null
  if (activeChat && activeChat.id === activeChatId) return activeChat
  const fromList = Array.isArray(chats)
    ? chats.find((c) => c && c.id === activeChatId)
    : null
  return fromList || null
}

// The title to render in the header - always the one belonging to activeChatId,
// never a stale chat's. Empty string while the active chat is still loading and
// not yet in the sidebar list (a brief, neutral state - better than a wrong
// title).
export function resolveHeaderTitle(activeChat, activeChatId, chats) {
  return resolveHeaderChat(activeChat, activeChatId, chats)?.title ?? ''
}
