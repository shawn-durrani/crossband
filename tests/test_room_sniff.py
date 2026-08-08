"""Session-start sniff (#28, third field test): remembered voices can arm.

The structural gap the third field test exposed: in a FRESH chat, room mode
only ever armed via a confirmed introduction or the explicit toggle, and the
diarization pass only ran once room mode was on - so a person whose voice is
remembered with sufficient anchors was undetectable by construction. The
sniff closes it: when a voice session starts in a chat where room mode is
OFF but sufficient remembered non-owner voices exist, the first TWO committed
utterances also run the existing diarization pass. A match on a remembered
non-owner voice, or two clusters in one utterance, arms room mode
server-side, seeds the roster with the matched people, and labels that turn;
otherwise the sniff ends and cost exactly two batch calls.

What these tests prove, in order of importance:

1. THE LATENCY PIN, inherited from phase 1: the committed transcript arrives
   while the sniff's batch call is deliberately wedged open - nothing the
   live path produces ever waits on a sniff pass.
2. A remembered sufficient voice speaking in a fresh chat arms room mode
   (durably AND in the live mirror), joins the roster linked to its anchors,
   and labels the turn with its name.
3. The sniff is BOUNDED: two negative passes end it - a third commit makes
   no batch call, and room mode stays off.
4. The sniff is PINNED OFF when no remembered people exist, and when the
   only sufficient voice is the owner's own.
5. Two clusters with no anchor match still arm (two people are audibly
   present) with ordinal labels, but seed nobody and raise no ask - the
   room-mode pass machinery takes over from the next utterance.
6. Every sniff pass logs with the sniff marker and is metered as real spend.
"""

import base64
import json
import threading
import time

import httpx
import pytest
from fastapi.testclient import TestClient

from backend import anchors, db, diarize
from backend.app import create_app
from backend.config import Settings
from backend.routers import voice as voice_router


@pytest.fixture
def app(tmp_path):
    diarize._ROOM_ENABLED.clear()
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
def batch_stt(monkeypatch):
    """The batch STT double. state['gate'] (a threading.Event) wedges every
    call open - the latency pin's lever."""
    state = {"calls": [], "responses": [], "gate": None}

    def fake_post(url, headers=None, data=None, files=None, timeout=None):
        if state["gate"] is not None:
            state["gate"].wait(10)
        state["calls"].append({"url": url, "data": dict(data or {}),
                               "audio": files["file"][1]})
        nxt = state["responses"].pop(0) if state["responses"] else _resp()
        if isinstance(nxt, Exception):
            raise nxt
        return httpx.Response(200, json=nxt,
                              request=httpx.Request("POST", url))

    monkeypatch.setattr(voice_router.voice.httpx, "post", fake_post)
    return state


def _resp(*entries):
    return {"language_code": "en", "text": "hello world",
            "words": [{"text": f"w{i}", "type": "word", "speaker_id": s,
                       "start": a, "end": b}
                      for i, (s, a, b) in enumerate(entries)]}


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
    for _ in range(clips):
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


def _flags(chat_id):
    con = db.connect()
    try:
        return db.get_room_flags(con, chat_id, open_only=True)
    finally:
        con.close()


def _message_labels(msg_id):
    con = db.connect()
    try:
        row = con.execute("SELECT voice_labels FROM messages WHERE id=?",
                          (msg_id,)).fetchone()
        return row["voice_labels"] if row else None
    finally:
        con.close()


def _insert_user_message(chat_id, text="hello world"):
    con = db.connect()
    try:
        return db.insert_message(con, chat_id, "user", text)
    finally:
        con.close()


def _stt_usage_rows():
    con = db.connect()
    try:
        return con.execute(
            "SELECT COUNT(*) FROM voice_usage WHERE kind='stt'").fetchone()[0]
    finally:
        con.close()


PREFIX_1 = anchors.PREFIX_PERSON_SECONDS


# ── 1. the latency pin ──────────────────────────────────────────────────────

def test_committed_transcript_arrives_while_the_sniff_is_wedged_open(
        app, relay, batch_stt):
    """The core law: the live path completes while the sniff's batch call is
    still blocked. Only after the gate opens does room mode arm."""
    _remember("Sam")
    batch_stt["gate"] = threading.Event()
    batch_stt["responses"] = [_resp(("speaker_0", 0.4, 0.9),
                                    ("speaker_0", PREFIX_1 + 0.5,
                                     PREFIX_1 + 0.9))]
    with TestClient(app, base_url="http://127.0.0.1") as c:
        chat = c.post("/api/chats", json={"participant_ids": []}).json()
        with c.websocket_connect("/api/voice/stt-stream") as ws:
            ws.send_json({"chat_id": chat["id"]})
            ws.send_json(_frame(loud_pcm(1.5), commit=True))
            # the committed transcript is here; the sniff is still wedged
            assert ws.receive_json() == {"final": "hello world"}
            assert _chat_room_mode(chat["id"]) is False
            batch_stt["gate"].set()      # release; the sniff catches up
            assert _wait_for(lambda: _chat_room_mode(chat["id"]))
            ws.send_json({"done": True})


