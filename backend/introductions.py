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
#
# THE THIRD FIELD TEST (#28, 2026-08-08 evening) is why several of these
# exist: "I'm gonna hand over to a guest" and "I'm here too. I'm Katrina,
# [the owner]'s wife, also known as Kat" both died HERE - no pattern matched,
# so the scan was never scheduled and room mode silently never armed. The
# handover shapes, the possessive relationship ("X's wife"), the alias
# phrases and the capitalised self-introduction below close that gap.
_RELATIONSHIP_WORDS = (
    r"wife|husband|partner|friend|mate|guest|mum|mom|dad|father|mother|"
    r"son|daughter|brother|sister|colleague|boss|neighbou?r|cousin|kids?")
_INTRO_PATTERNS = [
    r"\bthis is \w+",
    r"\bmeet \w+",
    r"\bsay (?:hi|hello|g'day) to\b",
    r"\bmy (?:" + _RELATIONSHIP_WORDS + r")\b",
    r"\b\w+'s (?:" + _RELATIONSHIP_WORDS + r")\b",   # "Alex's wife"
    r"\bis here with (?:me|us)\b",
    r"\bare here with (?:me|us)\b",
    r"\b(?:is|are) joining (?:me|us)\b",
    r"\bjoined? (?:me|us)\b",
    r"\bin the room with\b",
    r"\bhere with me\b",
    r"\bwhole (?:family|team|crew|gang)\b",
    r"\bi'?m here with\b",
    r"\bi'?m here too\b",
    r"\bwe have \w+ (?:here|with us)\b",
    r"\bthat(?:'s| was| is) \w+ (?:speaking|talking|asking)\b",
    # handover shapes: someone is about to speak, named or not
    r"\bhand(?:s|ing|ed)? (?:\w+ ){0,2}over\b",      # "hand(ing it) over to"
    r"\b(?:pass|give) (?:\w+ ){0,2}(?:mic|phone|headset)\b",
    r"\bsomeone else (?:wants?|is going|would like) to\b",
    r"\bwants? to say (?:hi|hello|g'day)\b",
    # self-introduction and alias shapes
    r"\bmy name(?:'s| is)\b",
    r"\balso known as \w+",
    r"\bcall me \w+",
]
# A guest introducing themselves: "I'm Katrina", "I am Dave". Compiled
# case-SENSITIVELY, unlike everything above: the capital letter is what
# separates a name from "I'm gonna" / "I'm here" - transcripts capitalise
# names and little else mid-sentence. Over-inclusive is fine (the model
# judges); missing the capital only costs falling back to the other shapes.
_SELF_INTRO_RE = re.compile(r"\b(?:[Ii]'m|[Ii] am) [A-Z][a-z'’-]+\b")
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
    return bool(_INTRO_RE.search(head) or _DEPART_RE.search(head)
                or _SELF_INTRO_RE.search(head))


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
    Returns {"introductions": [names], "departures": [names],
    "aliases": {name: preferred}} with cleaned, de-duplicated, bounded name
    lists. An alias ("also known as Kat", "call me Kat") survives only when
    it points at an introduced name, is a plausible name itself (never a
    relationship noun), and actually differs from the name."""
    out = {"introductions": [], "departures": [], "aliases": {}}
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
    raw_aliases = data.get("aliases")
    if isinstance(raw_aliases, dict):
        intro_by_lower = {n.lower(): n for n in out["introductions"]}
        for k, v in raw_aliases.items():
            name, alias = _clean_name(k), _clean_name(v)
            target = intro_by_lower.get(name.lower()) if name else None
            if not target or not alias:
                continue
            if relationship_noun(alias) or alias.lower() == target.lower():
                continue
            out["aliases"][target] = alias
            if len(out["aliases"]) >= MAX_NAMES_PER_TURN:
                break
    return out


def build_prompt(text: str, user_name: str, roster_names: list) -> str:
    """The confirmation prompt. The model only ever returns names - the
    transcript text stays in the request, never in anything persisted."""
    present = ", ".join(roster_names) if roster_names else "(nobody yet)"
    return (
        "You watch one message from a voice conversation and decide whether "
        "it INTRODUCES another human who is physically present and may "
        "speak, or ANNOUNCES that a present person has left ('Dave has "
        "left', 'she had to go'). Introductions come in several shapes: the "
        "owner introducing someone ('my wife Alex is here', 'say hi to "
        "Dave', 'the whole family is here: Ana, Ben and Cass'); a guest "
        "introducing THEMSELVES on the shared microphone ('I'm here too. "
        "I'm Kat, his wife'); and a handover that only announces someone is "
        "about to speak ('I'll hand over to a guest now', 'someone else "
        "wants to say hi'). Mentioning a person who is NOT in the room "
        "(talking ABOUT someone, a phone call, a story) is none of these.\n"
        f"The device owner is {user_name}; guests speak through the same "
        f"microphone. People already known present: {present}.\n"
        "Reply with ONLY JSON: {\"introductions\": [names...], "
        "\"departures\": [names...], \"aliases\": {name: preferred}}. Use "
        "the person's PROPER NAME as spoken; when the message gives both a "
        "name and a relationship ('this is Kat, my wife'), return only the "
        "name, never the relationship word. If someone is introduced or "
        "handed over to by relationship alone ('my wife is here', 'hand "
        "over to a guest') with no name anywhere in the message, return the "
        "relationship word itself (e.g. 'Wife', 'Guest') - the app resolves "
        "it rather than treating it as a name. When the message states a "
        "preferred short form ('also known as Kat', 'call me Kat'), add it "
        "to \"aliases\" keyed by that person's name. Empty lists "
        "when the message is neither. Never invent a name that is not in "
        f"the message, and never return {user_name} themselves - the "
        "owner is already known, including when the transcript spells "
        "their name slightly differently.\n\n"
        f"Message: {text[:1200]}"
    )


def cap_allows(present_count: int, adding: int, cap: int) -> int:
    """How many of `adding` new people fit under the cap. The cap counts
    PRESENT people; it frees as people leave."""
    return max(0, min(adding, cap - present_count))


# ---- naming hygiene (#28 phase 4, second-field-test defect 1) ----
#
# "This is me, Kat, [the owner]'s wife" minted a roster person named "Wife".
# A relationship word is HOW someone relates to the owner, never WHO they
# are: it must not become a person's name, a voice-label, a memory speaker
# class, or a keyterm. The rule lives here, at the single point where
# introduction names enter the system.

_RELATIONSHIP_NOUNS = {
    "wife", "husband", "partner", "spouse", "fiance", "fiancee",
    "girlfriend", "boyfriend",
    "mum", "mom", "mother", "dad", "father", "parent",
    "son", "daughter", "child", "kid", "baby",
    "brother", "sister", "sibling", "cousin",
    "grandma", "grandmother", "nan", "nanna", "granny",
    "grandpa", "grandfather", "pop", "poppy",
    "aunt", "aunty", "auntie", "uncle", "niece", "nephew",
    "friend", "mate", "buddy", "bestie",
    "colleague", "workmate", "coworker",
    "boss", "manager", "assistant",
    "neighbour", "neighbor", "roommate", "flatmate", "housemate",
    "guest", "visitor",
}
_RELATIONSHIP_LEADING = ("my", "our", "his", "her", "their", "the", "a", "an")


def relationship_noun(name: str) -> bool:
    """Is `name` a relationship word rather than a person's name? Checks the
    whole (cleaned) name: leading possessives drop ("my wife" -> "wife"), a
    trailing plural 's' drops ("kids" -> "kid"). Multi-word real names
    ('Mary Rose') never match - only a bare relationship phrase does."""
    words = [w for w in re.sub(r"[^a-z' ]", " ", (name or "").casefold()).split()
             if w]
    while words and words[0] in _RELATIONSHIP_LEADING:
        words = words[1:]
    if len(words) != 1:
        return False
    word = words[0].rstrip("'").strip()
    if word in _RELATIONSHIP_NOUNS:
        return True
    return word.endswith("s") and word[:-1] in _RELATIONSHIP_NOUNS


def match_remembered_name(text, known_names, exclude=frozenset()) -> str:
    """When an introduction gave only a relationship ('my wife is here'), the
    utterance may still contain a proper name the roster verdict missed - and
    if that name is one we already REMEMBER, re-identifying them beats both a
    placeholder and an interruption. Returns the single remembered name that
    appears (word-bounded, case-insensitive) in `text` and is not excluded
    (owner, already present); '' when none or more than one match -
    ambiguity is the ask-fallback's job, not a coin flip's."""
    head = (text or "")[:600]
    if not head:
        return ""
    excluded = {e.casefold() for e in exclude or ()}
    hits = []
    for name in known_names or []:
        if not isinstance(name, str) or not name.strip():
            continue
        if name.casefold() in excluded or relationship_noun(name):
            continue
        if re.search(r"\b" + re.escape(name) + r"\b", head, re.IGNORECASE):
            if name.casefold() not in {h.casefold() for h in hits}:
                hits.append(name)
    return hits[0] if len(hits) == 1 else ""


def _letters(name: str) -> str:
    return re.sub(r"[^a-z]", "", (name or "").casefold())


def _within_one_edit(a: str, b: str) -> bool:
    """Levenshtein distance <= 1, the cheap special case (one insert, delete
    or substitute). Enough for a transcriber's phonetic slip; anything
    further apart is honestly a different name."""
    if a == b:
        return True
    if abs(len(a) - len(b)) > 1:
        return False
    if len(a) > len(b):
        a, b = b, a
    i = j = edits = 0
    while i < len(a) and j < len(b):
        if a[i] == b[j]:
            i += 1
            j += 1
            continue
        edits += 1
        if edits > 1:
            return False
        if len(a) == len(b):
            i += 1  # substitution
        j += 1      # insertion into the longer string
    return edits + (len(b) - j) + (len(a) - i) <= 1


def owner_alias(name: str, owner: str) -> bool:
    """Is `name` plausibly the OWNER's own name as the transcriber spelt it?
    The owner's roster identity comes from the `user_name` setting, never
    from audio (#28 phase 3, field-test defect 3): a self-introduction
    transcribed as 'Shaun' must not mint a second roster person beside
    'Shawn'. Conservative on purpose - exact match (ignoring case and
    punctuation), or names at least three letters long within one edit of
    each other. A genuinely distinct guest wrongly caught here still
    surfaces later through the unknown-voice ask, which is the honest
    fallback; a phantom owner-double on the roster has no such correction."""
    a, b = _letters(name), _letters(owner)
    if not a or not b:
        return False
    if a == b:
        return True
    return len(a) >= 3 and len(b) >= 3 and _within_one_edit(a, b)


# ---------- the fire-and-forget scan ----------

# The per-scan verdict line (#28, third field test). That night's failure
# mode was SILENCE: both spoken triggers died at the prefilter, so the log
# showed no scan activity at all, and "rejected" was indistinguishable from
# "never ran". Every scan now ends in exactly ONE content-free INFO line,
# "introduction scan verdict: chat=<id> outcome=<value>", with the outcome
# drawn from this allowlist and nothing else - never transcript text, never
# a name. ask_raised and armed both imply room mode is on after the scan.
SCAN_OUTCOMES = (
    "no_prefilter_match",   # not introduction-shaped; no utility call made
    "model_rejected",       # prefilter hit, but the model found nobody
    "armed",                # room mode flipped ON by this scan
    "ask_raised",           # relationship-only, no remembered match: asking
    "roster_grew",          # already armed; people were added
    "roster_shrank",        # a departure freed roster slots
    "no_change",            # a confirmed verdict that changed nothing
    "scan_error",           # the scan itself failed (detail logged below it)
)


def _log_verdict(chat_id, outcome):
    if outcome not in SCAN_OUTCOMES:      # belt and braces: stay content-free
        outcome = "scan_error"
    log.info("introduction scan verdict: chat=%s outcome=%s", chat_id, outcome)


def schedule_scan(chat_id, message_id, text, cfg):
    """Fire the introduction scan for one persisted user turn and return
    IMMEDIATELY - the caller (POST /send) never awaits it, and a failure to
    even schedule must not break the send."""
    if not prefilter(text):
        _log_verdict(chat_id, "no_prefilter_match")
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
            _log_verdict(chat_id, "model_rejected")
            return
        outcome = await asyncio.to_thread(apply_scan, chat_id, verdict, cfg,
                                          text)
        _log_verdict(chat_id, outcome)
    except Exception:
        _log_verdict(chat_id, "scan_error")
        log.info("introduction scan failed: chat=%s", chat_id)
        log.debug("introduction scan failure detail", exc_info=True)


def _present_names(chat_id) -> list:
    con = db.connect()
    try:
        return [r["name"] for r in db.get_room_roster(con, chat_id,
                                                      present_only=True)]
    finally:
        con.close()


def apply_scan(chat_id, verdict, cfg, text=""):
    """Apply a confirmed verdict (synchronous; runs on a worker thread):

    - introductions: flip room mode on (durably AND in diarize's in-process
      registry so a live session tees from its next commit), append each name
      to the roster up to the cap, link a REMEMBERED person's anchors
      immediately (that is re-identification), and seed the owner's anchor
      from the introduction utterance the relay stashed.
    - naming hygiene (#28 phase 4): a relationship word is never stored as a
      person's name. Proper names in the same verdict win; a
      relationship-only introduction first tries to re-identify a REMEMBERED
      person named in the utterance, and otherwise raises the ask-fallback -
      room mode still flips on either way, because someone IS present.
    - alias capture (#28, third field test): "also known as Kat" / "call me
      Kat" sets a NEW person's preferred display name at creation. An
      existing person's preferred name is never overwritten - it may have
      been corrected by hand.
    - departures: mark the named people left (the cap frees).

    Returns the scan's outcome, one of SCAN_OUTCOMES - the verdict line the
    caller logs. `text` is the utterance (for the remembered-name match only
    - nothing from it is persisted or logged). Content-free logging
    throughout: counts, never names or text."""
    flipped = ask = False
    added = departed = 0
    con = db.connect()
    try:
        chat = con.execute("SELECT * FROM chats WHERE id=?",
                           (chat_id,)).fetchone()
        if not chat:
            return "no_change"
        owner = cfg.get("user_name", "User")
        # The owner's roster identity is the `user_name` SETTING (#28 phase
        # 3): a transcription of the owner's own name - however it was spelt
        # by ear - never mints a roster person. _seed_owner_anchor below adds
        # the owner under user_name; this filter is what keeps a phonetic
        # 'Shaun' from standing beside them.
        raw_intros = verdict.get("introductions") or []
        intros = [n for n in raw_intros if not owner_alias(n, owner)]
        if len(intros) < len(raw_intros):
            log.info("owner-alias introduction dropped: chat=%s n=%d",
                     chat_id, len(raw_intros) - len(intros))
        # Naming hygiene (#28 phase 4): strip relationship nouns. "Kat" and
        # "Wife" in one verdict is the proper name plus its echo; "Wife"
        # alone is an unnamed introduction, resolved below.
        named = [n for n in intros if not relationship_noun(n)]
        if len(named) < len(intros):
            log.info("relationship-noun introduction dropped: chat=%s n=%d",
                     chat_id, len(intros) - len(named))
        departs = verdict.get("departures") or []
        aliases = verdict.get("aliases")
        aliases = aliases if isinstance(aliases, dict) else {}
        if intros:
            if not chat["room_mode"]:
                db.set_chat_room_mode(con, chat_id, True)
                diarize.set_room_enabled(chat_id, True)
                flipped = True
                log.info("room mode ON via introduction: chat=%s", chat_id)
            present = db.get_room_roster(con, chat_id, present_only=True)
            present_names = {p["name"].lower() for p in present}
            if not named:
                # Only a relationship was given ("my wife is here"). Try the
                # utterance against REMEMBERED people before interrupting:
                # a known name in the same breath is a re-identification.
                known_names = [p["name"] for p in anchors.store().people()]
                match = match_remembered_name(
                    text, known_names,
                    exclude=present_names | {owner.casefold()})
                if match:
                    named = [match]
                    log.info("relationship-only introduction matched a "
                             "remembered person: chat=%s", chat_id)
                else:
                    ask = True
                    _raise_unnamed_intro_ask(con, chat_id)
                    log.info("relationship-only introduction with no match: "
                             "chat=%s ask raised", chat_id)
            new = [n for n in named if n.lower() not in present_names]
            cap = int(cfg.get("room_roster_max") or 6)
            allowed = cap_allows(len(present), len(new), cap)
            if allowed < len(new):
                log.info("roster cap reached: chat=%s cap=%d dropped=%d",
                         chat_id, cap, len(new) - allowed)
            for name in new[:allowed]:
                known = anchors.store().find_by_name(name)
                pid = known["person_id"] if (known and known["sufficient"]) else ""
                alias = aliases.get(name)
                if alias and known is None:
                    # Alias capture at CREATION only: mint the store entry so
                    # the preferred name has somewhere durable to live, and
                    # link the roster row to it. Anchor audio arrives later,
                    # exactly as for any pending person.
                    store = anchors.store()
                    pid = store.ensure_person(name)
                    if store.set_preferred_name(pid, alias):
                        log.info("alias captured at introduction: chat=%s",
                                 chat_id)
                db.add_room_person(con, chat_id, name, person_id=pid)
            added = min(allowed, len(new))
            log.info("roster grew: chat=%s added=%d", chat_id, added)
            if named:
                # An introduction is also the ANSWER to an open "someone new
                # is speaking - who?" ask: naming them closes it. The next
                # utterance then resolves by elimination (one pending name,
                # one unmatched cluster) or asks again if genuinely still
                # ambiguous. An UNNAMED introduction resolves nothing - the
                # ask it just raised must stand.
                db.resolve_room_flags(con, chat_id, kind="unknown_voice")
            _seed_owner_anchor(chat_id, cfg)
        for name in departs:
            if db.mark_room_person_left(con, chat_id, name):
                departed += 1
                log.info("roster departure: chat=%s", chat_id)
    finally:
        con.close()
    # One outcome per scan, most informative first: an ask implies room mode
    # is on; armed reports the flip; the roster outcomes report change while
    # already armed.
    if ask:
        return "ask_raised"
    if flipped:
        return "armed"
    if added:
        return "roster_grew"
    if departed:
        return "roster_shrank"
    return "no_change"


def _raise_unnamed_intro_ask(con, chat_id):
    """The ask-fallback for a relationship-only introduction: one OPEN
    'unknown_voice' ask per chat, same discipline as the diarization pass's -
    an unanswered ask must not stack while the same unnamed person keeps
    being mentioned."""
    open_asks = [f for f in db.get_room_flags(con, chat_id)
                 if f["kind"] == "unknown_voice"]
    if not open_asks:
        db.insert_room_flag(con, chat_id, "unknown_voice")


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
