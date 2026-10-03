"""The ask about a new voice, answered out loud (#523).

"Someone new is talking. Who's this?" could only be answered by tapping
the turn. Saying "that's Dave", or the new person saying "I'm Dave", now
does what the tap does: the turn the ask points at takes the name, the
session voice is Dave for the rest of the session and its other unnamed
turns take the name, and the turn's held audio goes into Dave's bank as
an introduction.

What these tests pin, in order:

1. WHO SAID IT DECIDES WHAT IT MEANS. The new voice names itself only in
   its own words ("I'm Dave", "it's Dave"). Someone the app has named, or
   the owner typing, names it with "that's Dave" or "it's Dave", never
   with "I'm Dave", and never with their own name. A voice nobody has
   named might be a second new person, so from it, nothing.
2. THE ANSWER DOES WHAT THE TAP DOES: the label, the voice's other turns,
   the seat and the clip, marked as introduced.
3. THE GUARDS HOLD. Solo never learns or seats. The owner's name, a
   spelling of it and an AI participant's name are never learnt as a
   person. Only a single-voice turn's audio is saved. An ask that points
   at two voices, or at no turn, is left alone. A remembered spelling
   names the remembered person, never a twin.
4. THROUGH THE SCAN. With no ask open an introduction does what it always
   did. With one open, the verdict line says ask_answered, content-free,
   and no "nothing changed" line is posted.
5. "THAT'S THE TV". The voice the ask points at is ignored for the rest of
   the session: its turns say "media", it is never named, never new again
   (so never asked about), never seated or saved, and a turn it speaks
   applies no instruction. The seats read it as background audio, and
   memory files it as an unknown guest. A tap still names it. The new
   voice itself saying it marks nothing, and nor does an ask on two voices.

Keyless. The session is installed by hand and the utility model is
mocked. Synthetic roster: Alex (the owner), Sam, Dave, Mateo.
"""

import asyncio
import json
import logging
import time

import pytest
from fastapi.testclient import TestClient

from backend import anchors, db, introductions, voice_ask, voice_sessions as vss
from backend.app import create_app
from backend.config import Settings
from roomkit import _message_labels, _wait_for, as_utility_completion, loud_pcm

CFG = {"user_name": "Alex", "room_roster_max": 6}
NEW = {"clusters": ["session"], "labels": [], "uncertain": [],
       "source": "session", "unresolved": "new_voice"}
OWNER = {"clusters": ["session"], "labels": ["Alex"], "uncertain": [],
         "source": "session", "owner": True, "score": 0.97}
TWO = {"clusters": ["session"], "labels": ["Alex", "Voice 2"],
       "uncertain": ["Voice 2"], "source": "session", "crosstalk": True}


@pytest.fixture
def app(tmp_path):
    settings = Settings(data_dir=str(tmp_path / "data"),
                        memory_url="http://127.0.0.1:1", user_name="Alex")
    a = create_app(settings)
    vss._reset_for_tests()
    anchors.clear_recent_audio()
    with TestClient(a, base_url="http://127.0.0.1"):
        yield a
    vss._reset_for_tests()
    anchors.clear_recent_audio()


def _chat(solo=False):
    con = db.connect()
    try:
        cur = con.execute(
            "INSERT INTO chats(title, created_at, updated_at, room_mode, "
            "ambient_off) VALUES('t', 0, 0, ?, ?)", (0 if solo else 1,
                                                     1 if solo else 0))
        con.commit()
        return cur.lastrowid
    finally:
        con.close()


def _turn(chat_id, turn_id="", labels=None, audio=None, voices=1):
    """One user turn: spoken when it has a turn id, with its label and the
    audio the voice check held for it."""
    con = db.connect()
    try:
        msg = db.insert_message(con, chat_id, "user", "a turn",
                                voice_turn_id=turn_id, notify=False)
        if labels is not None:
            db.set_message_voice_labels(con, msg["id"], labels)
    finally:
        con.close()
    if audio:
        anchors.remember_audio(msg["id"], loud_pcm(audio), 16000, voices)
    return msg["id"]


def _ask(chat_id, message_id):
    con = db.connect()
    try:
        return db.insert_room_flag(con, chat_id, "unknown_voice",
                                   message_id=message_id)
    finally:
        con.close()


