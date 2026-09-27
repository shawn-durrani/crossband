"""Room mode (#28, #482): the voice check, pinned to the one non-negotiable
- ZERO added latency on the live voice path.

Every spoken turn, in every mode, gets one check (backend/voice_pass.py),
and the check never calls ElevenLabs: identity is local or honestly
uncertain. What these tests prove, in order:

1. The realtime relay sends ElevenLabs byte for byte the frames it always
   has: control frames (the room toggle, an older client's silence-start
   hint) and the commit's turn id never leak upstream, no batch call is
   made and the only speech-to-text spend is the relay's own.
2. The tee slices each turn's audio on the same commit boundaries the
   realtime path produces, and the check is named from exactly that audio.
3. The live path never waits: the committed transcript reaches the client
   while the check is deliberately wedged open.
4. Failure posture: a check that blows up leaves the message unlabelled and
   the relay alive.
5. The label write rides the live-events stream as a content-free
   message_update event.
6. Labels key to the exact turn id, a dropped interjection labels nothing,
   and an exact id never waits on the id-less probe cadence.

The realtime socket is the same FakeEleven pattern as
tests/test_stt_relay.py, and the session naming is stood in for by
roomkit.fake_naming. Keyless throughout, like everything else.
"""

import asyncio
import base64
import json
import logging
import threading
import time

import pytest
from fastapi.testclient import TestClient

from backend import anchors, auth, db, diarize, events
from backend.app import create_app
from backend.config import Settings
from backend.routers import voice as voice_router
from roomkit import (_insert_user_message, _message_labels, _stt_usage_rows,
                     _wait_for, fake_naming, loud_pcm, naming_answer)


def _expect_final(ws, text="hello world"):
    """The relay's final frame (#85/#104): text plus, when the commit
    carried a turn_id, that same id stamped back so the client can enforce
    only-one-wins. Tests that send no turn_id get an unstamped frame."""
    got = ws.receive_json()
    assert got.get("final") == text, got
    return got


@pytest.fixture
def app(tmp_path):
    settings = Settings(data_dir=str(tmp_path / "data"),
                        memory_url="http://127.0.0.1:1")
    return create_app(settings)


class FakeEleven:
    """Async CM + iterator standing in for the realtime socket: a partial per
    audio frame, a committed transcript per commit frame (same double as
    tests/test_stt_relay.py)."""

    def __init__(self):
        self.sent = []
        self.queue = asyncio.Queue()

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
    # the voice check must never depend on the prewarm hook and vice versa
    monkeypatch.setattr(voice_router.engine, "prewarm_recall",
                        lambda *a, **kw: None)
    app.state.allowed_hosts = {"testserver", "127.0.0.1", "localhost", "::1"}
    # These suites drive the relays as a loopback browser would. The
    # TestClient's websocket host is "testserver", so the gate must read it
    # as this machine; otherwise it is a trusted non-loopback host and the
    # pre-enrolment gate correctly holds it (see test_auth_gate).
    monkeypatch.setattr(auth, "GATE_LOOPBACK_HOSTS",
                        auth.GATE_LOOPBACK_HOSTS | {"testserver"})
    return fake


@pytest.fixture
def cloud(monkeypatch):
    """Every batch call to ElevenLabs, recorded. The check must make none."""
    calls = []

    def fake_post(url, *a, **kw):
        calls.append(url)
        raise AssertionError("the voice check called ElevenLabs")

    monkeypatch.setattr(voice_router.voice.httpx, "post", fake_post)
    return calls


@pytest.fixture
def naming(monkeypatch):
    return fake_naming(monkeypatch)


def _frame(data=b"\x00\x00" * 160, commit=False):
    return {"audio": base64.b64encode(data).decode(),
            "sample_rate": 16000, "commit": commit}


def _upstream(frame):
    """What the relay has always sent to ElevenLabs for one client frame -
    the byte-for-byte expectation every session must match."""
    return {"message_type": "input_audio_chunk",
            "audio_base_64": frame["audio"],
            "commit": frame["commit"],
            "sample_rate": frame["sample_rate"]}


def _room_chat(client, name="Alex"):
    """A chat with durable room mode on and one rostered, remembered
    person."""
    chat = client.post("/api/chats", json={}).json()
    con = db.connect()
    db.set_chat_room_mode(con, chat["id"], True)
    store = anchors.store()
    pid = store.ensure_person(name)
    assert store.add_clip(pid, loud_pcm(2.0), 16000, source="introduction")
    for _ in range(2):
        assert store.add_clip(pid, loud_pcm(2.0), 16000, source="accumulated")
    db.add_room_person(con, chat["id"], name, person_id=pid)
    con.close()
    return chat, pid


