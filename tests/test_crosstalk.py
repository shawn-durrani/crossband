"""Crosstalk detect-and-mark, the best-effort split, and the capture-profile
log (#28 phase 4).

The physics, from the design on the issue: on a single microphone the quieter
voice's overlapped words are often unrecoverable, and their ABSENCE from the
transcript is undetectable. What can be done honestly:

1. DETECT AND MARK: the batch pass's per-word speaker map (which the cluster
   reduction used to discard) reveals a turn whose words carry two or more
   speakers. Such a turn gets a crosstalk marker in voice_labels - message
   content stays immutable - and counts as uncertain everywhere downstream.
2. BEST-EFFORT SPLIT: when the word map shows clean alternation (no
   overlapping intervals), attributed sub-segments ride the metadata; they
   persist ONLY when the batch words align with the realtime transcript the
   message actually carries, and fall back to the bare marker otherwise.
3. THE CAPTURE EXPERIMENT: room-mode sessions may capture with the browser's
   single-voice tuning (noise suppression, auto gain) off; the relay logs
   which profile a session used, content-free, so field tests can compare.

THE CORE LAW is inherited unchanged and re-pinned here where it is easiest to
see: nothing about crosstalk touches the upstream byte stream, and the pass
stays fire-and-forget - the phase 1-3 pins in tests/test_room_mode.py,
test_room_identify.py and test_stt_relay.py all still run against this code.
"""

import asyncio
import base64
import json
import logging
import time

import httpx
import pytest
from fastapi.testclient import TestClient

from backend import anchors, db, diarize
from backend.app import create_app
from backend.config import Settings
from backend.routers import voice as voice_router


# ── the pure rules ──────────────────────────────────────────────────────────

def _w(sid, start, end, text="w"):
    return {"text": text, "type": "word", "speaker_id": sid,
            "start": start, "end": end}


def test_crosstalk_info_is_none_for_a_lone_speaker():
    words = [_w("s0", 0.0, 0.3), _w("s0", 0.4, 0.7)]
    assert diarize.crosstalk_info(words) is None
    assert diarize.crosstalk_info([]) is None
    assert diarize.crosstalk_info(None) is None


def test_crosstalk_info_marks_two_speakers_and_reports_overlap():
    alternating = [_w("s0", 0.0, 0.3), _w("s1", 0.4, 0.7)]
    assert diarize.crosstalk_info(alternating) == {"crosstalk": True,
                                                   "overlap": False}
    simultaneous = [_w("s0", 0.0, 0.6), _w("s1", 0.2, 0.8)]
    assert diarize.crosstalk_info(simultaneous) == {"crosstalk": True,
                                                    "overlap": True}


def test_words_overlap_ignores_same_speaker_and_seam_straddles():
    # the same voice running its words together is not crosstalk
    assert diarize.words_overlap([_w("s0", 0.0, 0.5), _w("s0", 0.4, 0.9),
                                  _w("s1", 1.0, 1.4)]) is False
    # a straddle inside the epsilon is a timestamp artefact, not overlap
    eps = diarize._OVERLAP_EPS
    assert diarize.words_overlap([_w("s0", 0.0, 0.5),
                                  _w("s1", 0.5 - eps / 2, 0.9)]) is False
    # spacing entries and junk never crash the sweep
    words = [_w("s0", 0.0, 0.5), {"type": "spacing"}, "junk",
             {"text": "x", "type": "word", "speaker_id": "s1", "start": None},
             _w("s1", 0.1, 0.3)]
    assert diarize.words_overlap(words) is True


def test_split_segments_groups_clean_alternation_in_order():
    words = [_w("s0", 0.0, 0.2, "pass"), _w("s0", 0.3, 0.5, "the"),
             _w("s0", 0.6, 0.8, "salt"), _w("s1", 1.0, 1.2, "and"),
             _w("s1", 1.3, 1.5, "pepper"), _w("s0", 1.7, 1.9, "sure")]
    segs = diarize.split_segments(words, {"s0": "Shawn", "s1": "Alex"},
                                  uncertain_labels={"Alex"})
    assert segs == [
        {"label": "Shawn", "text": "pass the salt", "uncertain": False},
        {"label": "Alex", "text": "and pepper", "uncertain": True},
        {"label": "Shawn", "text": "sure", "uncertain": False},
    ]


