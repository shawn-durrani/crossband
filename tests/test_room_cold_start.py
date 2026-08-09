"""Cold-start enrolment (#28): a person who forgets their voice can get it
back by talking.

THE DEADLOCK. Every door into the anchor bank needed something an empty bank
does not have. A confident match needs clips to match against. The
introduction scan needs an introduction-shaped sentence. Tap-to-correct needs
a label to tap. So once a bank was emptied, the matcher deferred on every
single turn, nothing ever accumulated, and the seats were told "identity
pending" over and over about the only person in the room.

THE WAY OUT is elimination rather than recognition: in an ARMED room holding
exactly ONE person whose bank cannot identify them yet, an utterance the
matcher could not place can only be theirs. That is enough to bank the audio
and to name the turn - marked, honestly, as still being learned.

What these tests pin, in order:

1. The decision table, pure: which verdicts and which rosters qualify, and
   the four shapes that must never qualify (a multi verdict, two or more
   people present, a confident match, a matcher that failed outright).
2. _room_plan derives the by-elimination candidate from the roster it
   already read - and stops offering one the moment the bank is sufficient.
3. run_pass end to end: the turn is labelled learning, the audio is banked
   under source='cold-start', the roster row is linked, and NO ElevenLabs
   call fires (elimination is free).
4. The learning state reaches the seats: the projection heads the turn
   "<name> (learning this voice)" - not the pending head, not voice
   confirmed - and the stable-block explainer says what it means.

Synthetic roster throughout (Alex/Sam/Dave), keyless like every suite here.
"""

import asyncio
import json
import time

import pytest

from backend import anchors, db, diarize, voiceid
from backend.app import create_app
from backend.config import Settings
from backend.providers import (LEARNING_SUFFIX, PENDING_IDENTITY_HEAD,
                               VOICE_CONFIRMED_SUFFIX,
                               build_anthropic_messages, split_system_prompt)
from tests.conftest import make_msg
from tests.test_projection import PARTICIPANT, ROSTER


def loud_pcm(seconds, sample_rate=16000):
    """PCM-16 loud enough to clear the anchor store's quality gate."""
    return b"\x00\x40" * int(seconds * sample_rate)


def _defer(reason, score=0.3):
    return {"status": voiceid.DEFER, "person_id": None, "name": None,
            "score": score, "reason": reason}


def _match(name, pid, score=0.9):
    return {"status": voiceid.MATCH, "person_id": pid, "name": name,
            "score": score, "reason": "match"}


@pytest.fixture
def app(tmp_path):
    diarize._ROOM_ENABLED.clear()
    diarize._AMBIENT_OFF.clear()
    diarize._STASHED.clear()
    diarize._LAST_DECISION.clear()
    anchors.clear_recent_audio()
    settings = Settings(data_dir=str(tmp_path / "data"),
                        memory_url="http://127.0.0.1:1", user_name="Alex")
    return create_app(settings)


# ── 1. the decision table, pure ─────────────────────────────────────────────

def test_every_non_multi_defer_qualifies_when_one_person_is_pending():
    """The whole point: the reasons a cold bank actually produces - nobody
    to match against, or a best match under the bar - must all bank. Any
    other defer does too; there is no reason to be pickier once the room
    can hold only one person."""
    for reason in ("no_candidates", "below_threshold", "ambiguous",
                   "no_enrolled", "too_short", "unavailable"):
        assert diarize.cold_start_person(_defer(reason), "Alex") == "Alex", reason


def test_a_multi_verdict_never_qualifies():
    """Overlapping speech is the one thing elimination cannot survive: two
    people spoke, so the audio is ground truth for neither. It has its own
    path (the crosstalk split) and must keep it."""
    assert diarize.cold_start_person(_defer(voiceid.MULTI), "Alex") is None


def test_two_or_more_unidentifiable_people_never_qualify():
    """_room_plan offers no candidate unless exactly one present person is
    unidentifiable (reworded by remembered-first, #28 fourteenth field
    test - the rule generalised from one present person TOTAL); with two
    unplaceable people in the room the audio could be either, which is
    what the ask-fallback exists for."""
    assert diarize.cold_start_person(_defer("below_threshold"), None) is None
    assert diarize.cold_start_person(_defer("below_threshold"), "") is None


def test_a_confident_match_never_qualifies():
    """A named turn already has a better answer than elimination, and the
    ordinary accumulation path takes it."""
    assert diarize.cold_start_person(_match("Sam", "p1"), "Sam") is None
    assert diarize.cold_start_person(_match("Sam", "p1"), "Alex") is None