def _session(chat_id, turns):
    """An open voice session in which each turn id was spoken by a voice:
    {turn_id: slot}."""
    with vss._lock:
        vss._sessions[chat_id] = {
            "id": "s1", "base": "http://127.0.0.1:8910", "opened_at": 0.0,
            "last_at": time.time(), "pushed_s": 0.0, "turns": len(turns),
            "voices": {s: {"prints": [], "clean_s": 5.0}
                       for s in set(turns.values())},
            "turn_voice": list(turns.items()), "filled": {}}


def _labels(mid):
    raw = _message_labels(mid)
    return json.loads(raw) if raw else {}


def _open_asks(chat_id):
    con = db.connect()
    try:
        return [f for f in db.get_room_flags(con, chat_id)
                if f["kind"] == "unknown_voice"]
    finally:
        con.close()


def _roster(chat_id):
    con = db.connect()
    try:
        return db.get_room_roster(con, chat_id, present_only=True)
    finally:
        con.close()


def _verdict(name):
    return {"introductions": [name], "departures": [], "aliases": {}}


def _new_voice_asked(chat_id):
    """The new voice (slot 2) spoke two turns, the first raised the ask,
    and the owner (slot 1) spoke one. Returns the two new-voice turns."""
    asked = _turn(chat_id, "t-new", NEW, audio=3.0)
    later = _turn(chat_id, "t-new2", NEW, audio=2.0)
    _ask(chat_id, asked)
    return asked, later


def _answer(chat_id, turn, name, text=None):
    """Answer the open ask with `name`, said in `text` ("That's <name>."
    by default)."""
    text = f"That's {name}." if text is None else text
    return voice_ask.answer_with_name(chat_id, voice_ask.open_ask(chat_id),
                                      turn, _verdict(name), CFG, text)


# ---------- 1. who said it decides what it means ----------

@pytest.mark.parametrize("text,named", [
    ("I'm Dave", voice_ask.SELF),
    ("hi, I am Dave from next door", voice_ask.SELF),
    ("My name’s Dave", voice_ask.SELF),
    ("my name is Dave", voice_ask.SELF),
    ("just call me Dave", voice_ask.SELF),
    ("Dave here, hello", voice_ask.SELF),
    ("It's Dave", voice_ask.EITHER),
    ("oh this is Dave", voice_ask.EITHER),
    ("That's Dave.", voice_ask.POINTING),
    ("that was my brother Dave", voice_ask.POINTING),
    ("Dave's here", voice_ask.POINTING),
    ("I'm Davey", voice_ask.POINTING),        # another name, not Dave
])
def test_how_the_words_give_the_name(text, named):
    assert voice_ask.how_named("Dave", text) == named


@pytest.mark.parametrize("named,speaker,answers", [
    (voice_ask.SELF, voice_ask.SAME, True),         # "I'm Dave", new voice
    (voice_ask.EITHER, voice_ask.SAME, True),       # "it's Dave", new voice
    (voice_ask.POINTING, voice_ask.SAME, False),    # it names someone else
    (voice_ask.POINTING, voice_ask.OTHER, True),    # "that's Dave", owner
    (voice_ask.EITHER, voice_ask.OTHER, True),      # "it's Dave", owner
    (voice_ask.SELF, voice_ask.OTHER, False),       # about themselves
    (voice_ask.SELF, voice_ask.UNKNOWN, False),     # maybe a second new
    (voice_ask.POINTING, voice_ask.UNKNOWN, False),  # person: no guess
])
def test_who_said_it_decides_what_it_means(named, speaker, answers):
    assert voice_ask.answers_ask(named, speaker) is answers


def test_the_name_passes_every_introductions_guards():
    agents = ["claude", "Claude", "gpt", "GPT"]
    assert voice_ask.the_name(_verdict("Dave"), "Alex", agents) == "Dave"
    for name in ("Alex", "Alec", "Claude", "Clyde", "Wife", "my mate"):
        assert voice_ask.the_name(_verdict(name), "Alex", agents) == "", name
    two = {"introductions": ["Dave", "Sam"]}
    assert voice_ask.the_name(two, "Alex", agents) == ""
    # the guards leave the one real name standing
    mixed = {"introductions": ["Claude", "Dave", "Wife"]}
    assert voice_ask.the_name(mixed, "Alex", agents) == "Dave"


