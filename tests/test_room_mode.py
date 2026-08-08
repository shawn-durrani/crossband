"""Room mode (#28 phase 1): the parallel diarization pass, pinned to its one
non-negotiable - ZERO added latency on the live voice path.

What these tests prove, in order of importance:

1. Toggle OFF (or never mentioned) => the realtime relay behaves byte-for-byte
   identically upstream: the exact frames reach the faked ElevenLabs socket,
   no batch call is made, no task is scheduled, nothing is labelled.
2. Toggle ON => the upstream frames are STILL byte-for-byte identical (the tee
   is local); the buffered audio is sliced on the same commit boundaries the
   realtime path produces; the pass fires as a fire-and-forget task.
3. The live path never waits: the committed transcript reaches the client
   while the diarization call is deliberately wedged open.
4. Labels attach through the single update path when diarization says
   something (multiple clusters, or a different cluster than the previous
   utterance); a lone speaker stays unlabelled; failure leaves the message
   unlabelled and the relay alive.
5. The label write rides the live-events stream as a content-free
   message_update event.

The ElevenLabs batch call is mocked at the httpx level (voice.transcribe_
diarized's real parsing runs); the realtime socket is the same FakeEleven
pattern as tests/test_stt_relay.py. Keyless throughout, like everything else.
"""

import asyncio
import base64
import json
import logging
import threading
import time

import httpx
import pytest
from fastapi.testclient import TestClient

from backend import db, diarize, events
from backend.app import create_app
from backend.config import Settings
from backend.routers import voice as voice_router


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
    # the diarization pass must never depend on the prewarm hook and vice versa
    monkeypatch.setattr(voice_router.engine, "prewarm_recall",
                        lambda *a, **kw: None)
    app.state.allowed_hosts = {"testserver", "127.0.0.1", "localhost", "::1"}
    return fake


def _frame(data=b"\x00\x00" * 160, commit=False):
    return {"audio": base64.b64encode(data).decode(),
            "sample_rate": 16000, "commit": commit}


def _upstream(frame):
    """What the relay has always sent to ElevenLabs for one client frame -
    the byte-for-byte expectation both the off AND on paths must match."""
    return {"message_type": "input_audio_chunk",
            "audio_base_64": frame["audio"],
            "commit": frame["commit"],
            "sample_rate": frame["sample_rate"]}


def _diarized_words(*speaker_ids):
    """A minimal diarized batch response: one word per given cluster id."""
    return {"language_code": "en", "text": "hello world",
            "words": [{"text": f"w{i}", "type": "word", "speaker_id": sid,
                       "start": i * 0.4, "end": i * 0.4 + 0.3}
                      for i, sid in enumerate(speaker_ids)]}


@pytest.fixture
def batch_stt(monkeypatch):
    """httpx-level mock of the batch diarize POST: records every request and
    serves canned diarized responses (a list works FIFO; an exception is
    raised; a threading.Event wedges the call open)."""
    state = {"calls": [], "responses": []}

    def fake_post(url, headers=None, data=None, files=None, timeout=None):
        state["calls"].append({"url": url, "data": dict(data or {}),
                               "audio": files["file"][1]})
        nxt = state["responses"].pop(0) if state["responses"] else _diarized_words("speaker_0")
        if isinstance(nxt, Exception):
            raise nxt
        if isinstance(nxt, threading.Event):
            nxt.wait(timeout=10)
            nxt = state["responses"].pop(0) if state["responses"] else _diarized_words("speaker_0")
        return httpx.Response(200, json=nxt,
                              request=httpx.Request("POST", url))

    monkeypatch.setattr(voice_router.voice.httpx, "post", fake_post)
    return state


def _wait_for(pred, timeout=6.0, interval=0.02):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        v = pred()
        if v:
            return v
        time.sleep(interval)
    return pred()


def _message_labels(msg_id):
    con = db.connect()
    try:
        row = con.execute("SELECT voice_labels FROM messages WHERE id=?",
                          (msg_id,)).fetchone()
        return row["voice_labels"] if row else None
    finally:
        con.close()


def _stt_usage_rows():
    con = db.connect()
    try:
        return con.execute(
            "SELECT COUNT(*) FROM voice_usage WHERE kind='stt'").fetchone()[0]
    finally:
        con.close()


def _insert_user_message(chat_id, text="hello world", voice_turn_id=""):
    con = db.connect()
    try:
        return db.insert_message(con, chat_id, "user", text,
                                 voice_turn_id=voice_turn_id)
    finally:
        con.close()