# ── 1. the relay sends ElevenLabs what it always has ───────────────────────

def test_every_turn_is_checked_and_upstream_is_byte_for_byte_identical(
        app, relay, cloud, naming):
    """The core promise: the relay sends ElevenLabs EXACTLY the frames it
    has always sent while the check runs on every turn - no batch call,
    no doubled spend. The one check per commit is scheduled and ends."""
    frames = [_frame(b"\x01\x02" * 100), _frame(b"\x03\x04" * 100),
              _frame(b"\x05\x06" * 100, commit=True)]
    with TestClient(app, base_url="http://127.0.0.1") as c:
        chat = c.post("/api/chats", json={}).json()
        with c.websocket_connect("/api/voice/stt-stream") as ws:
            ws.send_json({"chat_id": chat["id"]})
            assert ws.receive_json()["session"]  # #134 handshake
            for f in frames[:2]:
                ws.send_json(f)
                assert ws.receive_json() == {"partial": "hello"}
            ws.send_json(frames[2])
            _expect_final(ws)
            msg = _insert_user_message(chat["id"])
            assert _wait_for(lambda: naming["calls"] == 1)
            labels = _wait_for(lambda: _message_labels(msg["id"]))
            ws.send_json({"done": True})
        assert relay.sent == [_upstream(f) for f in frames]
        assert cloud == []                         # no second transcription
        # a turn the naming had nothing for still says so, never the owner
        assert json.loads(labels)["unresolved"] == "listening"
        assert _wait_for(lambda: diarize._TASKS == set())
        # exactly ONE stt usage row: the relay's own realtime metering at
        # session end - no doubled spend
        assert _wait_for(lambda: _stt_usage_rows() == 1)
        time.sleep(0.2)
        assert _stt_usage_rows() == 1


def test_control_frames_send_nothing_upstream(app, relay, cloud, naming):
    """The room toggle and an older client's silence-start hint are ours
    alone: with them interleaved, the frames reaching ElevenLabs are the
    SAME list a session without them sends, and each turn is still
    checked once, at its commit."""
    frames = [_frame(b"\x01\x02" * 100), _frame(b"\x03\x04" * 100,
                                                commit=True)]
    with TestClient(app, base_url="http://127.0.0.1") as c:
        chat = c.post("/api/chats", json={}).json()
        with c.websocket_connect("/api/voice/stt-stream") as ws:
            ws.send_json({"chat_id": chat["id"], "room_mode": True})
            assert ws.receive_json()["session"]  # #134 handshake
            ws.send_json(frames[0])
            assert ws.receive_json() == {"partial": "hello"}
            ws.send_json({"room_mode": True, "sample_rate": 16000})
            ws.send_json({"speculative": True})
            ws.send_json({"speculative": True})   # a duplicate is harmless
            ws.send_json({"room_mode": False})
            ws.send_json(frames[1])
            _expect_final(ws)
            assert _wait_for(lambda: naming["calls"] == 1)
            ws.send_json({"done": True})
        assert relay.sent == [_upstream(f) for f in frames]
        assert "speculative" not in json.dumps(relay.sent)
        assert "room_mode" not in json.dumps(relay.sent)
        assert cloud == []


# ── 2. the tee slices on commit boundaries ──────────────────────────────────

def test_tee_slices_utterances_on_the_same_commit_boundaries(
        app, relay, cloud, naming):
    """Each commit's check is named from exactly that utterance's audio,
    including the commit frame's own chunk, and the next utterance starts
    clean."""
    u1 = [_frame(b"\x11\x11" * 80), _frame(b"\x22\x22" * 80),
          _frame(b"\x33\x33" * 80, commit=True)]
    u2 = [_frame(b"\x44\x44" * 80), _frame(b"\x55\x55" * 80, commit=True)]
    with TestClient(app, base_url="http://127.0.0.1") as c:
        chat, _pid = _room_chat(c)
        with c.websocket_connect("/api/voice/stt-stream") as ws:
            ws.send_json({"chat_id": chat["id"]})
            assert ws.receive_json()["session"]  # #134 handshake
            for f in u1 + u2:
                ws.send_json(f)
                ws.receive_json()
            assert _wait_for(lambda: naming["calls"] == 2)
            ws.send_json({"done": True})
    pcm1 = b"".join(base64.b64decode(f["audio"]) for f in u1)
    pcm2 = b"".join(base64.b64decode(f["audio"]) for f in u2)
    # The two checks are fire-and-forget tasks, so they may finish in either
    # order (#155); the audio each was named from is what's pinned.
    assert set(naming["pcms"]) == {pcm1, pcm2}
    assert cloud == []


