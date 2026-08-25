"""Remembered-first matching (#28, fourteenth field test): an ARMED room
recognises everyone the store remembers, not just the present roster.

THE FAILURE, live. The store held two fully-sufficient voices - the owner
and a guest learnt earlier the same day - yet every one of the guest's
turns deferred below_threshold. With room mode armed, the fast identify
path built its candidate list from the PRESENT ROSTER only, and the guest
was never rostered: she could not be rostered without being recognised,
and could not be recognised without being rostered. Her introduction could
not break the loop either - the spelling that night was four edits from
the remembered name, and the voice-match arm judged the wrong audio (a
stale pre-arm stash; that half is pinned in test_naming_law).

What these tests pin, in order:

1. THE REGRESSION PIN: a sufficient remembered NON-rostered person
   speaking in an armed room is named AND rostered on that first utterance
   - certain label, linked present row, no ElevenLabs call. This test
   fails against the roster-as-candidates code by construction: the
   matcher double can only name Sam when Sam's bank is among the
   candidates, exactly like the live matcher.
2. CANDIDATE PARITY: the armed pass, the ambient room-off check and the
   speculative silence-start check share ONE candidate construction -
   every sufficient remembered person - so the paths cannot drift apart
   again.
3. The roster cap still holds: past it the turn is still named, the
   roster simply does not grow. An already-rostered match adds no
   duplicate row.
4. A remembered match answers the open who-is-speaking ask, exactly as a
   naming introduction does.
5. COLD START, GENERALISED: when every OTHER present person is sufficient
   and exactly one present person is not, an unplaceable clear utterance
   banks to that one person by elimination - the owner being present and
   identified no longer blocks a new guest from learning. Two
   unidentifiable present people still offer nobody, and a confident
   match still never cold-starts.

Synthetic roster throughout (Alex the owner, Sam, Dave, Mateo), keyless.
"""

import asyncio
import json
import time

import pytest
from fastapi.testclient import TestClient

from backend import anchors, db, diarize, voiceid
from backend.app import create_app
from backend.config import Settings
from tests.conftest import speech_pcm

CFG = {"user_name": "Alex", "room_roster_max": 6}


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


@pytest.fixture
def no_batch(monkeypatch):
    monkeypatch.setattr(
        "backend.voice.transcribe_diarized",
        lambda *a, **k: pytest.fail("remembered-first must make NO EL call"))


@pytest.fixture
def quiet_mismatch(monkeypatch):
    """The fast label pass schedules the (keyless, no-op) mismatch check;
    these tests drive run_pass under asyncio.run, so silence it rather than
    leave a fire-and-forget task behind on a closing loop."""
    monkeypatch.setattr("backend.mismatch.schedule_check",
                        lambda *a, **k: None)


@pytest.fixture
def sam_matcher(monkeypatch):
    """An HONEST matcher double for the regression shape: it can name Sam
    whenever Sam's bank is among the candidates, and can only defer
    below_threshold otherwise - exactly what the live matcher does when the
    candidate list omits the speaker. Records each call's candidate names."""
    state = {"calls": []}

    def fake_identify(pcm, sample_rate, candidates, cfg, pending_present=False):
        state["calls"].append([c["name"] for c in candidates])
        sam = next((c for c in candidates if c["name"] == "Sam"), None)
        if sam:
            return {"status": voiceid.MATCH, "person_id": sam["person_id"],
                    "name": "Sam", "score": 0.9, "reason": "match"}
        return {"status": voiceid.DEFER, "person_id": None, "name": None,
                "score": 0.3, "reason": "below_threshold"}

    monkeypatch.setattr(voiceid, "identify_utterance", fake_identify)
    return state


def loud_pcm(seconds, sample_rate=16000):
    # Speech-shaped since #218: the anchor gate rejects non-speech.
    return speech_pcm(seconds, sample_rate)


def _remember(name, clips=3):
    store = anchors.store()
    pid = store.ensure_person(name)
    # #83: remembered = introduced once, then accumulated - an
    # accumulation-only bank rightly has no remembered-first rights now
    # (test_audition_gate.py owns that behaviour).
    assert store.add_clip(pid, loud_pcm(2.0), 16000, source="introduction")
    for _ in range(clips - 1):
        assert store.add_clip(pid, loud_pcm(2.0), 16000, source="accumulated")
    return pid


