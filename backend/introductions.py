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
import unicodedata

from . import anchors, db, depth, diarize, llm_util

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
# control plumbing the introduction, ambient-arm and PATCH paths share.
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


def build_prompt(text: str, user_name: str, roster_names: list,
                 participant_names: list | None = None) -> str:
    """The confirmation prompt. The model only ever returns names - the
    transcript text stays in the request, never in anything persisted."""
    present = ", ".join(roster_names) if roster_names else "(nobody yet)"
    agents = ", ".join(participant_names or [])
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
        "their name slightly differently."
        + (f" Never return the AI participants ({agents}): they are "
           "software in this chat, not people in the room, and the owner "
           "talks to and about them by name constantly." if agents else "")
        + f"\n\nMessage: {text[:1200]}"
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


def fold_name(name: str) -> str:
    """A name reduced to its comparable core: casefolded, diacritics
    stripped (NFKD-decomposed, combining marks dropped), letters only. The
    normalisation half of the variant-merge rule - a transcriber writing a
    name with or without its accents must compare equal."""
    decomposed = unicodedata.normalize("NFKD", name or "")
    stripped = "".join(c for c in decomposed if not unicodedata.combining(c))
    return re.sub(r"[^a-z]", "", stripped.casefold())


def _edit_distance(a: str, b: str, cap: int = 3) -> int:
    """Levenshtein distance, capped: anything beyond `cap` returns cap + 1.
    Names are short, so the plain dynamic program is cheap; the cap keeps a
    pasted-novel pathological input from mattering."""
    if a == b:
        return 0
    if abs(len(a) - len(b)) > cap:
        return cap + 1
    prev = list(range(len(b) + 1))
    for i, ca in enumerate(a, 1):
        cur = [i]
        best = i
        for j, cb in enumerate(b, 1):
            cost = min(prev[j] + 1, cur[j - 1] + 1,
                       prev[j - 1] + (0 if ca == cb else 1))
            cur.append(cost)
            best = min(best, cost)
        if best > cap:
            return cap + 1
        prev = cur
    return min(prev[-1], cap + 1)


def _within_one_edit(a: str, b: str) -> bool:
    """Levenshtein distance <= 1. Enough for a transcriber's phonetic slip;
    anything further apart is honestly a different name."""
    return _edit_distance(a, b, cap=1) <= 1


# ---- variant merging (#28: naming is law) ----
#
# The seventh-generation defect this closes: an introduction name that is a
# spelling variant of a person we already remember ("Matteo" beside a
# remembered "Mateo") used to mint a TWIN with an empty anchor bank. The rule:
# fold case and diacritics, then take edit distance on the folded forms.
# Distance scales with name length because two edits inside a three-letter
# name is usually a DIFFERENT name, not a variant - that case is "close", and
# close raises a merge question instead of silently folding two people
# together.

VARIANT_CONFIDENT = "confident"
VARIANT_CLOSE = "close"


def name_variant(a: str, b: str) -> str:
    """How alike are two names once folded? VARIANT_CONFIDENT (same person,
    re-identify), VARIANT_CLOSE (plausibly the same - ask, don't guess), or
    '' (different names). Confident: folded-equal, or within one edit at >= 4
    letters, or within two edits at >= 6 letters. Close: within two edits on
    shorter names - where an edit is a big fraction of the name - but only
    when a single edit did it or the names share a first letter; two edits
    from the first letter onward ("Sam" vs "Dan") is a different name, not a
    variant."""
    fa, fb = fold_name(a), fold_name(b)
    if not fa or not fb:
        return ""
    if fa == fb:
        return VARIANT_CONFIDENT
    shortest = min(len(fa), len(fb))
    d = _edit_distance(fa, fb, cap=2)
    if d <= 1 and shortest >= 4:
        return VARIANT_CONFIDENT
    if d <= 2 and shortest >= 6:
        return VARIANT_CONFIDENT
    if d <= 2 and shortest >= 3 and (d <= 1 or fa[0] == fb[0]):
        return VARIANT_CLOSE
    return ""


def variant_of(name: str, people, exclude=frozenset()):
    """The single remembered person `name` is a variant of, as
    (person, confidence). Checks each person's identity name, preferred name
    and merged-away names; a confident hit wins over a close one; TWO
    different people at the same best confidence is ambiguity, which returns
    (None, '') - merging the wrong two people is worse than asking. `people`
    is anchors.store().people(); `exclude` is casefolded names to skip (the
    owner)."""
    excluded = {e.casefold() for e in exclude or ()}
    best = {VARIANT_CONFIDENT: [], VARIANT_CLOSE: []}
    for p in people or []:
        if (p.get("name") or "").casefold() in excluded:
            continue
        candidates = [p.get("name"), p.get("preferred_name")] + list(
            p.get("merged_names") or [])
        verdicts = [name_variant(name, c) for c in candidates if c]
        if VARIANT_CONFIDENT in verdicts:
            best[VARIANT_CONFIDENT].append(p)
        elif VARIANT_CLOSE in verdicts:
            best[VARIANT_CLOSE].append(p)
    for confidence in (VARIANT_CONFIDENT, VARIANT_CLOSE):
        hits = best[confidence]
        if len(hits) == 1:
            return hits[0], confidence
        if len(hits) > 1:
            return None, ""  # ambiguous: never guess between two people
    return None, ""


