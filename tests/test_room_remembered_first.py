"""Remembered-first naming (#28, fourteenth field test): an ARMED room
recognises everyone the store remembers, not just the present roster.

THE FAILURE, live. The store held two fully-sufficient voices - the owner
and a guest learnt earlier the same day - yet every one of the guest's
turns went unnamed. With room mode armed, the check built its candidate
list from the PRESENT ROSTER only, and the guest was never rostered: she
could not be rostered without being recognised, and could not be
recognised without being rostered.

What these tests pin, in order, on the voice check (backend/voice_pass.py)
and the session naming's candidates (backend/voice_sessions.py):

1. THE REGRESSION PIN: a sufficient remembered NON-rostered person
   speaking in an armed room is named AND rostered on that turn - certain
   label, linked present row, no ElevenLabs call - and seated before the
   label lands, so the room state agrees with the name the seats read.
2. ONE CANDIDATE CONSTRUCTION: the naming's candidates are every
   sufficient remembered person (diarize.remembered_candidates), with a
   paused bank kept only for someone already seated.
3. The roster cap still holds: past it the turn is still named, the
   roster simply does not grow. An already-rostered match adds no
   duplicate row.
4. A remembered match answers the open who-is-speaking ask, exactly as a
   naming introduction does.
5. THE FIRST MEETING, BY ELIMINATION: when every OTHER present person is
   learnt and exactly one present person is not, a new voice is named as
   that one person, marked learning, and nothing is saved until someone
   confirms it. Two unlearnt present people offer nobody, and a
   confident match never names by elimination.

Synthetic roster throughout (Alex the owner, Sam, Dave, Mateo), keyless.
The session naming is stood in for by roomkit.fake_naming.
"""

import asyncio
import json
import time

import pytest
from fastapi.testclient import TestClient

from backend import anchors, db, diarize, voice_pass, voice_sessions
from backend.app import create_app
from backend.config import Settings
from roomkit import (_insert_user_message, _remember, fake_naming, loud_pcm,
                     naming_answer)

CFG = {"user_name": "Alex", "room_roster_max": 6}


@pytest.fixture
def app(tmp_path):
    settings = Settings(data_dir=str(tmp_path / "data"),
                        memory_url="http://127.0.0.1:1", user_name="Alex")
    return create_app(settings)


@pytest.fixture
def no_cloud(monkeypatch):
    monkeypatch.setattr(
        "backend.voice.httpx.post",
        lambda *a, **k: pytest.fail("the voice check must make NO EL call"))


@pytest.fixture
def quiet_mismatch(monkeypatch):
    """A named turn schedules the (keyless, no-op) mismatch check; these
    tests drive the pass under asyncio.run, so silence it rather than
    leave a fire-and-forget task behind on a closing loop."""
    monkeypatch.setattr("backend.mismatch.schedule_check",
                        lambda *a, **k: None)


@pytest.fixture
def naming(monkeypatch):
    return fake_naming(monkeypatch)


def _armed_chat(client, roster=()):
    """A chat with room mode on and (name, person_id) roster rows."""
    chat = client.post("/api/chats", json={"participant_ids": []}).json()
    con = db.connect()
    db.set_chat_room_mode(con, chat["id"], True)
    for name, pid in roster:
        db.add_room_person(con, chat["id"], name, person_id=pid)
    con.close()
    return chat


def _labels(msg_id):
    con = db.connect()
    try:
        row = con.execute("SELECT voice_labels FROM messages WHERE id=?",
                          (msg_id,)).fetchone()
        return row["voice_labels"] if row else None
    finally:
        con.close()


def _roster(chat_id):
    con = db.connect()
    try:
        return db.get_room_roster(con, chat_id, present_only=True)
    finally:
        con.close()


def _flags(chat_id):
    con = db.connect()
    try:
        return db.get_room_flags(con, chat_id, open_only=True)
    finally:
        con.close()


def _run(chat_id, turn_id, cfg=CFG, seconds=3.0):
    asyncio.run(voice_pass.run(chat_id, loud_pcm(seconds), 16000,
                               time.time(), diarize.RoomSession(), cfg,
                               turn_id))


