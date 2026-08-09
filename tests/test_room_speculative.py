"""Speculative identity at silence-start (#28 PR-B): the head start's pins.

The client's VAD knows an utterance's audio is complete when the silence gap
BEGINS - roughly two seconds before the commit frame. It sends a
content-free hint ({"speculative": true}, no audio); the relay fires the
LOCAL-ONLY identity check on the buffered utterance right then and caches
the verdict on the RoomSession; the commit-time pass claims it and attaches
the name with no re-embed. What these tests prove, in order:

1. THE BYTE-IDENTITY PIN: the hint frame sends NOTHING upstream - the
   ElevenLabs byte stream is identical with and without hints.
2. THE LATENCY PIN: the committed transcript arrives while the speculative
   check is deliberately wedged open - the live path never waits on it.
3. NO RE-EMBED: a fresh hint's cached verdict labels the commit with exactly
   ONE matcher call for the whole turn.
4. STALENESS IS HONEST: audio with real speech after the hint discards the
   cached verdict and the commit-time check runs fresh; pure-rule coverage
   for the tail-RMS judgment and the session claim.
5. REVALIDATION: a cached match on a person OUTSIDE the consuming pass's
   candidates is not trusted - the check runs fresh rather than smuggling a
   name in.
6. The hint respects the sacred disarm: a "solo mode" chat schedules no
   speculative check at all.
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
def no_batch(monkeypatch):
    """Every test here is local-only: an ElevenLabs batch call is a bug."""
    monkeypatch.setattr(
        voice_router.voice.httpx, "post",
        lambda *a, **kw: pytest.fail("no EL call may fire in these tests"))


@pytest.fixture
def matcher(monkeypatch):
    """The matcher double: records (pcm bytes, candidate names) per call,
    serves a FIFO of verdicts, and can be wedged open."""
    state = {"verdicts": [], "calls": [], "gate": None}

    def fake_identify(pcm, sample_rate, candidates, cfg):
        if state["gate"] is not None:
            state["gate"].wait(10)
        state["calls"].append((len(pcm), [c["name"] for c in candidates]))
        if state["verdicts"]:
            return state["verdicts"].pop(0)
        return {"status": "defer", "person_id": None, "name": None,
                "score": 0.0, "reason": "unavailable"}

    monkeypatch.setattr(voiceid, "identify_utterance", fake_identify)
    return state


def _match(name, pid, score=0.9):
    return {"status": "match", "person_id": pid, "name": name,
            "score": score, "reason": "match"}


def loud_pcm(seconds, sample_rate=16000):
    return b"\x00\x40" * int(seconds * sample_rate)


def quiet_pcm(seconds, sample_rate=16000):
    return b"\x01\x00" * int(seconds * sample_rate)


def _frame(data, commit=False):
    return {"audio": base64.b64encode(data).decode(),
            "sample_rate": 16000, "commit": commit}


def _hint():
    # exactly what the client sends: content-free, no audio key
    return {"speculative": True, "sample_rate": 16000}


def _upstream(frame):
    return {"message_type": "input_audio_chunk",
            "audio_base_64": frame["audio"],
            "commit": frame["commit"],
            "sample_rate": frame["sample_rate"]}


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


def _room_chat(client, name="Sam"):
    chat = client.post("/api/chats", json={"participant_ids": []}).json()
    con = db.connect()
    db.set_chat_room_mode(con, chat["id"], True)
    diarize.set_room_enabled(chat["id"], True)
    pid = _remember(name)
    db.add_room_person(con, chat["id"], name, person_id=pid)
    con.close()
    return chat, pid


def _message_labels(msg_id):
    con = db.connect()
    try:
        row = con.execute("SELECT voice_labels FROM messages WHERE id=?",
                          (msg_id,)).fetchone()
        return row["voice_labels"] if row else None
    finally:
        con.close()


def _insert_user_message(chat_id, text="hello world", voice_turn_id=""):
    con = db.connect()
    try:
        return db.insert_message(con, chat_id, "user", text,
                                 voice_turn_id=voice_turn_id)
    finally:
        con.close()


# ── 1. the byte-identity pin ────────────────────────────────────────────────

def test_hint_frames_send_nothing_upstream(app, relay, no_batch, matcher):
    """The hint is ours alone: with hints interleaved, the frames reaching
    ElevenLabs are byte-for-byte the frames a hint-free session sends."""
    frames = [_frame(loud_pcm(0.5)), _frame(loud_pcm(0.5), commit=True)]
    with TestClient(app, base_url="http://127.0.0.1") as c:
        chat, _pid = _room_chat(c)
        with c.websocket_connect("/api/voice/stt-stream") as ws:
            ws.send_json({"chat_id": chat["id"]})
            ws.send_json(frames[0])
            assert ws.receive_json() == {"partial": "hello"}
            ws.send_json(_hint())
            ws.send_json(_hint())   # a duplicate hint is harmless too
            ws.send_json(frames[1])
            assert ws.receive_json() == {"final": "hello world"}
            ws.send_json({"done": True})
        assert relay.sent == [_upstream(f) for f in frames]
        assert "speculative" not in json.dumps(relay.sent)


# ── 2. the latency pin ──────────────────────────────────────────────────────

def test_transcript_flows_while_the_speculative_check_is_wedged(
        app, relay, no_batch, matcher):
    """The hint's check is fire-and-forget: with the matcher deliberately
    blocked, the partial and committed transcripts still flow, and the label
    catches up only after release."""
    matcher["gate"] = threading.Event()
    with TestClient(app, base_url="http://127.0.0.1") as c:
        chat, pid = _room_chat(c)
        matcher["verdicts"] = [_match("Sam", pid)]
        with c.websocket_connect("/api/voice/stt-stream") as ws:
            ws.send_json({"chat_id": chat["id"]})
            ws.send_json(_frame(loud_pcm(1.5)))
            assert ws.receive_json() == {"partial": "hello"}
            ws.send_json(_hint())   # fires the wedged check - and returns
            ws.send_json({**_frame(quiet_pcm(0.5), commit=True),
                          "turn_id": "t-wedge"})
            assert not matcher["gate"].is_set()
            assert ws.receive_json() == {"final": "hello world"}
            msg = _insert_user_message(chat["id"], voice_turn_id="t-wedge")
            assert _message_labels(msg["id"]) == ""
            matcher["gate"].set()
            labels = _wait_for(lambda: _message_labels(msg["id"]))
            ws.send_json({"done": True})
    assert json.loads(labels)["labels"] == ["Sam"]


# ── 3. the cached verdict attaches with no re-embed ─────────────────────────

def test_fresh_hint_labels_the_commit_with_one_matcher_call(
        app, relay, no_batch, matcher):
    """The head start doing its job: the hint fires the ONLY matcher call of
    the turn (on the audio buffered at silence-start), and the commit's pass
    reuses the cached verdict - trailing silence notwithstanding - so the
    label attaches with no second embed."""
    with TestClient(app, base_url="http://127.0.0.1") as c:
        chat, pid = _room_chat(c)
        matcher["verdicts"] = [_match("Sam", pid)]
        speech = loud_pcm(1.5)
        with c.websocket_connect("/api/voice/stt-stream") as ws:
            ws.send_json({"chat_id": chat["id"]})
            ws.send_json(_frame(speech))
            assert ws.receive_json() == {"partial": "hello"}
            ws.send_json(_hint())
            # the check runs on exactly the audio buffered at hint time
            assert _wait_for(lambda: matcher["calls"])
            assert matcher["calls"][0][0] == len(speech)
            # the silence tail keeps streaming, then the commit lands
            ws.send_json({**_frame(quiet_pcm(1.0), commit=True),
                          "turn_id": "t-fresh"})
            assert ws.receive_json() == {"final": "hello world"}
            msg = _insert_user_message(chat["id"], voice_turn_id="t-fresh")
            labels = _wait_for(lambda: _message_labels(msg["id"]))
            time.sleep(0.2)   # give a wrongful second embed time to happen
            ws.send_json({"done": True})
    assert json.loads(labels)["labels"] == ["Sam"]
    assert len(matcher["calls"]) == 1   # THE pin: no re-embed at commit


# ── 4. staleness ────────────────────────────────────────────────────────────

def test_resumed_speech_after_the_hint_discards_the_cached_verdict(
        app, relay, no_batch, matcher):
    """Speech resumed after the hint: the cached verdict was computed on a
    half-finished utterance, so the commit-time check runs FRESH on the full
    audio. Pinned by making the stale cached verdict a match that the fresh
    check contradicts - the label must follow the fresh verdict."""
    with TestClient(app, base_url="http://127.0.0.1") as c:
        chat, pid = _room_chat(c)
        matcher["verdicts"] = [
            _match("Sam", pid),   # the hint's (stale) verdict
            {"status": "defer", "person_id": None, "name": None,
             "score": 0.2, "reason": "below_threshold"},   # the fresh one
        ]
        with c.websocket_connect("/api/voice/stt-stream") as ws:
            ws.send_json({"chat_id": chat["id"]})
            ws.send_json(_frame(loud_pcm(1.5)))
            assert ws.receive_json() == {"partial": "hello"}
            ws.send_json(_hint())
            assert _wait_for(lambda: matcher["calls"])
            # LOUD audio after the hint: the speaker kept talking
            ws.send_json({**_frame(loud_pcm(1.0), commit=True),
                          "turn_id": "t-stale"})
            assert ws.receive_json() == {"final": "hello world"}
            msg = _insert_user_message(chat["id"], voice_turn_id="t-stale")
            assert _wait_for(lambda: len(matcher["calls"]) == 2)
            time.sleep(0.3)
            ws.send_json({"done": True})
    # the stale match was NOT trusted: the turn stayed honestly unresolved
    assert _message_labels(msg["id"]) == ""


def test_speculative_freshness_rule_is_pure():
    entry = {"len": 1000, "task": object()}
    speech = b"\x00\x40" * 1000
    silence = b"\x01\x00" * 800
    # trailing near-silence: fresh; trailing speech: stale
    assert diarize._speculative_fresh({"len": len(speech)},
                                      speech + silence) is True
    assert diarize._speculative_fresh({"len": len(speech)},
                                      speech + b"\x00\x40" * 800) is False
    assert diarize._speculative_fresh({"len": len(speech)}, speech) is True
    # a hint claiming MORE audio than the utterance holds is nonsense
    assert diarize._speculative_fresh({"len": len(speech) + 2},
                                      speech) is False
    assert diarize._speculative_fresh({"len": 0}, speech) is False
    del entry


def test_take_speculative_claims_once_and_rejects_oversized_hints():
    s = diarize.RoomSession()
    s.speculative = {"len": 10, "task": "t"}
    assert s.take_speculative(12) == {"len": 10, "task": "t"}
    assert s.speculative is None            # claimed exactly once
    assert s.take_speculative(12) is None
    s.speculative = {"len": 20, "task": "t"}
    assert s.take_speculative(12) is None   # hint longer than the utterance


# ── 5. a cached match must survive revalidation ─────────────────────────────

def test_cached_match_outside_the_pass_candidates_is_not_trusted(
        app, relay, no_batch, matcher):
    """The speculative check matches against EVERY sufficient remembered
    voice; an armed pass names only its roster. A cached match on someone
    outside the pass's candidates re-runs the check instead of smuggling the
    name through."""
    with TestClient(app, base_url="http://127.0.0.1") as c:
        chat, _pid = _room_chat(c, name="Sam")
        outsider = _remember("Dave")   # sufficient, but NOT on the roster
        matcher["verdicts"] = [
            _match("Dave", outsider),  # the hint matches the outsider
            {"status": "defer", "person_id": None, "name": None,
             "score": 0.3, "reason": "below_threshold"},  # the fresh check
        ]
        with c.websocket_connect("/api/voice/stt-stream") as ws:
            ws.send_json({"chat_id": chat["id"]})
            ws.send_json(_frame(loud_pcm(1.5)))
            assert ws.receive_json() == {"partial": "hello"}
            ws.send_json(_hint())
            assert _wait_for(lambda: matcher["calls"])
            # the hint saw the store-wide candidate list
            assert "Dave" in matcher["calls"][0][1]
            ws.send_json({**_frame(quiet_pcm(0.5), commit=True),
                          "turn_id": "t-reval"})
            assert ws.receive_json() == {"final": "hello world"}
            msg = _insert_user_message(chat["id"], voice_turn_id="t-reval")
            assert _wait_for(lambda: len(matcher["calls"]) == 2)
            # the fresh check ran against the ROSTER candidates only
            assert matcher["calls"][1][1] == ["Sam"]
            time.sleep(0.3)
            ws.send_json({"done": True})
    assert _message_labels(msg["id"]) == ""   # Dave's name never attached


# ── 6. the sacred disarm ────────────────────────────────────────────────────

def test_hint_in_a_disarmed_chat_schedules_nothing(app, relay, no_batch,
                                                   matcher):
    from backend import introductions
    _remember("Sam")
    with TestClient(app, base_url="http://127.0.0.1") as c:
        chat = c.post("/api/chats", json={"participant_ids": []}).json()
        introductions.apply_command(chat["id"], introductions.COMMAND_DISARM,
                                    app.state.settings.as_cfg())
        with c.websocket_connect("/api/voice/stt-stream") as ws:
            ws.send_json({"chat_id": chat["id"]})
            ws.send_json(_frame(loud_pcm(1.5)))
            assert ws.receive_json() == {"partial": "hello"}
            ws.send_json(_hint())
            ws.send_json(_frame(loud_pcm(0.5), commit=True))
            assert ws.receive_json() == {"final": "hello world"}
            time.sleep(0.3)
            ws.send_json({"done": True})
    assert matcher["calls"] == []   # never consulted, hint or commit
