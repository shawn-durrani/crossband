"""Every voiced turn gets an identity check (#461).

The field evidence: in a room with two people, about half of what was said
carried no name and no reason, and a new voice was never asked about. Three
separate gaps, each pinned here or beside the pin it replaced:

1. THE BATCH PATH: a turn transcribed by the batch /stt POST (the fallback
   once realtime fails, and the salvage for a turn realtime lost) was never
   checked, because the check hung off the realtime relay alone. The client
   now sends a PCM-16 WAV copy with the turn id, and the route runs the
   same check the relay does. A turn the relay already checked is left
   alone, and a request without the copy behaves exactly as before.
2. THE CLAIMED LABEL: labels ride the insert, so the check nearly always
   found its own label already on the row and returned no id. Everything
   keyed off that id went quiet: the who-joined ask, the mismatch
   cross-check and tap-to-correct's audio. The ask is pinned in
   test_room_ambient; the other two are pinned here, on the armed pass.
3. SOLO: pinned in test_room_ambient and test_room_speculative, where the
   old "solo skips the check" pins were replaced.
"""

import json
import time

import pytest
from fastapi.testclient import TestClient

from backend import anchors, db, diarize, voiceid
from backend.app import create_app
from backend.config import Settings
from backend.routers import voice as voice_router
from roomkit import _remember, _wait_for, loud_pcm


@pytest.fixture
def app(tmp_path):
    settings = Settings(data_dir=str(tmp_path / "data"),
                        memory_url="http://127.0.0.1:1",
                        user_name="Alex")
    return create_app(settings)


@pytest.fixture
def batch(monkeypatch):
    """The batch transcription double: voice is on, and every POST to the
    provider is counted. A crosstalk split would also land here, and none
    of these turns may cause one."""
    state = {"transcribed": 0}

    def fake_transcribe(data, mime, cfg):
        state["transcribed"] += 1
        return "hello world", "scribe_v2"

    monkeypatch.setattr(voice_router.voice, "provider_for",
                        lambda cfg: voice_router.voice.PROVIDER_ELEVENLABS)
    monkeypatch.setattr(voice_router.voice, "transcribe", fake_transcribe)
    monkeypatch.setattr(
        voice_router.voice.httpx, "post",
        lambda *a, **kw: pytest.fail("no provider call beyond transcribe"))
    return state


@pytest.fixture
def matcher(monkeypatch):
    state = {"verdicts": [], "calls": 0, "seconds": []}

    def fake_identify(pcm, sample_rate, candidates, cfg,
                      pending_present=False):
        state["calls"] += 1
        state["seconds"].append(len(pcm) / 2 / sample_rate)
        if state["verdicts"]:
            return state["verdicts"].pop(0)
        return _defer("unavailable")

    monkeypatch.setattr(voiceid, "identify_utterance", fake_identify)
    return state


def _match(name, pid, score=0.8):
    return {"status": voiceid.MATCH, "person_id": pid, "name": name,
            "score": score, "reason": "match"}


def _defer(reason):
    return {"status": voiceid.DEFER, "person_id": None, "name": None,
            "score": 0.3, "reason": reason}


def _armed_chat(client, *people):
    chat = client.post("/api/chats", json={"participant_ids": []}).json()
    con = db.connect()
    try:
        db.set_chat_room_mode(con, chat["id"], True)
        for name, pid in people:
            db.add_room_person(con, chat["id"], name, person_id=pid)
    finally:
        con.close()
    diarize.set_room_enabled(chat["id"], True)
    return chat


def _claim_insert(chat_id, turn_id, text="hello world"):
    """What /send does: the parked label rides the insert."""
    con = db.connect()
    try:
        return db.insert_message(con, chat_id, "user", text,
                                 voice_turn_id=turn_id,
                                 voice_labels=diarize.claim_label(turn_id))
    finally:
        con.close()


