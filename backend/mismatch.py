"""The LLM attribution cross-check (#28 phase 2): content says who content says.

Voice evidence (anchors) decides labels; this module is the second opinion.
After the diarization pass attaches a NAMED label to a turn, a fire-and-forget
utility-model call reads the turn against recent conversation context and the
roster, and asks one narrow question: does what was said contradict the name
on it ("the turn labelled Shawn reads like his wife")? On a contradiction it
raises a room_flags row of kind 'mismatch'.

Two hard rules, both pinned by tests:

- It NEVER changes a label. The flag exists to be looked at; correction is a
  human act (the tap-to-correct endpoint), because the model's hunch about
  content must not overwrite audio evidence silently.
- It never blocks anything. It runs after the label write, inside its own
  task, keyless-degrading (no utility key = no check, quietly).

The flag row is content-free: it carries the label and the suspected NAME,
never transcript text - the copy the user reads is built client-side.
"""

import asyncio
import json
import logging
import re

from . import db, llm_util

log = logging.getLogger("crossband.mismatch")

_TASKS: set = set()

CONTEXT_TURNS = 6  # how much recent conversation the check may read


# ---------- pure rules (unit-tested directly, no I/O) ----------

def parse_verdict(text) -> dict:
    """Defensive parse of the model's JSON verdict. Anything malformed
    degrades to 'no mismatch' - a broken check must never raise a flag.
    Returns {"mismatch": bool, "suspected": str}."""
    out = {"mismatch": False, "suspected": ""}
    if not text:
        return out
    m = re.search(r"\{.*\}", text, re.DOTALL)
    if not m:
        return out
    try:
        data = json.loads(m.group(0))
    except json.JSONDecodeError:
        return out
    if not isinstance(data, dict) or data.get("mismatch") is not True:
        return out
    out["mismatch"] = True
    suspected = data.get("suspected")
    if isinstance(suspected, str):
        out["suspected"] = suspected.strip()[:40]
    return out


def build_prompt(turn_text: str, label: str, roster_names: list,
                 context_lines: list) -> str:
    context = "\n".join(context_lines[-CONTEXT_TURNS:]) or "(none)"
    names = ", ".join(n for n in roster_names if n) or "(unknown)"
    return (
        "You sanity-check speaker attribution in a room conversation. A "
        f"voice system labelled the turn below as spoken by {label}. People "
        f"in the room: {names}.\n"
        "Does the CONTENT of the turn contradict that label - e.g. the "
        "speaker refers to the labelled person in the third person, claims "
        "an identity or relationship that cannot be theirs, or directly "
        "says they are someone else? Weak stylistic hunches are NOT a "
        "mismatch; only contradictions are.\n"
        "Reply with ONLY JSON: {\"mismatch\": true/false, \"suspected\": "
        "\"name or empty\"} - suspected must be one of the people in the "
        "room, or empty.\n\n"
        f"Recent context:\n{context}\n\n"
        f"Turn labelled {label}: {turn_text[:1200]}"
    )


# ---------- the fire-and-forget check ----------

def schedule_check(chat_id, message_id, label, cfg):
    """Fire the cross-check for one freshly-labelled turn and return
    immediately. Callers (the diarization pass) never await it."""
    try:
        task = asyncio.get_running_loop().create_task(
            check_turn(chat_id, message_id, label, cfg))
    except RuntimeError:
        return None  # no running loop (pure-sync test contexts) - skip
    _TASKS.add(task)
    task.add_done_callback(_TASKS.discard)
    return task


async def check_turn(chat_id, message_id, label, cfg):
    """One turn's check. Reads the turn + context on a worker thread, asks the
    utility model, and on a confirmed mismatch inserts the flag row. Never
    touches voice_labels - there is deliberately no code path from here to
    db.set_message_voice_labels."""
    try:
        state = await asyncio.to_thread(_read_context, chat_id, message_id)
        if state is None:
            return
        turn_text, roster_names, context_lines = state
        prompt = build_prompt(turn_text, label, roster_names, context_lines)
        reply = await llm_util.utility_complete_logged(
            chat_id, "mismatch_check", prompt, cfg, max_tokens=120)
        verdict = parse_verdict(reply)
        if not verdict["mismatch"]:
            return
        suspected = verdict["suspected"]
        if suspected and suspected.lower() not in [n.lower() for n in roster_names]:
            suspected = ""  # never flag a name that is not in the room
        await asyncio.to_thread(_raise_flag, chat_id, message_id, label,
                                suspected)
        log.info("attribution mismatch flagged: chat=%s msg=%s", chat_id,
                 message_id)
    except Exception:
        log.info("mismatch check failed: chat=%s msg=%s", chat_id, message_id)
        log.debug("mismatch check failure detail", exc_info=True)


def _read_context(chat_id, message_id):
    con = db.connect()
    try:
        row = con.execute(
            "SELECT * FROM messages WHERE id=? AND chat_id=?",
            (message_id, chat_id)).fetchone()
        if not row or row["speaker"] != "user":
            return None
        roster = db.get_room_roster(con, chat_id, present_only=True)
        names = db.participant_names(con)
        recent = con.execute(
            "SELECT speaker, content FROM messages WHERE chat_id=? AND id<? "
            "ORDER BY id DESC LIMIT ?",
            (chat_id, message_id, CONTEXT_TURNS)).fetchall()
        lines = []
        for r in reversed(recent):
            who = "User" if r["speaker"] == "user" else names.get(
                r["speaker"], r["speaker"])
            lines.append(f"{who}: {r['content'][:300]}")
        return row["content"], [p["name"] for p in roster], lines
    finally:
        con.close()


def _raise_flag(chat_id, message_id, label, suspected):
    con = db.connect()
    try:
        # One open mismatch flag per turn - re-checks must not stack copies.
        open_flags = [f for f in db.get_room_flags(con, chat_id)
                      if f["kind"] == "mismatch"
                      and f["message_id"] == message_id]
        if open_flags:
            return
        db.insert_room_flag(con, chat_id, "mismatch", message_id=message_id,
                            label=label, suspected=suspected)
    finally:
        con.close()