# ── 3. the live path never waits on the check ──────────────────────────────

def test_committed_transcript_returns_while_the_check_is_wedged_open(
        app, relay, cloud, naming):
    """Round dispatch hangs off the committed transcript, so the transcript
    arriving while the naming is DELIBERATELY blocked proves dispatch has no
    dependence on the check. Once released, the label catches up on the
    already-persisted message."""
    naming["gate"] = threading.Event()
    with TestClient(app, base_url="http://127.0.0.1") as c:
        chat, pid = _room_chat(c)
        naming["answers"] = [naming_answer(name="Alex", pid=pid)]
        with c.websocket_connect("/api/voice/stt-stream") as ws:
            ws.send_json({"chat_id": chat["id"]})
            assert ws.receive_json()["session"]  # #134 handshake
            ws.send_json(_frame())
            assert ws.receive_json() == {"partial": "hello"}
            ws.send_json(_frame(commit=True))
            # The naming is wedged open right now - and the live transcript
            # still arrives. This is the zero-added-latency pin.
            assert not naming["gate"].is_set()
            _expect_final(ws)
            msg = _insert_user_message(chat["id"])
            assert _message_labels(msg["id"]) == ""  # nothing yet - it's async
            naming["gate"].set()  # release; the label catches up out of band
            labels = _wait_for(lambda: _message_labels(msg["id"]))
            ws.send_json({"done": True})
    parsed = json.loads(labels)
    assert parsed["labels"] == ["Alex"] and parsed["uncertain"] == []
    assert cloud == []


# ── 4. failure posture ──────────────────────────────────────────────────────

def test_a_failed_check_is_silent_and_the_relay_lives_on(
        app, relay, cloud, monkeypatch, caplog):
    """A check blowing up leaves the message unlabelled and everything else
    exactly as it was - the next utterance still transcribes live, nothing
    retries into the live path, and the log line is content-free."""
    from backend import voice_sessions

    def boom(*a, **k):
        raise RuntimeError("naming exploded")

    monkeypatch.setattr(voice_sessions, "name_single_turn", boom)
    caplog.set_level(logging.INFO, logger="crossband.voice_pass")
    with TestClient(app, base_url="http://127.0.0.1") as c:
        chat, _pid = _room_chat(c)
        with c.websocket_connect("/api/voice/stt-stream") as ws:
            ws.send_json({"chat_id": chat["id"]})
            assert ws.receive_json()["session"]  # #134 handshake
            ws.send_json(_frame(commit=True))
            _expect_final(ws)
            msg = _insert_user_message(chat["id"])
            assert _wait_for(lambda: any(
                "voice pass failed" in r.getMessage() for r in caplog.records))
            # the relay is alive: the NEXT utterance still transcribes
            ws.send_json(_frame())
            assert ws.receive_json() == {"partial": "hello"}
            ws.send_json(_frame(commit=True))
            _expect_final(ws)
            ws.send_json({"done": True})
        time.sleep(0.2)
        assert _message_labels(msg["id"]) == ""
    lines = [r.getMessage() for r in caplog.records
             if "voice pass" in r.getMessage()]
    assert lines and "hello" not in " ".join(lines)
    assert cloud == []


# ── 5. the label write rides the live-events stream ─────────────────────────

def test_label_update_rides_the_live_events_stream(tmp_path):
    """db.set_message_voice_labels is the single label update path, and like
    insert_message it must reach an already-connected client: a content-free
    message_update event (id + chat_id, never labels or text), on the SAME
    global stream, only for writes after connect."""
    create_app(Settings(data_dir=str(tmp_path / "data"),
                        memory_url="http://127.0.0.1:1"))

    async def go():
        events.bind_loop(asyncio.get_running_loop())
        con = db.connect()
        cur = con.execute(
            "INSERT INTO chats(title, created_at, updated_at) VALUES('t', 0, 0)")
        con.commit()
        chat_id = cur.lastrowid
        msg = db.insert_message(con, chat_id, "user", "hello there")
        primer = db.insert_message(con, chat_id, "system", "primer")

        # Drain the primer first: its yield proves the stream's cursors are
        # initialised (the labels cursor starts at connect time), so the label
        # write below is unambiguously an AFTER-connect update.
        gen = events.stream(since=msg["id"], heartbeat_secs=25)
        first = await asyncio.wait_for(gen.__anext__(), timeout=1.0)
        assert json.loads(first[6:])["id"] == primer["id"]

        db.set_message_voice_labels(con, msg["id"],
                                    {"clusters": ["speaker_0", "speaker_1"],
                                     "labels": ["Voice 1", "Voice 2"]})
        con.close()
        got = await asyncio.wait_for(gen.__anext__(), timeout=1.0)
        await gen.aclose()
        ev = json.loads(got[6:])
        assert ev == {"type": "message_update", "chat_id": chat_id,
                      "id": msg["id"]}
        assert "Voice" not in got and "hello" not in got  # content-free

    asyncio.run(go())


