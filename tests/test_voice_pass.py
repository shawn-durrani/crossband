"""The voice check (#482): one pass names every spoken turn.

What these tests pin, in order:

1. THE RULE. `decide` maps the session naming's answer and the chat's room
   state to a label and to what the room does: the owner is labelled and
   never seated; a known guest arms a room that was off and is seated;
   solo labels and does nothing else; a new voice arms and asks only once
   the owner's own voice is known; the first meeting names the one
   unlearnt person, marked learning; anything else is listening.
2. SAVING CLIPS. Only a single-voice turn of a voice named near certain,
   with enough clean speech behind it, and never a voice a person named.
3. WHOLE TURNS. Through `run`, with the session's answer faked and the
   real room state: labels land with source "session", a guest arms and
   seats, a new voice arms and asks, solo changes nothing, a turn no feed
   saw is named on its own, and a sure naming saves one clip and re-runs
   the hygiene audit. A named turn gets the mismatch cross-check. A long
   turn's tail takes the name its pieces had, and a long turn with two
   voices keeps the note without waiting for words (#469).
4. ONE PATH. Every voiced turn goes through this pass, whatever the room
   is doing, and nothing else runs.
5. NAMING. The calibrated scorer names one person per voice at its bar
   and marks a voice new under the new bar, and a name set by hand wins.
6. NO DIARISER. With no diariser configured, the feed stays off and every
   turn is named on its own, end to end.

Keyless and offline. Synthetic roster (Alex the owner, Sam, Dave).
"""

import asyncio
import json

import pytest
from fastapi.testclient import TestClient

from backend import anchors, db, diarize, voice_pass, voice_sessions as vss
from backend.app import create_app
from backend.config import Settings
from roomkit import _insert_user_message, _message_labels
from tests.conftest import speech_pcm

SR = 16000
OWNER = "Alex"
ONLY_CFG = {"user_name": OWNER, "diarize_shadow_url": "http://127.0.0.1:8910"}


def _plan(**kw):
    base = {"room_on": False, "solo": False, "present": [], "unlearnt": [],
            "owner_known": True, "is_owner": lambda n: n == OWNER}
    base.update(kw)
    return base


def _got(state="named", name="Sam", **kw):
    got = {"voice": 1, "state": state, "name": name, "pid": "p-" + name,
           "score": 0.97, "prob": 0.97, "human": False,
           "method": "calibrated", "voice_clean_s": 12.0,
           "clean_spans": [(0.0, 3.0)], "voices_in_turn": 1,
           "overlap_s": 0.0}
    got.update(kw)
    return got


# ---------- 1. the rule ----------

def test_nothing_to_name_is_listening():
    d = voice_pass.decide(None, _plan())
    assert d["labels"] == [] and d["unresolved"] == "listening"
    assert d["arm"] is None and d["seat"] is None and not d["ask"]


def test_the_owner_is_labelled_and_nothing_else_happens():
    d = voice_pass.decide(_got(name=OWNER), _plan())
    assert d["labels"] == [OWNER] and d["owner"] is True
    assert d["arm"] is None and d["seat"] is None


def test_a_known_guest_arms_a_room_that_was_off_and_is_seated():
    d = voice_pass.decide(_got(), _plan(room_on=False))
    assert d["labels"] == ["Sam"] and d["arm"] == "known"
    assert d["seat"] == "Sam"


def test_a_known_guest_in_an_armed_room_is_seated_once():
    d = voice_pass.decide(_got(), _plan(room_on=True, present=[OWNER]))
    assert d["arm"] is None and d["seat"] == "Sam"
    d = voice_pass.decide(_got(), _plan(room_on=True,
                                        present=[OWNER, "sam"]))
    assert d["seat"] is None


def test_solo_labels_and_never_arms_seats_or_asks():
    for got in (_got(), _got("new", "")):
        d = voice_pass.decide(got, _plan(solo=True))
        assert d["arm"] is None and d["seat"] is None and not d["ask"]
    assert voice_pass.decide(_got(), _plan(solo=True))["labels"] == ["Sam"]


def test_a_new_voice_asks_only_once_the_owner_is_known():
    d = voice_pass.decide(_got("new", ""), _plan(room_on=False))
    assert d["unresolved"] == "new_voice" and d["arm"] == "unknown"
    assert d["ask"] is True
    d = voice_pass.decide(_got("new", ""), _plan(room_on=True))
    assert d["arm"] is None and d["ask"] is True
    d = voice_pass.decide(_got("new", ""), _plan(owner_known=False))
    assert d["unresolved"] == "new_voice" and not d["ask"]
    assert d["arm"] is None


