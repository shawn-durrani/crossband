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
   monotonic age, written only from inside the never-awaited voice check -
   a named turn records "local", an unnamed one "unresolved" with its
   reason - so the CORE LAW (zero added latency on the live voice path) is
   untouched by construction.
4. The chat block: room on / ambient available / owner-disarmed flags plus
   the roster count, 404 for a chat that does not exist.
"""

import asyncio
import time

import pytest
from fastapi.testclient import TestClient

from backend import anchors, db, diarize, voice_pass, voiceid
from backend.app import create_app
from backend.config import Settings
from roomkit import fake_naming, loud_pcm, naming_answer


@pytest.fixture
def app(tmp_path):
    settings = Settings(data_dir=str(tmp_path / "data"),
                        memory_url="http://127.0.0.1:1", user_name="Alex")
    return create_app(settings)


def _mint_sufficient(name):
    store = anchors.store()
    pid = store.ensure_person(name)
    # #83: remembered = introduced once, then accumulated (see
    # test_audition_gate.py for the unvouched-bank pause).
    assert store.add_clip(pid, loud_pcm(2.0), 16000, source="introduction")
    for _ in range(2):
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
    diarize.record_decision(1, "unresolved", 40, "listening")
    assert diarize.last_decision(1)["path"] == "unresolved"
    assert diarize.last_decision(1)["reason"] == "listening"
    # junk paths (the retired "cloud" among them) and a missing chat id are
    # ignored, never stored
    diarize.record_decision(1, "teleport", 5)
    diarize.record_decision(1, "cloud", 1900)
    assert diarize.last_decision(1)["path"] == "unresolved"
    diarize.record_decision(None, "local", 5)
    assert diarize.last_decision(None) is None


def test_last_decision_store_is_bounded():
    diarize._LAST_DECISION.clear()
    for chat_id in range(20):
        diarize.record_decision(chat_id, "local", 100)
    assert len(diarize._LAST_DECISION) == diarize._DECISION_MAX_CHATS
    assert diarize.last_decision(19) is not None   # newest kept
    assert diarize.last_decision(0) is None        # oldest evicted


def _check(chat_id, monkeypatch, got):
    fake_naming(monkeypatch)["answers"] = [got]
    monkeypatch.setattr(
        "backend.voice.httpx.post",
        lambda *a, **k: pytest.fail("the voice check makes no EL call"))

    async def nowhere(*a, **k):
        return None
    monkeypatch.setattr(diarize, "_attach_until_deadline", nowhere)
    asyncio.run(voice_pass.run(chat_id, loud_pcm(1.0), 16000, time.time(),
                               diarize.RoomSession(), {"user_name": "Alex"},
                               None))


def test_an_unnamed_turn_records_the_reason_and_no_cloud(app, monkeypatch):
    """A turn the check can't name stays unresolved and fires no EL call.
    Since the thirteenth field test the pulse DOES record it, as an
    `unresolved` decision carrying the reason, so "identity pending" says
    which of several problems it was."""
    with TestClient(app, base_url="http://127.0.0.1") as c:
        chat = c.post("/api/chats", json={"participant_ids": []}).json()
    _check(chat["id"], monkeypatch, naming_answer("listening"))
    decision = diarize.last_decision(chat["id"])
    assert decision["path"] == diarize.DECISION_UNRESOLVED
    assert decision["reason"] == "listening"


def test_a_named_turn_records_a_local_decision(app, monkeypatch):
    """The owner named in a solo-style room-off chat is still an
    identification - it stamps a 'local' decision (the health strip's pulse
    for the common case)."""
    with TestClient(app, base_url="http://127.0.0.1") as c:
        pid = _mint_sufficient("Alex")  # the owner, sufficiently enrolled
        chat = c.post("/api/chats", json={"participant_ids": []}).json()
    _check(chat["id"], monkeypatch, naming_answer(name="Alex", pid=pid))
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
        # #312: refusal visibility rides the learning map, content-free
        for row in h["learning"].values():
            assert row["refused_last_week"] == 0
            assert row["refusal_reason"] == ""
        assert h["chat"] is None
        assert h["last_decision"] is None
        # #154: the live model is named - file, hash prefix, pin state
        assert h["model"]["file"] == voiceid.MODEL_FILENAME
        assert h["model"]["pinned_default"] is True


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


# ── the model identity readout (#154) ───────────────────────────────────────

def test_model_identity_names_the_default_pin():
    ident = voiceid.model_identity({})
    assert ident["file"] == voiceid.MODEL_FILENAME
    assert len(ident["sha256_prefix"]) == 12
    assert ident["pinned_default"] is True


def test_model_identity_marks_an_overridden_pin():
    cfg = {"voice_id_model_url": "https://example.test/other-model.onnx",
           "voice_id_model_sha256": "AB" * 32}
    ident = voiceid.model_identity(cfg)
    assert ident["file"] == "other-model.onnx"
    assert ident["sha256_prefix"] == "ab" * 6
    assert ident["pinned_default"] is False


def test_model_identity_is_content_free_and_passive(monkeypatch):
    # reading it never touches the state machine (same law as matcher_status)
    monkeypatch.setattr(voiceid, "sherpa_onnx", object())
    voiceid._set_state("cold")
    voiceid.model_identity({})
    assert voiceid._state == "cold"