def _post_stt(client, chat_id, *, turn_id="", wav=None):
    files = {"file": ("utterance.webm", b"\x1a\x45\xdf\xa3" * 64,
                      "audio/webm")}
    if wav is not None:
        files["pcm"] = ("turn.wav", wav, "audio/wav")
    data = {"duration_ms": "1500"}
    if turn_id:
        data["turn_id"] = turn_id
    return client.post(f"/api/chats/{chat_id}/stt", files=files, data=data)


def _wav(seconds=1.5, rate=16000):
    return diarize.pcm16_wav(loud_pcm(seconds, rate), rate)


def _chat_state(chat_id):
    con = db.connect()
    try:
        row = con.execute("SELECT room_mode, ambient_off FROM chats WHERE id=?",
                          (chat_id,)).fetchone()
        return bool(row["room_mode"]), bool(row["ambient_off"])
    finally:
        con.close()


# ── pure rules ──────────────────────────────────────────────────────────────

def test_check_route_table():
    assert diarize.check_route(True, True) == diarize.ROUTE_ROOM
    assert diarize.check_route(True, False) == diarize.ROUTE_ROOM
    assert diarize.check_route(False, True) == diarize.ROUTE_AMBIENT
    assert diarize.check_route(False, False) == diarize.ROUTE_NONE


def test_solo_decision_never_arms_seats_or_asks():
    assert diarize.solo_decision("noop_owner") == "noop_owner"
    assert diarize.solo_decision("arm_known") == "name_known"
    assert diarize.solo_decision("arm_unknown") == "mark_unknown"
    assert diarize.solo_decision("defer") == "defer"
    assert diarize.solo_decision("anything else") == "defer"
    assert not any(d.startswith("arm") for d in
                   diarize.SOLO_DECISIONS.values())


def test_carries_payload_reads_only_this_passes_label():
    mine = diarize.label_payload(["Sam"], score=0.81)
    assert diarize.carries_payload(json.dumps(mine), mine)
    # tuples and lists are the same label once stored
    assert diarize.carries_payload(
        json.dumps(mine), dict(mine, labels=("Sam",)))
    other = diarize.label_payload(["Dave"], score=0.81)
    assert not diarize.carries_payload(json.dumps(other), mine)
    assert not diarize.carries_payload("", mine)
    assert not diarize.carries_payload("{not json", mine)
    assert not diarize.carries_payload(json.dumps(mine), {})


def test_wav_round_trip_and_refusals():
    pcm = loud_pcm(1.0)
    assert diarize.wav_pcm16(diarize.pcm16_wav(pcm, 16000)) == (pcm, 16000)
    assert diarize.wav_pcm16(diarize.pcm16_wav(pcm, 48000)) == (pcm, 48000)
    stereo = bytearray(diarize.pcm16_wav(pcm, 16000))
    stereo[22:24] = (2).to_bytes(2, "little")
    assert diarize.wav_pcm16(bytes(stereo)) is None
    eight_bit = bytearray(diarize.pcm16_wav(pcm, 16000))
    eight_bit[34:36] = (8).to_bytes(2, "little")
    assert diarize.wav_pcm16(bytes(eight_bit)) is None
    assert diarize.wav_pcm16(diarize.pcm16_wav(pcm, 4000)) is None
    assert diarize.wav_pcm16(b"\x1a\x45\xdf\xa3" * 64) is None  # a webm
    assert diarize.wav_pcm16(b"") is None
    assert diarize.wav_pcm16(diarize.pcm16_wav(b"", 16000)) is None


def test_wav_keeps_the_newest_audio_past_the_cap(monkeypatch):
    monkeypatch.setattr(diarize, "MAX_UTTERANCE_SECONDS", 1)
    pcm = b"\x01\x00" * 16000 + b"\x02\x00" * 16000
    got, rate = diarize.wav_pcm16(diarize.pcm16_wav(pcm, 16000))
    assert rate == 16000 and got == b"\x02\x00" * 16000


# ── 1. the batch path ───────────────────────────────────────────────────────

