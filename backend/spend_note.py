"""The running-cost line for an escalated chat (#259).

A spoken depth change tells the chat replies will be slower and never that
they cost more, and anyone in the room may raise a seat. So while a seat
sits above its configured depth, the chat gets one short system line now
and then saying what that seat has spent since it was raised. Watermark
gated like the rolling summary and the auto title: it fires on a message
count, never on a clock, and `spend_note_every` sets the count.

What the line says, and what it may not: a per-seat figure, because depth
is per seat and one chat-wide line is wrong in a mixed room; the metered
figure alone as cash, with subscription-covered and unpriced use named
apart and never summed; and "a rate-card estimate", because nothing here
is a bill. The line is a system row, so it persists and every seat reads
it, and it never reaches membro: is_spend_note() is the ingest filter."""

import time

from . import accounting, db
from .depth import LEVEL_WORDS

SPEND_NOTE_HEAD = "Running cost:"


def is_spend_note(msg) -> bool:
    """True for a row this module wrote. The ingest path skips these so a
    dollar figure is never mined as a fact about anyone."""
    return (msg.get("speaker") == "system"
            and (msg.get("content") or "").startswith(SPEND_NOTE_HEAD))


def _money(x: float) -> str:
    return f"${x:.2f}"


def _since(ts: float) -> str:
    return time.strftime("%H:%M", time.localtime(ts)) if ts else "it was set"


def seat_line(name, effort, set_at, group) -> str:
    """One seat's clause: how it is set, since when, and what it has used."""
    word = LEVEL_WORDS.get(effort, effort)
    metered = float((group or {}).get(accounting.CAT_METERED, 0.0))
    subs = float((group or {}).get(accounting.CAT_SUBSCRIPTION, 0.0))
    unknown = float((group or {}).get(accounting.CAT_UNKNOWN, 0.0))
    parts = [f"{_money(metered)} metered"]
    if subs:
        parts.append("subscription-covered use apart")
    if unknown:
        parts.append("some unpriced use apart")
    return (f"{name} at {word} thinking since {_since(set_at)}, "
            + ", ".join(parts) + " since then")


def compose(seats: list) -> str:
    """seats: [(name, effort, set_at, group)]. One line, however many seats."""
    clauses = "; ".join(seat_line(*s) for s in seats)
    return (f"{SPEND_NOTE_HEAD} {clauses}. Rate-card estimates, not a bill. "
            "Anyone can say back to normal.")


def maybe_spend_note(con, chat, messages, cfg) -> bool:
    """Post the line when it is due. Due means: the knob is on, at least one
    seat is raised, and `spend_note_every` messages have landed since the
    later of the last line and the earliest raise. Returns True when a line
    was written."""
    every = int(cfg.get("spend_note_every") or 0)
    if every <= 0 or not messages:
        return False
    chat_id = chat["id"]
    raised = db.get_chat_seat_escalations(con, chat_id)
    if not raised:
        return False
    upto = int(chat.get("spend_note_upto") or 0)
    earliest = min((r["set_at"] for r in raised), default=0.0)
    fresh = [m for m in messages
             if m["id"] > upto and (m.get("created_at") or 0) >= earliest]
    if len(fresh) < every:
        return False
    names = {p["slug"]: (p["name"] or p["slug"])
             for p in db.get_chat_participants(con, chat_id)}
    events = list(accounting.iter_cost_events(
        con, pricing=cfg.get("pricing") or None, chat_id=chat_id))
    seats = []
    for r in raised:
        since = r["set_at"] or None
        summary = accounting.summarize(events, since=since)
        group = next((g for g in summary["by_party"] if g["key"] == r["slug"]), None)
        seats.append((names.get(r["slug"], r["slug"]), r["effort"], r["set_at"], group))
    row = db.insert_message(con, chat_id, "system", compose(seats))
    con.execute("UPDATE chats SET spend_note_upto=? WHERE id=?",
                (row["id"], chat_id))
    con.commit()
    return True