# ── 1. toggle off: the relay is behaviourally identical ─────────────────────

def test_room_mode_off_is_byte_for_byte_identical_and_makes_no_extra_calls(
        app, relay, batch_stt):
    """The core promise: a session that never mentions room mode produces
    EXACTLY the upstream frames the relay has always produced - no tee, no
    batch call, no background task, no label write."""
    frames = [_frame(b"\x01\x02" * 100), _frame(b"\x03\x04" * 100),
              _frame(b"\x05\x06" * 100, commit=True)]
    with TestClient(app, base_url="http://127.0.0.1") as c:
        chat = c.post("/api/chats", json={}).json()
        with c.websocket_connect("/api/voice/stt-stream") as ws:
            ws.send_json({"chat_id": chat["id"]})
            for f in frames[:2]:
                ws.send_json(f)
                assert ws.receive_json() == {"partial": "hello"}
            ws.send_json(frames[2])
            assert ws.receive_json() == {"final": "hello world"}
            ws.send_json({"done": True})
        msg = _insert_user_message(chat["id"])
        # give any wrongly-scheduled task every chance to run before asserting
        time.sleep(0.3)
        assert relay.sent == [_upstream(f) for f in frames]
        assert batch_stt["calls"] == []          # no second transcription pass
        assert diarize._TASKS == set()           # no task was even scheduled
        assert _message_labels(msg["id"]) == ""  # nothing labelled
        # exactly ONE stt usage row: the relay's own realtime metering at
        # session end, which predates this feature - no doubled spend
        assert _wait_for(lambda: _stt_usage_rows() == 1)
        time.sleep(0.2)
        assert _stt_usage_rows() == 1


def test_room_mode_on_leaves_upstream_frames_byte_for_byte_identical(
        app, relay, batch_stt):
    """The tee is local: with room mode ON (init flag plus a mid-session
    control frame), the frames reaching ElevenLabs are the SAME list the off
    path sends - control frames never leak upstream."""
    frames = [_frame(b"\x01\x02" * 100), _frame(b"\x03\x04" * 100,
                                                commit=True)]
    with TestClient(app, base_url="http://127.0.0.1") as c:
        chat = c.post("/api/chats", json={}).json()
        with c.websocket_connect("/api/voice/stt-stream") as ws:
            ws.send_json({"chat_id": chat["id"], "room_mode": True})
            ws.send_json(frames[0])
            assert ws.receive_json() == {"partial": "hello"}
            ws.send_json({"room_mode": True, "sample_rate": 16000})  # control frame
            ws.send_json(frames[1])
            assert ws.receive_json() == {"final": "hello world"}
            ws.send_json({"done": True})
        assert relay.sent == [_upstream(f) for f in frames]


# ── 2. the tee slices on commit boundaries ──────────────────────────────────

def test_tee_slices_utterances_on_the_same_commit_boundaries(
        app, relay, batch_stt):
    """Each commit fires ONE batch call carrying exactly that utterance's
    audio (including the commit frame's own chunk), WAV-wrapped, with
    diarize=true and no num_speakers hint - and the next utterance starts
    clean."""
    u1 = [_frame(b"\x11\x11" * 80), _frame(b"\x22\x22" * 80),
          _frame(b"\x33\x33" * 80, commit=True)]
    u2 = [_frame(b"\x44\x44" * 80), _frame(b"\x55\x55" * 80, commit=True)]
    with TestClient(app, base_url="http://127.0.0.1") as c:
        chat = c.post("/api/chats", json={}).json()
        with c.websocket_connect("/api/voice/stt-stream") as ws:
            ws.send_json({"chat_id": chat["id"], "room_mode": True})
            for f in u1 + u2:
                ws.send_json(f)
                ws.receive_json()
            assert _wait_for(lambda: len(batch_stt["calls"]) == 2)
            ws.send_json({"done": True})
    pcm1 = b"".join(base64.b64decode(f["audio"]) for f in u1)
    pcm2 = b"".join(base64.b64decode(f["audio"]) for f in u2)
    assert batch_stt["calls"][0]["audio"] == diarize.pcm16_wav(pcm1, 16000)
    assert batch_stt["calls"][1]["audio"] == diarize.pcm16_wav(pcm2, 16000)
    for call in batch_stt["calls"]:
        assert call["data"]["diarize"] == "true"
        assert "num_speakers" not in call["data"]   # phase 1: no hint