def owner_alias(name: str, owner: str) -> bool:
    """Is `name` plausibly the OWNER's own name as the transcriber spelt it?
    The owner's roster identity comes from the `user_name` setting, never
    from audio (#28 phase 3, field-test defect 3): a self-introduction
    transcribed as 'Shaun' must not mint a second roster person beside
    'Shawn'. Exact match (ignoring case and punctuation), one edit between
    short names, or - since #56 - ANY verdict the variant rule gives against
    the owner's name. Deliberately the most owner-biased check in the file:
    a genuinely distinct guest wrongly caught here still surfaces later
    through the unknown-voice ask or the manual doors, which are honest
    fallbacks; a phantom owner-double on the roster has no such correction."""
    a, b = _letters(name), _letters(owner)
    if not a or not b:
        return False
    if a == b:
        return True
    if len(a) >= 3 and len(b) >= 3 and _within_one_edit(a, b):
        return True
    # #56: the field case this closes. A transcriber's favourite spelling of
    # the owner's name can sit TWO folded edits away ("Sean" beside "Shawn")
    # and sailed past the one-edit line above, straight onto the roster as a
    # guest. The variant rule already knows how much alikeness is plausible
    # for a name's length; both its verdicts (confident AND close) count as
    # the owner here, because this check's failure modes are asymmetric in
    # exactly the way the docstring states.
    return bool(name_variant(name, owner))


def voice_match_name_compatible(introduced: str, person: dict) -> bool:
    """#81's gate on the introduction voice-arm: may a voice match rebind an
    introduction to this remembered person? Only when the introduced name is
    a plausible spelling of ANY of their names (identity, preferred, merged)
    - both variant verdicts count, because voice AND a near-name agreeing is
    as sure as this system gets. A name that is nobody's spelling means the
    arm's match contradicts the owner's own ears, and silently rebinding is
    exactly how two different humans merge into one record."""
    names = [person.get("name"), person.get("preferred_name")]
    names += list(person.get("merged_names") or [])
    return any(name_variant(introduced, n) for n in names if n)


def participant_alias(name: str, participants) -> bool:
    """Is `name` plausibly an AI PARTICIPANT's name as the transcriber spelt
    it? The #65 boundary: agents are addressed by name in nearly every
    sentence of a voice session, so an introduction-shaped turn ("This is
    Claude...") can hand the seating path a participant's name - and a
    seated participant becomes a pending anchor that by-elimination will
    happily fund with HUMAN audio, minting a remembered "voice" for a piece
    of software. Same treatment per participant as owner_alias gives the
    owner (exact, one edit, the variant rule both verdicts), with the same
    asymmetry argument: a real guest who happens to be caught here still
    surfaces through the unknown-voice ask; a phantom agent seat
    self-reinforces and has no honest correction path."""
    return any(owner_alias(name, p) for p in participants if p)


def _participant_names(con) -> list:
    """Every configured participant's slug and display name, for the #65
    boundary. ALL participants, not just this chat's: an agent that is not
    in the round is still software, never a person in the room."""
    rows = con.execute("SELECT slug, name FROM participants").fetchall()
    out = []
    for r in rows:
        out.extend(n for n in (r["slug"], r["name"]) if n)
    return out


# ---- spoken name corrections (#28: naming is law) ----
#
# A spoken "her name is spelt ..." correction used to do NOTHING - the
# rename UI was the only door. A correction-shaped turn now rides the
# same post-commit fire-and-forget scan: a deterministic prefilter decides
# whether the turn looks like a spelling/naming correction, one cheap
# utility call confirms it and extracts WHO and the corrected NAME, and a
# confirmed correction sets the person's preferred display name OWNER-SET
# (spoken by the owner's session, it carries the same authority as the
# rename UI). Matching is deliberately conservative: the named target must
# resolve to exactly one known person (roster or remembered, exact or
# variant), an unnamed target falls back to the most recent confidently
# labelled speaker, and any ambiguity does nothing rather than guessing.

_CORRECTION_PATTERNS = [
    r"\bname (?:is|'s) (?:actually |really )?spel(?:t|led)\b",
    r"\bname (?:is|'s) (?:actually|really)\b",   # "her name is actually Sam"
    r"\bspel(?:t|led) (?:with|like|as)\b",       # "it's actually spelt with a C"
    r"\bactually spel(?:t|led)\b",
    r"\bspell (?:it|that|her name|his name|their name)\b",
    r"\bnot spel(?:t|led)\b",
    r"\byou(?:'re| are) (?:mis)?spelling\b",
    r"\bcall (?:her|him|them) \w+",              # "call her Sam" ("call me" is
                                                 # an introduction shape)
    r"\bshould be spel(?:t|led)\b",
    # Pronunciation notes (#28, names collapse by voice): "Matteo is the
    # spelling but it's pronounced Mateo" declares two forms of one name -
    # neither of the shapes above matches it, so it died at the prefilter.
    r"\bpronounced\b",
    r"\b(?:is|'s) the spelling\b",
    r"\bthe spelling is\b",
]
_CORRECTION_RE = re.compile("|".join(_CORRECTION_PATTERNS), re.IGNORECASE)