# ── 1. the regression pin ───────────────────────────────────────────────────

def test_remembered_non_rostered_person_named_and_rostered_on_first_utterance(
        app, naming, no_cloud, quiet_mismatch, monkeypatch):
    """THE FOURTEENTH-FIELD-TEST PIN. The store remembers Sam (sufficient);
    the armed room's roster holds only the owner. Sam's turn must be named
    "Sam" (certain) and must seat Sam on the roster, linked to their bank,
    and the seat must land before the label does."""
    with TestClient(app, base_url="http://127.0.0.1") as c:
        owner_pid = _remember("Alex")
        sam_pid = _remember("Sam")
        chat = _armed_chat(c, roster=[("Alex", owner_pid)])
        # the naming's candidates hold Sam although Sam isn't seated
        cands = voice_sessions._live_candidates(chat["id"])
        assert "Sam" in [cand["name"] for cand in cands]
        naming["answers"] = [naming_answer(name="Sam", pid=sam_pid)]
        seated_first = []
        real_park = diarize.park_label
        monkeypatch.setattr(diarize, "park_label", lambda tid, payload: (
            seated_first.append(any(p["name"] == "Sam"
                                    for p in _roster(chat["id"]))),
            real_park(tid, payload)))
        msg = _insert_user_message(chat["id"], voice_turn_id="t1")
        _run(chat["id"], "t1")
        data = json.loads(_labels(msg["id"]))
        assert data["labels"] == ["Sam"]
        assert data["uncertain"] == []
        assert data["source"] == "session"
        row = next(p for p in _roster(chat["id"]) if p["name"] == "Sam")
        assert row["person_id"] == sam_pid       # seated AND linked
        assert seated_first == [True]            # before the label parked
        # and the decision was local - the health pulse agrees
        assert diarize.last_decision(chat["id"])["path"] == "local"


# ── 2. one candidate construction ───────────────────────────────────────────

def test_the_naming_candidates_are_every_remembered_person(app):
    """The drift that caused the field failure can not re-open: the
    naming's candidates are exactly remembered_candidates() - every
    sufficient remembered person, seated or not."""
    with TestClient(app, base_url="http://127.0.0.1") as c:
        owner_pid = _remember("Alex")
        _remember("Sam")
        _remember("Dave")
        store = anchors.store()
        store.ensure_person("Mateo")                  # insufficient: excluded
        assert store.find_by_name("Mateo")["sufficient"] is False
        chat = _armed_chat(c, roster=[("Alex", owner_pid)])
        seen = sorted(cand["name"] for cand in
                      voice_sessions._live_candidates(chat["id"]))
        expected = sorted(p["name"] for p in store.people()
                          if p["sufficient"])
        assert seen == expected == sorted(
            cand["name"] for cand in diarize.remembered_candidates())
        assert "Mateo" not in seen


# ── 3. the cap, and idempotence ─────────────────────────────────────────────

def test_past_the_cap_the_turn_is_named_but_the_roster_does_not_grow(
        app, naming, no_cloud, quiet_mismatch):
    with TestClient(app, base_url="http://127.0.0.1") as c:
        owner_pid = _remember("Alex")
        sam_pid = _remember("Sam")
        chat = _armed_chat(c, roster=[("Alex", owner_pid), ("Dave", "")])
        naming["answers"] = [naming_answer(name="Sam", pid=sam_pid)]
        msg = _insert_user_message(chat["id"], voice_turn_id="t1")
        _run(chat["id"], "t1", cfg=dict(CFG, room_roster_max=2))
        # identity is true regardless of the cap: the label attached
        assert json.loads(_labels(msg["id"]))["labels"] == ["Sam"]
        # but the roster held at the cap
        assert sorted(p["name"] for p in _roster(chat["id"])) \
            == ["Alex", "Dave"]


