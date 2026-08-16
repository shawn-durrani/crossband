"""The session-start EL sniff is RETIRED (#28 PR-B): pins that it never fires.

History, honestly: the sniff (third field test) closed a real gap - a
remembered voice could not arm a fresh chat - at the cost of up to two
ElevenLabs batch calls per session. The ambient local check then took over
the arming job for free, and the eighth field test convicted the EL identity
path outright: a solo utterance was falsely split against the anchor prefix,
matched, ARMED room mode and MIS-NAMED the turn - false split, false arm,
false name, from the cloud path alone. The owner decision followed: the
cloud identity fallback retires entirely, the sniff with it.

What this file pins now, in order:

1. STRUCTURAL RETIREMENT: the sniff's machinery is gone from the codebase -
   no function is left for any path to call.
2. The exact eighth-field-test conditions - a fresh chat, remembered
   non-owner voices, first utterances the matcher cannot decide - fire ZERO
   ElevenLabs calls: room mode stays off, nothing is named, nothing is
   metered. The false arm-and-name is structurally impossible.
3. Matcher unavailable (the CI default) means NO automatic voice arming at
   all - and the manual doors (the spoken arm command, standing in for all
   three) still work. Degraded means manual, never wrong.
4. A remembered voice still arms a fresh chat - through the ambient LOCAL
   check (test_room_ambient.py owns the full decision table; the arming pin
   here proves the sniff's old job is genuinely covered, not dropped).
"""

import base64
import json
import time

import pytest
from fastapi.testclient import TestClient

from backend import anchors, db, diarize, voiceid
from backend.app import create_app
from backend.config import Settings
from backend.routers import voice as voice_router


@pytest.fixture
def app(tmp_path):
    diarize._ROOM_ENABLED.clear()
    diarize._AMBIENT_OFF.clear()
    diarize._STASHED.clear()
    anchors.clear_recent_audio()
    settings = Settings(data_dir=str(tmp_path / "data"),
                        memory_url="http://127.0.0.1:1",
                        user_name="Alex")
    return create_app(settings)


class FakeEleven:
    def __init__(self):
        self.sent = []
        self.queue = __import__("asyncio").Queue()

    async def __aenter__(self):
        return self

    async def __aexit__(self, *a):
        return False

    async def send(self, raw):
        msg = json.loads(raw)
        self.sent.append(msg)
        if msg.get("commit"):
            self.queue.put_nowait(json.dumps(
                {"message_type": "committed_transcript", "text": "hello world"}))
        elif msg.get("audio_base_64"):
            self.queue.put_nowait(json.dumps(
                {"message_type": "partial_transcript", "text": "hello"}))

    def __aiter__(self):
        return self

    async def __anext__(self):
        return await self.queue.get()


@pytest.fixture
def relay(app, monkeypatch):
    fake = FakeEleven()
    monkeypatch.setattr(voice_router.websockets, "connect",
                        lambda *a, **kw: fake)
    monkeypatch.setattr(voice_router.voice, "enabled", lambda: True)
    monkeypatch.setattr(voice_router.voice, "api_key", lambda: "test-key")
    monkeypatch.setattr(voice_router.engine, "prewarm_recall",
                        lambda *a, **kw: None)
    app.state.allowed_hosts = {"testserver", "127.0.0.1", "localhost", "::1"}
    return fake


@pytest.fixture
def batch_calls(monkeypatch):
    """Counts EL batch STT calls - the whole point of this file is that the
    number stays ZERO."""
    state = {"calls": 0}

    def fake_post(url, headers=None, data=None, files=None, timeout=None):
        state["calls"] += 1
        import httpx
        return httpx.Response(200, json={"language_code": "en",
                                         "text": "hello world", "words": []},
                              request=httpx.Request("POST", url))

    monkeypatch.setattr(voice_router.voice.httpx, "post", fake_post)
    return state


def loud_pcm(seconds, sample_rate=16000):
    return b"\x00\x40" * int(seconds * sample_rate)


def _frame(data, commit=False):
    return {"audio": base64.b64encode(data).decode(),
            "sample_rate": 16000, "commit": commit}