# ── 2. a remembered voice arms a fresh chat ─────────────────────────────────

def test_remembered_voice_arms_room_mode_seeds_roster_and_labels(
        app, relay, batch_stt, caplog):
    import logging as _logging
    pid = _remember("Sam")
    batch_stt["responses"] = [_resp(("speaker_0", 0.4, 0.9),
                                    ("speaker_0", PREFIX_1 + 0.5,
                                     PREFIX_1 + 0.9))]
    with caplog.at_level(_logging.INFO, logger="crossband.diarize"):
        with TestClient(app, base_url="http://127.0.0.1") as c:
            chat = c.post("/api/chats", json={"participant_ids": []}).json()
            utter = loud_pcm(1.5)
            with c.websocket_connect("/api/voice/stt-stream") as ws:
                ws.send_json({"chat_id": chat["id"]})
                ws.send_json(_frame(utter, commit=True))
                assert ws.receive_json() == {"final": "hello world"}
                msg = _insert_user_message(chat["id"])
                labels = _wait_for(lambda: _message_labels(msg["id"]))
                assert _wait_for(lambda: _chat_room_mode(chat["id"]))
                # metered as real spend before the session even closes
                assert _wait_for(lambda: _stt_usage_rows() >= 1)
                ws.send_json({"done": True})
    # durable flag AND the live mirror the relay reads at commit boundaries
    assert diarize.room_enabled(chat["id"]) is True
    # the roster seeded with the matched person, linked to their anchors
    roster = _roster(chat["id"])
    assert [(p["name"], p["person_id"]) for p in roster] == [("Sam", pid)]
    # the turn labelled with the remembered name, confidently
    assert json.loads(labels) == {"clusters": ["speaker_0"],
                                  "labels": ["Sam"], "uncertain": []}
    # the request was the existing pass machinery: anchor prefix + hint
    call = batch_stt["calls"][0]
    assert call["data"]["num_speakers"] == "2"   # 1 sufficient person + 1
    prefix_pcm, _ = anchors.store().build_prefix([pid], 16000)
    assert call["audio"] == diarize.pcm16_wav(prefix_pcm + utter, 16000)
    # and every sniff pass logs with the sniff marker
    assert any("diarize pass (sniff)" in r.getMessage()
               for r in caplog.records)


def test_negative_first_pass_then_match_on_the_second_arms(
        app, relay, batch_stt):
    pid = _remember("Sam")
    batch_stt["responses"] = [
        _resp(("speaker_7", PREFIX_1 + 0.3, PREFIX_1 + 0.7)),   # stranger? no
        _resp(("speaker_0", 0.4, 0.9),
              ("speaker_0", PREFIX_1 + 0.5, PREFIX_1 + 0.9)),   # Sam
    ]
    with TestClient(app, base_url="http://127.0.0.1") as c:
        chat = c.post("/api/chats", json={"participant_ids": []}).json()
        with c.websocket_connect("/api/voice/stt-stream") as ws:
            ws.send_json({"chat_id": chat["id"]})
            ws.send_json(_frame(loud_pcm(1.5), commit=True))
            assert ws.receive_json() == {"final": "hello world"}
            assert _wait_for(lambda: len(batch_stt["calls"]) == 1)
            assert _chat_room_mode(chat["id"]) is False
            ws.send_json(_frame(loud_pcm(1.5), commit=True))
            assert ws.receive_json() == {"final": "hello world"}
            assert _wait_for(lambda: _chat_room_mode(chat["id"]))
            ws.send_json({"done": True})
    assert [p["person_id"] for p in _roster(chat["id"])] == [pid]


# ── 3. bounded: two calls, then the sniff ends ──────────────────────────────

def test_two_negative_passes_end_the_sniff(app, relay, batch_stt):
    _remember("Sam")
    batch_stt["responses"] = [
        _resp(("speaker_7", PREFIX_1 + 0.3, PREFIX_1 + 0.7)),
        _resp(("speaker_7", PREFIX_1 + 0.3, PREFIX_1 + 0.7)),
    ]
    with TestClient(app, base_url="http://127.0.0.1") as c:
        chat = c.post("/api/chats", json={"participant_ids": []}).json()
        with c.websocket_connect("/api/voice/stt-stream") as ws:
            ws.send_json({"chat_id": chat["id"]})
            for _ in range(3):
                ws.send_json(_frame(loud_pcm(1.5), commit=True))
                assert ws.receive_json() == {"final": "hello world"}
            time.sleep(0.4)
            ws.send_json({"done": True})
    assert len(batch_stt["calls"]) == 2      # the whole cost, exactly
    assert _chat_room_mode(chat["id"]) is False
    assert _roster(chat["id"]) == []


# ── 4. pinned off ───────────────────────────────────────────────────────────