def test_labelled_row_travels_on_the_per_chat_fetch(tmp_path):
    """The client's hydration fetch (messages-after, anchored just below the
    updated id) must carry the fresh voice_labels so the turn re-renders."""
    app = create_app(Settings(data_dir=str(tmp_path / "data"),
                              memory_url="http://127.0.0.1:1"))
    with TestClient(app, base_url="http://127.0.0.1") as c:
        chat = c.post("/api/chats", json={}).json()
        con = db.connect()
        msg = db.insert_message(con, chat["id"], "user", "hi both of you")
        db.set_message_voice_labels(con, msg["id"],
                                    {"clusters": ["a", "b"],
                                     "labels": ["Voice 1", "Voice 2"]})
        con.close()
        r = c.get(f"/api/chats/{chat['id']}/messages",
                  params={"after": msg["id"] - 1})
        rows = r.json()["messages"]
        assert [m["id"] for m in rows] == [msg["id"]]
        assert json.loads(rows[0]["voice_labels"])["labels"] == [
            "Voice 1", "Voice 2"]


# ── 6. exact label targeting via the commit frame's turn id (#28 phase 3) ───

def test_labels_key_to_the_exact_message_by_turn_id(app, relay, cloud,
                                                    naming):
    """The field-test smear, fixed: a NEIGHBOURING user turn is the oldest
    unlabelled row in the time window (the old matcher's pick), but the
    commit frame carried the client's turn id - so the labels land on the
    message persisted WITH that id and the neighbour stays untouched."""
    with TestClient(app, base_url="http://127.0.0.1") as c:
        chat, pid = _room_chat(c)
        naming["answers"] = [naming_answer(name="Alex", pid=pid)]
        with c.websocket_connect("/api/voice/stt-stream") as ws:
            ws.send_json({"chat_id": chat["id"]})
            assert ws.receive_json()["session"]  # #134 handshake
            ws.send_json({**_frame(commit=True), "turn_id": "turn-exact"})
            _expect_final(ws)
            # The decoy lands FIRST - the old window matcher would take it.
            decoy = _insert_user_message(chat["id"], "a neighbouring turn")
            target = _insert_user_message(chat["id"], "the utterance's turn",
                                          voice_turn_id="turn-exact")
            labels = _wait_for(lambda: _message_labels(target["id"]))
            ws.send_json({"done": True})
        assert json.loads(labels)["labels"] == ["Alex"]
        assert _message_labels(decoy["id"]) == ""


def test_dropped_interjection_never_smears_onto_a_neighbour(
        app, relay, cloud, naming, monkeypatch):
    """A too-short interjection commits WITH its turn id, but the client
    drops the transcript and never /sends - no row ever carries that id.
    The check must give up labelling NOTHING, even though a neighbouring
    user turn sits squarely in the old time window."""
    monkeypatch.setattr(diarize, "ID_ATTACH_WINDOW_SECS", 0.4)
    with TestClient(app, base_url="http://127.0.0.1") as c:
        chat, pid = _room_chat(c)
        naming["answers"] = [naming_answer(name="Alex", pid=pid)]
        with c.websocket_connect("/api/voice/stt-stream") as ws:
            ws.send_json({"chat_id": chat["id"]})
            assert ws.receive_json()["session"]  # #134 handshake
            ws.send_json({**_frame(commit=True), "turn_id": "turn-dropped"})
            _expect_final(ws)
            neighbour = _insert_user_message(chat["id"], "someone else's turn")
            # the check runs, retries to its (shrunk) deadline, and gives up
            assert _wait_for(lambda: naming["calls"] == 1)
            time.sleep(0.8)
            ws.send_json({"done": True})
        assert _message_labels(neighbour["id"]) == ""
        assert _wait_for(lambda: diarize._TASKS == set())  # nothing lingers


