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
the exception class if it failed, and two short hashes of the reply
text, one as sent and one normalised. A second send of the same text
within a few seconds of the first is recorded as a duplicate send, since
a doubled submission is one way a seat appears to answer twice.

Two flags mark a completed reply that says again what the chat already
holds. Neither changes anything: the reply is posted, spoken and sent to
memory as before (owner decision, 25 Sep).

- `repeat_of`: the same seat gave this exact reply earlier in the chat.
  It names that round.
- `copy_of`: this reply matches one of the last COPY_WINDOW replies in
  the chat from any seat, exactly or after `normalised`. It names that
  reply's seat, round and ledger entry, and how many replies back it
  was. The 25 Sep reproduction found local seats copying another seat's
  latest reply, which `repeat_of` never sees.

The echo guard's verdict on a completion lands on the same entry
(`echo_hit`), so a voice restatement the guard could only log names the
same seat and round the ledger does.

Bounded in memory, never persisted, reset on restart. Read back through
`GET /api/models/seat_trace` and folded into the voice diagnostics dump.
"""

import hashlib
import itertools
import logging
import re
import threading
import time
from collections import deque
from urllib.parse import urlparse

log = logging.getLogger("crossband.seat_trace")

MAX_ENTRIES = 400
DUPLICATE_SEND_WINDOW_S = 10.0
# A short reply repeated is a coincidence ("Yes.", "[pass]"); a long one is
# the failure shape. Repeats and copies are only judged on replies at least
# this long once normalised.
REPEAT_MIN_CHARS = 40
# How many of the chat's latest completed replies a copy is judged against.
# A seat can only copy what it can see, and every seat always sees at least
# the chat's latest keep_recent_messages (12 by default) word for word, so
# 12 replies reach back past all of that: four rounds of a three-seat room.
# Every copy in the 25 Sep reproduction was of the reply just before. Past
# the window, a match is more likely a stock answer than a copy, and the
# same-seat flag still covers a seat repeating itself from further back.
COPY_WINDOW = 12
# Echo guard actions that keep a completion out of the chat. A reply the
# chat never showed can't be the source of a later copy.
ECHO_DROPPED = ("retry", "suppressed")

# A model sometimes opens with the transcript's own attribution head,
# "[Name · time]:", copied from the lines it reads. One or more of those at
# the very start is set aside before two replies are compared.
_LEADING_LABEL_RE = re.compile(r"^(?:\s*\[[^\[\]\n]{1,120}\]\s*:)+")

_lock = threading.Lock()
_entries: deque = deque(maxlen=MAX_ENTRIES)
_last_send: dict = {}   # chat_id -> (text hash, sent at)
_seq = itertools.count(1)


def text_hash(text) -> str:
    return hashlib.sha256((text or "").encode("utf-8")).hexdigest()[:12]


def normalised(text) -> str:
    """The reply as a copy check compares it: any leading "[Name · time]:"
    head removed, runs of whitespace collapsed to one space, case folded.
    Wording and punctuation stay, so only a near-exact copy matches."""
    body = _LEADING_LABEL_RE.sub("", text or "", count=1)
    return " ".join(body.split()).casefold()


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
        "seq": None,
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
        "norm_sha": None,
        "repeat_of": None,
        "copy_of": None,
        "echo_guard": None,
    }
    with _lock:
        entry["seq"] = next(_seq)
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


def _earlier_replies(entry: dict) -> list:
    """The chat's completed replies that began before `entry`, oldest first."""
    rows = []
    with _lock:
        for e in _entries:
            if e is entry:
                break
            if (e["kind"] == "completion" and e["chat_id"] == entry["chat_id"]
                    and e["outcome"] == "ok" and e["text_sha"]):
                rows.append(e)
    return rows


def _shown(e: dict) -> bool:
    return (e.get("echo_guard") or {}).get("action") not in ECHO_DROPPED