def test_the_first_meeting_names_the_one_unlearnt_person_as_learning():
    d = voice_pass.decide(_got("new", ""), _plan(room_on=True,
                                                 unlearnt=["Dave"]))
    assert d["labels"] == ["Dave"] and d["learning"] is True
    assert d["uncertain"] == ["Dave"] and not d["ask"]
    # two unlearnt people: nobody can be named by elimination
    d = voice_pass.decide(_got("new", ""), _plan(room_on=True,
                                                 unlearnt=["Dave", "Sam"]))
    assert d["labels"] == [] and d["unresolved"] == "new_voice"


def test_a_voice_still_listening_is_labelled_listening():
    d = voice_pass.decide(_got("listening", ""), _plan())
    assert d["labels"] == [] and d["unresolved"] == "listening"


# ---------- 2. saving clips ----------

def test_only_a_sure_single_voice_turn_saves_a_clip():
    assert voice_pass.should_bank(_got(prob=0.995)) is True
    assert voice_pass.should_bank(_got(prob=0.98)) is False
    assert voice_pass.should_bank(_got(prob=0.995, voice_clean_s=6.0)) \
        is False
    assert voice_pass.should_bank(_got(prob=0.995, voices_in_turn=2)) \
        is False
    assert voice_pass.should_bank(_got(prob=0.995, overlap_s=0.4)) is False
    assert voice_pass.should_bank(_got(prob=1.0, human=True)) is False
    assert voice_pass.should_bank(_got("new", "")) is False
    # the fallback scorer saves over the banking bar (0.5 + 0.1)
    assert voice_pass.should_bank(_got(method="multi", score=0.65)) is True
    assert voice_pass.should_bank(_got(method="multi", score=0.55)) is False


# ---------- 3. whole turns ----------

@pytest.fixture
def app(tmp_path):
    settings = Settings(data_dir=str(tmp_path / "data"),
                        memory_url="http://127.0.0.1:1", user_name=OWNER)
    a = create_app(settings)
    vss._reset_for_tests()
    yield a
    vss._reset_for_tests()


def _person(name, clips=4, amp=3000):
    store = anchors.store()
    pid = store.ensure_person(name)
    for i in range(clips):
        assert store.add_clip(pid, speech_pcm(2.0, amp=amp + 50 * i), SR,
                              source="introduction")
    return pid


def _chat(client, room_on=False, solo=False):
    chat = client.post("/api/chats", json={"participant_ids": []}).json()
    con = db.connect()
    try:
        db.set_chat_room_state(con, chat["id"], room_mode=bool(room_on),
                               ambient_off=bool(solo))
    finally:
        con.close()
    return chat["id"]


def _run(chat_id, got, monkeypatch, single=None, turn="t1"):
    async def await_turn(turn_id, timeout=vss.LIVE_WAIT_S, step=0.02):
        return got
    monkeypatch.setattr(vss, "await_turn", await_turn)
    monkeypatch.setattr(vss, "name_single_turn",
                        lambda *a, **k: single)
    asyncio.run(voice_pass.run(chat_id, speech_pcm(3.0, amp=4000), SR,
                               db.now(), diarize.RoomSession(),
                               dict(ONLY_CFG), turn))


def _room(chat_id):
    con = db.connect()
    try:
        row = con.execute("SELECT room_mode, ambient_off FROM chats "
                          "WHERE id=?", (chat_id,)).fetchone()
        roster = [r["name"] for r in db.get_room_roster(con, chat_id,
                                                        present_only=True)]
        flags = [f["kind"] for f in db.get_room_flags(con, chat_id)]
    finally:
        con.close()
    return bool(row["room_mode"]), roster, flags


def test_a_known_guest_turn_is_labelled_arms_and_seats(app, monkeypatch):
    with TestClient(app, base_url="http://127.0.0.1") as c:
        _person(OWNER)
        sam = _person("Sam", amp=2000)
        chat = _chat(c)
        m = _insert_user_message(chat, voice_turn_id="t1")
        _run(chat, _got(pid=sam), monkeypatch)
        labels = json.loads(_message_labels(m["id"]))
        room_on, roster, _ = _room(chat)
    assert labels["labels"] == ["Sam"] and labels["source"] == "session"
    assert room_on is True and "Sam" in roster


def test_a_new_voice_arms_the_room_and_asks(app, monkeypatch):
    with TestClient(app, base_url="http://127.0.0.1") as c:
        _person(OWNER)
        chat = _chat(c)
        m = _insert_user_message(chat, voice_turn_id="t1")
        _run(chat, _got("new", "", pid=""), monkeypatch)
        labels = json.loads(_message_labels(m["id"]))
        room_on, _, flags = _room(chat)
    assert labels["labels"] == [] and labels["unresolved"] == "new_voice"
    assert room_on is True and flags == ["unknown_voice"]


