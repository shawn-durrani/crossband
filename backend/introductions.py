"""Introduction detection (#28 phase 2): the spoken introduction IS the trigger.

The owner already introduces a second human out loud ("my wife Alex is here",
"say hi to Dave"). This module turns that into room mode: a cheap lexical
prefilter runs over every user turn, and only a prefilter hit pays for a
utility-model confirmation. On a confirmed introduction the chat's room mode
flips on server-side, the named people join the roster (up to the configured
cap), and their anchors are marked pending; a confirmed departure ("Dave has
left") frees their roster slot. The owner's own anchor is seeded from the
introduction utterance itself - the voice that spoke it is the owner's by
design.

THE CORE LAW, inherited from phase 1: nothing here is ever awaited by
dispatch. routers/chats.py fires scan_user_turn as a fire-and-forget task
AFTER the user message is persisted; a slow or failed scan changes nothing
about the round that is already streaming. Every blocking step inside the
scan runs on a worker thread.

Keyless posture: with no utility-model key, llm_util returns None and the
scan quietly does nothing - room mode then only ever flips via the explicit
toggle, which keeps working.
"""

import asyncio
import json
import logging
import re

from . import anchors, db, diarize, llm_util

log = logging.getLogger("crossband.introductions")

# Strong refs for fire-and-forget scan tasks (asyncio only holds weak ones).
_TASKS: set = set()

MAX_NAME_CHARS = 40
MAX_NAMES_PER_TURN = 6  # a "whole family" introduction, bounded

# ---------- pure rules (unit-tested directly, no I/O) ----------

# Introduction-shaped phrases. Deliberately BROAD-ish: this only decides
# whether to spend one cheap utility call, and the model is the judge of
# whether a human was actually introduced. Departure phrases are narrower.
_INTRO_PATTERNS = [
    r"\bthis is \w+",
    r"\bmeet \w+",
    r"\bsay (?:hi|hello|g'day) to\b",
    r"\bmy (?:wife|husband|partner|friend|mate|mum|mom|dad|father|mother|"
    r"son|daughter|brother|sister|colleague|boss|neighbou?r|cousin|kids?)\b",
    r"\bis here with (?:me|us)\b",
    r"\bare here with (?:me|us)\b",
    r"\b(?:is|are) joining (?:me|us)\b",
    r"\bjoined? (?:me|us)\b",
    r"\bin the room with\b",
    r"\bhere with me\b",
    r"\bwhole (?:family|team|crew|gang)\b",
    r"\bi'?m here with\b",
    r"\bwe have \w+ (?:here|with us)\b",
    r"\bthat(?:'s| was| is) \w+ (?:speaking|talking|asking)\b",
]
_DEPART_PATTERNS = [
    r"\bhas left\b",
    r"\bhave left\b",
    r"\bjust left\b",
    r"\bis leaving\b",
    r"\bare leaving\b",
    r"\bhad to (?:go|leave)\b",
    r"\bstepped out\b",
    r"\bgone to bed\b",
    r"\bit'?s just me (?:now|again)\b",
]
_INTRO_RE = re.compile("|".join(_INTRO_PATTERNS), re.IGNORECASE)
_DEPART_RE = re.compile("|".join(_DEPART_PATTERNS), re.IGNORECASE)


def prefilter(text: str) -> bool:
    """Is this turn introduction- or departure-shaped enough to be worth one
    utility-model call? Cheap, over-inclusive on purpose; the model confirms.
    Bounded input - an introduction lives in the first breath of a turn, and
    an unbounded regex over a pasted document is silly work."""
    head = (text or "")[:600]
    if not head.strip():
        return False
    return bool(_INTRO_RE.search(head) or _DEPART_RE.search(head))


def _clean_name(raw) -> str:
    """A roster name: short, plain, no markup, title-cased if it arrived
    lowercase (spoken transcripts often are)."""
    if not isinstance(raw, str):
        return ""
    name = re.sub(r"[^\w \-'’]", "", raw).strip()[:MAX_NAME_CHARS]
    if not name or not re.search(r"[A-Za-z]", name):
        return ""
    return name if name[:1].isupper() else name[:1].upper() + name[1:]


def parse_verdict(text) -> dict:
    """Parse the utility model's JSON verdict, defensively: anything that is
    not the documented shape degrades to 'nothing found' rather than raising.
    Returns {"introductions": [names], "departures": [names]} with cleaned,
    de-duplicated, bounded name lists."""
    out = {"introductions": [], "departures": []}
    if not text:
        return out
    m = re.search(r"\{.*\}", text, re.DOTALL)
    if not m:
        return out
    try:
        data = json.loads(m.group(0))
    except json.JSONDecodeError:
        return out
    if not isinstance(data, dict):
        return out
    for key in ("introductions", "departures"):
        seen = []
        vals = data.get(key)
        for raw in (vals if isinstance(vals, list) else []):
            name = _clean_name(raw)
            if name and name.lower() not in [s.lower() for s in seen]:
                seen.append(name)
            if len(seen) >= MAX_NAMES_PER_TURN:
                break
        out[key] = seen
    return out