def _armed_chat(client, roster=()):
    """A chat with room mode on (durably + mirrored) and (name, person_id)
    roster rows."""
    chat = client.post("/api/chats", json={"participant_ids": []}).json()
    con = db.connect()
    db.set_chat_room_mode(con, chat["id"], True)
    diarize.set_room_enabled(chat["id"], True)
    for name, pid in roster:
        db.add_room_person(con, chat["id"], name, person_id=pid)
    con.close()
    return chat


def _insert_user_message(chat_id, text="hello world", voice_turn_id=""):
    con = db.connect()
    try:
        return db.insert_message(con, chat_id, "user", text,
                                 voice_turn_id=voice_turn_id)
    finally:
        con.close()


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


def _run(chat_id, session, turn_id, seconds=3.0):
    asyncio.run(diarize.run_pass(chat_id, loud_pcm(seconds), 16000,
                                 time.time(), session, CFG, turn_id=turn_id))


# ── 1. the regression pin ───────────────────────────────────────────────────

def test_remembered_non_rostered_person_named_and_rostered_on_first_utterance(
        app, sam_matcher, no_batch, quiet_mismatch):
    """THE FOURTEENTH-FIELD-TEST PIN. The store remembers Sam (sufficient);
    the armed room's roster holds only the owner. Sam's very first
    utterance must be named "Sam" (certain, local) and must seat Sam on the
    roster, linked to their bank - not defer below_threshold because the
    candidate list stopped at the roster."""
    with TestClient(app, base_url="http://127.0.0.1") as c:
        owner_pid = _remember("Alex")
        sam_pid = _remember("Sam")
        chat = _armed_chat(c, roster=[("Alex", owner_pid)])
        msg = _insert_user_message(chat["id"], voice_turn_id="t1")
        _run(chat["id"], diarize.RoomSession(enabled=True), "t1")
        labels = _labels(msg["id"])
        assert labels, ("the guest's first armed-room turn was never "
                        "labelled - the roster-as-candidates deadlock")
        data = json.loads(labels)
        assert data["labels"] == ["Sam"]
        assert data["uncertain"] == []
        assert data["source"] == "local"
        row = next(p for p in _roster(chat["id"]) if p["name"] == "Sam")
        assert row["person_id"] == sam_pid       # seated AND linked
        # and the decision was local - the health pulse agrees
        assert diarize.last_decision(chat["id"])["path"] == "local"


def test_matched_persons_anchor_still_accumulates_on_the_join_turn(
        app, sam_matcher, no_batch, quiet_mismatch):
    """The join turn is ordinary fast-path anchor food too: the matched
    person's bank refreshes from it exactly as an already-rostered match
    would."""
    with TestClient(app, base_url="http://127.0.0.1") as c:
        _remember("Alex")
        _remember("Sam")
        before = anchors.store().find_by_name("Sam")["clip_count"]
        chat = _armed_chat(c, roster=[("Alex",
                                       anchors.store().find_by_name("Alex")
                                       ["person_id"])])
        _insert_user_message(chat["id"], voice_turn_id="t1")
        _run(chat["id"], diarize.RoomSession(enabled=True), "t1",
             seconds=5.0)
        after = anchors.store().find_by_name("Sam")["clip_count"]
        assert after >= before   # keep-best-N may displace, never regress


# ── 2. candidate parity across the three local paths ────────────────────────

def test_armed_ambient_and_speculative_share_one_candidate_construction(
        app, sam_matcher, no_batch, quiet_mismatch):
    """The drift that caused the field failure can not re-open: the armed
    pass consults exactly remembered_candidates() - every sufficient
    remembered person - which is the same list the ambient plan and the
    speculative check build from the same store."""
    with TestClient(app, base_url="http://127.0.0.1") as c:
        owner_pid = _remember("Alex")
        _remember("Sam")
        _remember("Dave")
        store = anchors.store()
        pending_pid = store.ensure_person("Mateo")   # insufficient: excluded
        assert store.find_by_name("Mateo")["sufficient"] is False
        del pending_pid
        chat = _armed_chat(c, roster=[("Alex", owner_pid)])
        _insert_user_message(chat["id"], voice_turn_id="t1")
        _run(chat["id"], diarize.RoomSession(enabled=True), "t1")
        armed_seen = sorted(sam_matcher["calls"][0])
        expected = sorted(p["name"] for p in store.people()
                          if p["sufficient"])
        assert armed_seen == expected == sorted(
            cand["name"] for cand in diarize.remembered_candidates())
        # the ambient (room-off) plan builds the identical list
        off = c.post("/api/chats", json={"participant_ids": []}).json()
        ambient = diarize._ambient_plan(off["id"], 16000, CFG)
        assert sorted(cand["name"] for cand in ambient[0]) == expected


