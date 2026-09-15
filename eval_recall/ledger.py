"""Reads the user turns a replay re-asks memory about, straight from
crossband's own chat.db, read only.

A user turn is what the engine keys the ambient recall on: the text of the
latest user message, trimmed to QUERY_CHARS. A slash command runs no round,
so it never recalls and is skipped here; a chat with memory switched off
never recalls either, so its turns are skipped unless asked for."""

from dataclasses import dataclass
import sqlite3

QUERY_CHARS = 500  # engine.py trims the ambient query to this


@dataclass(frozen=True)
class Turn:
    chat_id: int
    message_id: int
    created_at: float
    query: str


def user_turns(db_path, chat_ids=None, max_turns: int = 0,
               include_memory_off: bool = False) -> list[Turn]:
    """Newest turns first while reading, returned oldest first so the report
    reads the way the chats were lived. max_turns=0 means every turn."""
    con = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    con.row_factory = sqlite3.Row
    try:
        sql = ("SELECT m.id, m.chat_id, m.created_at, m.content FROM messages m "
               "JOIN chats c ON c.id = m.chat_id WHERE m.speaker = 'user'")
        args: list = []
        if not include_memory_off:
            sql += " AND c.memory_enabled = 1"
        if chat_ids:
            sql += " AND m.chat_id IN (%s)" % ",".join("?" * len(chat_ids))
            args += [int(c) for c in chat_ids]
        sql += " ORDER BY m.id DESC"
        turns: list[Turn] = []
        for r in con.execute(sql, args):
            q = (r["content"] or "").strip()
            if not q or q.startswith("/"):
                continue
            turns.append(Turn(int(r["chat_id"]), int(r["id"]),
                              float(r["created_at"]), q[:QUERY_CHARS]))
            if max_turns and len(turns) >= max_turns:
                break
    finally:
        con.close()
    turns.reverse()
    return turns
