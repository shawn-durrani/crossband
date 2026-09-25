"""The running-cost line for an escalated chat (#259).

A spoken depth change tells the chat replies will be slower and never that
they cost more, and anyone in the room may raise a seat; research mode
(#253/#417) is the chat-wide version of the same raise, a bigger tool budget
that spends more per reply, and a seat stepped up to a stronger model for the
chat (#254) is a third. So while a seat sits above its configured depth or on
a stronger model, OR research mode is on, the chat gets one short system line
now and then saying what has been spent since it was raised. Watermark gated like the
rolling summary and the auto title: it fires on a message count, never on a
clock, and `spend_note_every` sets the count.

What the line says, and what it may not: a per-seat figure, because depth
is per seat and one chat-wide line is wrong in a mixed room, plus one
chat-wide clause for research mode, because that raise has no single seat
to name; the metered figure alone as cash, with subscription-covered and
unpriced use named apart and never summed; and "a rate-card estimate",
because nothing here is a bill. The line is a system row, so it persists
and every seat reads it, and it never reaches membro: is_spend_note() is
the ingest filter."""

import time

from . import accounting, db, model_step
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


def seat_line(name, effort, set_at, group, model_label="") -> str:
    """One seat's clause: how it is set, since when, and what it has used.
    `effort` is '' for a seat raised only by a model step-up (#254), and
    `model_label` names the stronger model a seat runs in this chat."""
    on = f" on {model_label}" if model_label else ""
    how = (f"at {LEVEL_WORDS.get(effort, effort)} thinking{on}" if effort
           else on.strip())
    metered = float((group or {}).get(accounting.CAT_METERED, 0.0))
    subs = float((group or {}).get(accounting.CAT_SUBSCRIPTION, 0.0))
    unknown = float((group or {}).get(accounting.CAT_UNKNOWN, 0.0))
    parts = [f"{_money(metered)} metered"]
    if subs:
        parts.append("subscription-covered use apart")
    if unknown:
        parts.append("some unpriced use apart")
    return (f"{name} {how} since {_since(set_at)}, "
            + ", ".join(parts) + " since then")


def research_line(set_at, totals) -> str:
    """The chat-wide clause for research mode (#253/#417): no single seat to
    name, so it names the chat instead. `totals` is
    accounting.summarize(...)["totals"] since the mode was set."""
    metered = float((totals or {}).get(accounting.CAT_METERED, 0.0))
    return (f"research mode since {_since(set_at)}, {_money(metered)} "
            "metered across the chat since then")


def compose(seats: list, research_clause: str = "") -> str:
    """seats: [(name, effort, set_at, group[, model_label])]. `research_clause`
    (spend_note.research_line, or "") leads the seat clauses - it is
    chat-wide, not one seat's raise."""
    clauses = ([research_clause] if research_clause else []) + \
        [seat_line(*s) for s in seats]
    text = "; ".join(clauses)
    return (f"{SPEND_NOTE_HEAD} {text}. Rate-card estimates, not a bill. "
            "Anyone can say back to normal.")


def maybe_spend_note(con, chat, messages, cfg) -> bool:
    """Post the line when it is due. Due means: the knob is on, and either a
    seat is raised or research mode is on for the chat, and
    `spend_note_every` messages have landed since the later of the last line
    and the earliest raise (a seat's, or research mode's own). Returns True
    when a line was written."""
    every = int(cfg.get("spend_note_every") or 0)
    if every <= 0 or not messages:
        return False
    chat_id = chat["id"]
    raised = {r["slug"]: r for r in db.get_chat_seat_escalations(con, chat_id)}
    # #254: a seat on a stronger model for this chat is raised too, while
    # the step-up still applies - an edit in settings ends it (live_step).
    steps = db.get_chat_seat_models(con, chat_id)
    roster = db.get_chat_participants(con, chat_id) if (raised or steps) else []
    by_slug = {p["slug"]: p for p in roster}
    steps = {slug: row for slug, row in steps.items()
             if slug in by_slug and model_step.live_step(by_slug[slug], row)}
    research_on = bool(chat.get("research_mode"))
    if not raised and not steps and not research_on:
        return False
    upto = int(chat.get("spend_note_upto") or 0)
    seats = []
    for slug in sorted(set(raised) | set(steps)):
        r, step = raised.get(slug), steps.get(slug)
        anchor = min([t for t in ((r or {}).get("set_at"),
                                  (step or {}).get("set_at")) if t] or [0.0])
        seats.append((slug, (r or {}).get("effort", ""), anchor,
                      (step or {}).get("label", "")))
    seats.sort(key=lambda s: (s[2], s[0]))
    anchors = [s[2] for s in seats]
    research_set_at = chat.get("research_set_at") or 0.0
    if research_on:
        anchors.append(research_set_at)
    earliest = min(anchors, default=0.0)
    fresh = [m for m in messages
             if m["id"] > upto and (m.get("created_at") or 0) >= earliest]
    if len(fresh) < every:
        return False
    names = {p["slug"]: (p["name"] or p["slug"])
             for p in (roster or db.get_chat_participants(con, chat_id))}
    events = list(accounting.iter_cost_events(
        con, pricing=cfg.get("pricing") or None, chat_id=chat_id))
    clauses = []
    for slug, effort, anchor, label in seats:
        summary = accounting.summarize(events, since=anchor or None)
        group = next((g for g in summary["by_party"] if g["key"] == slug), None)
        clauses.append((names.get(slug, slug), effort, anchor, group, label))
    research_clause = ""
    if research_on:
        summary = accounting.summarize(events, since=research_set_at or None)
        research_clause = research_line(research_set_at, summary["totals"])
    row = db.insert_message(con, chat_id, "system", compose(clauses, research_clause))
    con.execute("UPDATE chats SET spend_note_upto=? WHERE id=?",
                (row["id"], chat_id))
    con.commit()
    return True