def test_split_segments_refuses_overlap_missing_labels_and_noise():
    overlap = [_w("s0", 0.0, 0.6, "a"), _w("s1", 0.2, 0.8, "b")]
    assert diarize.split_segments(overlap, {"s0": "A", "s1": "B"}) == []
    # a cluster with no label cannot be honestly attributed - no split
    clean = [_w("s0", 0.0, 0.2, "a"), _w("s1", 0.4, 0.6, "b")]
    assert diarize.split_segments(clean, {"s0": "A"}) == []
    # a single-speaker utterance has nothing to split
    solo = [_w("s0", 0.0, 0.2, "a"), _w("s0", 0.4, 0.6, "b")]
    assert diarize.split_segments(solo, {"s0": "A"}) == []
    # more alternations than MAX_SEGMENTS is noise, not dialogue
    churn = [_w("s0" if i % 2 == 0 else "s1", i * 0.4, i * 0.4 + 0.2, "x")
             for i in range(diarize.MAX_SEGMENTS + 2)]
    assert diarize.split_segments(churn, {"s0": "A", "s1": "B"}) == []


def test_segments_align_matches_normalised_text_only():
    segs = [{"label": "A", "text": "Pass the salt"},
            {"label": "B", "text": "and pepper!"}]
    assert diarize.segments_align(segs, "pass the salt, AND pepper") is True
    # a different word means the two transcribers disagree - no split shown
    assert diarize.segments_align(segs, "pass the pepper and salt") is False
    assert diarize.segments_align([], "anything") is False
    assert diarize.segments_align(segs, "") is False


# ── the pass, end to end through the relay ──────────────────────────────────

@pytest.fixture
def app(tmp_path):
    diarize._ROOM_ENABLED.clear()
    diarize._STASHED.clear()
    anchors.clear_recent_audio()
    settings = Settings(data_dir=str(tmp_path / "data"),
                        memory_url="http://127.0.0.1:1")
    return create_app(settings)


class FakeEleven:
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
    monkeypatch.setattr(voice_router.engine, "prewarm_recall",
                        lambda *a, **kw: None)
    app.state.allowed_hosts = {"testserver", "127.0.0.1", "localhost", "::1"}
    return fake


@pytest.fixture
def multi_matcher(monkeypatch):
    """#28 PR-B: the anchored EL pass under test fires only on the local
    matcher's "multi" (overlapping speech) verdict - stub exactly that."""
    from backend import voiceid
    monkeypatch.setattr(
        voiceid, "identify_utterance",
        lambda *a, **k: {"status": "defer", "person_id": None, "name": None,
                         "score": 0.4, "reason": "multi"})


