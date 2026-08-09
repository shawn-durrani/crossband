"""The voice health strip's backend (#28): GET /api/voice/health and the
per-chat last-decision record.

What these tests pin:

1. CONTENT-FREE, absolutely. The endpoint returns states, counts and
   milliseconds - never a person's name, never transcript text - even when
   remembered people exist. The names the strip shows come from the roster
   and people snapshots the caller already has.
2. The matcher state readout: disabled (feature flag off), unavailable (no
   sherpa-onnx), and the cold/fetching/ready machine states, read without
   ever triggering the warm.
3. The last-decision record: bounded per-chat memory of path + ms +
   monotonic age, written only from inside the never-awaited background
   passes - the cloud batch pass and the local ambient check are both
   pinned to record - so the CORE LAW (zero added latency on the live voice
   path) is untouched by construction.
4. The chat block: room on / ambient available / owner-disarmed flags plus
   the roster count, 404 for a chat that does not exist.
"""

import asyncio
import time

import pytest
from fastapi.testclient import TestClient

from backend import anchors, db, diarize, voiceid
from backend.app import create_app
from backend.config import Settings


@pytest.fixture
def app(tmp_path):
    diarize._ROOM_ENABLED.clear()
    diarize._STASHED.clear()
    diarize._LAST_DECISION.clear()
    settings = Settings(data_dir=str(tmp_path / "data"),
                        memory_url="http://127.0.0.1:1", user_name="Alex")
    return create_app(settings)


def loud_pcm(seconds, sample_rate=16000):
    return b"\x00\x40" * int(seconds * sample_rate)


def _mint_sufficient(name):
    store = anchors.store()
    pid = store.ensure_person(name)
    for _ in range(3):
        assert store.add_clip(pid, loud_pcm(2.0), 16000, source="accumulated")
    return pid


# ── the matcher state readout ───────────────────────────────────────────────

def test_matcher_status_disabled_and_unavailable(monkeypatch):
    assert voiceid.matcher_status({"voice_id_enabled": False}) == "disabled"
    monkeypatch.setattr(voiceid, "sherpa_onnx", None)
    assert voiceid.matcher_status({}) == "unavailable"


def test_matcher_status_reads_the_state_machine_without_warming(monkeypatch):
    monkeypatch.setattr(voiceid, "sherpa_onnx", object())
    for state in ("cold", "fetching", "ready", "unavailable"):
        voiceid._set_state(state)
        assert voiceid.matcher_status({}) == state
    # reading the status never claims the warm (state stays what it was)
    voiceid._set_state("cold")
    voiceid.matcher_status({})
    assert voiceid._state == "cold"


# ── the last-decision record ────────────────────────────────────────────────

def test_record_and_read_last_decision():
    diarize._LAST_DECISION.clear()
    assert diarize.last_decision(1) is None
    diarize.record_decision(1, "local", 226.66)
    got = diarize.last_decision(1)
    assert got["path"] == "local"
    assert got["ms"] == 226.7
    assert got["age_s"] >= 0
    # newest wins
    diarize.record_decision(1, "cloud", 1900)
    assert diarize.last_decision(1)["path"] == "cloud"
    # junk paths and a missing chat id are ignored, never stored
    diarize.record_decision(1, "teleport", 5)
    assert diarize.last_decision(1)["path"] == "cloud"
    diarize.record_decision(None, "local", 5)
    assert diarize.last_decision(None) is None


def test_last_decision_store_is_bounded():
    diarize._LAST_DECISION.clear()
    for chat_id in range(20):
        diarize.record_decision(chat_id, "local", 100)
    assert len(diarize._LAST_DECISION) == diarize._DECISION_MAX_CHATS
    assert diarize.last_decision(19) is not None   # newest kept
    assert diarize.last_decision(0) is None        # oldest evicted