def test_solo_labels_the_guest_and_changes_nothing_else(app, monkeypatch):
    with TestClient(app, base_url="http://127.0.0.1") as c:
        _person(OWNER)
        sam = _person("Sam", amp=2000)
        chat = _chat(c, solo=True)
        m = _insert_user_message(chat, voice_turn_id="t1")
        _run(chat, _got(pid=sam), monkeypatch)
        labels = json.loads(_message_labels(m["id"]))
        room_on, roster, flags = _room(chat)
    assert labels["labels"] == ["Sam"]
    assert room_on is False and roster == [] and flags == []


def test_the_owner_is_labelled_with_the_owner_marker(app, monkeypatch):
    with TestClient(app, base_url="http://127.0.0.1") as c:
        alex = _person(OWNER)
        chat = _chat(c)
        m = _insert_user_message(chat, voice_turn_id="t1")
        _run(chat, _got(name=OWNER, pid=alex), monkeypatch)
        labels = json.loads(_message_labels(m["id"]))
        room_on, _, _ = _room(chat)
    assert labels["labels"] == [OWNER] and labels["owner"] is True
    assert room_on is False


def test_a_turn_no_feed_saw_is_named_on_its_own(app, monkeypatch):
    with TestClient(app, base_url="http://127.0.0.1") as c:
        _person(OWNER)
        sam = _person("Sam", amp=2000)
        chat = _chat(c, room_on=True)
        m = _insert_user_message(chat, voice_turn_id="t1")
        _run(chat, None, monkeypatch,
             single=_got(pid=sam, single=True))
        labels = json.loads(_message_labels(m["id"]))
    assert labels["labels"] == ["Sam"]


def test_nothing_named_anywhere_leaves_the_turn_listening(app, monkeypatch):
    with TestClient(app, base_url="http://127.0.0.1") as c:
        _person(OWNER)
        chat = _chat(c, room_on=True)
        m = _insert_user_message(chat, voice_turn_id="t1")
        _run(chat, None, monkeypatch, single=None)
        labels = json.loads(_message_labels(m["id"]))
    assert labels["labels"] == [] and labels["unresolved"] == "listening"


def test_a_sure_naming_saves_one_clip_per_allowance(app, monkeypatch):
    saved, audits = [], []
    monkeypatch.setattr(anchors.AnchorStore, "add_clip",
                        lambda self, pid, pcm, sr, source, score=None, **k:
                        saved.append((pid, source, len(pcm))) or True)
    from backend import voiceid
    monkeypatch.setattr(voiceid, "audit_banks_if_changed",
                        lambda cfg: audits.append(1))
    vss._sessions[99] = {"id": "s1", "voices": {}, "turn_voice": []}
    got = _got(pid="p-Sam", prob=0.995)
    assert voice_pass._bank(99, got, speech_pcm(3.0, amp=3000), SR,
                            ONLY_CFG) is True
    assert saved == [("p-Sam", "accumulated", 3 * SR * 2)]
    assert audits == [1]            # a saved clip re-runs the hygiene audit
    for _ in range(voice_pass.BANK_PER_SESSION):
        voice_pass._bank(99, got, speech_pcm(3.0, amp=3000), SR, ONLY_CFG)
    assert len(saved) == voice_pass.BANK_PER_SESSION


def test_the_fallback_banking_bar_is_the_configured_one():
    """voice_id_banking_extra reaches the fallback scorer's banking bar."""
    got = _got(method="multi", score=0.65)
    assert voice_pass.should_bank(got, {}) is True
    assert voice_pass.should_bank(got, {"voice_id_banking_extra": 0.2}) \
        is False