CORRECTION_TARGET_OWNER = "owner"


def correction_prefilter(text: str) -> bool:
    """Is this turn shaped like a naming correction, worth one utility-model
    confirmation? Same posture as the other prefilters: cheap, bounded,
    over-inclusive on purpose - the model is the judge."""
    head = (text or "")[:600]
    if not head.strip():
        return False
    return bool(_CORRECTION_RE.search(head))


def build_correction_prompt(text: str, user_name: str, known_names: list) -> str:
    """The correction-confirmation prompt. Same discipline as the other
    scans: transcript text stays in the request, the model returns only
    names, and nothing from here is persisted or logged."""
    known = ", ".join(known_names) if known_names else "(nobody yet)"
    return (
        "You watch one message from a voice conversation and decide whether "
        "it CORRECTS how a person's name is spelt or what they should be "
        "called: 'her name is spelt Aleks', 'it's actually spelt with a K', "
        "'call her Sam', 'my name is spelt Matteo'. A message may instead "
        "DECLARE two forms of one person's name - a written spelling plus "
        "how it is pronounced or another name they go by: 'Matteo is the "
        "spelling but it's pronounced Mateo'. Merely mentioning or "
        "introducing a name is NOT a correction; neither is correcting "
        "anything other than a person's name.\n"
        f"The device owner is {user_name}. People already known: {known}.\n"
        "Reply with ONLY JSON: {\"corrections\": [{\"who\": ..., \"name\": "
        "..., \"also\": ...}]}. `name` is the corrected name exactly as the "
        "message spells it (assemble it from spelled-out letters when given "
        "letter by letter); for a two-form declaration it is the WRITTEN "
        "spelling. `also` is the second form of a two-form declaration (the "
        "pronunciation, or the other name), else \"\". `who` is the person "
        "being corrected as previously known: one of the known names when "
        "the message says or implies which, "
        f"\"{CORRECTION_TARGET_OWNER}\" when the speaker corrects their own "
        "name ('my name is...'), or \"\" when the message does not say who. "
        "Empty list when the message corrects nobody's name. Never invent "
        "names that are not in the message or the known list.\n\n"
        f"Message: {text[:600]}"
    )


def parse_correction_verdict(text) -> list:
    """Parse the utility model's correction verdict, defensively: anything
    off-shape degrades to [] rather than raising. Returns a bounded list of
    {"who": str, "name": str} with cleaned names; `who` may be '', the
    owner marker, or a cleaned name. A two-form alias declaration (#28,
    names collapse by voice) carries an extra "also" key - the second form
    - included only when it survives cleaning, is not a relationship noun
    and actually differs from the name, so single-form entries keep their
    historical shape exactly."""
    if not text:
        return []
    m = re.search(r"\{.*\}", text, re.DOTALL)
    if not m:
        return []
    try:
        data = json.loads(m.group(0))
    except json.JSONDecodeError:
        return []
    if not isinstance(data, dict):
        return []
    out = []
    for entry in (data.get("corrections")
                  if isinstance(data.get("corrections"), list) else []):
        if not isinstance(entry, dict):
            continue
        name = _clean_name(entry.get("name"))
        if not name or relationship_noun(name):
            continue
        raw_who = entry.get("who")
        if raw_who == CORRECTION_TARGET_OWNER:
            who = CORRECTION_TARGET_OWNER
        else:
            who = _clean_name(raw_who)
        parsed = {"who": who, "name": name}
        also = _clean_name(entry.get("also"))
        if also and not relationship_noun(also) \
                and also.casefold() != name.casefold():
            parsed["also"] = also
        out.append(parsed)
        if len(out) >= MAX_NAMES_PER_TURN:
            break
    return out


def resolve_correction_target(who, owner, people, roster_names,
                              recent_speaker=""):
    """WHO does a confirmed correction rename? Returns the person's identity
    name (the anchor-store key), or '' when honesty says stop.

    - the owner marker, or a `who` that is plausibly the owner's own name:
      the owner's record (identity = the user_name setting);
    - a named `who`: exactly one known person (roster or remembered; exact
      match first, then the variant rule) - two candidates or none is
      ambiguity, and ambiguity does nothing;
    - no `who` at all: the most recent confidently-labelled speaker when the
      caller found one, else nothing. Guessing a rename target would let a
      stray phrase rename the wrong person, which is worse than ignoring a
      real correction (the rename UI still exists)."""
    owner = (owner or "").strip()
    if who == CORRECTION_TARGET_OWNER or (who and owner_alias(who, owner)):
        return owner
    if not who:
        return recent_speaker or ""
    candidates = []
    for p in people or []:
        names = [p.get("name"), p.get("preferred_name")] + list(
            p.get("merged_names") or [])
        if any(n and n.casefold() == who.casefold() for n in names):
            candidates.append(p.get("name"))
    for name in roster_names or []:
        if name.casefold() == who.casefold() \
                and name.casefold() not in {c.casefold() for c in candidates}:
            candidates.append(name)
    if not candidates:
        person, confidence = variant_of(who, people,
                                        exclude={owner.casefold()})
        if person is not None and confidence == VARIANT_CONFIDENT:
            candidates.append(person["name"])
    unique = list(dict.fromkeys(c.casefold() for c in candidates if c))
    if len(unique) != 1:
        return ""
    return next(c for c in candidates if c.casefold() == unique[0])


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
    "name_corrected",       # a spoken correction set a preferred name
    "alias_recorded",       # a two-form declaration ("X is the spelling,
                            # pronounced Y") put both names on one person
    "correction_unmatched", # a correction confirmed, but no unambiguous
                            # target person - nothing changed, by design
    "depth_set",            # spoken reasoning depth set for seat(s) (#105)
    "depth_cleared",        # spoken reasoning depth back to default (#105)
    "depth_once",           # a one-reply depth override parked (#105 slice 2)
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
    if not prefilter(text) and not command_prefilter(text) \
            and not correction_prefilter(text) \
            and not depth.depth_prefilter(text):
        _log_verdict(chat_id, "no_prefilter_match")
        return None
    task = asyncio.get_running_loop().create_task(
        scan_user_turn(chat_id, message_id, text, cfg))
    _TASKS.add(task)
    task.add_done_callback(_TASKS.discard)
    return task