def test_the_name_never_answers_with_an_apps_name():
    """#602: "that's Fisher" while the app is asking can't name the voice
    after an app the owner uses, here an invented "kingfisher"."""
    apps = ["kingfisher"]
    assert voice_ask.the_name(_verdict("Fisher"), "Alex", [], apps) == ""
    assert voice_ask.the_name(_verdict("Kingfisher"), "Alex", [], apps) == ""
    assert voice_ask.the_name(_verdict("Dave"), "Alex", [], apps) == "Dave"
    mixed = {"introductions": ["Fisher", "Dave"]}
    assert voice_ask.the_name(mixed, "Alex", [], apps) == "Dave"
    # with no apps configured the name stands, as before
    assert voice_ask.the_name(_verdict("Fisher"), "Alex", []) == "Fisher"


# ---------- 2. the answer does what the tap does ----------

def test_thats_dave_from_the_owner_names_the_new_voice(app):
    chat = _chat()
    asked, later = _new_voice_asked(chat)
    said = _turn(chat, "t-owner", OWNER)
    _session(chat, {"t-new": 2, "t-new2": 2, "t-owner": 1})
    assert _answer(chat, said, "Dave") == voice_ask.ANSWERED
    # the turn the ask points at takes the name, as introduced
    got = _labels(asked)
    assert got["labels"] == ["Dave"] and got["uncertain"] == []
    assert got["source"] == "introduction" and "unresolved" not in got
    from backend.memory_client import speaker_identity
    assert speaker_identity({"voice_labels": got}, "guest:Dave",
                            {})["method"] == "introduced"
    # the voice's other turn takes it too, and the owner's turn keeps its own
    assert _labels(later)["labels"] == ["Dave"]
    assert _labels(said)["labels"] == ["Alex"]
    # the voice is Dave for the rest of the session
    assert vss._sessions[chat]["voices"][2]["human"]["name"] == "Dave"
    # the ask is answered, and Dave is seated and linked
    assert _open_asks(chat) == []
    dave = anchors.store().find_by_name("Dave")
    seat = next(r for r in _roster(chat) if r["name"] == "Dave")
    assert seat["person_id"] == dave["person_id"]
    assert seat["seated_via"] == "introduction"
    # the held audio is Dave's first clip, marked as introduced and vouched
    clips = anchors.store().clips_of(dave["person_id"])
    assert [c["source"] for c in clips] == ["introduction"]
    assert anchors.bank_vouched(anchors.store()._load()["people"][
        dave["person_id"]])
    # peeked, not taken, so a tap can still correct the turn
    assert anchors.peek_audio(asked) is not None


def test_its_dave_from_the_owner_names_it_too(app):
    chat = _chat()
    asked, _ = _new_voice_asked(chat)
    said = _turn(chat, "t-owner", OWNER)
    _session(chat, {"t-new": 2, "t-owner": 1})
    assert _answer(chat, said, "Dave", "Oh, it's Dave.") == voice_ask.ANSWERED
    assert _labels(asked)["labels"] == ["Dave"]


def test_a_typed_answer_names_it_too(app):
    chat = _chat()
    asked, _ = _new_voice_asked(chat)
    typed = _turn(chat)
    _session(chat, {"t-new": 2, "t-new2": 2})
    assert _answer(chat, typed, "Dave") == voice_ask.ANSWERED
    assert _labels(asked)["labels"] == ["Dave"]


@pytest.mark.parametrize("words", ["Hi, I'm Mateo.", "It's Mateo."])
def test_the_new_voice_naming_itself_names_it(app, words):
    chat = _chat()
    asked, _ = _new_voice_asked(chat)
    said = _turn(chat, "t-new3", NEW, audio=2.0)
    _session(chat, {"t-new": 2, "t-new2": 2, "t-new3": 2})
    assert _answer(chat, said, "Mateo", words) == voice_ask.ANSWERED
    assert _labels(asked)["labels"] == ["Mateo"]
    assert _labels(said)["labels"] == ["Mateo"]   # its own turn too