def test_speculative_match_on_a_non_rostered_remembered_person_is_trusted(
        app, no_batch, quiet_mismatch, monkeypatch):
    """The revalidation rule survives remembered-first with its meaning
    intact: a cached verdict is reused when the matched person is among the
    pass's candidates - and a sufficient remembered NON-rostered person now
    IS a candidate, so the head start names (and seats) them with no
    re-embed. The narrowing case is pinned in
    test_stale_speculative_match_outside_the_candidates_reruns below."""
    calls = {"n": 0}

    def fresh_identify(*a, **k):
        calls["n"] += 1
        pytest.fail("the cached speculative verdict must be reused")

    monkeypatch.setattr(voiceid, "identify_utterance", fresh_identify)
    with TestClient(app, base_url="http://127.0.0.1") as c:
        owner_pid = _remember("Alex")
        sam_pid = _remember("Sam")
        chat = _armed_chat(c, roster=[("Alex", owner_pid)])
        msg = _insert_user_message(chat["id"], voice_turn_id="t1")
        pcm = loud_pcm(3.0)

        async def go():
            async def cached():
                return {"status": voiceid.MATCH, "person_id": sam_pid,
                        "name": "Sam", "score": 0.9, "reason": "match"}
            session = diarize.RoomSession(enabled=True)
            task = asyncio.get_running_loop().create_task(cached())
            await diarize.run_pass(chat["id"], pcm, 16000, time.time(),
                                   session, CFG, turn_id="t1",
                                   speculative={"len": len(pcm),
                                                "task": task})
        asyncio.run(go())
        assert json.loads(_labels(msg["id"]))["labels"] == ["Sam"]
        assert any(p["name"] == "Sam" for p in _roster(chat["id"]))
        assert calls["n"] == 0


def test_stale_speculative_match_outside_the_candidates_reruns(
        app, monkeypatch):
    """The other half of revalidation, at the unit seam: a cached match on
    someone NOT among the consuming pass's candidates (here: a person who
    lost sufficiency between hint and commit) is not trusted - the check
    runs fresh."""
    ran = {"n": 0}

    def fresh_identify(pcm, sample_rate, candidates, cfg, pending_present=False):
        ran["n"] += 1
        return {"status": voiceid.DEFER, "person_id": None, "name": None,
                "score": 0.2, "reason": "below_threshold"}

    monkeypatch.setattr(voiceid, "identify_utterance", fresh_identify)
    with TestClient(app, base_url="http://127.0.0.1"):
        pcm = loud_pcm(2.0)

        async def go():
            async def cached():
                return {"status": voiceid.MATCH, "person_id": "gone",
                        "name": "Dave", "score": 0.9, "reason": "match"}
            task = asyncio.get_running_loop().create_task(cached())
            return await diarize._utterance_verdict(
                1, pcm, 16000,
                [{"person_id": "sam-1", "name": "Sam"}], CFG,
                speculative={"len": len(pcm), "task": task})
        verdict = asyncio.run(go())
        assert ran["n"] == 1                       # re-ran, not smuggled
        assert verdict["reason"] == "below_threshold"


# ── 3. the cap, and idempotence ─────────────────────────────────────────────

def test_past_the_cap_the_turn_is_named_but_the_roster_does_not_grow(
        app, sam_matcher, no_batch, quiet_mismatch, tmp_path):
    diarize._ROOM_ENABLED.clear()
    app = create_app(Settings(data_dir=str(tmp_path / "capdata"),
                              memory_url="http://127.0.0.1:1",
                              user_name="Alex"))
    with TestClient(app, base_url="http://127.0.0.1") as c:
        owner_pid = _remember("Alex")
        _remember("Sam")
        chat = _armed_chat(c, roster=[("Alex", owner_pid), ("Dave", "")])
        msg = _insert_user_message(chat["id"], voice_turn_id="t1")
        cfg = dict(CFG, room_roster_max=2)   # the roster is already full
        asyncio.run(diarize.run_pass(chat["id"], loud_pcm(3.0), 16000,
                                     time.time(),
                                     diarize.RoomSession(enabled=True), cfg,
                                     turn_id="t1"))
        # identity is true regardless of the cap: the label attached
        assert json.loads(_labels(msg["id"]))["labels"] == ["Sam"]
        # but the roster held at the cap
        assert sorted(p["name"] for p in _roster(chat["id"])) \
            == ["Alex", "Dave"]


