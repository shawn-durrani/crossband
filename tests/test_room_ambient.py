"""Ambient room detection (#28): no trigger phrase required.

The arming ceremony existed to gate the cost of cloud identity; the local
matcher made identity ~free, so with room mode OFF every committed utterance
now gets a quiet LOCAL-ONLY check. What these tests prove, in order:

1. THE LATENCY PIN: the committed transcript arrives while the ambient
   check is deliberately wedged open - the live path never waits on it.
2. The decision table, end to end through the relay: the owner's voice is a
   no-op; a remembered non-owner arms room mode, joins the roster and names
   the turn; a clear stranger (decidable only because the owner is enrolled)
   arms, labels an ordinal, and raises the ask-fallback; an undecidable
   verdict defers with room mode untouched.
3. AMBIENT IS LOCAL-ONLY: none of those decisions makes an ElevenLabs batch
   call.
4. DISARM IS SACRED: "solo mode" sets a durable ambient-off (even with room
   mode already off), sessions in a disarmed chat never schedule ambient
   checks, a mid-session disarm is honoured at the next commit, and every
   explicit re-enable (arm command, introduction, manual toggle) clears it.
5. The bounded EL sniff survives as the fallback: with the matcher disabled,
   eligibility falls back to the sniff exactly as before ambient existed.
"""

import base64
import json
import threading
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
    """Counts EL batch STT calls - ambient decisions must make NONE."""
    state = {"calls": 0}

    def fake_post(url, headers=None, data=None, files=None, timeout=None):
        state["calls"] += 1
        import httpx
        return httpx.Response(200, json={"language_code": "en",
                                         "text": "hello world", "words": []},
                              request=httpx.Request("POST", url))

    monkeypatch.setattr(voice_router.voice.httpx, "post", fake_post)
    return state


def _verdict_match(name, pid, score=0.8):
    return {"status": voiceid.MATCH, "person_id": pid, "name": name,
            "score": score, "reason": "match"}


def _verdict_defer(reason, score=0.3):
    return {"status": voiceid.DEFER, "person_id": None, "name": None,
            "score": score, "reason": reason}


@pytest.fixture
def matcher(monkeypatch):
    """The local matcher double: a queue of verdicts, an optional wedge, and
    a call log. Patched at the voiceid module so diarize's reference sees it."""
    state = {"verdicts": [], "calls": 0, "gate": None}

    def fake_identify(pcm, sample_rate, candidates, cfg):
        if state["gate"] is not None:
            state["gate"].wait(10)
        state["calls"] += 1
        if state["verdicts"]:
            return state["verdicts"].pop(0)
        return _verdict_defer("unavailable")

    monkeypatch.setattr(voiceid, "identify_utterance", fake_identify)
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
    for _ in range(clips):
        assert store.add_clip(pid, loud_pcm(2.0), 16000, source="accumulated")
    return pid


def _chat_state(chat_id):
    con = db.connect()
    try:
        row = con.execute("SELECT room_mode, ambient_off FROM chats WHERE id=?",
                          (chat_id,)).fetchone()
        return bool(row["room_mode"]), bool(row["ambient_off"])
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


def _new_chat(c):
    return c.post("/api/chats", json={"participant_ids": []}).json()


# ── pure rules ──────────────────────────────────────────────────────────────

def test_ambient_decision_table():
    owner = _verdict_match("Alex", "p0")
    guest = _verdict_match("Sam", "p1")
    below = _verdict_defer("below_threshold")
    assert diarize.ambient_decision(owner, "Alex", True) == "noop_owner"
    assert diarize.ambient_decision(owner, "alex", False) == "noop_owner"
    assert diarize.ambient_decision(guest, "Alex", True) == "arm_known"
    assert diarize.ambient_decision(below, "Alex", True) == "arm_unknown"
    # without the owner enrolled, "below threshold" could BE the owner
    assert diarize.ambient_decision(below, "Alex", False) == "defer"
    for reason in ("ambiguous", "multi", "too_short", "unavailable",
                   "no_candidates"):
        assert diarize.ambient_decision(_verdict_defer(reason),
                                        "Alex", True) == "defer"
    assert diarize.ambient_decision(None, "Alex", True) == "defer"