def finish(entry: dict, reply_text: str, outcome: str, error: str = "") -> None:
    """Close the entry. `outcome` is ok, error, stalled or cancelled; `error`
    is an exception class name, never its message. A completed reply is
    checked against the seat's own earlier replies (`repeat_of`) and the
    chat's latest replies from any seat (`copy_of`). Flags only: nothing
    here changes the reply or the round."""
    entry["duration_ms"] = int((time.time() - entry["started_at"]) * 1000)
    entry["outcome"] = outcome
    entry["error"] = (error or "")[:64]
    if not reply_text:
        return
    norm = normalised(reply_text)
    entry["text_sha"] = text_hash(reply_text)
    entry["norm_sha"] = text_hash(norm)
    if outcome != "ok" or len(norm) < REPEAT_MIN_CHARS:
        return
    earlier = _earlier_replies(entry)
    own = [e for e in earlier if e["seat"] == entry["seat"]
           and e["text_sha"] == entry["text_sha"]]
    if own:
        entry["repeat_of"] = own[-1]["round_id"]
        log.warning("seat_trace repeat: seat=%s chat=%s round=%s repeats "
                    "round=%s chars=%d upstream=%s", entry["seat"],
                    entry["chat_id"], entry["round_id"],
                    entry["repeat_of"], entry["chars"], entry["upstream"])
    shown = [e for e in earlier if _shown(e)][-COPY_WINDOW:]
    for back, e in enumerate(reversed(shown), start=1):
        if e["norm_sha"] != entry["norm_sha"]:
            continue
        entry["copy_of"] = {
            "seat": e["seat"], "round_id": e["round_id"], "seq": e["seq"],
            "back": back,
            "match": "exact" if e["text_sha"] == entry["text_sha"]
            else "normalised",
        }
        break
    copy = entry["copy_of"]
    if copy and not (own and own[-1]["seq"] == copy["seq"]):
        # One line per finding: a seat's exact repeat of its own latest
        # reply already has the repeat line above.
        log.warning("seat_trace copy: seat=%s chat=%s round=%s seq=%s copies "
                    "seat=%s round=%s seq=%s back=%d match=%s chars=%d "
                    "upstream=%s", entry["seat"], entry["chat_id"],
                    entry["round_id"], entry["seq"], copy["seat"],
                    copy["round_id"], copy["seq"], copy["back"],
                    copy["match"], entry["chars"], entry["upstream"])


def echo_hit(entry: dict, action: str, ref: str, seat=None,
             source_text: str = "") -> dict:
    """Write the echo guard's verdict on a completion into its entry and
    return it. `action` is what the guard did (logged, retry, suppressed)
    and `ref` is "own" or "round". `seat` and `source_text` are the
    message the reply restated. Its text is only hashed, to find the ledger
    entry that produced it and name that entry's round the way `copy_of`
    does. The round and entry stay None when the ledger no longer holds it,
    after a restart or for a reply that was cut off."""
    source = None
    if seat is not None and source_text:
        sha = text_hash(source_text)
        with _lock:
            for e in reversed(_entries):
                if (e is not entry and e["kind"] == "completion"
                        and e["chat_id"] == entry["chat_id"]
                        and e["seat"] == seat and e["text_sha"] == sha):
                    source = e
                    break
    record = {"action": action, "ref": ref, "seat": seat,
              "round_id": source["round_id"] if source else None,
              "seq": source["seq"] if source else None}
    entry["echo_guard"] = record
    return record


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
            entry = {"kind": "duplicate_send", "seq": next(_seq),
                     "chat_id": chat_id, "started_at": now,
                     "after_ms": int((now - prev[1]) * 1000)}
            _entries.append(entry)
            log.warning("seat_trace duplicate send: chat=%s after_ms=%d",
                        chat_id, entry["after_ms"])
            return entry
    return None


def entries(chat_id=None, limit: int = 100) -> list:
    """Newest last. Copies, so a reader never sees a half-updated entry."""
    with _lock:
        rows = [{k: dict(v) if isinstance(v, dict) else v
                 for k, v in e.items()}
                for e in _entries
                if chat_id is None or e["chat_id"] == chat_id]
    return rows[-max(1, int(limit)):]


def _reset_for_tests() -> None:
    global _seq
    with _lock:
        _entries.clear()
        _last_send.clear()
        _seq = itertools.count(1)