def test_the_new_voice_naming_someone_else_leaves_the_ask(app):
    chat = _chat()
    asked, _ = _new_voice_asked(chat)
    said = _turn(chat, "t-new3", NEW, audio=2.0)
    _session(chat, {"t-new": 2, "t-new2": 2, "t-new3": 2})
    assert _answer(chat, said, "Sam", "That's my friend Sam.") == ""
    assert _labels(asked) == NEW
    assert anchors.store().people() == []


def test_a_named_person_saying_their_own_name_leaves_the_ask(app):
    """Sam, already named, says "I'm Sam" or "this is Sam": she's naming
    herself, not the new voice."""
    chat = _chat()
    asked, _ = _new_voice_asked(chat)
    sam = dict(OWNER, labels=["Sam"], owner=False)
    said = _turn(chat, "t-sam", sam)
    _session(chat, {"t-new": 2, "t-sam": 3})
    assert _answer(chat, said, "Sam", "Hi, this is Sam.") == ""
    assert _answer(chat, said, "Mateo", "I'm Mateo, actually.") == ""
    assert _labels(asked) == NEW


def test_another_unnamed_voice_may_be_a_second_new_person(app):
    """A voice the app hasn't named says "that's Dave" or "I'm Mateo". It
    might be a second new person, so the app doesn't guess."""
    chat = _chat()
    asked, _ = _new_voice_asked(chat)
    said = _turn(chat, "t-other", NEW, audio=2.0)
    _session(chat, {"t-new": 2, "t-new2": 2, "t-other": 3})
    assert _answer(chat, said, "Mateo", "I'm Mateo.") == ""
    assert _answer(chat, said, "Dave") == ""
    assert _labels(asked) == NEW


def test_with_no_session_a_named_speaker_still_answers(app):
    """With no diariser there is no session voice, but a turn named as the
    owner wasn't the new voice. The ask's turn is named and learnt from,
    as a tap would, and a turn nobody named answers nothing."""
    chat = _chat()
    asked, later = _new_voice_asked(chat)
    unnamed = _turn(chat, "t-x", NEW, audio=2.0)
    assert _answer(chat, unnamed, "Dave", "I'm Dave.") == ""
    said = _turn(chat, "t-owner", OWNER)
    assert _answer(chat, said, "Dave") == voice_ask.ANSWERED
    assert _labels(asked)["labels"] == ["Dave"]
    assert _labels(later) == NEW          # no session voice to carry it
    dave = anchors.store().find_by_name("Dave")
    assert len(anchors.store().clips_of(dave["person_id"])) == 1


# ---------- 3. the guards hold ----------

def test_solo_never_learns_or_seats(app):
    chat = _chat(solo=True)
    asked, _ = _new_voice_asked(chat)
    said = _turn(chat, "t-owner", OWNER)
    _session(chat, {"t-new": 2, "t-owner": 1})
    assert _answer(chat, said, "Dave") == ""
    assert _labels(asked) == NEW
    assert anchors.store().people() == [] and _roster(chat) == []


@pytest.mark.parametrize("name", ["Alex", "Alec", "Claude", "Clyde", "Wife"])
def test_the_owner_or_an_assistant_is_never_learnt_as_a_person(app, name):
    chat = _chat()
    asked, _ = _new_voice_asked(chat)
    said = _turn(chat, "t-owner", OWNER)
    _session(chat, {"t-new": 2, "t-owner": 1})
    assert _answer(chat, said, name) == ""
    assert _labels(asked) == NEW
    assert anchors.store().people() == []
    assert len(_open_asks(chat)) == 1


def test_an_ask_on_a_two_voice_turn_is_left_alone(app):
    chat = _chat()
    asked = _turn(chat, "t-two", TWO, audio=3.0, voices=2)
    _ask(chat, asked)
    said = _turn(chat, "t-owner", OWNER)
    _session(chat, {"t-two": 2, "t-owner": 1})
    assert _answer(chat, said, "Dave") == ""
    assert _labels(asked) == TWO
    assert anchors.store().people() == []