def test_a_named_turn_gets_the_mismatch_cross_check(app, monkeypatch):
    """The mismatch cross-check (backend/mismatch.py) doubts a name the
    words don't fit, and never changes a label. A named guest's turn gets
    it in every mode, the owner's only while the room is on, and a
    learning, listening or new-voice turn never does."""
    d = voice_pass.decide(_got(), _plan())
    assert voice_pass.cross_checks(d, _plan(), 3.0) is True
    assert voice_pass.cross_checks(d, _plan(), 1.0) is False   # too short
    solo = _plan(solo=True)
    assert voice_pass.cross_checks(voice_pass.decide(_got(), solo), solo,
                                   3.0) is True
    owner = voice_pass.decide(_got(name=OWNER), _plan())
    assert voice_pass.cross_checks(owner, _plan(), 3.0) is False
    assert voice_pass.cross_checks(owner, _plan(room_on=True), 3.0) is True
    for got, plan in ((_got("listening", ""), _plan()),
                      (_got("new", ""), _plan()),
                      (_got("new", ""), _plan(room_on=True,
                                              unlearnt=["Dave"]))):
        d = voice_pass.decide(got, plan)
        assert voice_pass.cross_checks(d, plan, 3.0) is False
    checks = []
    monkeypatch.setattr("backend.mismatch.schedule_check",
                        lambda *a, **k: checks.append(a[1:3]))
    with TestClient(app, base_url="http://127.0.0.1") as c:
        _person(OWNER)
        sam = _person("Sam", amp=2000)
        chat = _chat(c, room_on=True)
        m = _insert_user_message(chat, voice_turn_id="t1")
        _run(chat, _got(pid=sam), monkeypatch)
    assert checks == [(m["id"], "Sam")]


def test_a_long_turns_tail_is_labelled_from_its_pieces(app, monkeypatch):
    """#469 with no feed: the first piece was named Sam on its own, and the
    tail the message carries was too short to name. The message takes the
    name its pieces had, not "still listening"."""
    monkeypatch.setattr(diarize, "ID_ATTACH_WINDOW_SECS", 0.05)
    with TestClient(app, base_url="http://127.0.0.1") as c:
        _person(OWNER)
        sam = _person("Sam", amp=2000)
        chat = _chat(c, room_on=True)
        vss.note_piece("t1")
        vss.note_piece("t2", "t1")
        _run(chat, None, monkeypatch, single=_got(pid=sam, single=True),
             turn="t1")
        m = _insert_user_message(chat, voice_turn_id="t2")
        _run(chat, None, monkeypatch, single=None, turn="t2")
        labels = json.loads(_message_labels(m["id"]))
    assert labels["labels"] == ["Sam"] and "unresolved" not in labels


def test_a_long_turn_with_two_voices_keeps_the_note_without_the_split(
        app, monkeypatch):
    """The feed's answer covers two pieces with a voice each. The label
    lists both and marks the turn as two voices. The words the relay kept
    cover only the last piece, so the pass doesn't wait for them."""
    from backend import crosstalk
    waited = []

    async def await_words(turn_id, timeout=None, step=0.01):
        waited.append(turn_id)
    monkeypatch.setattr(crosstalk, "await_words", await_words)
    voices = {1: {"state": "named", "name": "Sam", "pid": "p-Sam",
                  "score": 0.97, "prob": 0.97, "human": False,
                  "seconds": 10.0, "first": 0.0},
              2: {"state": "named", "name": "Dave", "pid": "p-Dave",
                  "score": 0.96, "prob": 0.96, "human": False,
                  "seconds": 6.0, "first": 10.0}}
    got = _got(voices=voices, voices_in_turn=2, pieces=2, turn_s=16.0,
               spans=[{"slot": 1, "start": 0.0, "end": 10.0,
                       "overlap": False},
                      {"slot": 2, "start": 10.0, "end": 16.0,
                       "overlap": False}])
    with TestClient(app, base_url="http://127.0.0.1") as c:
        chat = _chat(c, room_on=True)
        m = _insert_user_message(chat, voice_turn_id="t1")
        _run(chat, got, monkeypatch)
        labels = json.loads(_message_labels(m["id"]))
    assert labels["crosstalk"] is True and labels["labels"] == ["Sam",
                                                                "Dave"]
    assert "segments" not in labels and waited == []


# ---------- 4. one path ----------

def test_every_voiced_turn_goes_through_this_pass(app, monkeypatch):
    seen = []
    monkeypatch.setattr(voice_pass, "schedule",
                        lambda *a, **k: seen.append(a[-1]) or object())
    for i in range(3):
        diarize.schedule_turn_check(1, b"\x01\x00" * SR, SR,
                                    diarize.RoomSession(), ONLY_CFG,
                                    turn_id=f"t{i}")
    assert seen == ["t0", "t1", "t2"]
    assert all(diarize.turn_checked(f"t{i}") for i in range(3))


def test_the_old_passes_and_switches_are_gone():
    """Retired with #482 item E: the three routes, the early check at
    silence-start, the cloud crosstalk split, the stash and the switches.
    A config file still naming the switches loads fine."""
    for name in ("run_pass", "run_ambient", "schedule_speculative",
                 "check_route", "stash_utterance", "ambient_decision",
                 "set_room_enabled"):
        assert not hasattr(diarize, name), name
    from backend import voice
    assert not hasattr(voice, "transcribe_diarized")
    from backend.config import Settings
    for key in ("voice_session_shadow", "voice_session_labels",
                "voice_session_live", "voice_session_only",
                "voice_shadow_model", "voice_id_pending_extra"):
        assert key not in Settings.model_fields, key