def test_ambient_eligibility_rules():
    on = {"voice_id_enabled": True}
    off = {"voice_id_enabled": False}
    somebody = [{"name": "Sam", "sufficient": True}]
    nobody = [{"name": "Sam", "sufficient": False}]
    assert diarize.ambient_eligible(somebody, on) is True
    assert diarize.ambient_eligible(nobody, on) is False
    assert diarize.ambient_eligible([], on) is False
    assert diarize.ambient_eligible(somebody, off) is False
    assert diarize.owner_sufficient(
        [{"name": "Alex", "sufficient": True}], "Alex") is True
    assert diarize.owner_sufficient(
        [{"name": "Alex", "sufficient": False}], "Alex") is False
    assert diarize.owner_sufficient(somebody, "Alex") is False


# ── 1. the latency pin ──────────────────────────────────────────────────────

def test_committed_transcript_arrives_while_ambient_is_wedged(
        app, relay, batch_calls, matcher):
    _remember("Sam")
    matcher["gate"] = threading.Event()
    matcher["verdicts"] = [_verdict_match("Sam", "ignored")]
    with TestClient(app, base_url="http://127.0.0.1") as c:
        chat = _new_chat(c)
        with c.websocket_connect("/api/voice/stt-stream") as ws:
            ws.send_json({"chat_id": chat["id"]})
            ws.send_json(_frame(loud_pcm(1.5), commit=True))
            # the live path completes while the ambient check is blocked
            assert ws.receive_json() == {"final": "hello world"}
            assert _chat_state(chat["id"])[0] is False
            matcher["gate"].set()
            assert _wait_for(lambda: _chat_state(chat["id"])[0])
            ws.send_json({"done": True})
    assert batch_calls["calls"] == 0  # local-only, even on the arm


# ── 2. the decision table, end to end ───────────────────────────────────────

def test_owner_voice_is_a_noop(app, relay, batch_calls, matcher):
    _remember("Alex")  # the owner, sufficiently enrolled
    pid_owner = anchors.store().find_by_name("Alex")["person_id"]
    matcher["verdicts"] = [_verdict_match("Alex", pid_owner)]
    with TestClient(app, base_url="http://127.0.0.1") as c:
        chat = _new_chat(c)
        with c.websocket_connect("/api/voice/stt-stream") as ws:
            ws.send_json({"chat_id": chat["id"]})
            ws.send_json(_frame(loud_pcm(1.5), commit=True))
            assert ws.receive_json() == {"final": "hello world"}
            assert _wait_for(lambda: matcher["calls"] >= 1)
            ws.send_json({"done": True})
    on, _ = _chat_state(chat["id"])
    assert on is False
    assert _roster(chat["id"]) == []
    assert batch_calls["calls"] == 0


def test_remembered_voice_arms_names_and_rosters(app, relay, batch_calls,
                                                 matcher):
    pid = _remember("Sam")
    matcher["verdicts"] = [_verdict_match("Sam", pid)]
    with TestClient(app, base_url="http://127.0.0.1") as c:
        chat = _new_chat(c)
        with c.websocket_connect("/api/voice/stt-stream") as ws:
            ws.send_json({"chat_id": chat["id"]})
            ws.send_json(_frame(loud_pcm(1.5), commit=True))
            assert ws.receive_json() == {"final": "hello world"}
            msg = _insert_user_message(chat["id"])
            labels = _wait_for(lambda: _message_labels(msg["id"]))
            assert _wait_for(lambda: _chat_state(chat["id"])[0])
            ws.send_json({"done": True})
    assert diarize.room_enabled(chat["id"]) is True
    assert [(p["name"], p["person_id"]) for p in _roster(chat["id"])] \
        == [("Sam", pid)]
    assert json.loads(labels)["labels"] == ["Sam"]
    assert json.loads(labels)["uncertain"] == []
    assert batch_calls["calls"] == 0