def test_only_a_single_voice_turns_audio_is_saved(app):
    """The label reads one voice, but the audio held for the turn had two:
    the name goes on, and nothing is learnt."""
    chat = _chat()
    asked = _turn(chat, "t-new", NEW, audio=3.0, voices=2)
    _ask(chat, asked)
    said = _turn(chat, "t-owner", OWNER)
    _session(chat, {"t-new": 2, "t-owner": 1})
    assert _answer(chat, said, "Dave") == voice_ask.ANSWERED
    assert _labels(asked)["labels"] == ["Dave"]
    dave = anchors.store().find_by_name("Dave")
    assert anchors.store().clips_of(dave["person_id"]) == []


def test_an_ask_that_points_at_no_turn_is_left_alone(app):
    chat = _chat()
    con = db.connect()
    try:
        db.insert_room_flag(con, chat, "unknown_voice")
    finally:
        con.close()
    said = _turn(chat, "t-owner", OWNER)
    assert not voice_ask.points_at_a_turn(voice_ask.open_ask(chat))
    assert _answer(chat, said, "Dave") == ""
    assert voice_ask.answer_with_name(chat, None, said, _verdict("Dave"),
                                      CFG, "That's Dave.") == ""


def test_a_tap_that_got_there_first_is_never_overwritten(app):
    chat = _chat()
    asked, _ = _new_voice_asked(chat)
    ask = voice_ask.open_ask(chat)
    tapped = {"clusters": ["session"], "labels": ["Sam"], "uncertain": [],
              "corrected": True, "source": "correction"}
    con = db.connect()
    try:
        db.set_message_voice_labels(con, asked, tapped)
    finally:
        con.close()
    said = _turn(chat, "t-owner", OWNER)
    _session(chat, {"t-new": 2, "t-owner": 1})
    assert voice_ask.answer_with_name(chat, ask, said, _verdict("Dave"),
                                      CFG, "That's Dave.") == ""
    assert _labels(asked) == tapped


def test_a_remembered_spelling_names_the_remembered_person(app):
    store = anchors.store()
    pid = store.ensure_person("Mateo")
    assert store.add_clip(pid, loud_pcm(2.0), 16000, source="introduction")
    chat = _chat()
    asked, _ = _new_voice_asked(chat)
    said = _turn(chat, "t-owner", OWNER)
    _session(chat, {"t-new": 2, "t-owner": 1})
    assert _answer(chat, said, "Matteo") == voice_ask.ANSWERED
    assert _labels(asked)["labels"] == ["Mateo"]
    assert [p["name"] for p in store.people()] == ["Mateo"]   # no twin
    assert len(store.clips_of(pid)) == 2


# ---------- 4. through the scan ----------

@pytest.fixture
def utility(monkeypatch):
    state = {"verdict": {}, "prompts": []}

    async def fake(prompt, cfg, max_tokens=2000):
        state["prompts"].append(prompt)
        return json.dumps(state["verdict"])

    monkeypatch.setattr("backend.llm_util.utility_complete_with_usage",
                        as_utility_completion(fake))
    return state


def _system_lines(chat_id):
    con = db.connect()
    try:
        return [r["content"] for r in con.execute(
            "SELECT content FROM messages WHERE chat_id=? AND "
            "speaker='system'", (chat_id,))]
    finally:
        con.close()


def _scan(chat_id, message_id, text):
    asyncio.run(introductions.scan_user_turn(chat_id, message_id, text, CFG))


def test_the_model_is_told_only_while_an_ask_points_at_a_turn(app, utility):
    from backend import intent
    chat = _chat()
    said = _turn(chat, "t-owner", OWNER)
    _scan(chat, said, "That's Dave.")
    assert intent.ASKING_NOTE not in utility["prompts"][-1]
    con = db.connect()
    try:
        db.insert_room_flag(con, chat, "unknown_voice")   # points at no turn
    finally:
        con.close()
    _scan(chat, said, "That's Dave.")
    assert intent.ASKING_NOTE not in utility["prompts"][-1]
    asked, _ = _new_voice_asked(chat)
    _scan(chat, said, "That's Dave.")
    assert intent.ASKING_NOTE in utility["prompts"][-1]