def _wait_for(pred, timeout=6.0, interval=0.02):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        v = pred()
        if v:
            return v
        time.sleep(interval)
    return pred()


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


def _chat_room_mode(chat_id):
    con = db.connect()
    try:
        row = con.execute("SELECT room_mode FROM chats WHERE id=?",
                          (chat_id,)).fetchone()
        return bool(row and row["room_mode"])
    finally:
        con.close()


def _roster(chat_id):
    con = db.connect()
    try:
        return db.get_room_roster(con, chat_id, present_only=True)
    finally:
        con.close()


def _stt_usage_rows():
    con = db.connect()
    try:
        return con.execute(
            "SELECT COUNT(*) FROM voice_usage WHERE kind='stt'").fetchone()[0]
    finally:
        con.close()


# ── 1. structural retirement ────────────────────────────────────────────────

def test_the_sniff_machinery_no_longer_exists():
    for name in ("run_sniff", "schedule_sniff", "_sniff_plan",
                 "sniff_eligible", "SNIFF_UTTERANCES", "_arm_from_sniff",
                 "_fast_sniff_pass"):
        assert not hasattr(diarize, name), name
    assert not hasattr(diarize.RoomSession(), "sniff_remaining")


# ── 2. the eighth field test can never recur ────────────────────────────────

def test_undecidable_first_utterances_fire_no_el_call_and_arm_nothing(
        app, relay, batch_calls, monkeypatch):
    """The exact conditions of the eighth field test: fresh chat, remembered
    non-owner voice, and first utterances the matcher can only call
    "ambiguous" (freshly rebuilt banks). Pre-PR-B this deferred into an EL
    sniff that falsely split a SOLO speaker against the guest's anchor
    prefix, armed room mode and mis-named the turn. Now: zero EL calls, room
    stays off, roster stays empty, nothing is named, nothing is metered
    beyond the relay's own realtime seconds."""
    _remember("Sam")
    monkeypatch.setattr(
        voiceid, "identify_utterance",
        lambda *a, **k: {"status": "defer", "person_id": None, "name": None,
                         "score": 0.4, "reason": "ambiguous"})
    with TestClient(app, base_url="http://127.0.0.1") as c:
        chat = c.post("/api/chats", json={"participant_ids": []}).json()
        with c.websocket_connect("/api/voice/stt-stream") as ws:
            ws.send_json({"chat_id": chat["id"]})
            assert ws.receive_json()["session"]  # #134 handshake
            for _ in range(3):
                ws.send_json(_frame(loud_pcm(1.5), commit=True))
                assert ws.receive_json() == {"final": "hello world"}
            time.sleep(0.4)  # give a wrongly-scheduled EL task time to fire
            ws.send_json({"done": True})
    assert batch_calls["calls"] == 0          # the sniff is gone, provably
    assert _chat_room_mode(chat["id"]) is False
    assert _roster(chat["id"]) == []
    # only the relay's own end-of-session realtime metering row exists
    assert _wait_for(lambda: _stt_usage_rows() == 1)
    time.sleep(0.2)
    assert _stt_usage_rows() == 1


# ── 3. matcher unavailable: manual, never wrong ─────────────────────────────

def test_matcher_unavailable_means_no_automatic_arming_and_no_el(
        app, relay, batch_calls):
    """The CI default IS the degraded case: sherpa/model absent, so every
    identify defers "unavailable". Pre-PR-B this fell back to the metered EL
    sniff; now nothing automatic happens at all - and the manual door still
    works (the spoken arm command, standing in for introductions and the
    toggle, which share the same control plumbing)."""
    from backend import introductions
    _remember("Sam")
    with TestClient(app, base_url="http://127.0.0.1") as c:
        chat = c.post("/api/chats", json={"participant_ids": []}).json()
        with c.websocket_connect("/api/voice/stt-stream") as ws:
            ws.send_json({"chat_id": chat["id"]})
            assert ws.receive_json()["session"]  # #134 handshake
            ws.send_json(_frame(loud_pcm(1.5), commit=True))
            assert ws.receive_json() == {"final": "hello world"}
            time.sleep(0.3)
            assert _chat_room_mode(chat["id"]) is False   # nothing automatic
            # degraded means MANUAL: the arm command still arms
            cfg = app.state.settings.as_cfg()
            introductions.apply_command(chat["id"], introductions.COMMAND_ARM,
                                        cfg)
            assert _chat_room_mode(chat["id"]) is True
            ws.send_json({"done": True})
    assert batch_calls["calls"] == 0
    assert [p["name"] for p in _roster(chat["id"])] == ["Alex"]  # the owner