# ---------- 5. naming ----------

def test_the_calibrated_scorer_names_one_person_per_voice(monkeypatch):
    from backend import voice_calibration as vc
    table = {1: {"a": 0.97, "s": 0.02}, 2: {"a": 0.95, "s": 0.40},
             3: {"a": 0.01, "s": 0.03}}
    # pooled fingerprints stand in as the slot number; the fake scorer
    # reads it back to give each voice its row of probabilities
    monkeypatch.setattr(vc, "probability",
                        lambda fp, secs, snap=None: table[fp[vc.SMALL]])
    monkeypatch.setattr(vss, "pooled",
                        lambda prints: prints[0][0] if prints else None)
    voices = {slot: {"prints": [(slot, 5.0)], "prints_eres": [(slot, 5.0)],
                     "clean_s": 5.0} for slot in (1, 2, 3)}
    out = vss.name_voices_calibrated(voices, {"a": OWNER, "s": "Sam"},
                                     {"calibrated": True})
    assert out[1]["name"] == OWNER and out[1]["state"] == "named"
    assert out[2]["state"] == "listening"      # Alex is taken, Sam 0.40
    assert out[3]["state"] == "new"            # under the new bar, 5 s


def test_a_name_set_by_hand_wins(monkeypatch):
    voices = {1: {"prints": [([1.0, 0.0], 5.0)], "clean_s": 5.0,
                  "human": {"name": "Dave", "pid": "d"}}}
    people = {"a": {"name": OWNER, "clips": [[1.0, 0.0]]}}
    out = vss.name_voices(voices, people, {"threshold": 0.5, "margin": 0.1})
    assert out[1]["name"] == "Dave" and out[1]["human"] is True


def test_naming_a_turn_by_hand_names_its_session_voice(app, monkeypatch):
    with TestClient(app, base_url="http://127.0.0.1") as c:
        chat = _chat(c, room_on=True)
        m1 = _insert_user_message(chat, voice_turn_id="t1")
        m2 = _insert_user_message(chat, voice_turn_id="t2")
        con = db.connect()
        try:
            for m in (m1, m2):
                db.set_message_voice_labels(con, m["id"], {
                    "clusters": ["session"], "labels": [], "uncertain": [],
                    "unresolved": "new_voice"})
        finally:
            con.close()
        vss._sessions[chat] = {"id": "s1", "voices": {4: {
            "prints": [], "clean_s": 6.0}}, "turn_voice": [("t1", 4),
                                                          ("t2", 4)],
            "filled": {}}
        assert vss.human_named(chat, "t1", "Dave", "p-dave", ONLY_CFG)
        assert vss._sessions[chat]["voices"][4]["human"]["name"] == "Dave"
        assert json.loads(_message_labels(m2["id"]))["labels"] == ["Dave"]


# ---------- 6. no diariser ----------

def test_with_no_diariser_every_turn_is_named_on_its_own(app, monkeypatch):
    """No `diarize_shadow_url` at all: the feed never starts, the pass
    finds no feed's answer, and the real single-turn naming names the
    whole turn as one voice against the remembered banks. The speaker
    model is faked, everything else is real."""
    sam_vec = [0.0, 1.0, 0.0, 0.0]
    cfg = {"user_name": OWNER}
    monkeypatch.setattr(vss, "embed_live", lambda pcm, sr, c: sam_vec)
    monkeypatch.setattr(vss, "embed_eres_live", lambda pcm, sr, c: None)
    assert not vss.enabled(cfg)
    vss.feed(7, speech_pcm(0.5), SR, cfg)
    vss.end_turn(7, "t1", cfg)
    assert vss._feeds == {} and vss.peek_turn("t1") == (False, False, None)
    with TestClient(app, base_url="http://127.0.0.1") as c:
        _person("Sam", amp=2000)
        chat = _chat(c, room_on=True)
        m = _insert_user_message(chat, voice_turn_id="t1")
        asyncio.run(voice_pass.run(chat, speech_pcm(3.0, amp=4000), SR,
                                   db.now(), diarize.RoomSession(), cfg,
                                   "t1"))
        labels = json.loads(_message_labels(m["id"]))
        _, roster, _ = _room(chat)
    assert labels["labels"] == ["Sam"] and labels["source"] == "session"
    assert "Sam" in roster