def test_cloud_pass_records_the_decision(app, monkeypatch):
    """run_pass's ElevenLabs path stamps a 'cloud' decision - driven with
    the batch call mocked, inside the never-awaited pass itself."""
    monkeypatch.setattr("backend.voice.transcribe_diarized",
                        lambda *a, **k: {"words": []})
    with TestClient(app, base_url="http://127.0.0.1") as c:
        chat = c.post("/api/chats", json={"participant_ids": []}).json()
        session = diarize.RoomSession(enabled=True)
        asyncio.run(diarize.run_pass(chat["id"], loud_pcm(1.0), 16000,
                                     time.time(), session, {}))
        got = diarize.last_decision(chat["id"])
        assert got is not None
        assert got["path"] == "cloud"


def test_ambient_local_check_records_the_decision(app, monkeypatch):
    """The ambient no-op owner check is still an identification - it stamps
    a 'local' decision (the health strip's pulse for the common solo case)."""
    with TestClient(app, base_url="http://127.0.0.1") as c:
        pid = _mint_sufficient("Alex")  # the owner, sufficiently enrolled

        def fake_identify(pcm, sample_rate, candidates, cfg):
            return {"status": "match", "person_id": pid, "name": "Alex",
                    "score": 0.8, "reason": "match"}

        monkeypatch.setattr("backend.voiceid.identify_utterance",
                            fake_identify)
        chat = c.post("/api/chats", json={"participant_ids": []}).json()
        session = diarize.RoomSession()
        asyncio.run(diarize.run_ambient(chat["id"], loud_pcm(1.0), 16000,
                                        time.time(), session,
                                        {"user_name": "Alex"}))
        got = diarize.last_decision(chat["id"])
        assert got is not None
        assert got["path"] == "local"


# ── the endpoint ────────────────────────────────────────────────────────────

def test_health_endpoint_shape_and_counts(app):
    with TestClient(app, base_url="http://127.0.0.1") as c:
        _mint_sufficient("Sam")
        anchors.store().ensure_person("Dave")  # no clips: not sufficient
        h = c.get("/api/voice/health").json()
        assert h["matcher"] in ("disabled", "unavailable", "cold",
                                "fetching", "ready")
        assert h["people_total"] == 2
        assert h["people_sufficient"] == 1
        assert h["chat"] is None
        assert h["last_decision"] is None


def test_health_endpoint_is_content_free(app):
    """No name may ever ride this endpoint - remembered people appear only
    as counts. The names the strip shows come from /api/voice/people."""
    with TestClient(app, base_url="http://127.0.0.1") as c:
        _mint_sufficient("Sam")
        _mint_sufficient("Mateo")
        chat = c.post("/api/chats", json={"participant_ids": []}).json()
        con = db.connect()
        db.add_room_person(con, chat["id"], "Sam")
        con.close()
        body = c.get(f"/api/voice/health?chat_id={chat['id']}").text
        assert "Sam" not in body
        assert "Mateo" not in body
        assert "Alex" not in body


def test_health_endpoint_chat_block_and_decision(app):
    with TestClient(app, base_url="http://127.0.0.1") as c:
        chat = c.post("/api/chats", json={"participant_ids": []}).json()
        con = db.connect()
        db.set_chat_room_mode(con, chat["id"], True)
        db.add_room_person(con, chat["id"], "Sam")
        con.close()
        diarize.record_decision(chat["id"], "local", 227)
        h = c.get(f"/api/voice/health?chat_id={chat['id']}").json()
        assert h["chat"] == {"room_mode": True, "ambient_off": False,
                             "roster_count": 1}
        assert h["last_decision"]["path"] == "local"
        assert h["last_decision"]["ms"] == 227
        assert h["last_decision"]["age_s"] >= 0
        # the sacred disarm shows honestly
        con = db.connect()
        db.set_chat_room_mode(con, chat["id"], False)
        db.set_chat_ambient_off(con, chat["id"], True)
        con.close()
        h = c.get(f"/api/voice/health?chat_id={chat['id']}").json()
        assert h["chat"]["room_mode"] is False
        assert h["chat"]["ambient_off"] is True
        # unknown chat 404s
        assert c.get("/api/voice/health?chat_id=99999").status_code == 404