def test_a_failed_matcher_never_qualifies():
    """verdict None means the matcher itself fell over - it did not defer,
    it did not answer. Banking is cheap to skip and awkward to undo, so an
    unknown state skips."""
    assert diarize.cold_start_person(None, "Alex") is None
    assert diarize.cold_start_person({}, "Alex") is None


# ── 2. _room_plan derives the candidate ─────────────────────────────────────

def test_room_plan_offers_the_one_pending_person_and_stops_when_sufficient(app):
    from fastapi.testclient import TestClient
    with TestClient(app, base_url="http://127.0.0.1") as c:
        chat = c.post("/api/chats", json={"participant_ids": []}).json()
        con = db.connect()
        try:
            db.add_room_person(con, chat["id"], "Alex")
        finally:
            con.close()
        # Empty bank: Alex is the by-elimination candidate.
        assert diarize._room_plan(chat["id"], 16000)[4] == "Alex"
        # A second UNIDENTIFIABLE person ends it - elimination needs one
        # door. (Deliberately reworded by remembered-first, #28 fourteenth
        # field test: the rule generalised from "exactly one present
        # person" to "exactly one present person whose bank cannot
        # identify them"; Sam here has no bank, so both people are
        # unidentifiable and the candidate honestly disappears. The
        # sufficient-other-people shape is pinned in
        # test_room_remembered_first.)
        con = db.connect()
        try:
            db.add_room_person(con, chat["id"], "Sam")
        finally:
            con.close()
        assert diarize._room_plan(chat["id"], 16000)[4] is None


def test_room_plan_stops_offering_once_the_bank_is_sufficient(app):
    from fastapi.testclient import TestClient
    with TestClient(app, base_url="http://127.0.0.1") as c:
        chat = c.post("/api/chats", json={"participant_ids": []}).json()
        store = anchors.store()
        pid = store.ensure_person("Alex")
        store.add_clip(pid, loud_pcm(anchors.SUFFICIENT_SECONDS + 1), 16000,
                       source="introduction")
        con = db.connect()
        try:
            db.add_room_person(con, chat["id"], "Alex", person_id=pid)
        finally:
            con.close()
        # Alex can be identified now, so cold start has nothing left to do -
        # this is the exit condition, and it is what ends the deadlock.
        assert diarize._room_plan(chat["id"], 16000)[4] is None


# ── 3. run_pass end to end ──────────────────────────────────────────────────

def _plan(solo_pending):
    # The trailing [] is _room_plan's remembered-first candidate list (#28,
    # fourteenth field test) - the plan tuple grew when the armed pass's
    # candidates became every sufficient remembered person. Empty keeps
    # these pins exercising the defer/cold-start paths unchanged.
    return (b"", [], ["Alex"], 2, solo_pending, [])


def test_cold_start_labels_learning_banks_the_audio_and_calls_no_cloud(
        app, monkeypatch):
    from fastapi.testclient import TestClient
    monkeypatch.setattr(diarize, "_room_plan",
                        lambda *a, **k: _plan("Alex"))
    monkeypatch.setattr(diarize.voiceid, "identify_utterance",
                        lambda *a, **k: _defer("below_threshold"))
    monkeypatch.setattr(
        diarize.voice, "transcribe_diarized",
        lambda *a, **k: pytest.fail("cold start must make NO ElevenLabs call"))
    seen = {}

    async def fake_attach(chat_id, commit_ts, payload, session, turn_id=None):
        seen["payload"] = payload
        return 77
    monkeypatch.setattr(diarize, "_attach_until_deadline", fake_attach)

    with TestClient(app, base_url="http://127.0.0.1") as c:
        chat = c.post("/api/chats", json={"participant_ids": []}).json()
        con = db.connect()
        try:
            db.add_room_person(con, chat["id"], "Alex")
        finally:
            con.close()
        session = diarize.RoomSession(enabled=True)
        asyncio.run(diarize.run_pass(chat["id"], loud_pcm(3.0), 16000,
                                     time.time(), session, {}, turn_id="t1"))

    payload = seen["payload"]
    assert payload["learning"] is True
    assert payload["labels"] == ["Alex"]
    # The name rides `uncertain` too, so every consumer written before the
    # learning marker existed keeps treating it as a guess rather than a
    # confident identification.
    assert payload["uncertain"] == ["Alex"]
    assert payload["source"] == diarize.COLD_START_SOURCE

    # The audio is banked, tagged with how it was earned, and the roster row
    # is linked - 'anchor pending' honestly ends for Alex.
    people = anchors.store().people()
    assert [p["name"] for p in people] == ["Alex"]
    assert people[0]["clip_count"] == 1
    con = db.connect()
    try:
        roster = db.get_room_roster(con, chat["id"])
    finally:
        con.close()
    assert roster[0]["person_id"] == people[0]["person_id"]


