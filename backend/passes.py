"""The sanctioned pass (#98): a structural outlet for "stay silent when
redundant".

The July diagnosis, both standing agents on the record: prompted to stay
silent when redundant AND to be helpful, models invent angles instead of
obeying the silence rule. No wording fixes that tension - passing has to
be a real, cheap, honourable move the APP enforces.

Owner decisions (2026-08-14, on the issue): a pass is INVISIBLE - nothing
persisted, nothing spoken, no transcript marker - and a model selected
FIRST to respond to a direct user question may not pass, so every direct
question gets at least one substantive answer. Same spirit, one addition:
a seat addressed by name may not pass either - being summoned IS the
demand for an answer (addressing detection lives in engine.py, reusing
pick_responders' own mention and vocative rules - one summoning grammar,
never two).

A pass with words in front of it (#460): the 25 September field test
stored "<nothing to add>  [pass]" and "<the room asked me to stay quiet>,
passing.  [pass]" as real turns, token and all. A reply that ENDS with
the token (trailing punctuation aside) is judged by what comes before it.
A short remark that says the seat has nothing to add or is staying quiet
makes it the pass it announced; anything else is a real reply, kept
without the token. "Says" is a fixed rule, never a model call: one clause
holds a quiet phrase, the remark is at most QUIET_MAX_CHARS long, and it
holds no question, no number and no "but" (any of those means the seat
had something to say). The field remarks mixed the quiet phrase with
other words ("you two carry on with the plan, passing"), so the rule
does not ask every word to be a quiet one.

The length cap sits under the ElevenLabs first chunk (backend/voice.py
tts_init_message): TTS makes no audio until it holds that many
characters or is flushed. The voice holds a reply's text while it could
still become a pass, and lets it go once it is longer than a quiet remark
can be, which is before TTS could have started on it. So the hold costs
no time to first audio, and a reply that turns out to be a pass never
reached TTS at all. frontend/src/voicePassGate.test.js measures that.
frontend/src/passView.js applies the same rule, pinned through
tests/fixtures/backend_contract.json, so the screen and the voice agree
with what the engine stores.

The question half of the guard asks for a second look (26 September
field test). The engine's only test for a direct question is a question
mark in the newest turn. After the owner asked the seats to stay quiet,
two people in the room asked each other things all evening, and the
first seat's pass was refused on each of those turns. The old retry note,
under a "you must answer" heading, said the owner had asked a direct
question and a substantive reply was owed. One seat trusted it: with no
such question in the newest turn, it went back and answered one it and
the other seat had answered minutes earlier, or said out loud that nobody
had asked it anything. The other seat passed again, which the engine
already accepts. The retry now says what the engine saw, a question mark,
under a neutral heading, and lets a second pass stand. A seat named in
the turn still gets the firm note. Replayed on made-up turns, the seat
that spoke under the old note passed under the new one, and still
answered a real question and a turn that named it. When it did speak
after the new note, it said the turn wasn't for it ("not a question for
me") and then wrote the token, so a remark saying so is a quiet remark
too.

Pure module: the engine consumes it, tests drive it without I/O.
"""
import re

PASS_TOKEN = "[pass]"

# The retry notes the guard states. They reach the seat as trusted app
# context, which the system prompt tells it never to doubt, so each one says
# only what the engine actually checked. Naming is checked, so the addressed
# note is firm. A direct question is not: the engine sees a question mark
# in the newest turn and nothing more, so the question note asks for a
# second look and lets a second [pass] stand.
ADDRESSED_NOTE = (
    "Your [pass] was refused: the newest turn names you, so you owe it an "
    "answer. Answer that turn now, as briefly as you like, but with actual "
    "content."
)
QUESTION_NOTE = (
    "One check before your [pass] stands. The newest turn has a question "
    "mark in it and you're first to reply, and a question put to the AI "
    "members is owed at least one real answer. Read the newest turn alone, "
    "and never go back to answer an earlier question instead. If it asks "
    "you or the other AI members something, answer it now, briefly, with "
    "actual content. If it doesn't (people in the room talking to each "
    "other, someone thinking out loud, or chatter while you've been asked "
    "to stay quiet), your pass stands: reply with exactly [pass] and "
    "nothing else. Whatever you write goes to the room, not to the app, so "
    "never report this check or say why you're passing. If you've been "
    "asked to stay quiet and aren't sure the turn is for you, it isn't."
)