def test_batch_turn_in_an_armed_room_is_named(app, batch, matcher):
    pid = _remember("Sam")
    matcher["verdicts"] = [_match("Sam", pid)]
    with TestClient(app, base_url="http://127.0.0.1") as c:
        chat = _armed_chat(c, ("Sam", pid))
        r = _post_stt(c, chat["id"], turn_id="tB1", wav=_wav())
        assert r.status_code == 200 and r.json()["text"] == "hello world"
        assert _wait_for(lambda: "tB1" in diarize._PENDING_LABELS)
        msg = _claim_insert(chat["id"], "tB1")
    assert json.loads(msg["voice_labels"])["labels"] == ["Sam"]
    assert matcher["calls"] == 1
    assert matcher["seconds"] == [1.5]
    assert batch["transcribed"] == 1


def test_batch_turn_the_matcher_cannot_name_says_why(app, batch, matcher):
    pid = _remember("Sam")
    matcher["verdicts"] = [_defer("below_threshold")]
    with TestClient(app, base_url="http://127.0.0.1") as c:
        chat = _armed_chat(c, ("Sam", pid))
        assert _post_stt(c, chat["id"], turn_id="tB2",
                         wav=_wav()).status_code == 200
        assert _wait_for(lambda: "tB2" in diarize._PENDING_LABELS)
        msg = _claim_insert(chat["id"], "tB2")
    parsed = json.loads(msg["voice_labels"])
    assert parsed["labels"] == [] and parsed["unresolved"] == "below_threshold"


def test_batch_turn_with_the_room_off_gets_the_room_off_check(app, batch,
                                                             matcher):
    """A clear stranger heard only through the batch path arms the room and
    raises the ask, exactly as through the relay."""
    _remember("Alex")
    matcher["verdicts"] = [_defer("below_threshold")]
    with TestClient(app, base_url="http://127.0.0.1") as c:
        chat = c.post("/api/chats", json={"participant_ids": []}).json()
        assert _post_stt(c, chat["id"], turn_id="tB3",
                         wav=_wav()).status_code == 200
        assert _wait_for(lambda: "tB3" in diarize._PENDING_LABELS)
        msg = _claim_insert(chat["id"], "tB3")
        assert _wait_for(lambda: _chat_state(chat["id"])[0])
        con = db.connect()
        try:
            flags = _wait_for(lambda: db.get_room_flags(con, chat["id"]))
        finally:
            con.close()
    assert json.loads(msg["voice_labels"])["labels"] == ["Voice 1"]
    assert [(f["kind"], f["message_id"]) for f in flags] \
        == [("unknown_voice", msg["id"])]
    # the room-off turn was stashed, as the relay stashes it, so a
    # confirmed introduction can still claim it
    assert diarize.peek_stashed_utterance(chat["id"]) is not None


def test_the_shadow_scores_each_checked_turn_once(app, batch, matcher,
                                                  monkeypatch):
    """The room-mode shadow test (#465) reads each armed turn after its
    identity check. A backup-path turn is one checked turn, so it hands the
    shadow one turn, and a second copy of the same turn hands it none."""
    from backend import voice_shadow
    handed = []
    monkeypatch.setattr(voice_shadow, "schedule",
                        lambda chat_id, pcm, rate, cfg, turn_id, today:
                        handed.append((turn_id, today.get("path"))))
    pid = _remember("Sam")
    matcher["verdicts"] = [_match("Sam", pid)]
    with TestClient(app, base_url="http://127.0.0.1") as c:
        chat = _armed_chat(c, ("Sam", pid))
        assert _post_stt(c, chat["id"], turn_id="tS1",
                         wav=_wav()).status_code == 200
        assert _wait_for(lambda: handed)
        assert _post_stt(c, chat["id"], turn_id="tS1",
                         wav=_wav()).status_code == 200
        time.sleep(0.3)
    assert handed == [("tS1", "local")]
    assert matcher["calls"] == 1