# ── 3. the live path never waits on the pass ────────────────────────────────

def test_committed_transcript_returns_while_diarization_is_wedged_open(
        app, relay, batch_stt, caplog):
    """Round dispatch hangs off the committed transcript, so the transcript
    arriving while the batch call is DELIBERATELY blocked proves dispatch has
    no dependence on the diarization pass. Once released, the labels catch up
    on the already-persisted message."""
    gate = threading.Event()
    batch_stt["responses"] = [gate, _diarized_words("speaker_0", "speaker_1")]
    caplog.set_level(logging.INFO, logger="crossband.diarize")
    with TestClient(app, base_url="http://127.0.0.1") as c:
        chat = c.post("/api/chats", json={}).json()
        with c.websocket_connect("/api/voice/stt-stream") as ws:
            ws.send_json({"chat_id": chat["id"], "room_mode": True})
            ws.send_json(_frame())
            assert ws.receive_json() == {"partial": "hello"}
            ws.send_json(_frame(commit=True))
            # The pass is wedged open right now - and the live transcript
            # still arrives. This is the zero-added-latency pin.
            assert not gate.is_set()
            assert ws.receive_json() == {"final": "hello world"}
            msg = _insert_user_message(chat["id"])
            assert _message_labels(msg["id"]) == ""  # nothing yet - it's async
            gate.set()  # let the pass finish; labels catch up out of band
            labels = _wait_for(lambda: _message_labels(msg["id"]))
            ws.send_json({"done": True})
    # Phase 4 grew this payload: a two-cluster utterance is crosstalk by
    # definition, so the marker rides the same write. The phase-1 halves
    # (clusters + ordinals) are unchanged; the LATENCY pin above - the
    # transcript arriving while the pass is wedged - is what this test
    # exists for, and it held.
    assert json.loads(labels) == {"clusters": ["speaker_0", "speaker_1"],
                                  "labels": ["Voice 1", "Voice 2"],
                                  "crosstalk": True, "overlap": False}
    # The latency instrumentation: an INFO line with the pass duration,
    # content-free - stage timings and counts, never transcript text.
    lines = [r.getMessage() for r in caplog.records
             if "diarize pass:" in r.getMessage()]
    assert lines and "ms=" in lines[0]
    assert "hello" not in " ".join(lines)


# ── 4. labelling rules end to end ───────────────────────────────────────────

def test_lone_speaker_stays_unlabelled_across_utterances(app, relay, batch_stt):
    """The overwhelmingly common case: one person talking. Two utterances,
    same single cluster - diarization found nothing to say, so no label is
    written at all (silence is the honest default)."""
    batch_stt["responses"] = [_diarized_words("speaker_0"),
                              _diarized_words("speaker_0")]
    with TestClient(app, base_url="http://127.0.0.1") as c:
        chat = c.post("/api/chats", json={}).json()
        with c.websocket_connect("/api/voice/stt-stream") as ws:
            ws.send_json({"chat_id": chat["id"], "room_mode": True})
            ws.send_json(_frame(commit=True))
            ws.receive_json()
            m1 = _insert_user_message(chat["id"], "first turn")
            assert _wait_for(lambda: _stt_usage_rows() == 1)  # pass 1 done
            ws.send_json(_frame(commit=True))
            ws.receive_json()
            m2 = _insert_user_message(chat["id"], "second turn")
            assert _wait_for(lambda: _stt_usage_rows() == 2)  # pass 2 done
            ws.send_json({"done": True})
        time.sleep(0.2)
        assert _message_labels(m1["id"]) == ""
        assert _message_labels(m2["id"]) == ""


def test_new_cluster_on_a_later_utterance_labels_that_turn(app, relay, batch_stt):
    """A different cluster than the previous utterance = someone else spoke:
    the second turn gets the session's next ordinal ("Voice 2"), the first
    stays unlabelled. Best-effort per the issue - clusters are per-request -
    but this is the phase 1 contract."""
    batch_stt["responses"] = [_diarized_words("speaker_0"),
                              _diarized_words("speaker_1")]
    with TestClient(app, base_url="http://127.0.0.1") as c:
        chat = c.post("/api/chats", json={}).json()
        with c.websocket_connect("/api/voice/stt-stream") as ws:
            ws.send_json({"chat_id": chat["id"], "room_mode": True})
            ws.send_json(_frame(commit=True))
            ws.receive_json()
            m1 = _insert_user_message(chat["id"], "first turn")
            assert _wait_for(lambda: _stt_usage_rows() == 1)
            ws.send_json(_frame(commit=True))
            ws.receive_json()
            m2 = _insert_user_message(chat["id"], "second turn")
            labels = _wait_for(lambda: _message_labels(m2["id"]))
            ws.send_json({"done": True})
        assert _message_labels(m1["id"]) == ""
        assert json.loads(labels)["labels"] == ["Voice 2"]