def test_an_already_rostered_match_adds_no_duplicate_row(
        app, sam_matcher, no_batch, quiet_mismatch):
    with TestClient(app, base_url="http://127.0.0.1") as c:
        owner_pid = _remember("Alex")
        sam_pid = _remember("Sam")
        chat = _armed_chat(c, roster=[("Alex", owner_pid), ("Sam", sam_pid)])
        msg = _insert_user_message(chat["id"], voice_turn_id="t1")
        _run(chat["id"], diarize.RoomSession(enabled=True), "t1")
        assert json.loads(_labels(msg["id"]))["labels"] == ["Sam"]
        assert [p["name"] for p in _roster(chat["id"])] == ["Alex", "Sam"]


# ── 4. the open ask is answered ─────────────────────────────────────────────

def test_a_remembered_match_answers_the_open_unknown_voice_ask(
        app, sam_matcher, no_batch, quiet_mismatch):
    """Ambient armed on an unknown voice and asked who is speaking; the
    next utterance confidently matches remembered Sam. Naming them answers
    the ask, exactly as a naming introduction does - and if a genuinely
    different stranger keeps talking, the next unplaceable turn raises a
    fresh ask."""
    with TestClient(app, base_url="http://127.0.0.1") as c:
        owner_pid = _remember("Alex")
        _remember("Sam")
        chat = _armed_chat(c, roster=[("Alex", owner_pid)])
        con = db.connect()
        db.insert_room_flag(con, chat["id"], "unknown_voice")
        con.close()
        _insert_user_message(chat["id"], voice_turn_id="t1")
        _run(chat["id"], diarize.RoomSession(enabled=True), "t1")
        assert _flags(chat["id"]) == []


# ── 5. cold start, generalised ──────────────────────────────────────────────

def test_room_plan_offers_the_one_insufficient_person_beside_a_sufficient_owner(
        app):
    """The generalisation itself (#28, fourteenth field test): the owner is
    present, sufficient and identified - and Dave, present with no bank, is
    STILL the by-elimination candidate. Under the old exactly-one-present
    rule this offered nobody, so a new guest could never start learning
    while the owner was in the room."""
    with TestClient(app, base_url="http://127.0.0.1") as c:
        owner_pid = _remember("Alex")
        chat = _armed_chat(c, roster=[("Alex", owner_pid), ("Dave", "")])
        assert diarize._room_plan(chat["id"], 16000)[4] == "Dave"
        # a second unidentifiable person ends it: ambiguity offers nobody
        con = db.connect()
        db.add_room_person(con, chat["id"], "Mateo")
        con.close()
        assert diarize._room_plan(chat["id"], 16000)[4] is None


def test_unplaceable_utterance_banks_to_the_one_insufficient_person(
        app, sam_matcher, no_batch, quiet_mismatch):
    """End to end with the owner present and identified: an utterance that
    matches nobody sufficient banks to Dave by elimination - labelled
    learning, banked under source='cold-start', roster row linked, no
    ElevenLabs call. (The matcher double can only name Sam, who is not in
    this room's store, so the verdict is an honest below_threshold.)"""
    with TestClient(app, base_url="http://127.0.0.1") as c:
        owner_pid = _remember("Alex")
        chat = _armed_chat(c, roster=[("Alex", owner_pid), ("Dave", "")])
        msg = _insert_user_message(chat["id"], voice_turn_id="t1")
        _run(chat["id"], diarize.RoomSession(enabled=True), "t1")
        data = json.loads(_labels(msg["id"]))
        assert data["labels"] == ["Dave"]
        assert data["uncertain"] == ["Dave"]     # honest: still a guess
        assert data["learning"] is True
        dave = anchors.store().find_by_name("Dave")
        assert dave is not None and dave["clip_count"] == 1
        row = next(p for p in _roster(chat["id"]) if p["name"] == "Dave")
        assert row["person_id"] == dave["person_id"]


def test_a_confident_match_still_never_cold_starts(
        app, sam_matcher, no_batch, quiet_mismatch):
    """Same room shape, but the utterance confidently matches remembered
    Sam: the match wins, Dave's bank stays empty - a named turn already has
    a better answer than elimination."""
    with TestClient(app, base_url="http://127.0.0.1") as c:
        owner_pid = _remember("Alex")
        _remember("Sam")
        chat = _armed_chat(c, roster=[("Alex", owner_pid), ("Dave", "")])
        msg = _insert_user_message(chat["id"], voice_turn_id="t1")
        _run(chat["id"], diarize.RoomSession(enabled=True), "t1")
        assert json.loads(_labels(msg["id"]))["labels"] == ["Sam"]
        dave = anchors.store().find_by_name("Dave")
        assert dave is None or dave["clip_count"] == 0
