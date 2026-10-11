"""How much of a spoken reply was heard before it was cut off (#634).

When someone cuts a spoken reply off, the browser knows from the word
timings how far into it they'd heard, and says so with the stop: how many
words of the reply's spoken text played, and the last few of them. This
module finds that point in the saved reply and records it with
db.set_message_heard. Readers then see only the heard part and its cut-off
note (db.heard_view), so the models and memory never treat unheard words
as said. The saved content itself stays as the seat wrote it.

The spoken text and the saved text aren't quite the same: speech leaves out
things like link addresses. So the point is found by the last words heard,
nearest the word count, and by the count alone when those words aren't
found.
"""

import re

from . import db

# A word, as the browser counts them: letters and digits, with an
# apostrophe inside allowed.
WORD = re.compile(r"[^\W_]+(?:['’][^\W_]+)*")

# The note a cut reply ends with. The engine adds it to a reply still being
# written when it's cut, and this module adds it to one already saved.
_MARKER = re.compile(r"\s*\[cut off by [^\]\n]{1,80}\]\s*$")

# A reply with a written part after its spoken part (#80) keeps all of it.
WRITTEN_TOKEN = "[written]"

# At most this many replies are trimmed by one stop, and these limits keep
# what the browser sends small.
MAX_ENTRIES = 8
MAX_TAIL_WORDS = 8


def cut_marker(cfg):
    return f"[cut off by {cfg.get('user_name') or 'User'}]"


def strip_cut_marker(content):
    """The reply without the cut-off note on its end, if it has one."""
    return _MARKER.sub("", content or "")


def _norm(word):
    return word.lower().replace("’", "'")


def heard_offset(body, words, tail):
    """Characters of `body` heard, given `words` words heard whose last few
    are `tail`. None when it was all heard."""
    tokens = [(m.end(), _norm(m.group())) for m in WORD.finditer(body)]
    if words >= len(tokens):
        return None
    tail = [_norm(w) for w in tail or [] if WORD.fullmatch(w or "")][-MAX_TAIL_WORDS:]
    n = max(0, int(words))
    if tail:
        k = len(tail)
        ends = [i + k for i in range(len(tokens) - k + 1)
                if [t[1] for t in tokens[i:i + k]] == tail]
        near = [e for e in ends if abs(e - n) <= max(10, n // 2)]
        if near:
            n = min(near, key=lambda e: (abs(e - n), -e))
    if n >= len(tokens):
        return None
    if n == 0:
        return 0
    end = tokens[n - 1][0]
    while end < len(body) and body[end] in ".,;:!?…)\"'”’":
        end += 1
    return end


def _target(con, chat_id, entry):
    """The saved reply an entry is about: the one it names, or else the
    seat's newest reply since the last user turn, which is the one the stop
    just saved."""
    seat = entry.get("seat") or ""
    if entry.get("message_id"):
        row = con.execute(
            "SELECT id, speaker, content, heard FROM messages "
            "WHERE id=? AND chat_id=?", (entry["message_id"], chat_id)).fetchone()
        return row if row and row["speaker"] == seat else None
    last_user = con.execute(
        "SELECT COALESCE(MAX(id), 0) FROM messages WHERE chat_id=? AND speaker='user'",
        (chat_id,)).fetchone()[0]
    return con.execute(
        "SELECT id, speaker, content, heard FROM messages "
        "WHERE chat_id=? AND speaker=? AND id>? ORDER BY id DESC LIMIT 1",
        (chat_id, seat, last_user)).fetchone()


def apply(chat_id, entries, cfg):
    """Record how much of each cut reply was heard. Returns how many were
    recorded. A reply heard in full, one already recorded, one with a
    written part, and anything the entry can't be matched to are left
    alone."""
    done = 0
    note = cut_marker(cfg)
    con = db.connect()
    try:
        for entry in list(entries or [])[:MAX_ENTRIES]:
            seat = entry.get("seat") or ""
            if not seat or seat in ("user", "system"):
                continue
            row = _target(con, chat_id, entry)
            if not row or row["heard"]:
                continue
            body = strip_cut_marker(row["content"])
            if WRITTEN_TOKEN in body:
                continue
            upto = heard_offset(body, int(entry.get("words") or 0), entry.get("tail") or [])
            if upto is None:
                continue
            db.set_message_heard(con, row["id"], upto, note)
            done += 1
    finally:
        con.close()
    return done
