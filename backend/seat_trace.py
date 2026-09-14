"""A content-free ledger of what each model seat's completion did (#162).

The field failure: a local seat repeated an earlier reply verbatim across
several new turns, and nothing in the app could say whether the seat was
generating at all, whether the client had sent twice, or whether a
reconnect had replayed old output. This ledger answers those questions
after the fact without holding a word of what anyone said.

One entry per completion attempt: which chat, round and seat, when the
upstream call started, how long the first token took, how long the
whole reply took, how many chunks and characters arrived, the finish
reason the server gave, how it ended (ok, error, stalled, cancelled) and
the exception class if it failed, and a short hash of the reply text. A
reply whose hash matches an earlier completed reply by the same seat in
the same chat is marked as a repeat of that round, and one warning line
says so. A second send of the same text within a few seconds of the
first is recorded as a duplicate send, since a doubled submission is one
way a seat appears to answer twice.

Bounded in memory, never persisted, reset on restart. Read back through
`GET /api/models/seat_trace` and folded into the voice diagnostics dump.
"""

import hashlib
import logging
import threading
import time
from collections import deque
from urllib.parse import urlparse

log = logging.getLogger("crossband.seat_trace")

MAX_ENTRIES = 400
DUPLICATE_SEND_WINDOW_S = 10.0
# A short reply repeated is a coincidence ("Yes.", "[pass]"); a long one is
# the failure shape. Repeats are only judged on replies at least this long.
REPEAT_MIN_CHARS = 40

_lock = threading.Lock()
_entries: deque = deque(maxlen=MAX_ENTRIES)
_last_send: dict = {}   # chat_id -> (text hash, sent at)


def text_hash(text) -> str:
    return hashlib.sha256((text or "").encode("utf-8")).hexdigest()[:12]


def _upstream(participant) -> str:
    """Where the seat's requests go: the base_url's host and port for a
    self-hosted seat, else the provider name. Never a key, never a path."""
    base = (participant.get("base_url") or "").strip()
    if base:
        u = urlparse(base if "://" in base else f"http://{base}")
        return u.netloc or base
    return participant.get("provider") or "?"


def begin(chat_id, round_id, participant) -> dict:
    entry = {
        "kind": "completion",
        "chat_id": chat_id,
        "round_id": round_id,
        "seat": participant.get("slug") or participant.get("name"),
        "model": participant.get("model") or "",
        "upstream": _upstream(participant),
        "started_at": time.time(),
        "first_token_ms": None,
        "duration_ms": None,
        "chunks": 0,
        "chars": 0,
        "tool_calls": 0,
        "finish": None,
        "outcome": "streaming",
        "error": "",
        "text_sha": None,
        "repeat_of": None,
    }
    with _lock:
        _entries.append(entry)
    return entry


def text(entry: dict, delta: str) -> None:
    if entry["first_token_ms"] is None:
        entry["first_token_ms"] = int((time.time() - entry["started_at"]) * 1000)
    entry["chunks"] += 1
    entry["chars"] += len(delta or "")


def tool(entry: dict) -> None:
    entry["tool_calls"] += 1


def finish_reason(entry: dict, reason) -> None:
    if reason:
        entry["finish"] = str(reason)[:32]


def finish(entry: dict, reply_text: str, outcome: str, error: str = "") -> None:
    """Close the entry. `outcome` is ok, error, stalled or cancelled; `error`
    is an exception class name, never its message. A completed reply is
    checked against the seat's earlier completed replies in the same chat."""
    entry["duration_ms"] = int((time.time() - entry["started_at"]) * 1000)
    entry["outcome"] = outcome
    entry["error"] = (error or "")[:64]
    if reply_text:
        entry["text_sha"] = text_hash(reply_text)
    if outcome == "ok" and reply_text and len(reply_text) >= REPEAT_MIN_CHARS:
        with _lock:
            earlier = [e for e in _entries
                       if e is not entry and e["kind"] == "completion"
                       and e["chat_id"] == entry["chat_id"]
                       and e["seat"] == entry["seat"]
                       and e["outcome"] == "ok"
                       and e["text_sha"] == entry["text_sha"]]
        if earlier:
            entry["repeat_of"] = earlier[-1]["round_id"]
            log.warning("seat_trace repeat: seat=%s chat=%s round=%s repeats "
                        "round=%s chars=%d upstream=%s", entry["seat"],
                        entry["chat_id"], entry["round_id"],
                        entry["repeat_of"], entry["chars"], entry["upstream"])


def note_send(chat_id, text_in: str) -> dict | None:
    """Record a user send by hash. A second send of the same text inside
    DUPLICATE_SEND_WINDOW_S is written down as a duplicate send and
    returned; nothing is blocked, the round runs as before."""
    h = text_hash(text_in)
    now = time.time()
    with _lock:
        prev = _last_send.get(chat_id)
        _last_send[chat_id] = (h, now)
        if prev and prev[0] == h and now - prev[1] <= DUPLICATE_SEND_WINDOW_S:
            entry = {"kind": "duplicate_send", "chat_id": chat_id,
                     "started_at": now,
                     "after_ms": int((now - prev[1]) * 1000)}
            _entries.append(entry)
            log.warning("seat_trace duplicate send: chat=%s after_ms=%d",
                        chat_id, entry["after_ms"])
            return entry
    return None


def entries(chat_id=None, limit: int = 100) -> list:
    """Newest last. Copies, so a reader never sees a half-updated entry."""
    with _lock:
        rows = [dict(e) for e in _entries
                if chat_id is None or e["chat_id"] == chat_id]
    return rows[-max(1, int(limit)):]


def _reset_for_tests() -> None:
    with _lock:
        _entries.clear()
        _last_send.clear()