async def scan_user_turn(chat_id, message_id, text, cfg):
    """One user turn's scan: the command confirmation first (#28, chat 198),
    then the introduction confirmation, then the name-correction
    confirmation (#28: naming is law), each gated on its own prefilter so a
    turn normally pays for at most one utility call. A confirmed command's
    outcome wins the verdict line when it changed anything; the rare turn
    that matches two shapes ("group mode on - this is Dave") applies both.
    Every failure ends here (log only)."""
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
            agents = await asyncio.to_thread(_all_participant_names)
            prompt = build_prompt(text, cfg.get("user_name", "User"), roster,
                                  participant_names=agents)
            reply = await llm_util.utility_complete(prompt, cfg,
                                                    max_tokens=200)
            verdict = parse_verdict(reply)
            if verdict["introductions"] or verdict["departures"]:
                intro_outcome = await asyncio.to_thread(apply_scan, chat_id,
                                                        verdict, cfg, text,
                                                        message_id)
                if outcome is None:
                    outcome = intro_outcome
        if correction_prefilter(text):
            # Naming is law (#28): a spoken correction sets the preferred
            # name, owner-set. Its own prefilter + utility call, same
            # fire-and-forget scan, still one verdict line per turn.
            known = await asyncio.to_thread(_correction_known_names, chat_id)
            reply = await llm_util.utility_complete(
                build_correction_prompt(text, cfg.get("user_name", "User"),
                                        known),
                cfg, max_tokens=120)
            corrections = parse_correction_verdict(reply)
            if corrections:
                corr_outcome = await asyncio.to_thread(
                    apply_corrections, chat_id, corrections, cfg)
                if outcome is None or outcome == "no_change":
                    outcome = corr_outcome
        if depth.depth_prefilter(text):
            # Spoken reasoning depth (#105): own prefilter + utility call,
            # same fire-and-forget scan, still one verdict line per turn.
            seats = await asyncio.to_thread(_all_participant_names)
            reply = await llm_util.utility_complete(
                depth.build_depth_prompt(text, seats), cfg, max_tokens=200)
            changes = depth.parse_depth_verdict(reply)
            if changes:
                depth_outcome = await asyncio.to_thread(
                    depth.apply_depth, chat_id, changes, cfg)
                if outcome is None or outcome == "no_change":
                    outcome = depth_outcome
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


def _all_participant_names() -> list:
    """Worker-thread wrapper over _participant_names for the prompt path."""
    con = db.connect()
    try:
        return _participant_names(con)
    finally:
        con.close()


def _correction_known_names(chat_id) -> list:
    """The names a correction may target, for the confirmation prompt:
    everyone present plus everyone remembered, de-duplicated, identity names
    (the store keys a correction resolves to)."""
    names = _present_names(chat_id)
    for p in anchors.store().people():
        if p["name"].casefold() not in {n.casefold() for n in names}:
            names.append(p["name"])
    return names


def _recent_confident_speaker(con, chat_id, owner) -> str:
    """The most recent speaker context for an unnamed correction ("it's
    actually spelt with a K"): the newest user turn whose voice labels carry
    exactly ONE confident non-owner name. Anything else - unlabelled turns
    only, ordinals, uncertainty, crosstalk, several names - returns '', and
    the correction does nothing rather than guess. Bounded read."""
    rows = con.execute(
        "SELECT voice_labels FROM messages WHERE chat_id=? AND speaker='user' "
        "AND voice_labels != '' ORDER BY id DESC LIMIT 10",
        (chat_id,)).fetchall()
    for row in rows:
        try:
            data = json.loads(row["voice_labels"])
        except json.JSONDecodeError:
            continue
        if not isinstance(data, dict) or data.get("crosstalk") is True:
            continue
        labels = [l for l in (data.get("labels")
                              if isinstance(data.get("labels"), list) else [])
                  if isinstance(l, str) and l.strip()]
        uncertain = set(data.get("uncertain")
                        if isinstance(data.get("uncertain"), list) else [])
        named = [l for l in dict.fromkeys(labels)
                 if l not in uncertain and not re.match(r"^Voice \d+$", l)
                 and l.casefold() != (owner or "").casefold()]
        if len(named) == 1 and len(dict.fromkeys(labels)) == 1:
            return named[0]
        return ""  # the newest labelled turn is the context; never dig past it
    return ""