def test_an_already_rostered_match_adds_no_duplicate_row(
        app, naming, no_cloud, quiet_mismatch):
    with TestClient(app, base_url="http://127.0.0.1") as c:
        owner_pid = _remember("Alex")
        sam_pid = _remember("Sam")
        chat = _armed_chat(c, roster=[("Alex", owner_pid), ("Sam", sam_pid)])
        naming["answers"] = [naming_answer(name="Sam", pid=sam_pid)]
        msg = _insert_user_message(chat["id"], voice_turn_id="t1")
        _run(chat["id"], "t1")
        assert json.loads(_labels(msg["id"]))["labels"] == ["Sam"]
        assert [p["name"] for p in _roster(chat["id"])] == ["Alex", "Sam"]


# ── 4. the open ask is answered ─────────────────────────────────────────────

def test_a_remembered_match_answers_the_open_unknown_voice_ask(
        app, naming, no_cloud, quiet_mismatch):
    """The room armed on an unknown voice and asked who is speaking; the
    next turn is confidently named as remembered Sam. Naming them answers
    the ask, exactly as a naming introduction does."""
    with TestClient(app, base_url="http://127.0.0.1") as c:
        owner_pid = _remember("Alex")
        sam_pid = _remember("Sam")
        chat = _armed_chat(c, roster=[("Alex", owner_pid)])
        con = db.connect()
        db.insert_room_flag(con, chat["id"], "unknown_voice")
        con.close()
        naming["answers"] = [naming_answer(name="Sam", pid=sam_pid)]
        _insert_user_message(chat["id"], voice_turn_id="t1")
        _run(chat["id"], "t1")
        assert _flags(chat["id"]) == []


# ── 5. the first meeting, by elimination ────────────────────────────────────

def test_the_plan_offers_the_one_unlearnt_person_beside_a_learnt_owner(app):
    """The owner is present and learnt - and Dave, present with no bank,
    is STILL the one unlearnt person. A second unlearnt person makes two,
    and the rule (voice_pass.decide) then names nobody."""
    with TestClient(app, base_url="http://127.0.0.1") as c:
        owner_pid = _remember("Alex")
        chat = _armed_chat(c, roster=[("Alex", owner_pid), ("Dave", "")])
        assert voice_pass._plan(chat["id"], CFG)["unlearnt"] == ["Dave"]
        con = db.connect()
        db.add_room_person(con, chat["id"], "Mateo")
        con.close()
        assert sorted(voice_pass._plan(chat["id"], CFG)["unlearnt"]) \
            == ["Dave", "Mateo"]


def test_a_new_voice_is_the_one_unlearnt_person_and_nothing_is_saved(
        app, naming, no_cloud, quiet_mismatch):
    """End to end with the owner present and learnt: a voice that matches
    nobody is named Dave by elimination - labelled learning and uncertain,
    no ask - and none of its audio is saved until someone confirms it."""
    with TestClient(app, base_url="http://127.0.0.1") as c:
        owner_pid = _remember("Alex")
        chat = _armed_chat(c, roster=[("Alex", owner_pid), ("Dave", "")])
        naming["answers"] = [naming_answer("new")]
        msg = _insert_user_message(chat["id"], voice_turn_id="t1")
        _run(chat["id"], "t1")
        data = json.loads(_labels(msg["id"]))
        assert data["labels"] == ["Dave"]
        assert data["uncertain"] == ["Dave"]     # honest: still a guess
        assert data["learning"] is True
        dave = anchors.store().find_by_name("Dave")
        assert dave is None or dave["clip_count"] == 0
        assert _flags(chat["id"]) == []


def test_a_confident_match_never_names_by_elimination(
        app, naming, no_cloud, quiet_mismatch):
    """Same room shape, but the voice is confidently remembered Sam: the
    match wins, and Dave is not named."""
    with TestClient(app, base_url="http://127.0.0.1") as c:
        owner_pid = _remember("Alex")
        sam_pid = _remember("Sam")
        chat = _armed_chat(c, roster=[("Alex", owner_pid), ("Dave", "")])
        naming["answers"] = [naming_answer(name="Sam", pid=sam_pid)]
        msg = _insert_user_message(chat["id"], voice_turn_id="t1")
        _run(chat["id"], "t1")
        data = json.loads(_labels(msg["id"]))
        assert data["labels"] == ["Sam"] and not data.get("learning")
        dave = anchors.store().find_by_name("Dave")
        assert dave is None or dave["clip_count"] == 0