def test_diarization_failure_is_silent_and_the_relay_lives_on(
        app, relay, batch_stt, caplog):
    """Failure posture: the batch call blowing up leaves the message
    unlabelled and everything else exactly as it was - the next utterance
    still transcribes live, nothing retries into the live path."""
    caplog.set_level(logging.INFO, logger="crossband.diarize")
    batch_stt["responses"] = [httpx.ConnectError("nope"),
                              _diarized_words("speaker_0")]
    with TestClient(app, base_url="http://127.0.0.1") as c:
        chat = c.post("/api/chats", json={}).json()
        with c.websocket_connect("/api/voice/stt-stream") as ws:
            ws.send_json({"chat_id": chat["id"], "room_mode": True})
            ws.send_json(_frame(commit=True))
            assert ws.receive_json() == {"final": "hello world"}
            msg = _insert_user_message(chat["id"])
            assert _wait_for(lambda: any(
                "diarize pass failed" in r.getMessage() for r in caplog.records))
            # the relay is alive: the NEXT utterance still transcribes
            ws.send_json(_frame())
            assert ws.receive_json() == {"partial": "hello"}
            ws.send_json(_frame(commit=True))
            assert ws.receive_json() == {"final": "hello world"}
            ws.send_json({"done": True})
        time.sleep(0.2)
        assert _message_labels(msg["id"]) == ""


def test_room_mode_pass_is_metered_as_stt_spend(app, relay, batch_stt):
    """The toggle's cost warning ("voice minutes roughly double") must be
    true in the books: each pass logs its seconds to voice_usage like every
    other transcribed second."""
    with TestClient(app, base_url="http://127.0.0.1") as c:
        chat = c.post("/api/chats", json={}).json()
        with c.websocket_connect("/api/voice/stt-stream") as ws:
            ws.send_json({"chat_id": chat["id"], "room_mode": True})
            ws.send_json(_frame(b"\x00\x01" * 16000, commit=True))  # 1s at 16k
            ws.receive_json()
            # while the session is open the ONLY stt row is the pass's own
            # (the relay meters its realtime seconds at session end)
            assert _wait_for(lambda: _stt_usage_rows() == 1)
            con = db.connect()
            row = con.execute(
                "SELECT units FROM voice_usage WHERE kind='stt'").fetchone()
            con.close()
            assert row["units"] == pytest.approx(1.0)
            ws.send_json({"done": True})


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

def test_labels_key_to_the_exact_message_by_turn_id(app, relay, batch_stt):
    """The field-test smear, fixed: a NEIGHBOURING user turn is the oldest
    unlabelled row in the time window (the old matcher's pick), but the
    commit frame carried the client's turn id - so the labels land on the
    message persisted WITH that id and the neighbour stays untouched."""
    batch_stt["responses"] = [_diarized_words("speaker_0", "speaker_1")]
    with TestClient(app, base_url="http://127.0.0.1") as c:
        chat = c.post("/api/chats", json={}).json()
        with c.websocket_connect("/api/voice/stt-stream") as ws:
            ws.send_json({"chat_id": chat["id"], "room_mode": True})
            ws.send_json({**_frame(commit=True), "turn_id": "turn-exact"})
            assert ws.receive_json() == {"final": "hello world"}
            # The decoy lands FIRST - the old window matcher would take it.
            decoy = _insert_user_message(chat["id"], "a neighbouring turn")
            target = _insert_user_message(chat["id"], "the utterance's turn",
                                          voice_turn_id="turn-exact")
            labels = _wait_for(lambda: _message_labels(target["id"]))
            ws.send_json({"done": True})
        assert json.loads(labels)["labels"] == ["Voice 1", "Voice 2"]
        assert _message_labels(decoy["id"]) == ""


