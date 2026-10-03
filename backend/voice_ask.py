"""The ask about a new voice, answered out loud (#523).

When the voice check hears a session voice nobody knows, with 4 s of clean
speech, it asks once: "Someone new is talking. Who's this?"
(backend/voice_pass.py, diarize._raise_unknown_voice). The ask is a room
flag that points at the turn that raised it. Tapping that turn and picking
a name answers it (routers/room.py). This module answers it from what
someone says, as the merged intent scan hears it (backend/intent.py). While
an ask points at a turn, the scan's prompt says so (intent.ASKING_NOTE),
and nothing else in the prompt changes.

  * "that's Dave", said by someone the app has named, or typed, names the
    voice the ask points at;
  * "I'm Dave", said by the new voice itself, names it the same way;
  * "that's the TV" (a radio, a video, a podcast) says the voice isn't a
    person. It is then ignored for the rest of its session: never named,
    seated, asked about again or learnt from (voice_sessions.media_voice),
    and its turns carry the unresolved reason "media", which the seats
    read as background audio.

A name does what the tap does. The turn the ask points at takes the name,
with the source "introduction", which memory reads as introduced. The
session voice that spoke it is that person for the rest of the session,
and its other unnamed turns take the name (voice_sessions.human_named).
The turn's held audio goes into the person's bank as an introduction,
when it holds one voice. The introduction's own seating
(introductions.apply_scan) runs first, as it always has. When a turn says
both, the TV wins the ask and any name is an ordinary introduction.

THE RULES, pinned in tests/test_voice_ask.py:

  * Only while an ask is open, and only when it points at a turn with one
    voice in it. Otherwise the introduction does what it always did.
  * Exactly one name, after the guards every introduction passes: the
    owner's name or a spelling of it, an AI participant's name, a
    relationship word and a TV are dropped first.
  * Who said it decides what it means (answers_ask). The new voice names
    itself only in words that give the name as its own ("I'm Dave",
    "it's Dave"). Anyone else, a person the app already named or the owner
    typing, names it in any words but the ones only a person says of
    themselves ("that's Dave", "it's Dave", but not "I'm Dave"). A voice
    nobody has named yet might be a second new person, so from one the
    app doesn't guess.
  * The new voice itself saying "that's the TV" is a person pointing at
    one, so it marks nothing.
  * Solo never learns or seats, so nothing here runs in solo.
  * Worker threads only, and content-free logs: ids and yes or no. The
    turn's words are read here and never stored or logged.
"""

import json
import logging
import re

from . import anchors, db, introductions, room_state, voice_sessions

log = logging.getLogger("crossband.voice_ask")

ASK_KIND = "unknown_voice"
ANSWERED = "ask_answered"       # the scan's outcome when a name answered it
MEDIA_IGNORED = "media_ignored"  # and when "that's the TV" did

SAME = "same"                   # the asked-about voice said it
OTHER = "other"                 # someone named, or the owner typing
UNKNOWN = "unknown"             # a voice nobody has named, or no telling

SELF = "self"                   # words only said of yourself: "I'm Dave"
EITHER = "either"               # "it's Dave", "this is Dave": either way
POINTING = "pointing"           # any other words: "that's Dave"

_SELF_RE = r"\b(?:i'?m|i am|my name(?:'s| is)|call me)\s+{n}\b|\b{n}\s+here\b"
_EITHER_RE = r"\b(?:it'?s|this is)\s+{n}\b"


def how_named(name, text) -> str:
    """How the words give `name` (pure): SELF for words a person says only
    of themselves ("I'm Dave", "I am Dave", "my name's Dave", "call me
    Dave", "Dave here"), EITHER for "it's Dave" and "this is Dave", which
    answer "who's this?" from anyone, else POINTING."""
    head = introductions._plain_quotes(text or "")[:600]
    n = re.escape(name or "")
    if name and re.search(_SELF_RE.format(n=n), head, re.IGNORECASE):
        return SELF
    if name and re.search(_EITHER_RE.format(n=n), head, re.IGNORECASE):
        return EITHER
    return POINTING


def answers_ask(named, speaker) -> bool:
    """Does an introduction name the voice the ask points at (pure)?
    `named` is how_named's reading of the words, and `speaker` SAME, OTHER
    or UNKNOWN. The new voice names itself in its own words, and anyone
    else in any words but "I'm Dave", which is about themselves."""
    if speaker == SAME:
        return named in (SELF, EITHER)
    if speaker == OTHER:
        return named in (EITHER, POINTING)
    return False