def test_with_no_ask_open_an_introduction_does_what_it_always_did(
        app, utility, caplog):
    chat = _chat()
    quiet = _turn(chat, "t-new", NEW, audio=3.0)
    said = _turn(chat, "t-owner", OWNER)
    _session(chat, {"t-new": 2, "t-owner": 1})
    utility["verdict"] = _verdict("Dave")
    with caplog.at_level(logging.INFO, logger="crossband.introductions"):
        _scan(chat, said, "That's Dave.")
    assert [r["name"] for r in _roster(chat)] == ["Dave"]
    assert _labels(quiet) == NEW
    assert any("outcome=roster_grew" in r.getMessage()
               for r in caplog.records)


def test_the_scan_answers_the_ask_and_says_so_content_free(
        app, utility, caplog):
    """Dave is already seated, so the introduction alone changes nothing
    and would post "nothing changed". Answering the ask is the change."""
    chat = _chat()
    con = db.connect()
    try:
        db.add_room_person(con, chat, "Dave")
    finally:
        con.close()
    asked, _ = _new_voice_asked(chat)
    said = _turn(chat, "t-owner", OWNER)
    _session(chat, {"t-new": 2, "t-new2": 2, "t-owner": 1})
    utility["verdict"] = _verdict("Dave")
    with caplog.at_level(logging.INFO):
        _scan(chat, said, "That's Dave.")
    assert _labels(asked)["labels"] == ["Dave"]
    assert _system_lines(chat) == []
    lines = [r.getMessage() for r in caplog.records
             if r.name in ("crossband.introductions", "crossband.voice_ask")]
    assert any("outcome=ask_answered" in m for m in lines)
    assert any("new-voice ask answered out loud" in m for m in lines)
    for m in lines:
        assert "Dave" not in m and "That's" not in m, m
    assert "ask_answered" in introductions.SCAN_OUTCOMES


def test_the_self_introduced_turn_is_learnt_from_too(app, utility):
    """The new voice says "I'm Mateo": the asked turn is named and learnt
    from, and so is the turn that said it, once its label names Mateo."""
    chat = _chat()
    asked, _ = _new_voice_asked(chat)
    said = _turn(chat, "t-new3", NEW, audio=2.5)
    _session(chat, {"t-new": 2, "t-new2": 2, "t-new3": 2})
    utility["verdict"] = _verdict("Mateo")
    _scan(chat, said, "Hi, I'm Mateo.")
    assert _wait_for(lambda: anchors.store().find_by_name("Mateo"))
    pid = anchors.store().find_by_name("Mateo")["person_id"]
    assert _labels(said)["labels"] == ["Mateo"]
    clips = anchors.store().clips_of(pid)
    assert len(clips) == 2
    assert {c["source"] for c in clips} == {"introduction"}


# ---------- 5. "that's the TV" ----------

MEDIA = {"clusters": ["session"], "labels": [], "uncertain": [],
         "source": "session", "unresolved": "media"}


def _tv(chat_id, turn):
    return voice_ask.answer_media(chat_id, voice_ask.open_ask(chat_id), turn,
                                  CFG)


def test_thats_the_tv_ignores_that_voice_for_the_session(app):
    chat = _chat()
    asked, later = _new_voice_asked(chat)
    said = _turn(chat, "t-owner", OWNER)
    _session(chat, {"t-new": 2, "t-new2": 2, "t-owner": 1})
    assert _tv(chat, said) == voice_ask.MEDIA_IGNORED
    assert _labels(asked) == MEDIA and _labels(later) == MEDIA
    assert _labels(said) == OWNER
    assert _open_asks(chat) == []
    assert vss._sessions[chat]["voices"][2][vss.MEDIA] is True
    # nobody is minted, seated or learnt from
    assert anchors.store().people() == [] and _roster(chat) == []


def test_a_tv_voice_is_never_named_and_never_new_again():
    """Its evidence would name it Alex, or mark it new, and it stays a TV.
    The other voice still takes Alex: one person per voice isn't used up."""
    alex, other = [1.0, 0.0], [0.0, 1.0]
    people = {"a": {"name": "Alex", "clips": [alex]}}
    bar = {"threshold": 0.5, "margin": 0.1}
    voices = {1: {"prints": [(alex, 12.0)], "clean_s": 12.0, vss.MEDIA: True},
              2: {"prints": [(other, 12.0)], "clean_s": 12.0, vss.MEDIA: True},
              3: {"prints": [(alex, 3.0)], "clean_s": 3.0}}
    out = vss.name_voices(voices, people, bar)
    assert out[1]["state"] == vss.MEDIA and out[1]["name"] == ""
    assert out[2]["state"] == vss.MEDIA           # would be new, 12 s
    assert out[3]["state"] == "named" and out[3]["name"] == "Alex"