def test_dropped_interjection_never_smears_onto_a_neighbour(
        app, relay, batch_stt, monkeypatch):
    """A too-short interjection commits WITH its turn id, but the client
    drops the transcript and never /sends - no row ever carries that id.
    The pass must give up labelling NOTHING, even though a neighbouring
    user turn sits squarely in the old time window."""
    monkeypatch.setattr(diarize, "MATCH_WINDOW_SECS", 0.8)
    monkeypatch.setattr(diarize, "MATCH_PROBE_SECS", 0.05)
    batch_stt["responses"] = [_diarized_words("speaker_0", "speaker_1")]
    with TestClient(app, base_url="http://127.0.0.1") as c:
        chat = c.post("/api/chats", json={}).json()
        with c.websocket_connect("/api/voice/stt-stream") as ws:
            ws.send_json({"chat_id": chat["id"], "room_mode": True})
            ws.send_json({**_frame(commit=True), "turn_id": "turn-dropped"})
            assert ws.receive_json() == {"final": "hello world"}
            neighbour = _insert_user_message(chat["id"], "someone else's turn")
            # the pass runs, probes to its deadline, and gives up
            assert _wait_for(lambda: _stt_usage_rows() == 1)
            time.sleep(1.2)  # past the (shrunk) match window
            ws.send_json({"done": True})
        assert _message_labels(neighbour["id"]) == ""
        assert diarize._TASKS == set()  # the pass ended; nothing lingers


def test_commit_turn_id_never_leaks_upstream(app, relay, batch_stt):
    """The correlation id is ours alone: frames reaching ElevenLabs are
    byte-for-byte what they always were, turn id or not."""
    frames = [_frame(b"\x01\x02" * 100), _frame(b"\x03\x04" * 100, commit=True)]
    with TestClient(app, base_url="http://127.0.0.1") as c:
        chat = c.post("/api/chats", json={}).json()
        with c.websocket_connect("/api/voice/stt-stream") as ws:
            ws.send_json({"chat_id": chat["id"], "room_mode": True})
            ws.send_json(frames[0])
            assert ws.receive_json() == {"partial": "hello"}
            ws.send_json({**frames[1], "turn_id": "turn-private"})
            assert ws.receive_json() == {"final": "hello world"}
            ws.send_json({"done": True})
        assert relay.sent == [_upstream(f) for f in frames]
        assert "turn-private" not in json.dumps(relay.sent)


# ── 7. attach immediately, meter after (#28, night test 4) ──────────────────

def test_exact_turn_id_attach_never_waits_on_the_probe_cadence(
        app, relay, batch_stt, monkeypatch):
    """With a turn id the attach is a direct lookup plus a FAST retry for the
    /send race - the probe cadence may play no part. Pinned by making the
    cadence pathological (30s): the target row lands ~0.15s after the batch
    reply parsed, and the labels must still attach well inside a second -
    under the old cadence-driven probing this test would time out."""
    monkeypatch.setattr(diarize, "MATCH_PROBE_SECS", 30.0)
    batch_stt["responses"] = [_diarized_words("speaker_0", "speaker_1")]
    with TestClient(app, base_url="http://127.0.0.1") as c:
        chat = c.post("/api/chats", json={}).json()
        with c.websocket_connect("/api/voice/stt-stream") as ws:
            ws.send_json({"chat_id": chat["id"], "room_mode": True})
            ws.send_json({**_frame(commit=True), "turn_id": "turn-fast"})
            assert ws.receive_json() == {"final": "hello world"}
            # let the pass reach its first lookup and MISS (the /send race)
            time.sleep(0.15)
            target = _insert_user_message(chat["id"], "the racing turn",
                                          voice_turn_id="turn-fast")
            labels = _wait_for(lambda: _message_labels(target["id"]),
                               timeout=1.0)
            ws.send_json({"done": True})
        assert labels, "labels did not attach ahead of the probe cadence"
        assert json.loads(labels)["labels"] == ["Voice 1", "Voice 2"]


