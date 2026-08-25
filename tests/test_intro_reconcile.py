"""A self-introduction reconciles with its own turn's voice label (#220).

Field failure 2026-08-24: an introduction turn's audio had already been
confidently labelled as a DIFFERENT remembered person - who was seated
(voice-match) and whose bank banked the utterance - while the words
introduced a remembered name. The nobody's-spelling contradiction machinery
never sees remembered-A-said versus remembered-B-matched, so the wrong seat
and the contested clip survived. These tests prove, in order:

1. The contradiction unwinds: the turn relabels to the introduced person,
   the wrong voice-match seat retracts, the contested clips leave the
   wrongly-fed bank, and the merge question is raised.
2. The guards: an agreeing label reconciles nothing; an owner-spoken
   introduction reconciles nothing; an owner-corrected label is law; a seat
   a human placed is never unwound (the clips still retract).
3. The clip retraction is surgical: only this utterance's automated
   captures go - other clips, and human-backed clips, stay.

All synthetic fixtures; the utility model and the matcher are never called
(apply_scan is exercised directly, voice_id disabled for hermeticity).
"""

import json

import pytest
from fastapi.testclient import TestClient

from backend import anchors, db, diarize, introductions
from backend.app import create_app
from backend.config import Settings
from tests.conftest import speech_pcm


@pytest.fixture
def world(tmp_path):
    """An armed chat where remembered Dave was wrongly matched: Dave seated
    via voice-match, the turn labelled Dave, the utterance banked to Dave -
    and the words are about to introduce remembered Sam."""
    diarize._ROOM_ENABLED.clear()
    diarize._STASHED.clear()
    anchors.clear_recent_audio()
    settings = Settings(data_dir=str(tmp_path / "data"),
                        memory_url="http://127.0.0.1:1", user_name="Alex")
    app = create_app(settings)
    cfg = settings.as_cfg()
    cfg["voice_id_enabled"] = False   # nothing here may need the matcher
    store = anchors.store()
    sam = store.ensure_person("Sam")
    dave = store.ensure_person("Dave")
    # Speech-shaped since #218 (the clip gate rejects non-speech); a distinct
    # amplitude per source keeps every clip's bytes distinguishable, which is
    # what the contested-clip containment check reads.
    for pid, amp in ((sam, 9000), (dave, 7000)):
        store.add_clip(pid, speech_pcm(2.0, 16000, amp=amp), 16000,
                       source="introduction")
    utterance = speech_pcm(3.0, 16000, amp=11000)   # distinct from every clip
    with TestClient(app, base_url="http://127.0.0.1") as c:
        chat_id = c.post("/api/chats", json={"participant_ids": []}).json()["id"]
    con = db.connect()
    try:
        db.set_chat_room_mode(con, chat_id, True)
        message_id = db.insert_message(
            con, chat_id, "user", "this is Sam",
            voice_labels={"clusters": ["local"], "labels": ["Dave"],
                          "uncertain": [], "source": "local",
                          "score": 0.51})["id"]
        db.add_room_person(con, chat_id, "Dave", person_id=dave,
                           seated_via="voice-match")
    finally:
        con.close()
    # What _accumulate_fast_anchor did with the mis-matched utterance.
    assert store.add_clip(dave, utterance, 16000, source="accumulated")
    anchors.remember_audio(message_id, utterance, 16000, 1)
    return {"app": app, "cfg": cfg, "chat_id": chat_id,
            "message_id": message_id, "store": store, "sam": sam,
            "dave": dave, "utterance": utterance}


def _labels(message_id):
    con = db.connect()
    try:
        row = con.execute("SELECT voice_labels FROM messages WHERE id=?",
                          (message_id,)).fetchone()
        return json.loads(row["voice_labels"]) if row["voice_labels"] else {}
    finally:
        con.close()


def _present(chat_id):
    con = db.connect()
    try:
        return {p["name"] for p in db.get_room_roster(con, chat_id,
                                                      present_only=True)}
    finally:
        con.close()


def _flags(chat_id, kind):
    con = db.connect()
    try:
        return [f for f in db.get_room_flags(con, chat_id)
                if f["kind"] == kind]
    finally:
        con.close()