def test_matcher_disabled_by_flag_schedules_nothing(tmp_path, monkeypatch):
    """voice_id_enabled=false: ambient is not even eligible, so no check is
    scheduled and no arming happens - the pre-PR-B fallback to the sniff is
    deliberately gone (this replaces its test)."""
    diarize._ROOM_ENABLED.clear()
    diarize._AMBIENT_OFF.clear()
    anchors.clear_recent_audio()
    settings = Settings(data_dir=str(tmp_path / "data"),
                        memory_url="http://127.0.0.1:1",
                        user_name="Alex", voice_id_enabled=False)
    app = create_app(settings)
    fake = FakeEleven()
    monkeypatch.setattr(voice_router.websockets, "connect",
                        lambda *a, **kw: fake)
    monkeypatch.setattr(voice_router.voice, "enabled", lambda: True)
    monkeypatch.setattr(voice_router.voice, "api_key", lambda: "test-key")
    monkeypatch.setattr(voice_router.engine, "prewarm_recall",
                        lambda *a, **kw: None)
    app.state.allowed_hosts = {"testserver", "127.0.0.1", "localhost", "::1"}
    _remember("Sam")
    monkeypatch.setattr(
        voiceid, "identify_utterance",
        lambda *a, **k: pytest.fail("matcher disabled - never consulted"))
    monkeypatch.setattr(
        voice_router.voice.httpx, "post",
        lambda *a, **kw: pytest.fail("no EL call may fire"))
    with TestClient(app, base_url="http://127.0.0.1") as c:
        chat = c.post("/api/chats", json={"participant_ids": []}).json()
        with c.websocket_connect("/api/voice/stt-stream") as ws:
            ws.send_json({"chat_id": chat["id"]})
            assert ws.receive_json()["session"]  # #134 handshake
            ws.send_json(_frame(loud_pcm(1.5), commit=True))
            assert ws.receive_json() == {"final": "hello world"}
            time.sleep(0.3)
            ws.send_json({"done": True})
    assert _chat_room_mode(chat["id"]) is False


# ── 4. the sniff's old job is covered, not dropped ──────────────────────────

def test_remembered_voice_still_arms_a_fresh_chat_locally(
        app, relay, batch_calls, monkeypatch):
    """The gap the sniff existed for stays closed: a remembered non-owner
    voice speaking in a fresh chat arms room mode and joins the roster - via
    the ambient LOCAL check, with zero EL calls. (The full ambient decision
    table lives in test_room_ambient.py.)"""
    pid = _remember("Sam")
    monkeypatch.setattr(
        voiceid, "identify_utterance",
        lambda *a, **k: {"status": "match", "person_id": pid, "name": "Sam",
                         "score": 0.9, "reason": "match"})
    with TestClient(app, base_url="http://127.0.0.1") as c:
        chat = c.post("/api/chats", json={"participant_ids": []}).json()
        with c.websocket_connect("/api/voice/stt-stream") as ws:
            ws.send_json({"chat_id": chat["id"]})
            assert ws.receive_json()["session"]  # #134 handshake
            ws.send_json(_frame(loud_pcm(1.5), commit=True))
            assert ws.receive_json() == {"final": "hello world"}
            assert _wait_for(lambda: _chat_room_mode(chat["id"]))
            ws.send_json({"done": True})
    assert diarize.room_enabled(chat["id"]) is True
    assert [(p["name"], p["person_id"]) for p in _roster(chat["id"])] \
        == [("Sam", pid)]
    assert batch_calls["calls"] == 0