@pytest.fixture
def batch_stt(monkeypatch):
    state = {"calls": [], "responses": []}

    def fake_post(url, headers=None, data=None, files=None, timeout=None):
        state["calls"].append({"url": url, "data": dict(data or {}),
                               "audio": files["file"][1]})
        nxt = state["responses"].pop(0) if state["responses"] else \
            {"language_code": "en", "text": "", "words": []}
        if isinstance(nxt, Exception):
            raise nxt
        return httpx.Response(200, json=nxt,
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


def _setup_room(client, sufficient=(), pending=()):
    chat = client.post("/api/chats", json={"participant_ids": []}).json()
    con = db.connect()
    db.set_chat_room_mode(con, chat["id"], True)
    diarize.set_room_enabled(chat["id"], True)
    store = anchors.store()
    for name in sufficient:
        pid = store.ensure_person(name)
        # #83: a remembered-sufficient person IS an introduced person - the
        # first clip carries the introduction that vouched the bank.
        assert store.add_clip(pid, loud_pcm(2.0), 16000,
                              source="introduction")
        for _ in range(2):
            assert store.add_clip(pid, loud_pcm(2.0), 16000,
                                  source="accumulated")
        db.add_room_person(con, chat["id"], name, person_id=pid)
    for name in pending:
        db.add_room_person(con, chat["id"], name)
    con.close()
    return chat


def _words_resp(words):
    return {"language_code": "en", "text": "hello world", "words": words}


# one sufficient person's anchor prefix is exactly this long - where the
# mocked utterance timestamps start (same arithmetic as test_room_identify)
PREFIX_1 = anchors.PREFIX_PERSON_SECONDS


def _two_voice_room_response(text_a="hello", text_b="world", overlap=False):
    """Prefix word for Shawn, then a two-voice utterance: Shawn says
    `text_a`, the pending person's cluster says `text_b` - overlapping when
    asked, cleanly alternating otherwise."""
    b_start = PREFIX_1 + (0.3 if overlap else 0.6)
    return _words_resp([
        _w("speaker_0", 0.4, 0.9),                                # prefix: Shawn
        _w("speaker_0", PREFIX_1 + 0.1, PREFIX_1 + 0.5, text_a),
        _w("speaker_1", b_start, b_start + 0.4, text_b),
    ])


def test_room_crosstalk_turn_is_marked_and_split_when_aligned(
        app, relay, batch_stt, multi_matcher):
    """The whole layer in one pass: two voices in one utterance -> the
    crosstalk marker AND, because the words alternate cleanly and read as
    the committed transcript, the best-effort split - Shawn's words as
    Shawn, the just-introduced person's as her uncertain elimination label."""
    batch_stt["responses"] = [_two_voice_room_response("hello", "world")]
    with TestClient(app, base_url="http://127.0.0.1") as c:
        chat = _setup_room(c, sufficient=["Shawn"], pending=["Alex"])
        with c.websocket_connect("/api/voice/stt-stream") as ws:
            ws.send_json({"chat_id": chat["id"]})
            ws.send_json(_frame(loud_pcm(1.5), commit=True))
            assert ws.receive_json() == {"final": "hello world"}
            msg = _insert_user_message(chat["id"], "hello world")
            labels = _wait_for(lambda: _message_labels(msg["id"]))
            ws.send_json({"done": True})
    data = json.loads(labels)
    assert data["labels"] == ["Shawn", "Alex"]
    assert data["uncertain"] == ["Alex"]
    assert data["crosstalk"] is True
    assert data["overlap"] is False
    assert data["segments"] == [
        {"label": "Shawn", "text": "hello", "uncertain": False},
        {"label": "Alex", "text": "world", "uncertain": True},
    ]


def test_misaligned_split_falls_back_to_the_marker_alone(app, relay, batch_stt, multi_matcher):
    """The two transcribers heard different words: the split would contradict
    the message it annotates, so only the marker persists."""
    batch_stt["responses"] = [_two_voice_room_response("goodbye", "world")]
    with TestClient(app, base_url="http://127.0.0.1") as c:
        chat = _setup_room(c, sufficient=["Shawn"], pending=["Alex"])
        with c.websocket_connect("/api/voice/stt-stream") as ws:
            ws.send_json({"chat_id": chat["id"]})
            ws.send_json(_frame(loud_pcm(1.5), commit=True))
            assert ws.receive_json() == {"final": "hello world"}
            msg = _insert_user_message(chat["id"], "hello world")
            labels = _wait_for(lambda: _message_labels(msg["id"]))
            ws.send_json({"done": True})
    data = json.loads(labels)
    assert data["crosstalk"] is True
    assert "segments" not in data


def test_simultaneous_speech_is_marked_but_never_split(app, relay, batch_stt, multi_matcher):
    """Overlapping word intervals mean the quieter voice's words may simply
    be GONE - a split would present the wreckage as tidy dialogue."""
    batch_stt["responses"] = [_two_voice_room_response("hello", "world",
                                                       overlap=True)]
    with TestClient(app, base_url="http://127.0.0.1") as c:
        chat = _setup_room(c, sufficient=["Shawn"], pending=["Alex"])
        with c.websocket_connect("/api/voice/stt-stream") as ws:
            ws.send_json({"chat_id": chat["id"]})
            ws.send_json(_frame(loud_pcm(1.5), commit=True))
            assert ws.receive_json() == {"final": "hello world"}
            msg = _insert_user_message(chat["id"], "hello world")
            labels = _wait_for(lambda: _message_labels(msg["id"]))
            ws.send_json({"done": True})
    data = json.loads(labels)
    assert data["crosstalk"] is True
    assert data["overlap"] is True
    assert "segments" not in data


def test_single_voice_turn_carries_no_crosstalk_keys(app, relay, batch_stt, multi_matcher):
    """The overwhelmingly common case must persist byte-identically to what
    phases 1-3 wrote: no crosstalk, no overlap, no segments keys at all."""
    batch_stt["responses"] = [_words_resp([
        _w("speaker_0", 0.4, 0.9),
        _w("speaker_0", PREFIX_1 + 0.5, PREFIX_1 + 0.9, "hello"),
    ])]
    with TestClient(app, base_url="http://127.0.0.1") as c:
        chat = _setup_room(c, sufficient=["Shawn"])
        with c.websocket_connect("/api/voice/stt-stream") as ws:
            ws.send_json({"chat_id": chat["id"]})
            ws.send_json(_frame(loud_pcm(1.5), commit=True))
            assert ws.receive_json() == {"final": "hello world"}
            msg = _insert_user_message(chat["id"])
            labels = _wait_for(lambda: _message_labels(msg["id"]))
            ws.send_json({"done": True})
    assert json.loads(labels) == {"clusters": ["speaker_0"],
                                  "labels": ["Shawn"], "uncertain": []}


def test_unmatched_clusters_split_as_uncertain_ordinals(
        app, relay, batch_stt, multi_matcher):
    """Two voices neither of which matches an anchor: the split - when
    alignable - carries the session ordinals, every segment uncertain by
    construction. (Deliberately reworked in #28 PR-B: this pin used to ride
    the no-roster phase-1 pass, which retired with the cloud identity path;
    the ordinal machinery survives in the anchored pass and is pinned
    there.)"""
    batch_stt["responses"] = [_words_resp([
        _w("speaker_0", 0.4, 0.9),                       # prefix: Shawn
        _w("s5", PREFIX_1 + 0.1, PREFIX_1 + 0.5, "hello"),
        _w("s6", PREFIX_1 + 0.7, PREFIX_1 + 1.1, "world"),
    ])]
    with TestClient(app, base_url="http://127.0.0.1") as c:
        chat = _setup_room(c, sufficient=["Shawn"])
        with c.websocket_connect("/api/voice/stt-stream") as ws:
            ws.send_json({"chat_id": chat["id"]})
            ws.send_json(_frame(loud_pcm(1.5), commit=True))
            assert ws.receive_json() == {"final": "hello world"}
            msg = _insert_user_message(chat["id"], "hello world")
            labels = _wait_for(lambda: _message_labels(msg["id"]))
            ws.send_json({"done": True})
    data = json.loads(labels)
    assert data["labels"] == ["Voice 1", "Voice 2"]
    assert data["crosstalk"] is True
    assert data["segments"] == [
        {"label": "Voice 1", "text": "hello", "uncertain": True},
        {"label": "Voice 2", "text": "world", "uncertain": True},
    ]


def test_correction_keeps_the_marker_but_drops_the_split(app):
    """Tap-to-correct answers WHO spoke, not WHAT was lost: the corrected
    payload keeps crosstalk/overlap (so ingest keeps quarantining the turn)
    and drops the split whose per-voice labels no longer apply."""
    with TestClient(app, base_url="http://127.0.0.1") as c:
        chat = c.post("/api/chats", json={"participant_ids": []}).json()
        msg = _insert_user_message(chat["id"], "hello world")
        con = db.connect()
        db.set_message_voice_labels(con, msg["id"], {
            "clusters": ["s0", "s1"], "labels": ["Voice 1", "Voice 2"],
            "uncertain": ["Voice 1", "Voice 2"], "crosstalk": True,
            "overlap": False,
            "segments": [{"label": "Voice 1", "text": "hello",
                          "uncertain": True}]})
        con.close()
        r = c.post(f"/api/chats/{chat['id']}/messages/{msg['id']}/speaker",
                   json={"name": "Alex"})
        assert r.status_code == 200
    data = json.loads(_message_labels(msg["id"]))
    assert data["labels"] == ["Alex"] and data["corrected"] is True
    assert data["crosstalk"] is True and data["overlap"] is False
    assert "segments" not in data


# ── the capture-profile log (#28 phase 4 experiment) ────────────────────────

def _upstream(frame):
    """What the relay has always sent upstream for one client frame."""
    return {"message_type": "input_audio_chunk",
            "audio_base_64": frame["audio"],
            "commit": frame["commit"],
            "sample_rate": frame["sample_rate"]}


def test_capture_profile_is_logged_and_never_reaches_upstream(
        app, relay, caplog):
    """The init's capture profile lands as one content-free INFO line, and
    the upstream byte stream is exactly what it always was - the profile is
    a log fact, never a payload field."""
    with TestClient(app, base_url="http://127.0.0.1") as c:
        chat = c.post("/api/chats", json={"participant_ids": []}).json()
        frame = _frame(loud_pcm(0.5), commit=True)
        with caplog.at_level(logging.INFO, logger="crossband.voice"):
            with c.websocket_connect("/api/voice/stt-stream") as ws:
                ws.send_json({"chat_id": chat["id"],
                              "capture_profile": "room-open"})
                ws.send_json(frame)
                assert ws.receive_json() == {"final": "hello world"}
                ws.send_json({"done": True})
    lines = [r.getMessage() for r in caplog.records
             if "capture profile" in r.getMessage()]
    assert len(lines) == 1
    assert "profile=room-open" in lines[0]
    assert relay.sent == [_upstream(frame)]


def test_capture_profile_control_frame_logs_and_sends_nothing_upstream(
        app, relay, caplog):
    """A mid-session profile change (room mode flipping) rides the control
    frame: logged content-free, nothing upstream - the byte-identity law."""
    with TestClient(app, base_url="http://127.0.0.1") as c:
        chat = c.post("/api/chats", json={"participant_ids": []}).json()
        with caplog.at_level(logging.INFO, logger="crossband.voice"):
            with c.websocket_connect("/api/voice/stt-stream") as ws:
                ws.send_json({"chat_id": chat["id"],
                              "capture_profile": "solo-tuned"})
                ws.send_json({"room_mode": True,
                              "capture_profile": "room-open"})
                frame = _frame(loud_pcm(0.5), commit=True)
                ws.send_json(frame)
                assert ws.receive_json() == {"final": "hello world"}
                ws.send_json({"done": True})
    lines = [r.getMessage() for r in caplog.records
             if "capture profile" in r.getMessage()]
    assert [l.split("profile=")[1] for l in lines] == ["solo-tuned",
                                                       "room-open"]
    assert relay.sent == [_upstream(frame)]


def test_unrecognised_capture_profile_is_never_logged(app, relay, caplog):
    """Allowlist, not passthrough: the log stays content-free by
    construction, so junk (or anything transcript-shaped) a client sends in
    that field simply does not appear."""
    with TestClient(app, base_url="http://127.0.0.1") as c:
        chat = c.post("/api/chats", json={"participant_ids": []}).json()
        with caplog.at_level(logging.INFO, logger="crossband.voice"):
            with c.websocket_connect("/api/voice/stt-stream") as ws:
                ws.send_json({"chat_id": chat["id"],
                              "capture_profile": "secret words here"})
                ws.send_json({"done": True})
    assert not [r for r in caplog.records
                if "capture profile" in r.getMessage()]
    assert voice_router.capture_profile({"capture_profile": None}) == ""
    assert voice_router.capture_profile({}) == ""
    assert voice_router.capture_profile(
        {"capture_profile": "solo-tuned"}) == "solo-tuned"