def test_sniff_pinned_off_with_no_remembered_people(app, relay, batch_stt):
    with TestClient(app, base_url="http://127.0.0.1") as c:
        chat = c.post("/api/chats", json={"participant_ids": []}).json()
        with c.websocket_connect("/api/voice/stt-stream") as ws:
            ws.send_json({"chat_id": chat["id"]})
            ws.send_json(_frame(loud_pcm(1.0), commit=True))
            assert ws.receive_json() == {"final": "hello world"}
            time.sleep(0.3)
            ws.send_json({"done": True})
    assert batch_stt["calls"] == []
    assert _chat_room_mode(chat["id"]) is False


def test_sniff_pinned_off_when_only_the_owner_is_remembered(
        app, relay, batch_stt):
    """Owner-only anchors buy the sniff nothing: there is nobody else it
    could re-identify, and a genuinely new second voice still has the
    introduction and ask paths."""
    _remember("Alex")                        # the app fixture's user_name
    with TestClient(app, base_url="http://127.0.0.1") as c:
        chat = c.post("/api/chats", json={"participant_ids": []}).json()
        with c.websocket_connect("/api/voice/stt-stream") as ws:
            ws.send_json({"chat_id": chat["id"]})
            ws.send_json(_frame(loud_pcm(1.0), commit=True))
            assert ws.receive_json() == {"final": "hello world"}
            time.sleep(0.3)
            ws.send_json({"done": True})
    assert batch_stt["calls"] == []
    assert _chat_room_mode(chat["id"]) is False


def test_sniff_eligibility_rules():
    person = lambda name, ok: {"name": name, "sufficient": ok}
    assert diarize.sniff_eligible([person("Sam", True)], "Alex") is True
    assert diarize.sniff_eligible([person("Alex", True)], "Alex") is False
    assert diarize.sniff_eligible([person("alex", True)], "Alex") is False
    assert diarize.sniff_eligible([person("Sam", False)], "Alex") is False
    assert diarize.sniff_eligible([], "Alex") is False
    assert diarize.sniff_eligible(None, "Alex") is False
    assert diarize.sniff_eligible(
        [person("Alex", True), person("Sam", True)], "Alex") is True


# ── 5. owner match alone never arms; two clusters always do ─────────────────

def test_owner_match_alone_does_not_arm(app, relay, batch_stt):
    """The owner speaking alone in a fresh chat is the overwhelmingly common
    case, and it must stay exactly what it always was: no room mode, no
    roster, no labels."""
    _remember("Alex")                        # owner, created first
    _remember("Sam")                         # the reason the sniff runs
    two = 2 * PREFIX_1
    batch_stt["responses"] = [_resp(
        ("speaker_0", 0.4, 0.9),             # Alex's anchor segment
        ("speaker_0", two + 0.5, two + 0.9))]  # utterance: Alex again
    with TestClient(app, base_url="http://127.0.0.1") as c:
        chat = c.post("/api/chats", json={"participant_ids": []}).json()
        with c.websocket_connect("/api/voice/stt-stream") as ws:
            ws.send_json({"chat_id": chat["id"]})
            ws.send_json(_frame(loud_pcm(1.5), commit=True))
            assert ws.receive_json() == {"final": "hello world"}
            assert _wait_for(lambda: batch_stt["calls"])
            time.sleep(0.3)
            ws.send_json({"done": True})
    assert _chat_room_mode(chat["id"]) is False
    assert _roster(chat["id"]) == []


def test_two_clusters_arm_with_ordinals_but_seed_and_ask_nothing(
        app, relay, batch_stt):
    """Two voices in the first utterance of a fresh chat: audibly a room, so
    room mode arms - but with no anchor match nobody is guessed onto the
    roster and no ask is raised; the room-mode pass machinery (which does
    ask) owns every utterance from here."""
    _remember("Sam")
    batch_stt["responses"] = [_resp(
        ("speaker_5", PREFIX_1 + 0.1, PREFIX_1 + 0.4),
        ("speaker_6", PREFIX_1 + 0.5, PREFIX_1 + 0.9))]
    with TestClient(app, base_url="http://127.0.0.1") as c:
        chat = c.post("/api/chats", json={"participant_ids": []}).json()
        with c.websocket_connect("/api/voice/stt-stream") as ws:
            ws.send_json({"chat_id": chat["id"]})
            ws.send_json(_frame(loud_pcm(1.5), commit=True))
            assert ws.receive_json() == {"final": "hello world"}
            msg = _insert_user_message(chat["id"])
            labels = _wait_for(lambda: _message_labels(msg["id"]))
            assert _wait_for(lambda: _chat_room_mode(chat["id"]))
            ws.send_json({"done": True})
    data = json.loads(labels)
    assert data["labels"] == ["Voice 1", "Voice 2"]
    assert data["uncertain"] == ["Voice 1", "Voice 2"]
    assert _roster(chat["id"]) == []         # nobody guessed onto the roster
    assert _flags(chat["id"]) == []          # the sniff itself never asks