def _labels(raw):
    try:
        data = json.loads(raw) if raw else {}
    except (TypeError, ValueError):
        return None
    return data if isinstance(data, dict) else None


def single_voice(raw) -> bool:
    """Does a turn's label hold one voice? A crosstalk label, or one that
    doesn't parse, doesn't."""
    data = _labels(raw)
    return data is not None and data.get("crosstalk") is not True


def main_name(raw) -> str:
    """The name a turn's label gives its main voice, when it's sure of it:
    the first label, not uncertain and not a "Voice N". "" otherwise."""
    data = _labels(raw) or {}
    names = [n for n in data.get("labels") or () if isinstance(n, str)
             and n.strip()]
    if not names or names[0] in (data.get("uncertain") or ()) \
            or re.match(r"^Voice \d+$", names[0]):
        return ""
    return names[0]


def speaker_of(chat_id, turn, ask_turn):
    """(who, name): who said `turn`, next to the voice that spoke
    `ask_turn` (both message rows), and the name the app gave that
    speaker, if any. A typed turn is the owner at the keyboard. The voice
    that spoke the asked turn is SAME, found by session voice while the
    open session holds both turns. Any other voice the app named is
    OTHER, and one it didn't name is UNKNOWN."""
    if not turn.get("voice_turn_id"):
        return OTHER, ""
    mine = voice_sessions.voice_of_turn(chat_id, turn["voice_turn_id"])
    theirs = voice_sessions.voice_of_turn(chat_id,
                                          ask_turn.get("voice_turn_id"))
    if mine is not None and mine == theirs:
        return SAME, ""
    name = main_name(turn.get("voice_labels"))
    return (OTHER if name else UNKNOWN), name


def open_ask(chat_id):
    """The chat's open "who's this?" ask as a flag row, or None (worker
    thread). Read before the introduction is applied, which closes it."""
    con = db.connect()
    try:
        asks = [f for f in db.get_room_flags(con, chat_id)
                if f["kind"] == ASK_KIND]
    finally:
        con.close()
    return asks[-1] if asks else None


def points_at_a_turn(ask) -> bool:
    """Is this open ask one a spoken answer can name (pure)? Only one that
    points at a turn: the relationship-only introduction's ask points at
    none."""
    return bool(ask and ask.get("message_id"))


def the_name(verdict, owner, participants, apps=()) -> str:
    """The one name an introduction gives, after the guards every
    introduction passes, or "" when there isn't exactly one (pure)."""
    names = [n for n in verdict.get("introductions") or ()
             if not introductions.owner_alias(n, owner)
             and not introductions.participant_alias(n, participants)
             and not introductions.app_alias(n, apps)
             and not introductions.relationship_noun(n)
             and not introductions.media_noun(n)]
    return names[0] if len(names) == 1 else ""


def _person(store, name, owner):
    """The remembered person a name means, the way the introduction
    resolved it: the same name, a merged one, or a confident spelling of
    one, else a new person under the name."""
    person = store.find_by_name(name)
    if person is None:
        variant, confidence = introductions.variant_of(
            name, store.people(), exclude={owner.casefold()})
        if variant is not None \
                and confidence == introductions.VARIANT_CONFIDENT:
            person = variant
    if person is None:
        store.ensure_person(name)
        person = store.find_by_name(name)
    return person


def _message(con, chat_id, message_id):
    row = con.execute(
        "SELECT id, voice_turn_id, voice_labels FROM messages "
        "WHERE id=? AND chat_id=? AND speaker='user'",
        (message_id, chat_id)).fetchone()
    return dict(row) if row else None