def _scan(world, intros=("Sam",)):
    return introductions.apply_scan(
        world["chat_id"], {"introductions": list(intros), "departures": []},
        world["cfg"], text="this is Sam", message_id=world["message_id"])


def test_contradiction_unwinds_seat_label_and_clips(world):
    _scan(world)
    # the words win: the turn now says Sam, confidently
    labels = _labels(world["message_id"])
    assert labels["labels"] == ["Sam"] and labels["uncertain"] == []
    # the wrong automated seat is gone; the introduced person is seated
    assert _present(world["chat_id"]) == {"Sam"}
    # the contested clip left Dave's bank; his introduction clip survives
    dave_clips = world["store"].clips_of(world["dave"])
    assert [c["source"] for c in dave_clips] == ["introduction"]
    # and the owner learns the two banks collide
    flags = _flags(world["chat_id"], "merge_question")
    assert len(flags) == 1
    assert flags[0]["label"] == "Sam" and flags[0]["suspected"] == "Dave"


def test_retraction_is_recorded_for_the_durable_home(world):
    """#33: a retracted clip must not resurrect through a membro rebuild."""
    _scan(world)
    kinds = [c["kind"] for c in world["store"].pending_corrections()]
    assert "delete" in kinds


def test_agreeing_label_reconciles_nothing(world):
    con = db.connect()
    try:
        db.set_message_voice_labels(con, world["message_id"], {
            "clusters": ["local"], "labels": ["Sam"], "uncertain": []})
    finally:
        con.close()
    _scan(world)
    # Dave's seat and bank are untouched; no merge question
    assert "Dave" in _present(world["chat_id"])
    sources = [c["source"] for c in world["store"].clips_of(world["dave"])]
    assert "accumulated" in sources
    assert _flags(world["chat_id"], "merge_question") == []


def test_owner_label_reconciles_nothing(world):
    """The owner introducing someone else is the normal shape, not a
    contradiction - even when the introduced name is remembered."""
    con = db.connect()
    try:
        db.set_message_voice_labels(con, world["message_id"], {
            "clusters": ["local"], "labels": ["Alex"], "uncertain": []})
    finally:
        con.close()
    _scan(world)
    assert "Dave" in _present(world["chat_id"])
    assert _flags(world["chat_id"], "merge_question") == []


def test_corrected_label_is_law(world):
    """A label the owner tapped into place outranks the introduction scan -
    no automated step may change it back."""
    con = db.connect()
    try:
        db.set_message_voice_labels(con, world["message_id"], {
            "clusters": ["local"], "labels": ["Dave"], "uncertain": [],
            "corrected": True})
    finally:
        con.close()
    _scan(world)
    assert _labels(world["message_id"])["labels"] == ["Dave"]
    assert "Dave" in _present(world["chat_id"])


def test_human_placed_seat_survives_but_clips_still_retract(world):
    con = db.connect()
    try:
        db.mark_room_person_left(con, world["chat_id"], "Dave")
        db.add_room_person(con, world["chat_id"], "Dave",
                           person_id=world["dave"], seated_via="introduction")
    finally:
        con.close()
    _scan(world)
    assert "Dave" in _present(world["chat_id"])      # the human seat stands
    sources = [c["source"] for c in world["store"].clips_of(world["dave"])]
    assert "accumulated" not in sources              # the clip still goes
    assert _labels(world["message_id"])["labels"] == ["Sam"]


def test_retraction_never_touches_human_backed_clips(world):
    """Even a byte-identical clip stays when a human stood behind it: only
    automated captures are eligible."""
    world["store"].add_clip(world["dave"], world["utterance"], 16000,
                            source="correction")
    _scan(world)
    sources = [c["source"] for c in world["store"].clips_of(world["dave"])]
    assert "correction" in sources and "accumulated" not in sources


def test_unremembered_label_is_left_to_the_existing_doors(world):
    """A label that is nobody remembered contradicts differently (the
    variant/voice machinery owns it): this path must not touch it."""
    con = db.connect()
    try:
        db.set_message_voice_labels(con, world["message_id"], {
            "clusters": ["local"], "labels": ["Voice 2"], "uncertain": []})
    finally:
        con.close()
    _scan(world)
    assert _labels(world["message_id"])["labels"] == ["Voice 2"]
    assert _flags(world["chat_id"], "merge_question") == []
