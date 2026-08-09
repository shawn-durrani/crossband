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

The same scan also detects explicit room-mode COMMANDS (#28, chat 198):
"group mode, please" / "room mode on" arms, "solo mode" / "just me now"
disarms - spoken or typed, both arrive through /send. See the command
lexicon below for why this lives here rather than in a second scanner.

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
# exist: "I'm gonna hand over to a guest" and "I'm here too. I'm Samantha,
# [the owner]'s wife, also known as Sam" both died HERE - no pattern matched,
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
# A guest introducing themselves: "I'm Samantha", "I am Dave". Compiled
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

# ---- room-mode commands (#28, chat 198) ----
#
# "Group mode, please", spoken in a live voice chat, did NOTHING: it is not
# an introduction, so the scan correctly logged no_prefilter_match, room
# mode stayed off - and the seats then verbally agreed to a mode switch no
# code was performing. The fix is a small command lexicon riding the SAME
# post-commit fire-and-forget scan: a deterministic prefilter decides
# whether the turn is command-shaped, one cheap utility call confirms it is
# a command (not talk ABOUT the mode) and reads the direction, and a
# confirmed command flips the chat's durable room mode through exactly the
# control plumbing the introduction, sniff and PATCH paths already share.
# Typed and spoken turns take the identical path - both arrive via /send.
#
# The arm/disarm split below is about SHAPES, not direction: "room mode
# off" matches the mode-noun arm pattern too. The prefilter only decides
# whether to spend one utility call; the model is the judge of direction.
_MODE_NOUN = (r"(?:room|group|multi[\s-]*user|multi[\s-]*person|"
              r"multi[\s-]*speaker)[\s-]*mode")
_ARM_COMMAND_PATTERNS = [
    # "group mode, please" / "room mode on" / "turn on multi-user mode" /
    # "we're in group mode" / "switch to room mode": the mode noun is the tell.
    rf"\b{_MODE_NOUN}\b",
]
_DISARM_COMMAND_PATTERNS = [
    r"\bsolo[\s-]*mode\b",           # "solo mode (please)"
    r"\bback to solo\b",
    r"\bjust me (?:now|again)\b",    # "(it's) just me now" - also a departure
]
_COMMAND_RE = re.compile(
    "|".join(_ARM_COMMAND_PATTERNS + _DISARM_COMMAND_PATTERNS), re.IGNORECASE)

COMMAND_ARM = "arm"
COMMAND_DISARM = "disarm"


def command_prefilter(text: str) -> bool:
    """Is this turn shaped like a room-mode command, worth one utility-model
    confirmation? Same posture as prefilter(): cheap, bounded, over-inclusive
    on purpose - a question ABOUT the mode also matches, and the model is
    what separates a command from a mention."""
    head = (text or "")[:600]
    if not head.strip():
        return False
    return bool(_COMMAND_RE.search(head))


def build_command_prompt(text: str) -> str:
    """The command-confirmation prompt. Mirrors build_prompt's discipline:
    the transcript text stays in the request, never in anything persisted,
    and the model returns only a direction."""
    return (
        "You watch one message from a conversation with a voice assistant "
        "and decide whether it asks the app to switch ROOM MODE (also "
        "called group, multi-user or multi-person mode - several people "
        "sharing one microphone) on or off. Direct requests and plain "
        "announcements both count: 'group mode, please', 'room mode on', "
        "'turn on multi-user mode', 'we're in group mode now' all mean ON; "
        "'solo mode', 'room mode off', 'back to solo', 'it's just me now' "
        "all mean OFF. Merely talking ABOUT the mode is NEITHER: a question "
        "('is group mode on?', 'what does room mode cost?'), a recollection "
        "('group mode worked well yesterday'), or a mention in passing must "
        "return none.\n"
        "Reply with ONLY JSON: {\"mode_command\": \"on\"|\"off\"|\"none\"}."
        "\n\n"
        f"Message: {text[:600]}"
    )


def parse_command_verdict(text) -> str:
    """Parse the utility model's command verdict, defensively: anything that
    is not the documented shape degrades to '' (no command) rather than
    raising. Returns COMMAND_ARM, COMMAND_DISARM or ''."""
    if not text:
        return ""
    m = re.search(r"\{.*\}", text, re.DOTALL)
    if not m:
        return ""
    try:
        data = json.loads(m.group(0))
    except json.JSONDecodeError:
        return ""
    if not isinstance(data, dict):
        return ""
    value = data.get("mode_command")
    if value == "on":
        return COMMAND_ARM
    if value == "off":
        return COMMAND_DISARM
    return ""


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
    lists. An alias ("also known as Sam", "call me Sam") survives only when
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
        "I'm Sam, his wife'); and a handover that only announces someone is "
        "about to speak ('I'll hand over to a guest now', 'someone else "
        "wants to say hi'). Mentioning a person who is NOT in the room "
        "(talking ABOUT someone, a phone call, a story) is none of these.\n"
        f"The device owner is {user_name}; guests speak through the same "
        f"microphone. People already known present: {present}.\n"
        "Reply with ONLY JSON: {\"introductions\": [names...], "
        "\"departures\": [names...], \"aliases\": {name: preferred}}. Use "
        "the person's PROPER NAME as spoken; when the message gives both a "
        "name and a relationship ('this is Sam, my wife'), return only the "
        "name, never the relationship word. If someone is introduced or "
        "handed over to by relationship alone ('my wife is here', 'hand "
        "over to a guest') with no name anywhere in the message, return the "
        "relationship word itself (e.g. 'Wife', 'Guest') - the app resolves "
        "it rather than treating it as a name. When the message states a "
        "preferred short form ('also known as Sam', 'call me Sam'), add it "
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
# "This is me, Sam, [the owner]'s wife" minted a roster person named "Wife".
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
    "no_prefilter_match",   # neither introduction- nor command-shaped; no
                            # utility call made
    "model_rejected",       # a prefilter hit, but the model found nothing
    "armed",                # room mode flipped ON by this scan
    "armed_by_command",     # room mode flipped ON by a mode command
    "disarmed_by_command",  # room mode flipped OFF by a mode command
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
    """Fire the scan for one persisted user turn and return IMMEDIATELY - the
    caller (POST /send) never awaits it, and a failure to even schedule must
    not break the send. One scan covers BOTH detections (introductions and
    room-mode commands), so every user turn still ends in exactly one
    verdict line."""
    if not prefilter(text) and not command_prefilter(text):
        _log_verdict(chat_id, "no_prefilter_match")
        return None
    task = asyncio.get_running_loop().create_task(
        scan_user_turn(chat_id, message_id, text, cfg))
    _TASKS.add(task)
    task.add_done_callback(_TASKS.discard)
    return task


async def scan_user_turn(chat_id, message_id, text, cfg):
    """One user turn's scan: the command confirmation first (#28, chat 198),
    then the introduction confirmation, each gated on its own prefilter so a
    turn normally pays for at most one utility call. A confirmed command's
    outcome wins the verdict line when it changed anything; the rare turn
    that is both a command and an introduction ("group mode on - this is
    Dave") applies both. Every failure ends here (log only)."""
    try:
        outcome = None
        command_confirmed = False
        if command_prefilter(text):
            reply = await llm_util.utility_complete(
                build_command_prompt(text), cfg, max_tokens=60)
            direction = parse_command_verdict(reply)
            if direction:
                command_confirmed = True
                result = await asyncio.to_thread(apply_command, chat_id,
                                                 direction, cfg)
                if result != "no_change":
                    outcome = result
        if prefilter(text):
            roster = await asyncio.to_thread(_present_names, chat_id)
            prompt = build_prompt(text, cfg.get("user_name", "User"), roster)
            reply = await llm_util.utility_complete(prompt, cfg,
                                                    max_tokens=200)
            verdict = parse_verdict(reply)
            if verdict["introductions"] or verdict["departures"]:
                intro_outcome = await asyncio.to_thread(apply_scan, chat_id,
                                                        verdict, cfg, text)
                if outcome is None:
                    outcome = intro_outcome
        if outcome is None:
            # A confirmed command that changed nothing (arm while already
            # armed, disarm while off) is an honest no_change; with nothing
            # confirmed at all, the model rejected the turn.
            outcome = "no_change" if command_confirmed else "model_rejected"
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
    - alias capture (#28, third field test): "also known as Sam" / "call me
      Sam" sets a NEW person's preferred display name at creation. An
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
        # Naming hygiene (#28 phase 4): strip relationship nouns. "Sam" and
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


def apply_command(chat_id, direction, cfg):
    """Apply a confirmed room-mode command (synchronous; runs on a worker
    thread). Returns the outcome for the verdict line.

    ARM is the introduction's control path without any names: the durable
    flip plus diarize's live mirror, so a live session's pass machinery
    starts at its next commit boundary. The OWNER joins the roster (linked
    to their remembered anchors when those exist), which makes the pass run
    ANCHORED rather than as the phase-1 ordinal pass - an unknown second
    voice then raises the ask-fallback instead of a bare ordinal, and a
    remembered person who speaks is re-identified through the normal
    ask/introduction/correction flows. A SPOKEN command also seeds the
    owner's anchor from the stashed utterance, exactly as an introduction
    does - the voice that spoke the command is the owner's by design.

    DISARM is the override-off semantics ("solo mode", "just me now"): the
    durable flip off plus the live mirror, everyone still present marked
    left (the phrase says the room is back to one person; the cap frees and
    the roster chip disappears), and any open "who is speaking?" ask
    resolved - it is moot once solo. Mismatch flags stay: they doubt past
    turns, and going solo answers nothing about those."""
    con = db.connect()
    try:
        chat = con.execute("SELECT * FROM chats WHERE id=?",
                           (chat_id,)).fetchone()
        if not chat:
            return "no_change"
        if direction == COMMAND_ARM:
            if chat["room_mode"]:
                return "no_change"
            db.set_chat_room_mode(con, chat_id, True)
            diarize.set_room_enabled(chat_id, True)
            log.info("room mode ON via command: chat=%s", chat_id)
            _roster_owner(con, chat_id, cfg)
            outcome = "armed_by_command"
        elif direction == COMMAND_DISARM:
            if not chat["room_mode"]:
                return "no_change"
            db.set_chat_room_mode(con, chat_id, False)
            diarize.set_room_enabled(chat_id, False)
            departed = 0
            for row in db.get_room_roster(con, chat_id, present_only=True):
                if db.mark_room_person_left(con, chat_id, row["name"]):
                    departed += 1
            db.resolve_room_flags(con, chat_id, kind="unknown_voice")
            log.info("room mode OFF via command: chat=%s departed=%d",
                     chat_id, departed)
            outcome = "disarmed_by_command"
        else:
            return "no_change"
    finally:
        con.close()
    if outcome == "armed_by_command":
        _seed_owner_anchor(chat_id, cfg)
    # The wake-up bell for the roster chip and the client's room-mode adopt
    # (the phase-2 plumbing). The roster writes above already rang it; this
    # covers the flips that touched no roster row, so a connected client
    # always refetches the snapshot and sees the new mode.
    from . import events
    events.notify_room_update()
    return outcome


def _roster_owner(con, chat_id, cfg):
    """Put the owner on the roster for a command arm - linked to their
    remembered anchors when they exist, anchor-pending otherwise. A command
    names nobody, so the owner is the only person honestly known present;
    anyone else joins by introduction, by voice match, or by answering the
    ask."""
    owner = cfg.get("user_name", "User")
    person = anchors.store().find_by_name(owner)
    pid = person["person_id"] if person else ""
    present = {p["name"].lower()
               for p in db.get_room_roster(con, chat_id, present_only=True)}
    if owner.lower() not in present:
        db.add_room_person(con, chat_id, owner, person_id=pid)
    elif pid:
        db.link_room_person(con, chat_id, owner, pid)