def answer_with_name(chat_id, ask, message_id, verdict, cfg, text="") -> str:
    """Answer an open ask with the one name a turn introduced (worker
    thread). `ask` is the open flag, read before the introduction was
    applied, `message_id` the turn that said the name and `text` its
    words. Returns ANSWERED when the voice the ask points at took the
    name, else ""."""
    if not points_at_a_turn(ask) or not message_id:
        return ""
    owner = (cfg.get("user_name") or "User").strip()
    store = anchors.store()
    con = db.connect()
    try:
        chat = con.execute("SELECT ambient_off FROM chats WHERE id=?",
                           (chat_id,)).fetchone()
        if not chat or chat["ambient_off"]:
            return ""
        name = the_name(verdict, owner, introductions._participant_names(con),
                        introductions.app_names(cfg))
        asked = _message(con, chat_id, ask["message_id"])
        turn = _message(con, chat_id, message_id)
        if not name or not asked or not turn \
                or not single_voice(asked["voice_labels"]):
            return ""
        if (_labels(asked["voice_labels"]) or {}).get("corrected"):
            return ""           # a tap answered it while the model listened
        who, their_name = speaker_of(chat_id, turn, asked)
        if their_name and introductions.name_variant(name, their_name):
            return ""           # someone the app knows, saying their name
        named = how_named(name, text)
        if not answers_ask(named, who):
            return ""
        person = _person(store, name, owner)
        if person is None or introductions.owner_alias(person["name"], owner):
            return ""
        pid, pname = person["person_id"], person["name"]
        old = _labels(asked["voice_labels"]) or {}
        db.set_message_voice_labels(con, asked["id"], {
            "clusters": old.get("clusters") or [voice_sessions.SESSION_SOURCE],
            "labels": [pname], "uncertain": [], "source": "introduction"})
        db.resolve_room_flags(con, chat_id, flag_id=ask["id"])
        db.resolve_room_flags(con, chat_id, message_id=asked["id"])
        room_state.seat(chat_id, pname, cfg, via="introduction",
                        person_id=pid, message_id=message_id,
                        enforce_cap=True, link_existing=True, con=con)
    finally:
        con.close()
    voiced = voice_sessions.human_named(chat_id, asked["voice_turn_id"],
                                        pname, pid, cfg)
    learned = False
    held = anchors.peek_audio(asked["id"])
    if held and held[2] == 1:
        # Peeked, not taken, so a tap on the turn can still correct it.
        learned = store.add_clip(pid, held[0], held[1],
                                 source="introduction", dedupe=True)
        if learned:
            from . import voiceid
            voiceid.audit_banks_if_changed(cfg)
    log.info("new-voice ask answered out loud: chat=%s by=%s words=%s "
             "session_voice=%s learned=%s", chat_id, who, named, voiced,
             learned)
    return ANSWERED


def answer_media(chat_id, ask, message_id, cfg) -> str:
    """Answer an open ask with "that's the TV" (worker thread): the turn the
    ask points at, and every other turn its session voice spoke, carry the
    unresolved reason "media", and the voice is ignored for the rest of the
    session. With no session voice to follow, only that turn is marked.
    Returns MEDIA_IGNORED, or "no_change" when the ask points at no
    single-voice turn or the new voice itself said it."""
    from . import diarize
    if not points_at_a_turn(ask) or not message_id:
        return "no_change"
    con = db.connect()
    try:
        chat = con.execute("SELECT ambient_off FROM chats WHERE id=?",
                           (chat_id,)).fetchone()
        asked = _message(con, chat_id, ask["message_id"])
        turn = _message(con, chat_id, message_id)
        if not chat or chat["ambient_off"] or not asked or not turn \
                or not single_voice(asked["voice_labels"]):
            return "no_change"
        old = _labels(asked["voice_labels"]) or {}
        if old.get("corrected"):
            return "no_change"      # a tap answered it while the model listened
        who, _ = speaker_of(chat_id, turn, asked)
        if who == SAME:
            return "no_change"      # a person pointing at the TV isn't one
        db.set_message_voice_labels(con, asked["id"], diarize.label_payload(
            [], clusters=old.get("clusters") or [voice_sessions.SESSION_SOURCE],
            source=voice_sessions.SESSION_SOURCE,
            unresolved=voice_sessions.MEDIA))
        db.resolve_room_flags(con, chat_id, flag_id=ask["id"])
        db.resolve_room_flags(con, chat_id, message_id=asked["id"])
    finally:
        con.close()
    voiced = voice_sessions.media_voice(chat_id, asked["voice_turn_id"], cfg)
    log.info("new-voice ask answered as a TV: chat=%s by=%s session_voice=%s",
             chat_id, who, voiced)
    return MEDIA_IGNORED


def said_by_media(message_id) -> bool:
    """Was this turn spoken by a voice someone said is a TV (worker
    thread)? Its label carries the unresolved reason "media"."""
    con = db.connect()
    try:
        row = con.execute("SELECT voice_labels FROM messages WHERE id=?",
                          (message_id,)).fetchone()
    finally:
        con.close()
    data = _labels(row["voice_labels"]) if row else None
    return bool(data) and data.get("unresolved") == voice_sessions.MEDIA