def build_prompt(text: str, user_name: str, roster_names: list) -> str:
    """The confirmation prompt. The model only ever returns names - the
    transcript text stays in the request, never in anything persisted."""
    present = ", ".join(roster_names) if roster_names else "(nobody yet)"
    return (
        "You watch one message from a voice conversation and decide whether "
        "it INTRODUCES another human who is physically present and may speak "
        "(e.g. 'my wife Alex is here', 'say hi to Dave', 'the whole family "
        "is here: Ana, Ben and Cass'), or ANNOUNCES that a present person "
        "has left ('Dave has left', 'she had to go'). Mentioning a person "
        "who is NOT in the room (talking ABOUT someone, a phone call, a "
        "story) is neither.\n"
        f"The speaker is {user_name}. People already known present: {present}.\n"
        "Reply with ONLY JSON: {\"introductions\": [names...], "
        "\"departures\": [names...]}. Use the name as spoken; empty lists "
        "when the message is neither. Never invent a name that is not in "
        "the message.\n\n"
        f"Message: {text[:1200]}"
    )


def cap_allows(present_count: int, adding: int, cap: int) -> int:
    """How many of `adding` new people fit under the cap. The cap counts
    PRESENT people; it frees as people leave."""
    return max(0, min(adding, cap - present_count))


# ---------- the fire-and-forget scan ----------

def schedule_scan(chat_id, message_id, text, cfg):
    """Fire the introduction scan for one persisted user turn and return
    IMMEDIATELY - the caller (POST /send) never awaits it, and a failure to
    even schedule must not break the send."""
    if not prefilter(text):
        return None
    task = asyncio.get_running_loop().create_task(
        scan_user_turn(chat_id, message_id, text, cfg))
    _TASKS.add(task)
    task.add_done_callback(_TASKS.discard)
    return task


async def scan_user_turn(chat_id, message_id, text, cfg):
    """One user turn's scan: utility-model confirmation, then roster/room-mode
    application. Every failure ends here (log only)."""
    try:
        roster = await asyncio.to_thread(_present_names, chat_id)
        prompt = build_prompt(text, cfg.get("user_name", "User"), roster)
        reply = await llm_util.utility_complete(prompt, cfg, max_tokens=200)
        verdict = parse_verdict(reply)
        if not verdict["introductions"] and not verdict["departures"]:
            return
        await asyncio.to_thread(apply_scan, chat_id, verdict, cfg)
    except Exception:
        log.info("introduction scan failed: chat=%s", chat_id)
        log.debug("introduction scan failure detail", exc_info=True)


def _present_names(chat_id) -> list:
    con = db.connect()
    try:
        return [r["name"] for r in db.get_room_roster(con, chat_id,
                                                      present_only=True)]
    finally:
        con.close()


def apply_scan(chat_id, verdict, cfg):
    """Apply a confirmed verdict (synchronous; runs on a worker thread):

    - introductions: flip room mode on (durably AND in diarize's in-process
      registry so a live session tees from its next commit), append each name
      to the roster up to the cap, link a REMEMBERED person's anchors
      immediately (that is re-identification), and seed the owner's anchor
      from the introduction utterance the relay stashed.
    - departures: mark the named people left (the cap frees).

    Content-free logging throughout: counts, never names or text."""
    con = db.connect()
    try:
        chat = con.execute("SELECT * FROM chats WHERE id=?",
                           (chat_id,)).fetchone()
        if not chat:
            return
        intros = verdict.get("introductions") or []
        departs = verdict.get("departures") or []
        if intros:
            if not chat["room_mode"]:
                db.set_chat_room_mode(con, chat_id, True)
                diarize.set_room_enabled(chat_id, True)
                log.info("room mode ON via introduction: chat=%s", chat_id)
            present = db.get_room_roster(con, chat_id, present_only=True)
            present_names = {p["name"].lower() for p in present}
            new = [n for n in intros if n.lower() not in present_names]
            cap = int(cfg.get("room_roster_max") or 6)
            allowed = cap_allows(len(present), len(new), cap)
            if allowed < len(new):
                log.info("roster cap reached: chat=%s cap=%d dropped=%d",
                         chat_id, cap, len(new) - allowed)
            for name in new[:allowed]:
                known = anchors.store().find_by_name(name)
                pid = known["person_id"] if (known and known["sufficient"]) else ""
                db.add_room_person(con, chat_id, name, person_id=pid)
            log.info("roster grew: chat=%s added=%d", chat_id,
                     min(allowed, len(new)))
            # An introduction is also the ANSWER to an open "someone new is
            # speaking - who?" ask: naming them closes it. The next utterance
            # then resolves by elimination (one pending name, one unmatched
            # cluster) or asks again if genuinely still ambiguous.
            db.resolve_room_flags(con, chat_id, kind="unknown_voice")
            _seed_owner_anchor(chat_id, cfg)
        for name in departs:
            if db.mark_room_person_left(con, chat_id, name):
                log.info("roster departure: chat=%s", chat_id)
    finally:
        con.close()


def _seed_owner_anchor(chat_id, cfg):
    """The owner's anchor comes from the introduction utterance: the relay
    stashes each finished utterance while room mode is off, and the voice
    that spoke the introduction is the owner's by design. Quietly a no-op
    when there is no live voice session (a TYPED introduction has no audio)."""
    stashed = diarize.take_stashed_utterance(chat_id)
    if not stashed:
        return
    pcm, sample_rate = stashed
    owner = cfg.get("user_name", "User")
    store = anchors.store()
    pid = store.ensure_person(owner)
    if store.add_clip(pid, pcm, sample_rate, source="introduction"):
        log.info("owner anchor seeded from introduction: chat=%s", chat_id)
    con = db.connect()
    try:
        # The owner rides the roster too - the "In the room" chip should show
        # everyone the diarizer is being asked to tell apart.
        present = {p["name"].lower()
                   for p in db.get_room_roster(con, chat_id, present_only=True)}
        if owner.lower() not in present:
            db.add_room_person(con, chat_id, owner, person_id=pid)
        else:
            db.link_room_person(con, chat_id, owner, pid)
    finally:
        con.close()