def test_cold_start_records_the_local_pulse(app, monkeypatch):
    """A by-elimination decision IS a decision the health strip should show,
    and it came from this device, so it stamps a 'local' pulse."""
    from fastapi.testclient import TestClient
    monkeypatch.setattr(diarize, "_room_plan", lambda *a, **k: _plan("Alex"))
    monkeypatch.setattr(diarize.voiceid, "identify_utterance",
                        lambda *a, **k: _defer("no_candidates"))
    monkeypatch.setattr(diarize, "_bank_cold_start", lambda *a, **k: None)

    async def fake_attach(*a, **k):
        return 5
    monkeypatch.setattr(diarize, "_attach_until_deadline", fake_attach)
    with TestClient(app, base_url="http://127.0.0.1") as c:
        chat = c.post("/api/chats", json={"participant_ids": []}).json()
        session = diarize.RoomSession(enabled=True)
        asyncio.run(diarize.run_pass(chat["id"], loud_pcm(3.0), 16000,
                                     time.time(), session, {}))
        got = diarize.last_decision(chat["id"])
        assert got and got["path"] == "local"


def test_without_a_candidate_a_defer_still_labels_nothing(app, monkeypatch):
    """THE RETIREMENT PIN, unchanged (#28 PR-B): outside the cold-start
    shape a deferred verdict leaves the turn unresolved - no label, no
    banking, no pulse, no cloud call."""
    from fastapi.testclient import TestClient
    monkeypatch.setattr(diarize, "_room_plan", lambda *a, **k: _plan(None))
    monkeypatch.setattr(diarize.voiceid, "identify_utterance",
                        lambda *a, **k: _defer("below_threshold"))
    monkeypatch.setattr(
        diarize.voice, "transcribe_diarized",
        lambda *a, **k: pytest.fail("no cloud call on a defer"))
    monkeypatch.setattr(
        diarize, "_attach_until_deadline",
        lambda *a, **k: pytest.fail("a defer with no candidate labels nothing"))
    with TestClient(app, base_url="http://127.0.0.1") as c:
        chat = c.post("/api/chats", json={"participant_ids": []}).json()
        session = diarize.RoomSession(enabled=True)
        asyncio.run(diarize.run_pass(chat["id"], loud_pcm(3.0), 16000,
                                     time.time(), session, {}))
        # Since the thirteenth field test a defer records WHY on the
        # pulse (the reason was already computed); the point of this
        # pin is that no LABEL is written, which still holds.
        decision = diarize.last_decision(chat["id"])
        assert decision["path"] == diarize.DECISION_UNRESOLVED
        assert decision["reason"] == "below_threshold"
        assert anchors.store().people() == []


def test_a_clip_that_fails_the_quality_gate_is_not_banked(app):
    """A cold start is a reason to accumulate, never a reason to accept
    noise: the ordinary quality gate still decides what lands in the bank."""
    from fastapi.testclient import TestClient
    with TestClient(app, base_url="http://127.0.0.1") as c:
        chat = c.post("/api/chats", json={"participant_ids": []}).json()
        con = db.connect()
        try:
            db.add_room_person(con, chat["id"], "Alex")
        finally:
            con.close()
        diarize._bank_cold_start(chat["id"], "Alex", b"\x01\x00" * 48000,
                                 16000, {})
        assert anchors.store().people()[0]["clip_count"] == 0


# ── 4. the learning state reaches the seats ─────────────────────────────────

def _learning_msg(name="Alex", **extra):
    m = make_msg(1, "user", "we should get going")
    m["voice_labels"] = json.dumps(
        {"clusters": ["local"], "labels": [name], "uncertain": [name],
         "learning": True, "source": "cold-start", **extra})
    m["voice_turn_id"] = "t1"
    return m


def test_the_learning_head_names_the_person_and_says_it_is_learning(names, cfg):
    """The projection's whole job here: stop saying "identity pending" about
    someone the room can only contain, without over-claiming. Not the
    pending head, not voice-confirmed - the name, plus what it is worth."""
    cfg = dict(cfg, room_mode=True)
    text = build_anthropic_messages(
        "claude", [_learning_msg("Sam")], names, cfg)[0]["content"][0]["text"]
    assert text.startswith(f"[Sam{LEARNING_SUFFIX} · ")
    assert PENDING_IDENTITY_HEAD not in text
    assert VOICE_CONFIRMED_SUFFIX not in text
    assert "unidentified speaker" not in text