def test_commit_turn_id_never_leaks_upstream(app, relay, cloud):
    """The correlation id is ours alone: frames reaching ElevenLabs are
    byte-for-byte what they always were, turn id or not."""
    frames = [_frame(b"\x01\x02" * 100), _frame(b"\x03\x04" * 100, commit=True)]
    with TestClient(app, base_url="http://127.0.0.1") as c:
        chat = c.post("/api/chats", json={}).json()
        with c.websocket_connect("/api/voice/stt-stream") as ws:
            ws.send_json({"chat_id": chat["id"], "room_mode": True})
            assert ws.receive_json()["session"]  # #134 handshake
            ws.send_json(frames[0])
            assert ws.receive_json() == {"partial": "hello"}
            ws.send_json({**frames[1], "turn_id": "turn-private"})
            _expect_final(ws)
            ws.send_json({"done": True})
        assert relay.sent == [_upstream(f) for f in frames]
        assert "turn-private" not in json.dumps(relay.sent)


def test_exact_turn_id_attach_never_waits_on_the_probe_cadence(
        app, relay, cloud, naming, monkeypatch):
    """With a turn id the attach is a direct lookup plus a FAST retry for the
    /send race - the probe cadence may play no part. Pinned by making the
    cadence pathological (30s): the target row lands ~0.15s after the check
    has its label, and the label must still attach well inside a second -
    under cadence-driven probing this test would time out."""
    monkeypatch.setattr(diarize, "MATCH_PROBE_SECS", 30.0)
    with TestClient(app, base_url="http://127.0.0.1") as c:
        chat, pid = _room_chat(c)
        naming["answers"] = [naming_answer(name="Alex", pid=pid)]
        with c.websocket_connect("/api/voice/stt-stream") as ws:
            ws.send_json({"chat_id": chat["id"]})
            assert ws.receive_json()["session"]  # #134 handshake
            ws.send_json({**_frame(commit=True), "turn_id": "turn-fast"})
            _expect_final(ws)
            # let the check reach its first lookup and MISS (the /send race)
            assert _wait_for(lambda: naming["calls"] == 1)
            time.sleep(0.15)
            target = _insert_user_message(chat["id"], "the racing turn",
                                          voice_turn_id="turn-fast")
            labels = _wait_for(lambda: _message_labels(target["id"]),
                               timeout=1.0)
            ws.send_json({"done": True})
        assert labels, "labels did not attach ahead of the probe cadence"
        assert json.loads(labels)["labels"] == ["Alex"]


# ── pure rules (no I/O) ─────────────────────────────────────────────────────

def test_room_session_buffer_slices_and_caps():
    s = diarize.RoomSession()
    s.add_audio(b"\x01" * 10, 16000)
    s.add_audio(b"\x02" * 10, 16000)
    pcm, sr = s.take_utterance()
    assert pcm == b"\x01" * 10 + b"\x02" * 10 and sr == 16000
    assert s.take_utterance()[0] == b""              # sliced clean
    # the cap keeps the TAIL (newest audio)
    cap = diarize.MAX_UTTERANCE_SECONDS * 16000 * 2
    s.add_audio(b"\x00" * cap, 16000)
    s.add_audio(b"\xff" * 10, 16000)
    pcm, _ = s.take_utterance()
    assert len(pcm) == cap and pcm.endswith(b"\xff" * 10)


def test_pick_target_takes_oldest_unlabelled_only():
    rows = [{"id": 5, "voice_labels": '{"labels": ["Voice 1"]}'},
            {"id": 7, "voice_labels": ""},
            {"id": 9, "voice_labels": ""}]
    assert diarize.pick_target(rows, set())["id"] == 7
    assert diarize.pick_target(rows, {7})["id"] == 9
    assert diarize.pick_target(rows, {7, 9}) is None
    assert diarize.pick_target([], set()) is None


def test_pcm16_wav_header_is_well_formed():
    pcm = b"\x01\x02" * 100
    wav = diarize.pcm16_wav(pcm, 16000)
    assert wav[:4] == b"RIFF" and wav[8:12] == b"WAVE"
    assert wav[22:24] == (1).to_bytes(2, "little")            # mono
    assert wav[24:28] == (16000).to_bytes(4, "little")        # sample rate
    assert wav[40:44] == len(pcm).to_bytes(4, "little")       # data size
    assert wav[44:] == pcm