def test_the_calibrated_scorer_keeps_a_tv_a_tv(monkeypatch):
    from backend import voice_calibration as vc
    table = {1: {"a": 0.99}, 2: {"a": 0.01}}
    monkeypatch.setattr(vc, "probability",
                        lambda fp, secs, snap=None: table[fp[vc.SMALL]])
    monkeypatch.setattr(vss, "pooled",
                        lambda prints: prints[0][0] if prints else None)
    voices = {slot: {"prints": [(slot, 9.0)], "prints_eres": [(slot, 9.0)],
                     "clean_s": 9.0, vss.MEDIA: True} for slot in (1, 2)}
    out = vss.name_voices_calibrated(voices, {"a": "Alex"},
                                     {"calibrated": True})
    assert out[1]["state"] == out[2]["state"] == vss.MEDIA


def test_the_end_of_session_pass_keeps_a_tv_a_tv(app, monkeypatch):
    """By the session's end the TV's evidence would name it Alex. The pass
    names every voice once more, and the TV stays a TV on every turn."""
    alex = [1.0, 0.0]
    monkeypatch.setattr(vss, "_live_candidates", lambda chat_id: [])
    monkeypatch.setattr(vss, "bank", lambda *a, **k: {
        "a": {"name": "Alex", "clips": [alex]}})
    monkeypatch.setattr(vss, "_bar", lambda *a, **k: {
        "threshold": 0.5, "margin": 0.1, "source": "t"})
    chat = _chat()
    first = _turn(chat, "t-tv", MEDIA)
    last = _turn(chat, "t-tv2", NEW)     # its label landed before the mark
    sess = {"id": "s1", "opened_at": 0.0, "turns": 2, "filled": {},
            "turn_voice": [("t-tv", 2), ("t-tv2", 2)],
            "voices": {2: {"prints": [(alex, 12.0)], "clean_s": 12.0,
                           vss.MEDIA: True}}}
    row = vss.end_session(chat, sess, CFG, "idle")
    assert row["voices"]["2"]["state"] == vss.MEDIA
    assert row["filled"] == 1
    assert _labels(first) == MEDIA and _labels(last) == MEDIA


def test_a_tap_still_names_a_tv_voice(app):
    chat = _chat()
    asked, later = _new_voice_asked(chat)
    said = _turn(chat, "t-owner", OWNER)
    _session(chat, {"t-new": 2, "t-new2": 2, "t-owner": 1})
    assert _tv(chat, said) == voice_ask.MEDIA_IGNORED
    assert vss.human_named(chat, "t-new", "Dave", "p-dave", CFG)
    voice = vss._sessions[chat]["voices"][2]
    assert vss.MEDIA not in voice and voice["human"]["name"] == "Dave"
    assert _labels(later)["labels"] == ["Dave"]


def test_the_pass_labels_a_tv_turn_and_does_nothing_else():
    from backend import voice_pass
    plan = {"room_on": False, "solo": False, "present": [], "unlearnt": [],
            "owner_known": True, "is_owner": lambda n: n == "Alex"}
    got = {"voice": 2, "state": vss.MEDIA, "name": "", "pid": "",
           "score": 0.99, "prob": 0.99, "human": False, "method": "multi",
           "voice_clean_s": 20.0, "clean_spans": [(0.0, 9.0)],
           "voices_in_turn": 1, "overlap_s": 0.0}
    d = voice_pass.decide(got, plan)
    assert d["labels"] == [] and d["unresolved"] == "media"
    assert d["arm"] is None and d["seat"] is None and d["ask"] is False
    assert not voice_pass.should_bank(got, {})
    assert "media" in __import__("backend.diarize").diarize.DEFER_REASONS