def test_the_learning_head_speaks_the_preferred_name(names, cfg):
    """#28, naming is law: a learning head resolves through the preferred
    map exactly like every other named head."""
    cfg = dict(cfg, room_mode=True, preferred_names={"dave": "Mateo"})
    text = build_anthropic_messages(
        "claude", [_learning_msg("Dave")], names, cfg)[0]["content"][0]["text"]
    assert text.startswith(f"[Mateo{LEARNING_SUFFIX} · ")


def test_a_learning_marker_without_a_single_label_falls_through(names, cfg):
    """Defensive: the marker only ever accompanies one by-elimination name.
    Anything else takes the ordinary uncertain path rather than inventing a
    head - two names cannot both be the only person in the room."""
    cfg = dict(cfg, room_mode=True)
    two = _learning_msg("Sam")
    two["voice_labels"] = json.dumps(
        {"clusters": ["a", "b"], "labels": ["Sam", "Dave"],
         "uncertain": ["Sam", "Dave"], "learning": True})
    text = build_anthropic_messages(
        "claude", [two], names, cfg)[0]["content"][0]["text"]
    assert LEARNING_SUFFIX not in text
    assert "nidentified speaker" in text


def test_the_learning_explainer_lives_in_the_stable_block(cfg):
    """Constant text (it names nobody and varies with nothing), so it sits
    in the STABLE cached block with the rest of the room-label meanings -
    the cache-split pins in tests/test_cache_split.py depend on that."""
    stable, volatile = split_system_prompt(
        PARTICIPANT, ROSTER, dict(cfg), None, "", False)
    for tell in ("(learning this voice)", "by elimination",
                 "likely rather than certain"):
        assert tell in stable, tell
        assert tell not in volatile


# ── the ordering pin (#28, twelfth field test) ──────────────────────────────

def test_a_finished_verdict_is_on_the_row_at_insert(tmp_path):
    """THE ORDERING BUG. The identity check finishes seconds before /send
    creates the row (it fires at the start of the silence gap), but the label
    could only be written AFTER the row existed - and /send dispatches the
    round that renders the transcript in the same breath. So the model
    answering a turn read "identity pending" on the very turn the browser
    already showed as confirmed. A parked verdict must therefore ride the
    INSERT itself, not a write that follows it."""
    from backend import diarize
    from backend.app import create_app
    from backend.config import Settings
    from fastapi.testclient import TestClient

    diarize._PENDING_LABELS.clear()
    app = create_app(Settings(data_dir=str(tmp_path / "data"),
                              memory_url="http://127.0.0.1:1",
                              user_name="Alex"))
    payload = {"clusters": ["local"], "labels": ["Alex"], "uncertain": []}
    diarize.park_label("turn-abc", payload)
    with TestClient(app, base_url="http://127.0.0.1") as c:
        chat = c.post("/api/chats", json={"participant_ids": []}).json()
        con = db.connect()
        try:
            msg = db.insert_message(con, chat["id"], "user", "hello",
                                    voice_turn_id="turn-abc",
                                    voice_labels=diarize.claim_label("turn-abc"))
            row = con.execute("SELECT voice_labels, labels_updated_at "
                              "FROM messages WHERE id=?",
                              (msg["id"],)).fetchone()
        finally:
            con.close()
    # the label is ON the row from the moment it exists - no later write
    assert json.loads(row["voice_labels"])["labels"] == ["Alex"]
    assert row["labels_updated_at"] > 0      # the live cursor sees it too
    # and the park is single-use, so the pass cannot double-write it
    assert diarize.claim_label("turn-abc") is None


def test_an_unclaimed_park_expires_and_never_leaks_to_another_turn(tmp_path):
    from backend import diarize
    diarize._PENDING_LABELS.clear()
    diarize.park_label("turn-1", {"labels": ["Alex"], "uncertain": []})
    # a different turn never sees it
    assert diarize.claim_label("turn-2") is None
    # bounded: flooding parks evicts oldest rather than growing forever
    for n in range(diarize._PENDING_MAX + 5):
        diarize.park_label(f"flood-{n}", {"labels": ["Sam"], "uncertain": []})
    assert len(diarize._PENDING_LABELS) <= diarize._PENDING_MAX