def guard_note(addressed: bool) -> str:
    """The note a refused pass's one retry states: firm for a seat named in
    the newest turn, a second look for a first responder to a question."""
    return ADDRESSED_NOTE if addressed else QUESTION_NOTE


# What a quiet remark says, one phrase at a time. Matched against a
# normalised clause (lowercase, straight apostrophes, words joined by single
# spaces). Plain regex syntax only: passView.js compiles the same string,
# and the contract fixture pins the two equal.
QUIET_PHRASE_PATTERN = (
    r"\b(?:"
    r"nothing (?:(?:new|more|further|else|much|useful|really) )*"
    r"(?:to (?:add|say|contribute)|from me|from my end|on my end|"
    r"on my side|here)"
    r"|nothing(?: (?:new|more|further|else))?$"
    r"|(?:don't|do not|didn't|haven't|have not|not) (?:have |got |see )?"
    r"(?:anything|much|a lot)(?: (?:new|more|further|else))? "
    r"to (?:add|say|contribute)"
    r"|no (?:(?:further|more|new) )?(?:input|comments?|thoughts|additions)"
    r"|(?:stay|stays|staying|stayed|keep|keeps|keeping|remain|remaining|"
    r"be|being|go|going) (?:quiet|quietly|silent)"
    r"|(?:quiet|silent|listening|eavesdropping|lurking) mode"
    r"|(?:hold|holds|holding|held) (?:back|off|fire)"
    r"|(?:sit|sits|sitting) (?:this|that) (?:one )?out"
    r"|stay(?:ing)? out of (?:it|this|the way)"
    r"|passing"
    r"|pass(?=$| for now| on this| this (?:one|time|round|turn))"
    r"|listening(?=$| in| quietly| for now| along)"
    r"|eavesdropping|lurking|standing by"
    r"|(?:leave|let) (?:you|you two|you both|you all|the two of you|"
    r"y'all) (?:to it|carry on|chat|talk|continue|get on with it)"
    r"|(?:not|wasn't|was not|isn't|is not) "
    r"(?:(?:a question|one) (?:for|to)|aimed at|addressed to|"
    r"meant for|directed at) (?:me|us)"
    r"|(?:not|wasn't|isn't|nobody|no one|nobody's|no one's) "
    r"(?:asking|addressing|asked|addressed) (?:me|us)"
    r"|no question (?:for|aimed at|to) (?:me|us)"
    r")\b"
)
QUIET_PHRASE = re.compile(QUIET_PHRASE_PATTERN)

# A quiet remark is short, and shorter than the TTS first chunk (see
# above), with room for whitespace the TTS buffer counts.
QUIET_MAX_CHARS = 100
# A remark that turns ("but") had something to say.
QUIET_CONTRAST = frozenset({"but", "though", "although", "however",
                            "except"})

_WORD_RE = re.compile(r"[a-z0-9']+")
_DIGIT_RE = re.compile(r"[0-9]")
# Clause breaks: sentence and clause punctuation, and a dash.
CLAUSE_BREAK_PATTERN = r"[.,;:!\u2026\n]+|\s[-\u2013\u2014]\s|[\u2013\u2014]"
_CLAUSE_RE = re.compile(CLAUSE_BREAK_PATTERN)
# What a model may put after the token ("[pass].", "*[pass]*").
AFTER_TOKEN = ".!*_~`\"')\u2026"