def test_label_write_lands_before_the_meter_write(
        app, relay, batch_stt, monkeypatch):
    """Metering moved BEHIND the label attach: the spend is bookkeeping, the
    label is what the round's seats are waiting on, so nothing may queue in
    front of it. Pinned on call order, with both writes still landing."""
    order = []
    real_labels = db.set_message_voice_labels
    real_meter = diarize._meter

    def labels_spy(con, message_id, payload):
        order.append("labels")
        return real_labels(con, message_id, payload)

    def meter_spy(chat_id, pcm, sample_rate, cfg):
        order.append("meter")
        return real_meter(chat_id, pcm, sample_rate, cfg)

    monkeypatch.setattr(db, "set_message_voice_labels", labels_spy)
    monkeypatch.setattr(diarize, "_meter", meter_spy)
    batch_stt["responses"] = [_diarized_words("speaker_0", "speaker_1")]
    with TestClient(app, base_url="http://127.0.0.1") as c:
        chat = c.post("/api/chats", json={}).json()
        with c.websocket_connect("/api/voice/stt-stream") as ws:
            ws.send_json({"chat_id": chat["id"], "room_mode": True})
            target = _insert_user_message(chat["id"], "already persisted",
                                          voice_turn_id="turn-order")
            ws.send_json({**_frame(commit=True), "turn_id": "turn-order"})
            assert ws.receive_json() == {"final": "hello world"}
            assert _wait_for(lambda: _stt_usage_rows() == 1)  # pass finished
            ws.send_json({"done": True})
        assert order == ["labels", "meter"]
        assert _message_labels(target["id"])  # and the labels really landed


def test_labelling_failure_still_meters_the_spend(
        app, relay, batch_stt, monkeypatch):
    """The other half of moving the meter: the batch call's spend became real
    the moment it returned, so a labelling crash must still book it - just
    behind where the labels would have gone, never silently unbilled."""
    def broken_labels(con, message_id, payload):
        raise RuntimeError("label write exploded")

    monkeypatch.setattr(db, "set_message_voice_labels", broken_labels)
    batch_stt["responses"] = [_diarized_words("speaker_0", "speaker_1")]
    with TestClient(app, base_url="http://127.0.0.1") as c:
        chat = c.post("/api/chats", json={}).json()
        with c.websocket_connect("/api/voice/stt-stream") as ws:
            ws.send_json({"chat_id": chat["id"], "room_mode": True})
            msg = _insert_user_message(chat["id"], "turn that fails",
                                       voice_turn_id="turn-boom")
            ws.send_json({**_frame(commit=True), "turn_id": "turn-boom"})
            assert ws.receive_json() == {"final": "hello world"}
            assert _wait_for(lambda: _stt_usage_rows() == 1)  # metered anyway
            ws.send_json({"done": True})
        assert _message_labels(msg["id"]) == ""  # the label write did fail


# ── pure rules (no I/O) ─────────────────────────────────────────────────────

def test_utterance_clusters_orders_and_filters():
    words = [
        {"text": "hi", "type": "word", "speaker_id": "s0"},
        {"text": " ", "type": "spacing", "speaker_id": "s9"},   # not a word
        {"text": "there", "type": "word", "speaker_id": "s1"},
        {"text": "again", "type": "word", "speaker_id": "s0"},  # dedup, order kept
        {"text": "hm", "type": "word"},                          # unattributed
    ]
    assert diarize.utterance_clusters(words) == ["s0", "s1"]
    assert diarize.utterance_clusters(None) == []
    assert diarize.utterance_clusters([{"text": "x"}]) == []


def test_should_label_truth_table():
    # nothing found => never label
    assert diarize.should_label([], None) is False
    assert diarize.should_label([], ["s0"]) is False
    # first utterance, one voice => the common case, no label
    assert diarize.should_label(["s0"], None) is False
    # more than one cluster in the utterance => always label
    assert diarize.should_label(["s0", "s1"], None) is True
    assert diarize.should_label(["s0", "s1"], ["s0", "s1"]) is True
    # single cluster, same as previous => still one voice, no label
    assert diarize.should_label(["s0"], ["s0"]) is False
    # single cluster, DIFFERENT from previous => someone else spoke
    assert diarize.should_label(["s1"], ["s0"]) is True
    assert diarize.should_label(["s1"], ["s0", "s1"]) is True


def test_session_ordinals_are_first_seen_and_stable():
    s = diarize.RoomSession(enabled=True)
    assert s.assign(["s3", "s7"]) == ["Voice 1", "Voice 2"]
    assert s.assign(["s7"]) == ["Voice 2"]           # stable across utterances
    assert s.assign(["s1", "s3"]) == ["Voice 3", "Voice 1"]


def test_room_session_buffer_slices_and_caps():
    s = diarize.RoomSession(enabled=True)
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


def test_toggling_room_mode_clears_the_partial_buffer():
    s = diarize.RoomSession(enabled=True)
    s.add_audio(b"\x01" * 10, 16000)
    s.set_enabled(False)
    s.set_enabled(True)
    assert s.take_utterance()[0] == b""


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