def test_clear_stranger_arms_and_asks(app, relay, batch_calls, matcher):
    _remember("Alex")  # owner enrolled: below-threshold means NOT the owner
    matcher["verdicts"] = [_verdict_defer("below_threshold")]
    with TestClient(app, base_url="http://127.0.0.1") as c:
        chat = _new_chat(c)
        with c.websocket_connect("/api/voice/stt-stream") as ws:
            ws.send_json({"chat_id": chat["id"]})
            ws.send_json(_frame(loud_pcm(1.5), commit=True))
            assert ws.receive_json() == {"final": "hello world"}
            msg = _insert_user_message(chat["id"])
            labels = _wait_for(lambda: _message_labels(msg["id"]))
            assert _wait_for(lambda: _chat_state(chat["id"])[0])
            assert _wait_for(lambda: _flags(chat["id"]))
            ws.send_json({"done": True})
    # armed, the owner rostered so the armed pass runs anchored
    assert [p["name"] for p in _roster(chat["id"])] == ["Alex"]
    # the stranger's turn carries an ordinal, marked uncertain
    parsed = json.loads(labels)
    assert parsed["labels"] == ["Voice 1"]
    assert parsed["uncertain"] == ["Voice 1"]
    # and exactly one open ask
    flags = _flags(chat["id"])
    assert [f["kind"] for f in flags] == ["unknown_voice"]
    assert batch_calls["calls"] == 0


def test_stranger_without_owner_enrolment_defers(app, relay, batch_calls,
                                                 matcher):
    _remember("Sam")  # a guest is remembered, but the OWNER is not enrolled
    matcher["verdicts"] = [_verdict_defer("below_threshold")]
    with TestClient(app, base_url="http://127.0.0.1") as c:
        chat = _new_chat(c)
        with c.websocket_connect("/api/voice/stt-stream") as ws:
            ws.send_json({"chat_id": chat["id"]})
            ws.send_json(_frame(loud_pcm(1.5), commit=True))
            assert ws.receive_json() == {"final": "hello world"}
            assert _wait_for(lambda: matcher["calls"] >= 1)
            ws.send_json({"done": True})
    assert _chat_state(chat["id"])[0] is False
    assert _flags(chat["id"]) == []


# ── 4. disarm is sacred ─────────────────────────────────────────────────────

def test_solo_command_sets_ambient_off_even_when_room_already_off(app):
    from backend import introductions
    with TestClient(app, base_url="http://127.0.0.1") as c:
        chat = _new_chat(c)
        cfg = app.state.settings.as_cfg()
        outcome = introductions.apply_command(chat["id"],
                                              introductions.COMMAND_DISARM,
                                              cfg)
    assert outcome == "disarmed_by_command"
    on, ambient_off = _chat_state(chat["id"])
    assert on is False and ambient_off is True
    assert diarize.ambient_off(chat["id"]) is True


def test_disarmed_chat_never_schedules_ambient(app, relay, batch_calls,
                                               matcher):
    from backend import introductions
    pid = _remember("Sam")
    matcher["verdicts"] = [_verdict_match("Sam", pid)] * 3
    with TestClient(app, base_url="http://127.0.0.1") as c:
        chat = _new_chat(c)
        cfg = app.state.settings.as_cfg()
        introductions.apply_command(chat["id"], introductions.COMMAND_DISARM,
                                    cfg)
        with c.websocket_connect("/api/voice/stt-stream") as ws:
            ws.send_json({"chat_id": chat["id"]})
            ws.send_json(_frame(loud_pcm(1.5), commit=True))
            assert ws.receive_json() == {"final": "hello world"}
            ws.send_json(_frame(loud_pcm(1.5), commit=True))
            assert ws.receive_json() == {"final": "hello world"}
            time.sleep(0.3)  # give a wrongly-scheduled task time to act
            ws.send_json({"done": True})
    assert matcher["calls"] == 0          # never even consulted
    assert _chat_state(chat["id"]) == (False, True)
    assert batch_calls["calls"] == 0