def _norm(text):
    return (text or "").lower().replace("\u2019", "'").replace("\u2018", "'")


def _words(text):
    """Lowercased words, straight apostrophes, quotes trimmed off the ends.
    passView.js wordsOf is the same rule."""
    return [w for w in (m.strip("'") for m in _WORD_RE.findall(_norm(text)))
            if w]


def _clauses(text):
    """The remark's clauses, each as its words joined by single spaces."""
    return [" ".join(_words(c)) for c in _CLAUSE_RE.split(_norm(text))]


def _barred(text, words):
    """A question, a number or a turn anywhere in the remark."""
    return ("?" in text or _DIGIT_RE.search(text) is not None
            or any(w in QUIET_CONTRAST for w in words))


def is_quiet_remark(text: str) -> bool:
    """A short remark that says the seat has nothing to add or is staying
    quiet, or no words at all ("…"). The test a remark before a trailing
    [pass] must meet for the reply to be the pass it announced."""
    text = text or ""
    words = _words(text)
    if not words:
        return True
    if len(text.strip()) > QUIET_MAX_CHARS or _barred(text, words):
        return False
    return any(QUIET_PHRASE.search(c) for c in _clauses(text) if c)


def _split_tail(text, partial):
    """(before, tail?): the reply with a trailing pass token taken off,
    whitespace, case and trailing punctuation aside. With `partial`, a
    trailing START of the token ("[", "[p" ... "[pass") counts too: a reply
    cut short there was on its way to writing it."""
    s = (text or "").rstrip()
    low = s.lower()
    bare = low.rstrip(AFTER_TOKEN + " \t\n")
    if bare.endswith(PASS_TOKEN):
        return s[:len(bare) - len(PASS_TOKEN)], True
    if partial:
        for n in range(len(PASS_TOKEN) - 1, 0, -1):
            if low.endswith(PASS_TOKEN[:n]):
                return s[:-n], True
    return s, False


def is_pass(text: str) -> bool:
    """A finished reply that is a pass: a bare [pass], surrounding
    whitespace and case aside, or [pass] closing a remark that only says
    the seat has nothing to add or is staying quiet (#460). A reply that
    merely CONTAINS the token, or ends with it after real words, chose to
    speak."""
    before, tail = _split_tail(text, partial=False)
    return tail and is_quiet_remark(before)


def is_cut_pass(text: str) -> bool:
    """A reply that stopped early - a barge-in, a stall, a provider error -
    while its text could still have become a pass: "[", "[p" ... "[pass]",
    whitespace and case aside (#456), or the same after a quiet remark
    ("Nothing to add. [pa", #460). It is judged as the pass it was on its
    way to being: nothing persisted, shown, spoken or ingested. Only a
    reply that did NOT finish asks this; a finished reply is judged by
    is_pass alone, so a whole reply of "[p" still speaks."""
    if not (text or "").strip():
        return False
    before, tail = _split_tail(text, partial=True)
    return tail and is_quiet_remark(before)


def strip_pass(text: str, partial: bool = False) -> str:
    """A real reply with a trailing [pass] taken off (#460): the words are
    kept and the token never reaches the chat, the voice or memory. With
    `partial`, a trailing start of the token goes too (a reply cut off
    mid-token). Text with nothing before the token comes back unchanged,
    so a tool turn whose only text is [pass] keeps its row as before."""
    before, tail = _split_tail(text, partial=partial)
    if tail and before.strip():
        return before.rstrip()
    return text


def is_direct_question(text: str) -> bool:
    """The guard trigger: the user's turn asks something."""
    return "?" in (text or "")


def may_pass(idx: int, addressed: bool, user_text: str) -> bool:
    """The guard (#98): first responder to a direct question, or any
    explicitly-addressed seat, may not pass."""
    if addressed:
        return False
    return not (idx == 0 and is_direct_question(user_text))