def _resolve_by_declared_names(name, also, owner, people, roster_names):
    """Resolve an alias declaration's target from its own two names (#28,
    names collapse by voice): each form is looked up exactly as a `who`
    would be (owner alias, exact identity/preferred/merged, confident
    variant). Returns the identity name when the forms agree on ONE known
    person, '' when neither form is known (the caller may then fall back to
    the recent speaker), and None when the forms name two DIFFERENT people
    - that is ambiguity, and ambiguity does nothing."""
    hits = []
    for candidate in (name, also):
        got = resolve_correction_target(candidate, owner, people,
                                        roster_names, recent_speaker="")
        if got and got.casefold() not in {h.casefold() for h in hits}:
            hits.append(got)
    if len(hits) > 1:
        return None
    return hits[0] if hits else ""


def apply_corrections(chat_id, corrections, cfg):
    """Apply confirmed name corrections (synchronous; worker thread). For
    each: resolve the target person conservatively (resolve_correction_target
    above), then set their preferred display name OWNER-SET - the spoken
    correction carries the same authority as the rename UI, and locks the
    name against every automated path.

    ALIAS DECLARATIONS (#28, names collapse by voice) are the exception to
    the owner-set stamp: "Matteo is the spelling but it's pronounced Mateo"
    is a statement that BOTH forms are this one person, not a rename fight.
    Both names are recorded as merged names (so find_by_name, keyterms and
    future introductions resolve either), and the declared spelling becomes
    the preferred display name only while no owner-set name exists -
    set_preferred_name with owner_set=False refuses a locked name, so a
    name the owner has already chosen stays exactly as chosen. Returns the
    scan outcome. Content-free logging: counts only, never names."""
    store = anchors.store()
    people = store.people()
    owner = cfg.get("user_name", "User")
    con = db.connect()
    try:
        roster_names = [r["name"] for r in
                        db.get_room_roster(con, chat_id, present_only=True)]
        recent = _recent_confident_speaker(con, chat_id, owner)
    finally:
        con.close()
    applied = aliased = 0
    for corr in corrections:
        target = resolve_correction_target(corr["who"], owner, people,
                                           roster_names, recent_speaker=recent)
        also = corr.get("also") or ""
        if also and not corr["who"]:
            # An unattributed alias declaration: its own two names are
            # stronger evidence than the most recent speaker, so try them
            # first. Two different known people is ambiguity - stop; two
            # unknown forms fall back to the recent-speaker target above.
            by_names = _resolve_by_declared_names(corr["name"], also, owner,
                                                  people, roster_names)
            if by_names is None:
                continue
            if by_names:
                target = by_names
        if not target:
            continue
        person = store.find_by_name(target)
        pid = person["person_id"] if person else store.ensure_person(target)
        if also:
            changed = store.add_merged_name(pid, corr["name"])
            changed = store.add_merged_name(pid, also) or changed
            changed = store.set_preferred_name(pid, corr["name"],
                                               owner_set=False) or changed
            if changed:
                aliased += 1
        elif store.set_preferred_name(pid, corr["name"], owner_set=True):
            applied += 1
    log.info("name corrections applied: chat=%s applied=%d aliases=%d of=%d",
             chat_id, applied, aliased, len(corrections))
    if applied or aliased:
        from . import events
        events.notify_room_update()
        return "name_corrected" if applied else "alias_recorded"
    return "correction_unmatched"