def test_mid_session_disarm_is_honoured_at_the_next_commit(
        app, relay, batch_calls, matcher):
    from backend import introductions
    pid = _remember("Sam")
    matcher["verdicts"] = [_verdict_defer("ambiguous"),
                           _verdict_match("Sam", pid)]
    with TestClient(app, base_url="http://127.0.0.1") as c:
        chat = _new_chat(c)
        cfg = app.state.settings.as_cfg()
        with c.websocket_connect("/api/voice/stt-stream") as ws:
            ws.send_json({"chat_id": chat["id"]})
            ws.send_json(_frame(loud_pcm(1.5), commit=True))
            assert ws.receive_json() == {"final": "hello world"}
            assert _wait_for(lambda: matcher["calls"] == 1)   # ran, deferred
            introductions.apply_command(chat["id"],
                                        introductions.COMMAND_DISARM, cfg)
            ws.send_json(_frame(loud_pcm(1.5), commit=True))
            assert ws.receive_json() == {"final": "hello world"}
            time.sleep(0.3)
            ws.send_json({"done": True})
    assert matcher["calls"] == 1          # the second commit never checked
    assert _chat_state(chat["id"]) == (False, True)


def test_every_reenable_clears_ambient_off(app):
    from backend import introductions
    cfg_key = None
    with TestClient(app, base_url="http://127.0.0.1") as c:
        cfg = app.state.settings.as_cfg()
        from backend import introductions as intro
        # arm command clears it
        chat = _new_chat(c)
        intro.apply_command(chat["id"], intro.COMMAND_DISARM, cfg)
        assert _chat_state(chat["id"])[1] is True
        intro.apply_command(chat["id"], intro.COMMAND_ARM, cfg)
        assert _chat_state(chat["id"])[1] is False
        # a confirmed introduction clears it
        chat2 = _new_chat(c)
        intro.apply_command(chat2["id"], intro.COMMAND_DISARM, cfg)
        assert _chat_state(chat2["id"])[1] is True
        intro.apply_scan(chat2["id"],
                         {"introductions": ["Sam"], "departures": []},
                         cfg, text="this is Sam")
        assert _chat_state(chat2["id"])[1] is False
        # the manual toggle-on clears it, and (#28, fifth field test) behaves
        # like the arm command: the owner joins the roster so the pass runs
        # ANCHORED - the slow no-roster path can no longer be reached from
        # the control that looks like it should enable identification
        chat3 = _new_chat(c)
        intro.apply_command(chat3["id"], intro.COMMAND_DISARM, cfg)
        assert _chat_state(chat3["id"])[1] is True
        c.patch(f"/api/chats/{chat3['id']}", json={"room_mode": True})
        assert _chat_state(chat3["id"])[1] is False
        assert diarize.ambient_off(chat3["id"]) is False
        assert [p["name"] for p in _roster(chat3["id"])] == ["Alex"]


# ── 5. fallback: the bounded EL sniff survives ──────────────────────────────

def test_matcher_disabled_falls_back_to_the_bounded_sniff(
        tmp_path, monkeypatch):
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
    seen = {}
    orig = diarize.schedule_sniff

    def spy(*a, **kw):
        seen["sniffed"] = True
        return orig(*a, **kw)

    monkeypatch.setattr(diarize, "schedule_sniff", spy)
    monkeypatch.setattr(
        voice_router.voice.httpx, "post",
        lambda *a, **kw: (_ for _ in ()).throw(RuntimeError("no network")))
    with TestClient(app, base_url="http://127.0.0.1") as c:
        chat = _new_chat(c)
        with c.websocket_connect("/api/voice/stt-stream") as ws:
            ws.send_json({"chat_id": chat["id"]})
            ws.send_json(_frame(loud_pcm(1.5), commit=True))
            assert ws.receive_json() == {"final": "hello world"}
            assert _wait_for(lambda: seen.get("sniffed"))
            ws.send_json({"done": True})
    assert _chat_state(chat["id"])[0] is False  # failed EL call armed nothing