def test_the_seats_hear_background_audio_and_memory_an_unknown_guest():
    from backend import providers
    from backend.memory_client import ingest_speaker
    msg = {"speaker": "user", "voice_turn_id": "t", "created_at": 0,
           "voice_labels": json.dumps(MEDIA)}
    head = providers._user_turn_head(msg, {"user_name": "Alex",
                                           "room_mode": True})
    assert head == providers.MEDIA_HEAD
    assert "not a person" in head and "Alex" not in head
    assert ingest_speaker(msg, owner_name="Alex") == "guest:unknown"


def test_the_new_voice_saying_thats_the_tv_marks_nothing(app):
    chat = _chat()
    asked, _ = _new_voice_asked(chat)
    said = _turn(chat, "t-new3", NEW, audio=2.0)
    _session(chat, {"t-new": 2, "t-new2": 2, "t-new3": 2})
    assert _tv(chat, said) == "no_change"
    assert _labels(asked) == NEW and len(_open_asks(chat)) == 1


def test_a_tv_on_a_two_voice_ask_or_no_ask_changes_nothing(app):
    chat = _chat()
    asked = _turn(chat, "t-two", TWO, audio=3.0, voices=2)
    said = _turn(chat, "t-owner", OWNER)
    _session(chat, {"t-two": 2, "t-owner": 1})
    assert _tv(chat, said) == "no_change"              # no ask open
    _ask(chat, asked)
    assert _tv(chat, said) == "no_change"              # two voices
    assert _labels(asked) == TWO


def test_with_no_session_only_the_asked_turn_is_marked(app):
    chat = _chat()
    asked, later = _new_voice_asked(chat)
    said = _turn(chat, "t-owner", OWNER)
    assert _tv(chat, said) == voice_ask.MEDIA_IGNORED
    assert _labels(asked) == MEDIA and _labels(later) == NEW


def test_a_tv_is_never_introduced_as_a_person(app):
    for name in ("TV", "the TV", "Telly", "just the radio", "a video"):
        assert introductions.media_noun(name), name
    for name in ("Dave", "Tiv", "Radio Dave", "Mateo"):
        assert not introductions.media_noun(name), name
    chat = _chat()
    introductions.apply_scan(chat, {"introductions": ["TV", "the Radio"],
                                    "departures": []}, CFG)
    assert _roster(chat) == [] and _open_asks(chat) == []
    assert voice_ask.the_name({"introductions": ["TV"]}, "Alex", []) == ""


def test_the_scan_answers_the_ask_with_the_tv(app, utility, caplog):
    chat = _chat()
    asked, _ = _new_voice_asked(chat)
    said = _turn(chat, "t-owner", OWNER)
    _session(chat, {"t-new": 2, "t-new2": 2, "t-owner": 1})
    utility["verdict"] = {"media": True, "introductions": ["TV"]}
    with caplog.at_level(logging.INFO):
        _scan(chat, said, "That's the TV.")
    assert _labels(asked) == MEDIA
    assert _roster(chat) == [] and _system_lines(chat) == []
    lines = [r.getMessage() for r in caplog.records
             if r.name in ("crossband.introductions", "crossband.voice_ask")]
    assert any("outcome=media_ignored" in m for m in lines)
    assert any("new-voice ask answered as a TV" in m for m in lines)
    for m in lines:
        assert "TV." not in m, m


def test_a_tv_heard_with_no_ask_open_says_nothing_changed(app, utility):
    chat = _chat()
    said = _turn(chat, "t-owner", OWNER)
    utility["verdict"] = {"media": True}
    _scan(chat, said, "That's the TV.")
    assert _system_lines(chat) == [
        "Heard that a voice is a TV or radio, and nothing changed: the app "
        "wasn't asking about one new voice."]


def test_nothing_a_tv_says_is_an_instruction(app, utility, caplog):
    """A show on the TV says "this is Dave" or "group mode on": once that
    voice is marked, none of it is applied."""
    chat = _chat()
    tv_turn = _turn(chat, "t-tv", MEDIA)
    utility["verdict"] = {"introductions": ["Dave"], "mode_command": "off"}
    with caplog.at_level(logging.INFO, logger="crossband.introductions"):
        _scan(chat, tv_turn, "This is Dave, and it's just me now.")
    assert _roster(chat) == [] and _system_lines(chat) == []
    assert any("outcome=media_turn" in r.getMessage()
               for r in caplog.records)
    assert "media_turn" in introductions.SCAN_OUTCOMES