def apply_scan(chat_id, verdict, cfg, text="", message_id=None):
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
    - variant merging (#28: naming is law): an introduction name that is
      confidently a spelling variant of a remembered person (folded case and
      diacritics, an edit or two), or whose utterance voice-matches an
      enrolled non-owner bank, RE-IDENTIFIES that person instead of minting
      a twin; a close-but-not-confident name keeps the new person and
      raises one merge-question flag. A voice-match re-identification also
      RECORDS the introduced spelling on the matched person (#28,
      fourteenth field test - names collapse by voice), so find_by_name,
      keyterms and future introductions resolve the new spelling without
      needing the voice door again.
    - departures: mark the named people left (the cap frees).

    Returns the scan's outcome, one of SCAN_OUTCOMES - the verdict line the
    caller logs. `text` is the utterance (for the remembered-name match only
    - nothing from it is persisted or logged); `message_id` is the persisted
    user turn, which is how the voice-match arm finds the utterance's own
    audio in an ARMED room (#28, fourteenth field test). Content-free
    logging throughout: counts, never names or text."""
    flipped = ask = False
    added = departed = 0
    con = db.connect()
    try:
        chat = con.execute("SELECT * FROM chats WHERE id=?",
                           (chat_id,)).fetchone()
        if not chat:
            return "no_change"
        # Whether the room was armed BEFORE this scan touched anything: it
        # decides where the voice-match arm finds the introduction's audio,
        # and whether the stash may be trusted as the owner's (#28,
        # fourteenth field test - see the seed gate below).
        armed_before = bool(chat["room_mode"])
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
        # The participant boundary (#65): an AI participant's name - however
        # the transcriber spelt it - must never reach the roster. A seated
        # agent becomes a pending anchor, and by-elimination then banks HUMAN
        # audio under it until the phantom is a remembered voice.
        pnames = _participant_names(con)
        before = len(intros)
        intros = [n for n in intros if not participant_alias(n, pnames)]
        if len(intros) < before:
            log.info("participant-alias introduction dropped: chat=%s n=%d",
                     chat_id, before - len(intros))
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
            # An introduction is an explicit owner re-enable: clear any
            # sacred ambient-off so remembered voices resume being noticed.
            db.set_chat_ambient_off(con, chat_id, False)
            diarize.set_ambient_off(chat_id, False)
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
            store = anchors.store()
            people = store.people() if new else []
            # Variant merging's voice arm (#28: naming is law), once per
            # scan: does the introduction utterance's own audio match an
            # enrolled non-owner bank? A confident match means the person
            # speaking IS someone we remember, whatever the transcriber made
            # of their name - re-identify them instead of minting a twin.
            voice_person = _voice_matched_person(
                chat_id, people, cfg, owner, message_id=message_id,
                armed=armed_before) if new else None
            seed_owner = True
            for name in new[:allowed]:
                known = store.find_by_name(name)
                variant = None
                if known is None:
                    # Variants merge instead of duplicating: a name that is
                    # confidently a spelling of a remembered person (case,
                    # diacritics, a transcriber's edit or two) re-identifies
                    # them; a CLOSE-but-not-confident match keeps the new
                    # person but asks the merge question rather than guess.
                    variant, confidence = variant_of(
                        name, people, exclude={owner.casefold()})
                    if variant is not None and confidence == VARIANT_CONFIDENT:
                        known = variant
                        variant = None
                        log.info("introduction variant re-identified a "
                                 "remembered person: chat=%s", chat_id)
                if known is not None:
                    # The introduced name IS remembered. If this very turn's
                    # voice label confidently names a DIFFERENT remembered
                    # person, the label, the seat and the banked audio are
                    # contested (#220) - the words win, and the contradiction
                    # is unwound before the introduction seats anyone.
                    try:
                        _reconcile_intro_label(con, chat_id, message_id,
                                               known, owner, cfg)
                    except Exception:
                        log.info("introduction label reconcile failed: "
                                 "chat=%s", chat_id)
                        log.debug("label reconcile failure detail",
                                  exc_info=True)
                if known is None and voice_person is not None:
                    if voice_match_name_compatible(name, voice_person):
                        known = voice_person
                        variant = None
                        # The stashed utterance is the GUEST's voice, not the
                        # owner's - claiming it as the owner's anchor would be
                        # exactly the cross-contamination the sixth field test
                        # found. Discard it instead.
                        seed_owner = False
                        log.info("introduction voice-matched a remembered "
                                 "person: chat=%s", chat_id)
                        # Names collapse by voice (#28, fourteenth field
                        # test): the introduced spelling matched nobody by
                        # spelling but the voice is this remembered person's
                        # AND the name is plausibly theirs (#81) - record
                        # the spelling on them so keyterms, find_by_name and
                        # future introductions resolve it directly.
                        if store.add_merged_name(known["person_id"], name):
                            log.info("introduced spelling recorded on the "
                                     "voice-matched person: chat=%s", chat_id)
                    else:
                        # #81: the voice arm says "sounds like a remembered
                        # person" while the owner's own ears just heard a
                        # name that is no spelling of theirs. An explicit
                        # introduction outranks an implicit match: seat the
                        # new person under the introduced name (anchor-
                        # pending, like any introduction), surface the
                        # suspicion as the merge question, and never fold
                        # dissimilar names together without the owner. The
                        # contested audio seeds nobody.
                        seed_owner = False
                        _raise_merge_question(con, chat_id, name, voice_person)
                        log.info("introduction voice-match contradicts the "
                                 "introduced name: chat=%s new person "
                                 "seated, merge question raised", chat_id)
                roster_name = known["name"] if known else name
                pid = known["person_id"] if (known and known["sufficient"]) else ""
                alias = aliases.get(name)
                if alias and known is None:
                    # Alias capture at CREATION only: mint the store entry so
                    # the preferred name has somewhere durable to live, and
                    # link the roster row to it. Anchor audio arrives later,
                    # exactly as for any pending person. owner_set=False:
                    # an automated capture must never claim (or take) the
                    # owner's authority over a name.
                    pid = store.ensure_person(name)
                    if store.set_preferred_name(pid, alias, owner_set=False):
                        log.info("alias captured at introduction: chat=%s",
                                 chat_id)
                if variant is not None:
                    _raise_merge_question(con, chat_id, name, variant)
                    log.info("introduction close to a remembered name: "
                             "chat=%s merge question raised", chat_id)
                db.add_room_person(con, chat_id, roster_name, person_id=pid,
                                   seated_by_message_id=message_id,
                                   seated_via="introduction")
            added = min(allowed, len(new))
            log.info("roster grew: chat=%s added=%d", chat_id, added)
            if not seed_owner:
                diarize.take_stashed_utterance(chat_id)  # guest audio: drop it
            if named:
                # An introduction is also the ANSWER to an open "someone new
                # is speaking - who?" ask: naming them closes it. The next
                # utterance then resolves by elimination (one pending name,
                # one unmatched cluster) or asks again if genuinely still
                # ambiguous. An UNNAMED introduction resolves nothing - the
                # ask it just raised must stand.
                db.resolve_room_flags(con, chat_id, kind="unknown_voice")
            if not armed_before:
                # The stash is the ROOM-OFF path's memory of the utterance
                # that spoke the introduction. In a chat that was ALREADY
                # armed, nothing has been stashed since arming - whatever
                # sits there is pre-arm audio whose speaker is unknowable,
                # and the fourteenth field test's log shows exactly that:
                # a guest's pre-arm utterance was banked as the OWNER's
                # anchor because her introduction arrived after ambient had
                # armed the room. Seeding is gated on THIS scan having
                # armed the room, when the stash is plausibly the
                # introduction utterance itself.
                _seed_owner_anchor(chat_id, cfg, message_id=message_id)
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


def _turn_confident_label(con, message_id) -> str:
    """The message's own voice label, when it is a confident SINGLE name:
    exactly one label, nothing uncertain, no crosstalk, not owner-corrected.
    Empty string otherwise - a doubtful label contradicts nothing."""
    if not message_id:
        return ""
    row = con.execute("SELECT voice_labels FROM messages WHERE id=?",
                      (message_id,)).fetchone()
    if not row or not row["voice_labels"]:
        return ""
    try:
        data = json.loads(row["voice_labels"])
    except json.JSONDecodeError:
        return ""
    if not isinstance(data, dict) or data.get("crosstalk") is True \
            or data.get("corrected") is True:
        return ""
    labels = data.get("labels") or []
    if len(labels) != 1 or (data.get("uncertain") or []):
        return ""
    return str(labels[0] or "")


def _reconcile_intro_label(con, chat_id, message_id, known, owner, cfg):
    """An explicit self-introduction outranks an implicit voice match (#220)
    even when BOTH names are remembered people. Field failure 2026-08-24:
    an introduction turn's own audio had already been confidently labelled
    as remembered person B - seating B (voice-match) and banking the
    utterance into B's bank - while the words introduced remembered person
    A. The nobody's-spelling machinery in apply_scan never sees that shape,
    because A resolved by name. Here the words win: relabel the turn to A,
    retract B's automated voice-match seat, retract the clips this
    utterance banked to B, and raise the merge question so the owner learns
    the two banks are colliding. Returns True when a contradiction was
    found and unwound."""
    label = _turn_confident_label(con, message_id)
    if not label or owner_alias(label, owner):
        # No confident label, or the OWNER spoke the introduction ("say hi
        # to Alex") - nothing contradicts.
        return False
    a_names = {known["name"].casefold(),
               (known.get("preferred_name") or "").casefold()} \
        | {m.casefold() for m in known.get("merged_names") or []}
    if label.casefold() in a_names:
        return False   # label and words agree: ordinary re-identification
    store = anchors.store()
    other = store.find_by_name(label)
    if other is None or other["person_id"] == known["person_id"]:
        # The label is not a remembered person's (or the same person under
        # another spelling): the existing variant/voice doors own that.
        return False
    # 1. The turn's label: the introduced name replaces the matched one.
    row = con.execute("SELECT voice_labels FROM messages WHERE id=?",
                      (message_id,)).fetchone()
    try:
        old = json.loads(row["voice_labels"])
    except (TypeError, json.JSONDecodeError):
        old = {}
    db.set_message_voice_labels(con, message_id, {
        "clusters": old.get("clusters") or ["local"],
        "labels": [known["name"]], "uncertain": [],
        "source": old.get("source") or "local"})
    # 2. The seat: only B's AUTOMATED seat retracts. A seat a human placed
    # (introduction, correction, owner) is never unwound by this path.
    seat_retracted = False
    for seat in db.get_room_roster(con, chat_id, present_only=True):
        if seat["name"].casefold() == (other["name"] or "").casefold() \
                and seat.get("seated_via") == "voice-match":
            seat_retracted = db.mark_room_person_left(con, chat_id,
                                                      seat["name"])
    # 3. The contested clips this utterance banked to B, where the audio is
    # still known (single-voice, remembered per message id).
    removed = 0
    entry = anchors.peek_audio(message_id)
    if entry and entry[2] == 1:
        removed = store.retract_utterance_clips(other["person_id"], entry[0])
        if removed:
            from . import voiceid
            voiceid.audit_banks_if_changed(cfg)
    # 4. The owner learns the two banks collide.
    _raise_merge_question(con, chat_id,
                          known.get("preferred_name") or known["name"], other)
    log.info("introduction contradicted the turn's voice label: chat=%s "
             "seat_retracted=%d clips_retracted=%d", chat_id,
             1 if seat_retracted else 0, removed)
    return True


def _raise_merge_question(con, chat_id, new_name, existing_person):
    """The merge question (#28: naming is law): a just-introduced name sits
    close to a remembered person's, but not close enough to fold them
    together silently. Reuses the ask plumbing - one OPEN merge question per
    chat, label = the new name, suspected = the remembered person's display
    name. Dismissing it keeps them separate; renaming one onto the other in
    Remembered voices merges them."""
    open_flags = [f for f in db.get_room_flags(con, chat_id)
                  if f["kind"] == "merge_question"]
    if open_flags:
        return
    display = existing_person.get("preferred_name") or existing_person["name"]
    db.insert_room_flag(con, chat_id, "merge_question", label=new_name,
                        suspected=display)


def _voice_matched_person(chat_id, people, cfg, owner, message_id=None,
                          armed=False):
    """Does the introduction utterance's audio confidently match an enrolled
    NON-owner voice? Runs the local matcher against every sufficient person
    and returns the matched person dict or None. Every doubt is None: this
    is an extra door to re-identification, never a required one. Runs on
    the scan's worker thread.

    WHERE THE AUDIO COMES FROM (#28, fourteenth field test). Room off: the
    stashed utterance, peeked, never consumed - the owner-anchor seed path
    still owns that decision. ARMED room: nothing is stashed once room mode
    is on, so any stash is stale pre-arm audio that did NOT speak this
    introduction - that night the scan judged the wrong utterance and the
    match was missed. The label passes remember each labelled utterance's
    audio per message id, and that is the audio that actually spoke the
    introduction; a multi-voice utterance is trusted for nobody."""
    from . import voiceid
    if not voiceid.enabled(cfg):
        return None
    if armed:
        entry = anchors.peek_audio(message_id) if message_id else None
        if not entry or entry[2] != 1:
            return None
        pcm, sample_rate = entry[0], entry[1]
    else:
        stashed = diarize.peek_stashed_utterance(chat_id)
        if not stashed:
            return None
        pcm, sample_rate = stashed
    candidates = [{"person_id": p["person_id"], "name": p["name"]}
                  for p in people if p.get("sufficient")]
    if not candidates:
        return None
    try:
        verdict = voiceid.identify_utterance(pcm, sample_rate, candidates, cfg)
    except Exception:
        log.debug("introduction voice-match failed", exc_info=True)
        return None
    if not voiceid.matched(verdict) \
            or (verdict.get("name") or "").casefold() == (owner or "").casefold():
        return None
    return next((p for p in people
                 if p["person_id"] == verdict["person_id"]), None)


def _seed_owner_anchor(chat_id, cfg, message_id=None):
    """The owner's anchor comes from the introduction utterance: the relay
    stashes each finished utterance while room mode is off, and the voice
    that spoke the introduction is the owner's by design. Quietly a no-op
    when there is no live voice session (a TYPED introduction has no audio).
    `message_id` (#84): the triggering message when the caller has one (the
    introduction scan does; a command arm names no message)."""
    stashed = diarize.take_stashed_utterance(chat_id)
    if not stashed:
        return
    pcm, sample_rate = stashed
    owner = cfg.get("user_name", "User")
    store = anchors.store()
    pid = store.ensure_person(owner)
    if store.add_clip(pid, pcm, sample_rate, source="introduction"):
        log.info("owner anchor seeded from introduction: chat=%s", chat_id)
        # Every bank change re-runs the pairwise hygiene audit (#28 PR-B).
        from . import voiceid
        voiceid.audit_banks_if_changed(cfg)
    con = db.connect()
    try:
        # The owner rides the roster too - the "In the room" chip should show
        # everyone the diarizer is being asked to tell apart.
        present = {p["name"].lower()
                   for p in db.get_room_roster(con, chat_id, present_only=True)}
        if owner.lower() not in present:
            db.add_room_person(con, chat_id, owner, person_id=pid,
                               seated_by_message_id=message_id,
                               seated_via="owner")
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
            # An arm command is an explicit re-enable: clear any sacred
            # ambient-off, even if room mode is already on.
            db.set_chat_ambient_off(con, chat_id, False)
            diarize.set_ambient_off(chat_id, False)
            if chat["room_mode"]:
                return "no_change"
            db.set_chat_room_mode(con, chat_id, True)
            diarize.set_room_enabled(chat_id, True)
            log.info("room mode ON via command: chat=%s", chat_id)
            _roster_owner(con, chat_id, cfg)
            outcome = "armed_by_command"
        elif direction == COMMAND_DISARM:
            # "Solo mode" is the SACRED disarm: it always sets ambient-off so
            # automatic arming stops until an explicit re-enable, even when
            # room mode was already off (nothing had armed yet, but the owner
            # is stating a preference for privacy).
            db.set_chat_ambient_off(con, chat_id, True)
            diarize.set_ambient_off(chat_id, True)
            if not chat["room_mode"]:
                log.info("ambient off via command (room already off): chat=%s",
                         chat_id)
                return "disarmed_by_command"
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
        db.add_room_person(con, chat_id, owner, person_id=pid,
                           seated_via="owner")
    elif pid:
        db.link_room_person(con, chat_id, owner, pid)


def roster_owner_only(chat_id, cfg):
    """Connection-owning wrapper over _roster_owner, for callers on a worker
    thread that hold no connection (diarize's ambient-unknown arm)."""
    con = db.connect()
    try:
        _roster_owner(con, chat_id, cfg)
    finally:
        con.close()