def test_batch_skips_a_turn_the_relay_already_checked(app, batch, matcher):
    pid = _remember("Sam")
    with TestClient(app, base_url="http://127.0.0.1") as c:
        chat = _armed_chat(c, ("Sam", pid))
        # the relay's commit for this turn scheduled its check first
        diarize._note_turn_checked("tB4")
        assert _post_stt(c, chat["id"], turn_id="tB4",
                         wav=_wav()).status_code == 200
        time.sleep(0.3)
    assert matcher["calls"] == 0
    assert batch["transcribed"] == 1


def test_batch_without_the_copy_only_transcribes(app, batch, matcher):
    pid = _remember("Sam")
    with TestClient(app, base_url="http://127.0.0.1") as c:
        chat = _armed_chat(c, ("Sam", pid))
        assert _post_stt(c, chat["id"], turn_id="tB5").status_code == 200
        # no turn id: nothing to label exactly, so no check either
        assert _post_stt(c, chat["id"], wav=_wav()).status_code == 200
        # a copy that is not PCM-16 mono is ignored, never an error
        assert _post_stt(c, chat["id"], turn_id="tB6",
                         wav=b"\x1a\x45\xdf\xa3" * 64).status_code == 200
        time.sleep(0.3)
    assert matcher["calls"] == 0
    assert batch["transcribed"] == 3
    assert not diarize.turn_checked("tB5") and not diarize.turn_checked("tB6")


# ── 2. the claimed label, on the armed pass ─────────────────────────────────

def test_claimed_named_label_keeps_audio_and_gets_the_cross_check(
        app, batch, matcher, monkeypatch):
    """The armed pass's named turn: the label is claimed by the insert, and
    tap-to-correct's audio and the mismatch cross-check must still follow.
    Driven through the batch path, which shares the armed pass with the
    relay."""
    pid = _remember("Sam")
    matcher["verdicts"] = [_match("Sam", pid)]
    checks = []
    monkeypatch.setattr("backend.mismatch.schedule_check",
                        lambda *a, **k: checks.append(a))
    with TestClient(app, base_url="http://127.0.0.1") as c:
        chat = _armed_chat(c, ("Sam", pid))
        assert _post_stt(c, chat["id"], turn_id="tC1",
                         wav=_wav()).status_code == 200
        assert _wait_for(lambda: "tC1" in diarize._PENDING_LABELS)
        msg = _claim_insert(chat["id"], "tC1")
        assert _wait_for(lambda: checks)
    assert json.loads(msg["voice_labels"])["labels"] == ["Sam"]
    assert checks[0][1] == msg["id"] and checks[0][2] == "Sam"
    assert anchors.peek_audio(msg["id"]) is not None


def test_someone_elses_label_on_the_row_is_left_alone(app, batch, matcher,
                                                     monkeypatch):
    """The claim fix must not adopt a row another writer labelled: only a
    row carrying exactly this pass's payload counts as its own."""
    pid = _remember("Sam")
    matcher["verdicts"] = [_match("Sam", pid)]
    checks = []
    monkeypatch.setattr("backend.mismatch.schedule_check",
                        lambda *a, **k: checks.append(a))
    with TestClient(app, base_url="http://127.0.0.1") as c:
        chat = _armed_chat(c, ("Sam", pid))
        con = db.connect()
        try:
            msg = db.insert_message(
                con, chat["id"], "user", "hello world", voice_turn_id="tC2",
                voice_labels=diarize.label_payload(["Dave"],
                                                   source="correction"))
        finally:
            con.close()
        assert _post_stt(c, chat["id"], turn_id="tC2",
                         wav=_wav()).status_code == 200
        assert _wait_for(lambda: matcher["calls"] == 1)
        time.sleep(0.3)
    con = db.connect()
    try:
        row = con.execute("SELECT voice_labels FROM messages WHERE id=?",
                          (msg["id"],)).fetchone()
    finally:
        con.close()
    assert json.loads(row["voice_labels"])["labels"] == ["Dave"]
    assert checks == []
    assert anchors.peek_audio(msg["id"]) is None
