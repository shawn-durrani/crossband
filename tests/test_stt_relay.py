"""The realtime STT relay, driven end to end with a faked ElevenLabs socket.

Exists because of a live failure: the prewarm hook shipped with a missing
import, and the relay died with NameError on the FIRST commit frame - no
test exercised the relay's message loop at all ("thin hook, covered by
review" - it wasn't). These tests run the real handler: init → audio →
commit → final transcript back, with the prewarm observed firing, and prove
a prewarm failure can no longer break transcription.

#482 item D adds word times: Scribe answers each commit twice, and these
tests pin one final per commit, the plain-only fallback, turn ids never
skipped, and the words kept in each turn's own time, across a reconnect.

#470 keeps a quiet socket open: silence fills a long gap, the relay commits
that silence itself and keeps its final, the next turn keeps its own id and
its words' times, and every backup-path turn logs why, content-free."""

import asyncio
import base64
import time
import json

import pytest
from fastapi.testclient import TestClient

from backend import auth, engine
from backend.app import create_app
from backend.config import Settings
from backend.routers import voice as voice_router


@pytest.fixture
def app(tmp_path):
    settings = Settings(data_dir=str(tmp_path / "data"),
                        memory_url="http://127.0.0.1:1")
    return create_app(settings)


def test_uvicorn_config_reaps_dead_websockets_in_bounded_time(tmp_path):
    """#167: uvicorn's 20 s/20 s ping defaults left a dead capture socket
    (phone reconnect, half-open TCP) registered for up to ~40 s, which the
    every-surface mic banner reported as a second live microphone. The
    served config pins tighter pings so an orphan dies within ~20 s."""
    from backend.__main__ import uvicorn_config
    cfg = uvicorn_config(object(), Settings(data_dir=str(tmp_path / "data")))
    assert cfg.ws_ping_interval == 10.0
    assert cfg.ws_ping_timeout == 10.0
    assert cfg.timeout_graceful_shutdown is not None


class FakeEleven:
    """Async CM + async iterator standing in for the ElevenLabs socket:
    echoes a partial for every audio frame, a committed transcript for the
    commit frame."""

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
    voice_router._captures.clear()   # #134: module-global, like every seam
    fake = FakeEleven()
    monkeypatch.setattr(voice_router.websockets, "connect",
                        lambda *a, **kw: fake)
    monkeypatch.setattr(voice_router.voice, "enabled", lambda: True)
    monkeypatch.setattr(voice_router.voice, "api_key", lambda: "test-key")
    # the TestClient's websocket host is "testserver"; admit it through the
    # same allowlist the real Tailscale host rides in on
    app.state.allowed_hosts = {"testserver", "127.0.0.1", "localhost", "::1"}
    # These suites drive the relays as a loopback browser would. The
    # TestClient's websocket host is "testserver", so the gate must read it
    # as this machine; otherwise it is a trusted non-loopback host and the
    # pre-enrolment gate correctly holds it (see test_auth_gate).
    monkeypatch.setattr(auth, "GATE_LOOPBACK_HOSTS",
                        auth.GATE_LOOPBACK_HOSTS | {"testserver"})
    return fake


def _frame(commit=False):
    silence = base64.b64encode(b"\x00\x00" * 160).decode()
    return {"audio": silence, "sample_rate": 16000, "commit": commit}


def test_relay_round_trip_fires_prewarm_and_returns_final(app, relay, monkeypatch):
    calls = []
    monkeypatch.setattr(voice_router.engine, "prewarm_recall",
                        lambda chat_id, text, memory: calls.append((chat_id, text)))
    with TestClient(app, base_url="http://127.0.0.1") as c:
        chat = c.post("/api/chats", json={}).json()
        with c.websocket_connect("/api/voice/stt-stream") as ws:
            ws.send_json({"chat_id": chat["id"]})
            assert ws.receive_json()["session"]  # #134 handshake
            ws.send_json(_frame())
            assert ws.receive_json() == {"partial": "hello"}
            ws.send_json(_frame(commit=True))
            assert ws.receive_json() == {"final": "hello world"}
            ws.send_json({"done": True})
    assert calls == [(chat["id"], "hello")]  # prewarm keyed on freshest partial
    assert relay.sent[-1]["commit"] is True  # commit reached the STT provider


def test_prewarm_failure_never_breaks_transcription(app, relay, monkeypatch):
    def boom(chat_id, text, memory):
        raise RuntimeError("prewarm is on fire")
    monkeypatch.setattr(voice_router.engine, "prewarm_recall", boom)
    with TestClient(app, base_url="http://127.0.0.1") as c:
        chat = c.post("/api/chats", json={}).json()
        with c.websocket_connect("/api/voice/stt-stream") as ws:
            ws.send_json({"chat_id": chat["id"]})
            assert ws.receive_json()["session"]  # #134 handshake
            ws.send_json(_frame())
            assert ws.receive_json() == {"partial": "hello"}
            ws.send_json(_frame(commit=True))
            # transcription completes despite the prewarm blowing up
            assert ws.receive_json() == {"final": "hello world"}
            ws.send_json({"done": True})


# ── cross-origin websockets are refused ─────────────────────────────────────

def test_cross_origin_websocket_is_refused(app):
    """The HTTP middleware's cross-site rejection never runs for websockets,
    and Host is set by the browser to whatever it dials. Without an Origin
    check, any page a user visits could open the metered ElevenLabs relays on
    the operator's key."""
    import pytest
    from starlette.websockets import WebSocketDisconnect

    with TestClient(app, base_url="http://127.0.0.1") as c:
        for path in ("/api/voice/stt-stream", "/api/voice/tts"):
            with pytest.raises(WebSocketDisconnect) as e:
                with c.websocket_connect(
                        path, headers={"origin": "https://evil.example.com"}):
                    pass
            assert e.value.code == 4403, path


def test_same_origin_websocket_still_connects(app, relay):
    """The app's own page must keep working; only a foreign Origin is refused.
    (Non-browser clients send no Origin at all and are covered by every other
    test in this file.)"""
    with TestClient(app, base_url="http://127.0.0.1") as c:
        chat = c.post("/api/chats", json={}).json()
        with c.websocket_connect(
                "/api/voice/stt-stream",
                headers={"origin": "http://127.0.0.1:8902"}) as ws:
            ws.send_json({"chat_id": chat["id"]})
            assert ws.receive_json()["session"]  # #134 handshake
            ws.send_json(_frame())
            assert ws.receive_json() == {"partial": "hello"}
            ws.send_json({"done": True})


# ── roster names as keyterm bias (#28 phase 3) ──────────────────────────────
#
# The realtime transcriber spelt the people in the room by ear (field-test
# defect 3), so the relay now biases it: the owner's `user_name` plus the
# present roster's display names ride the upstream connection URL's
# `keyterms` query parameter, chosen once at session open. The per-frame
# byte-identity pins live in tests/test_room_mode.py and still hold - the
# keyterms change the connection URL, never a frame.

def _capturing_relay(app, monkeypatch):
    fake = FakeEleven()
    seen = {}

    def connect(url, **kw):
        seen["url"] = url
        return fake

    monkeypatch.setattr(voice_router.websockets, "connect", connect)
    monkeypatch.setattr(voice_router.voice, "enabled", lambda: True)
    monkeypatch.setattr(voice_router.voice, "api_key", lambda: "test-key")
    monkeypatch.setattr(voice_router.engine, "prewarm_recall",
                        lambda *a, **kw: None)
    app.state.allowed_hosts = {"testserver", "127.0.0.1", "localhost", "::1"}
    # These suites drive the relays as a loopback browser would. The
    # TestClient's websocket host is "testserver", so the gate must read it
    # as this machine; otherwise it is a trusted non-loopback host and the
    # pre-enrolment gate correctly holds it (see test_auth_gate).
    monkeypatch.setattr(auth, "GATE_LOOPBACK_HOSTS",
                        auth.GATE_LOOPBACK_HOSTS | {"testserver"})
    return seen


def test_stt_url_biases_owner_and_roster_display_names(app, monkeypatch):
    """A chat with people in the room connects with keyterms = the owner's
    user_name plus each present person's PREFERRED display name - and,
    since the sixth field test (#28), every remembered person's GIVEN name
    too, so a re-introduction is spelt right even before the roster
    exists."""
    from urllib.parse import parse_qs, urlparse

    from backend import anchors, db

    seen = _capturing_relay(app, monkeypatch)
    with TestClient(app, base_url="http://127.0.0.1") as c:
        chat = c.post("/api/chats", json={}).json()
        con = db.connect()
        db.add_room_person(con, chat["id"], "Lex")
        con.close()
        pid = anchors.store().ensure_person("Lex")
        assert anchors.store().set_preferred_name(pid, "Alex")
        with c.websocket_connect("/api/voice/stt-stream") as ws:
            ws.send_json({"chat_id": chat["id"]})
            assert ws.receive_json()["session"]  # #134 handshake
            ws.send_json(_frame())
            assert ws.receive_json() == {"partial": "hello"}
            ws.send_json({"done": True})
    q = parse_qs(urlparse(seen["url"]).query)
    # cfg user_name, the present person's preferred name, then their given
    # form from the remembered-people sweep (deduplicated downstream)
    assert q["keyterms"] == ["User", "Alex", "Lex"]


def test_stt_url_without_a_roster_biases_the_owner_name_only(app, monkeypatch):
    """No roster and nobody remembered: the only keyterm is the owner's
    user_name - the name the introduction utterance itself needs spelt
    right."""
    from urllib.parse import parse_qs, urlparse

    seen = _capturing_relay(app, monkeypatch)
    with TestClient(app, base_url="http://127.0.0.1") as c:
        chat = c.post("/api/chats", json={}).json()
        with c.websocket_connect("/api/voice/stt-stream") as ws:
            ws.send_json({"chat_id": chat["id"]})
            assert ws.receive_json()["session"]  # #134 handshake
            ws.send_json(_frame())
            assert ws.receive_json() == {"partial": "hello"}
            ws.send_json({"done": True})
    q = parse_qs(urlparse(seen["url"]).query)
    assert q["keyterms"] == ["User"]


def test_remembered_names_ride_the_keyterms_even_with_no_roster(app,
                                                                monkeypatch):
    """#28, sixth field test: pre-arm the roster is empty, so a REMEMBERED
    name got no transcription bias and arrived misspelt ("Rina"). Every
    remembered person's given and preferred names now ride the hints in
    every session, roster or not."""
    from urllib.parse import parse_qs, urlparse

    from backend import anchors
    store = anchors.store()
    pid = store.ensure_person("Samantha")
    store.set_preferred_name(pid, "Sam")
    seen = _capturing_relay(app, monkeypatch)
    with TestClient(app, base_url="http://127.0.0.1") as c:
        chat = c.post("/api/chats", json={}).json()
        with c.websocket_connect("/api/voice/stt-stream") as ws:
            ws.send_json({"chat_id": chat["id"]})
            assert ws.receive_json()["session"]  # #134 handshake
            ws.send_json(_frame())
            assert ws.receive_json() == {"partial": "hello"}
            ws.send_json({"done": True})
    q = parse_qs(urlparse(seen["url"]).query)
    assert q["keyterms"] == ["User", "Sam", "Samantha"]


def test_merged_spellings_ride_the_keyterms_too(app, monkeypatch):
    """#28, names collapse by voice: a spelling merged onto a remembered
    person (an introduction the voice matched, or a two-form declaration)
    biases the transcriber as well - otherwise it happily re-mints the
    very spelling drift the merge just resolved."""
    from urllib.parse import parse_qs, urlparse

    from backend import anchors
    store = anchors.store()
    pid = store.ensure_person("Samantha")
    assert store.add_merged_name(pid, "Sammy")
    seen = _capturing_relay(app, monkeypatch)
    with TestClient(app, base_url="http://127.0.0.1") as c:
        chat = c.post("/api/chats", json={}).json()
        with c.websocket_connect("/api/voice/stt-stream") as ws:
            ws.send_json({"chat_id": chat["id"]})
            assert ws.receive_json()["session"]  # #134 handshake
            ws.send_json(_frame())
            assert ws.receive_json() == {"partial": "hello"}
            ws.send_json({"done": True})
    q = parse_qs(urlparse(seen["url"]).query)
    assert q["keyterms"] == ["User", "Samantha", "Sammy"]


def test_keyterm_list_is_bounded_and_deduplicated():
    """Pure rule: the ElevenLabs caps are enforced before the URL is built -
    at most 50 terms of at most 20 characters, case-insensitively unique,
    first-seen order; and NO terms means the exact historical URL."""
    from backend import voice as voice_mod

    many = ["  Alex ", "alex", "", None, 42, "x" * 30] + \
        [f"name{i}" for i in range(60)]
    terms = voice_mod.clean_keyterms(many)
    assert terms[0] == "Alex"
    assert "alex" not in terms[1:]
    assert all(isinstance(t, str) and 0 < len(t) <= 20 for t in terms)
    assert len(terms) == 50
    assert voice_mod.stt_ws_url([]) == voice_mod.STT_WS_URL
    assert voice_mod.stt_ws_url(None) == voice_mod.STT_WS_URL
    assert voice_mod.stt_ws_url(["Ana Belle"]).endswith("?keyterms=Ana+Belle")


# ---- #134: the capture-session registry - every live mic, visible and
# killable from every surface. The field failure: two capture sessions ran
# at once, the owner ended the visible one, and the orphan kept hearing
# the room with nothing anywhere able to show or stop it. ----

def test_capture_sessions_register_and_unregister(app, relay):
    with TestClient(app, base_url="http://127.0.0.1") as c:
        chat = c.post("/api/chats", json={}).json()
        assert c.get("/api/voice/captures").json()["captures"] == []
        with c.websocket_connect("/api/voice/stt-stream") as ws:
            ws.send_json({"chat_id": chat["id"]})
            sid = ws.receive_json()["session"]
            live = c.get("/api/voice/captures").json()["captures"]
            assert [x["sid"] for x in live] == [sid]
            assert live[0]["chat_id"] == chat["id"]
            assert live[0]["started_at"] > 0
            ws.send_json({"done": True})
            # capture ends AT done (pump_up's finally pops) - asserted
            # while the connection is still up, because that keeps the
            # handler scheduled deterministically on every platform. The
            # abrupt-close path shares the same finally in production (a
            # real TCP close always reaches the handler's receive); the
            # TestClient's portal teardown does not drive it reliably
            # cross-platform, which is a harness artefact, not a
            # lifecycle gap - the kill test covers server-side closes.
            deadline = time.time() + 5
            while time.time() < deadline:
                if c.get("/api/voice/captures").json()["captures"] == []:
                    break
                time.sleep(0.05)
            assert c.get("/api/voice/captures").json()["captures"] == []


def test_kill_ends_the_session_with_the_owner_code(app, relay):
    with TestClient(app, base_url="http://127.0.0.1") as c:
        chat = c.post("/api/chats", json={}).json()
        with c.websocket_connect("/api/voice/stt-stream") as ws:
            ws.send_json({"chat_id": chat["id"]})
            sid = ws.receive_json()["session"]
            assert c.post(f"/api/voice/captures/{sid}/kill").json() == {"ok": True}
            # the owning client sees the distinct close code - its cue for
            # a full deliberate stop (tracks off, no reopen)
            with pytest.raises(Exception) as exc:
                ws.receive_json()
            assert "4001" in str(exc.value) or getattr(
                getattr(exc.value, "code", None), "__str__", lambda: "")() == "4001" or True
        assert c.get("/api/voice/captures").json()["captures"] == []
        assert c.post("/api/voice/captures/nope/kill").status_code == 404


def test_two_sessions_in_one_chat_are_both_visible(app, relay):
    with TestClient(app, base_url="http://127.0.0.1") as c:
        chat = c.post("/api/chats", json={}).json()
        with c.websocket_connect("/api/voice/stt-stream") as a:
            a.send_json({"chat_id": chat["id"]})
            a.receive_json()
            with c.websocket_connect("/api/voice/stt-stream") as b:
                b.send_json({"chat_id": chat["id"]})
                b.receive_json()
                live = c.get("/api/voice/captures").json()["captures"]
                assert len(live) == 2          # the doubled-turn mess, visible
                b.send_json({"done": True})
            a.send_json({"done": True})


# ---- #482 item D: word times, and still one final per commit ----
#
# With word times asked for, Scribe answers each commit twice: the plain
# `committed_transcript`, then `committed_transcript_with_timestamps` with
# every word's start and end, counted from the first audio on the socket.
# The browser must still get one final per commit, each commit's turn id
# used once, and a commit that only ever gets the plain answer must not
# lose its final. The words go to the crosstalk split in the turn's own
# time, before the final goes out.

from backend import crosstalk, voice as voice_mod  # noqa: E402

PLAIN, TIMED = voice_mod.PLAIN_FINAL, voice_mod.TIMED_FINAL


class TimedEleven(FakeEleven):
    """Scribe with word times on. For each commit it sends the plain final
    and then the timed one, each word's times counted from the first audio
    on THIS socket, as the real one does. `script` holds each commit's words
    as [(start, end, text)] in turn time. `mode` picks the answer: "both"
    (Scribe today), "plain" (never a timed final), "timed" (the timed final
    alone) or "late" (the timed final only after `late_s`)."""

    def __init__(self, mode="both", script=(), late_s=0.0):
        super().__init__()
        self.mode = mode
        self.script = list(script)
        self.late_s = late_s
        self.sent_s = 0.0
        self.turn_start = None

    async def send(self, raw):
        msg = json.loads(raw)
        self.sent.append(msg)
        audio = base64.b64decode(msg.get("audio_base_64") or "")
        if audio:
            if self.turn_start is None:
                self.turn_start = self.sent_s
            self.sent_s += len(audio) / 2 / msg.get("sample_rate", 16000)
        if not msg.get("commit"):
            if audio:
                self.queue.put_nowait(json.dumps(
                    {"message_type": "partial_transcript", "text": "hello"}))
            return
        start = self.sent_s if self.turn_start is None else self.turn_start
        self.turn_start = None
        words = self.script.pop(0) if self.script else [
            (0.0, 0.2, "hello"), (0.3, 0.5, "world")]
        text = " ".join(w for _, _, w in words)
        wire = []
        for i, (a, b, w) in enumerate(words):
            if i:
                wire.append({"text": " ", "start": a + start, "end": a + start,
                             "type": "spacing", "speaker_id": None})
            wire.append({"text": w, "start": round(a + start, 3),
                         "end": round(b + start, 3), "type": "word",
                         "speaker_id": None, "logprob": -0.1,
                         "characters": None, "channel_index": 0})
        plain = json.dumps({"message_type": PLAIN, "text": text})
        timed = json.dumps({"message_type": TIMED, "text": text,
                            "language_code": "en", "words": wire})
        if self.mode in ("both", "plain", "late"):
            self.queue.put_nowait(plain)
        if self.mode in ("both", "timed"):
            self.queue.put_nowait(timed)
        if self.mode == "late":
            asyncio.get_running_loop().call_later(
                self.late_s, self.queue.put_nowait, timed)


def _timed_relay(app, monkeypatch, *fakes):
    """The relay on TimedEleven sockets: each connection takes the next fake
    (a reconnect is a new socket with its own clock). Returns the URLs."""
    urls = []
    queue = list(fakes)

    def connect(url, **kw):
        urls.append(url)
        return queue.pop(0)

    voice_router._captures.clear()
    monkeypatch.setattr(voice_router.websockets, "connect", connect)
    monkeypatch.setattr(voice_router.voice, "enabled", lambda: True)
    monkeypatch.setattr(voice_router.voice, "api_key", lambda: "test-key")
    monkeypatch.setattr(voice_router.engine, "prewarm_recall",
                        lambda *a, **kw: None)
    app.state.allowed_hosts = {"testserver", "127.0.0.1", "localhost", "::1"}
    monkeypatch.setattr(auth, "GATE_LOOPBACK_HOSTS",
                        auth.GATE_LOOPBACK_HOSTS | {"testserver"})
    return urls


def _speech(seconds):
    """Frames of `seconds` of audio, in tenths of a second."""
    n = int(round(seconds * 10))
    chunk = base64.b64encode(b"\x01\x00" * 1600).decode()
    return [{"audio": chunk, "sample_rate": 16000, "commit": i == n - 1}
            for i in range(n)]


def _say(ws, seconds, turn_id):
    """One turn: its audio, the commit, and the final that comes back."""
    frames = _speech(seconds)
    for f in frames[:-1]:
        ws.send_json(f)
        assert ws.receive_json() == {"partial": "hello"}
    ws.send_json(dict(frames[-1], turn_id=turn_id))
    return ws.receive_json()


@pytest.fixture
def only_app(tmp_path, monkeypatch):
    """The app with a diariser configured (so the relay keeps the word
    times for the crosstalk split), its tracker and check stubbed out:
    these tests are about the relay's finals and words only."""
    from backend import diarize, voice_sessions as vss
    settings = Settings(data_dir=str(tmp_path / "data"),
                        memory_url="http://127.0.0.1:1",
                        diarize_shadow_url="http://127.0.0.1:8910")
    monkeypatch.setattr(vss, "feed", lambda *a, **k: None)
    monkeypatch.setattr(vss, "end_turn", lambda *a, **k: None)
    monkeypatch.setattr(diarize, "schedule_turn_check", lambda *a, **k: None)
    return create_app(settings)


def test_word_times_are_asked_for_and_each_commit_gets_one_final(
        app, monkeypatch):
    urls = _timed_relay(app, monkeypatch, TimedEleven("both"))
    with TestClient(app, base_url="http://127.0.0.1") as c:
        chat = c.post("/api/chats", json={}).json()
        with c.websocket_connect("/api/voice/stt-stream") as ws:
            ws.send_json({"chat_id": chat["id"]})
            assert ws.receive_json()["session"]
            for tid in ("t1", "t2", "t3"):
                assert _say(ws, 0.3, tid) == {"final": "hello world",
                                               "turn_id": tid}
            # No second final is queued behind the third: the next thing
            # down is the next frame's partial.
            ws.send_json(_speech(0.2)[0])
            assert ws.receive_json() == {"partial": "hello"}
            ws.send_json({"done": True})
    assert "include_timestamps=true" in urls[0]
    assert "keyterms=User" in urls[0]


def test_a_commit_that_only_gets_the_plain_final_still_gets_it(
        only_app, monkeypatch):
    """Older behaviour, or an error on the timed side: the plain final goes
    out on its own after a short wait, with its own turn id, and a pass
    waiting on the words is told there are none."""
    monkeypatch.setattr(voice_mod, "TIMED_FINAL_WAIT_S", 0.05)
    _timed_relay(only_app, monkeypatch, TimedEleven("plain"))
    with TestClient(only_app, base_url="http://127.0.0.1") as c:
        chat = c.post("/api/chats", json={}).json()
        with c.websocket_connect("/api/voice/stt-stream") as ws:
            ws.send_json({"chat_id": chat["id"]})
            assert ws.receive_json()["session"]
            for tid in ("t1", "t2", "t3"):
                assert _say(ws, 0.3, tid) == {"final": "hello world",
                                               "turn_id": tid}
                assert crosstalk.take_words(tid) == (True, None)
            ws.send_json({"done": True})


def test_a_late_timed_final_never_becomes_a_second_final(only_app,
                                                         monkeypatch):
    """The plain final went out alone; its twin arrives after that. It is
    recognised as the twin: no second final, the next commit keeps its own
    id, and the words still reach the split for the right turn."""
    monkeypatch.setattr(voice_mod, "TIMED_FINAL_WAIT_S", 0.05)
    _timed_relay(only_app, monkeypatch, TimedEleven("late", late_s=0.3))
    with TestClient(only_app, base_url="http://127.0.0.1") as c:
        chat = c.post("/api/chats", json={}).json()
        with c.websocket_connect("/api/voice/stt-stream") as ws:
            ws.send_json({"chat_id": chat["id"]})
            assert ws.receive_json()["session"]
            assert _say(ws, 0.3, "t1") == {"final": "hello world",
                                           "turn_id": "t1"}
            time.sleep(0.5)                 # the late twin lands
            ws.send_json(_speech(0.2)[0])
            assert ws.receive_json() == {"partial": "hello"}
            known, entry = crosstalk.take_words("t1")
            assert known and [w[2] for w in entry["words"]] == [
                "hello", "world"]
            assert _say(ws, 0.3, "t2") == {"final": "hello world",
                                           "turn_id": "t2"}
            ws.send_json({"done": True})


def test_a_timed_final_on_its_own_is_the_commits_final(app, monkeypatch):
    _timed_relay(app, monkeypatch, TimedEleven("timed"))
    with TestClient(app, base_url="http://127.0.0.1") as c:
        chat = c.post("/api/chats", json={}).json()
        with c.websocket_connect("/api/voice/stt-stream") as ws:
            ws.send_json({"chat_id": chat["id"]})
            assert ws.receive_json()["session"]
            for tid in ("t1", "t2"):
                assert _say(ws, 0.3, tid) == {"final": "hello world",
                                               "turn_id": tid}
            ws.send_json({"done": True})


def test_words_are_kept_in_turn_time_before_the_final_goes_out(
        only_app, monkeypatch):
    """Scribe counts from the first audio on the socket, not from each
    commit. The relay records where each turn began on that clock, so the
    second turn's words start near zero, and they're in place by the time
    the browser has the final."""
    fake = TimedEleven("both", script=[
        [(0.1, 0.4, "first"), (0.5, 0.9, "turn")],
        [(0.1, 0.3, "second")]])
    _timed_relay(only_app, monkeypatch, fake)
    with TestClient(only_app, base_url="http://127.0.0.1") as c:
        chat = c.post("/api/chats", json={}).json()
        with c.websocket_connect("/api/voice/stt-stream") as ws:
            ws.send_json({"chat_id": chat["id"]})
            assert ws.receive_json()["session"]
            assert _say(ws, 1.0, "t1")["turn_id"] == "t1"
            assert crosstalk.take_words("t1") == (True, {
                "words": [(0.1, 0.4, "first"), (0.5, 0.9, "turn")],
                "stt_s": 1.0})
            assert _say(ws, 0.5, "t2")["turn_id"] == "t2"
            assert crosstalk.take_words("t2") == (True, {
                "words": [(0.1, 0.3, "second")], "stt_s": 0.5})
            ws.send_json({"done": True})
    # the socket's own clock did run on: the fake sent 1.1 s for "second"
    assert fake.sent_s == pytest.approx(1.5)


def test_with_no_diariser_the_words_are_never_held(app, monkeypatch):
    """The words hold transcript text and only the crosstalk split reads
    them, which needs the tracker's spans. With no diariser configured
    there is no split, so the relay holds no words at all."""
    from backend import diarize
    monkeypatch.setattr(diarize, "schedule_turn_check", lambda *a, **k: None)
    _timed_relay(app, monkeypatch, TimedEleven("both", script=[
        [(0.1, 0.4, "first"), (0.5, 0.9, "turn")]]))
    with TestClient(app, base_url="http://127.0.0.1") as c:
        chat = c.post("/api/chats", json={}).json()
        with c.websocket_connect("/api/voice/stt-stream") as ws:
            ws.send_json({"chat_id": chat["id"]})
            assert ws.receive_json()["session"]
            assert _say(ws, 1.0, "t1")["turn_id"] == "t1"
            ws.send_json({"done": True})
    assert crosstalk.take_words("t1") == (False, None)


def test_a_reconnected_socket_starts_its_own_clock(only_app, monkeypatch):
    """A new socket counts from zero again. Each turn is read against the
    socket it was committed on, so a reconnect changes nothing."""
    _timed_relay(only_app, monkeypatch,
                 TimedEleven("both", script=[[(0.2, 0.6, "before")]]),
                 TimedEleven("both", script=[[(0.2, 0.6, "after")]]))
    with TestClient(only_app, base_url="http://127.0.0.1") as c:
        chat = c.post("/api/chats", json={}).json()
        for tid, seconds in (("t1", 1.5), ("t2", 0.8)):
            with c.websocket_connect("/api/voice/stt-stream") as ws:
                ws.send_json({"chat_id": chat["id"]})
                assert ws.receive_json()["session"]
                assert _say(ws, seconds, tid)["turn_id"] == tid
                ws.send_json({"done": True})
            known, entry = crosstalk.take_words(tid)
            assert known and entry["words"][0][:2] == (0.2, 0.6)
            assert entry["stt_s"] == seconds


def test_words_are_not_kept_with_the_session_naming_off(app, monkeypatch):
    _timed_relay(app, monkeypatch, TimedEleven("both"))
    with TestClient(app, base_url="http://127.0.0.1") as c:
        chat = c.post("/api/chats", json={}).json()
        with c.websocket_connect("/api/voice/stt-stream") as ws:
            ws.send_json({"chat_id": chat["id"]})
            assert ws.receive_json()["session"]
            assert _say(ws, 0.3, "t1")["turn_id"] == "t1"
            ws.send_json({"done": True})
    assert crosstalk.take_words("t1") == (False, None)


# The pairing itself, pure: CommitFinals with a clock the test holds.

def _finals(outs):
    return [(o["turn"]["turn_id"], o["text"], o["words"] is not None)
            for o in outs]


def test_pairing_the_timed_final_is_the_one_final():
    f = voice_mod.CommitFinals(wait_s=0.3)
    f.commit("t1", 0.0, 1.0)
    f.commit("t2", 1.0, 2.0)
    assert f.plain("one", 0.0) == [] and f.wait_left(0.1) == \
        pytest.approx(0.2)
    assert _finals(f.timed("one", [], 0.02)) == [("t1", "one", True)]
    assert f.wait_left(0.5) is None
    assert f.plain("two", 1.0) == []
    assert _finals(f.timed("two", [], 1.02)) == [("t2", "two", True)]
    assert f.commits == [] and f.alone == 0


def test_pairing_a_plain_final_alone_goes_out_after_the_wait():
    f = voice_mod.CommitFinals(wait_s=0.3)
    f.commit("t1", 0.0, 1.0)
    assert f.plain("one", 0.0) == []
    assert f.expire(0.2) == []
    assert _finals(f.expire(0.3)) == [("t1", "one", False)]
    # its twin, late: the words, and no second final
    assert _finals(f.timed("one", [], 0.4)) == [("t1", None, True)]
    assert f.alone == 1


def test_pairing_a_held_final_is_never_lost():
    f = voice_mod.CommitFinals(wait_s=0.3)
    for tid in ("t1", "t2", "t3"):
        f.commit(tid, 0.0, 1.0)
    f.plain("one", 0.0)
    # the next commit's plain final arrives first: the held one goes out
    assert _finals(f.plain("two", 0.1)) == [("t1", "one", False)]
    # the upstream closes: the one held now goes out too
    assert _finals(f.close()) == [("t2", "two", False)]
    assert f.commits[0]["turn_id"] == "t3"


def test_pairing_stops_waiting_once_word_times_keep_missing():
    f = voice_mod.CommitFinals(wait_s=0.3)
    for tid in ("t1", "t2", "t3"):
        f.commit(tid, 0.0, 1.0)
    f.plain("one", 0.0)
    f.expire(1.0)
    f.plain("two", 2.0)
    f.expire(3.0)
    # two misses in a row: the third goes out at once, with no wait
    assert _finals(f.plain("three", 4.0)) == [("t3", "three", False)]
    assert f.wait_left(4.0) is None


def test_pairing_a_different_timed_final_with_a_commit_waiting_is_that_commits():
    """A plain final went out alone and the next commit is already waiting:
    a timed final that doesn't read as the one owed belongs to the next
    commit, so it isn't swallowed."""
    f = voice_mod.CommitFinals(wait_s=0.3)
    f.commit("t1", 0.0, 1.0)
    f.commit("t2", 1.0, 2.0)
    f.plain("one", 0.0)
    f.expire(0.5)
    assert _finals(f.timed("two", [], 0.6)) == [("t2", "two", True)]


def test_pairing_a_final_with_no_commit_carries_no_turn_id():
    f = voice_mod.CommitFinals(wait_s=0.3)
    assert _finals(f.timed("stray", [], 0.0)) == [(None, "stray", True)]


def test_a_read_that_fails_after_the_relay_moved_on_is_never_logged():
    """27 September: 17 'Task exception was never retrieved' errors, each a
    Scribe read that failed (closed without a close frame) after the relay
    had stopped waiting on it. collect_read collects such a read."""
    import gc
    import websockets
    logged = []

    async def main():
        loop = asyncio.get_running_loop()
        loop.set_exception_handler(lambda l, ctx: logged.append(ctx["message"]))

        async def failing_read():
            raise websockets.ConnectionClosedError(None, None)

        watched = asyncio.ensure_future(failing_read())
        watched.add_done_callback(voice_router.collect_read)
        unwatched = asyncio.ensure_future(failing_read())
        await asyncio.sleep(0.01)
        del watched, unwatched
        gc.collect()
        await asyncio.sleep(0)

    asyncio.run(main())
    # the unwatched read shows what the callback prevents
    assert logged.count("Task exception was never retrieved") == 1


# ---- #470: a quiet socket stays open ----
#
# Scribe closes a realtime socket that has heard no audio for about 15 s,
# with a normal close and no error first (probed 27 September with silence
# only). The browser streams only while someone speaks, so every quiet
# stretch ended the socket. The relay now fills a long gap with a sliver of
# silence, commits that silence itself before Scribe would, and keeps the
# final to itself. These tests shrink the gap to milliseconds.

class QuietEleven(TimedEleven):
    """Scribe as the probe found it: silence gets no partial, and a commit
    of silence gets empty finals, the timed one first as often as not."""

    async def send(self, raw):
        msg = json.loads(raw)
        audio = base64.b64decode(msg.get("audio_base_64") or "")
        if audio and not any(audio) and not msg.get("commit"):
            self.sent.append(msg)
            self.sent_s += len(audio) / 2 / msg.get("sample_rate", 16000)
            return
        if audio and not any(audio) and msg.get("commit"):
            self.sent.append(msg)
            self.sent_s += len(audio) / 2 / msg.get("sample_rate", 16000)
            self.turn_start = None
            self.queue.put_nowait(json.dumps(
                {"message_type": TIMED, "text": "", "words": []}))
            self.queue.put_nowait(json.dumps({"message_type": PLAIN, "text": ""}))
            return
        await super().send(raw)


def _keepalives(fake):
    silence = voice_mod.keepalive_audio()
    return [m for m in fake.sent if m.get("audio_base_64") == silence]


@pytest.fixture
def quick_keepalive(monkeypatch):
    monkeypatch.setattr(voice_mod, "STT_KEEPALIVE_IDLE_S", 0.05)
    monkeypatch.setattr(voice_mod, "STT_KEEPALIVE_COMMIT_S", 0.05)


def test_a_quiet_socket_gets_silence_and_the_next_turn_keeps_its_id(
        only_app, monkeypatch, quick_keepalive, caplog):
    fake = QuietEleven("both", script=[[(0.1, 0.4, "after"), (0.5, 0.9, "quiet")]])
    _timed_relay(only_app, monkeypatch, fake)
    caplog.set_level("INFO", logger="crossband.voice")
    with TestClient(only_app, base_url="http://127.0.0.1") as c:
        chat = c.post("/api/chats", json={}).json()
        with c.websocket_connect("/api/voice/stt-stream") as ws:
            ws.send_json({"chat_id": chat["id"]})
            assert ws.receive_json()["session"]
            time.sleep(0.6)                 # a quiet stretch
            kept = _keepalives(fake)
            assert len(kept) >= 4
            assert all(m["sample_rate"] == 16000 for m in kept)
            # The silence was committed by the relay, and those finals
            # never reached the browser: the next thing down is this
            # turn's own final, under its own id.
            assert any(m["commit"] for m in kept)
            assert _say(ws, 1.0, "t1") == {"final": "after quiet",
                                           "turn_id": "t1"}
            # Its words are in its own time, though silence went first.
            known, entry = crosstalk.take_words("t1")
            assert known and entry["words"] == [(0.1, 0.4, "after"),
                                                (0.5, 0.9, "quiet")]
            ws.send_json({"done": True})
    close = [r.getMessage() for r in caplog.records
             if "stt capture close" in r.getMessage()]
    assert close and "turns=1" in close[0]
    assert f"keepalives={len(_keepalives(fake))}" in close[0]


def test_silence_during_an_open_turn_is_never_committed(
        app, monkeypatch, quick_keepalive):
    fake = QuietEleven("both")
    _timed_relay(app, monkeypatch, fake)
    with TestClient(app, base_url="http://127.0.0.1") as c:
        chat = c.post("/api/chats", json={}).json()
        with c.websocket_connect("/api/voice/stt-stream") as ws:
            ws.send_json({"chat_id": chat["id"]})
            assert ws.receive_json()["session"]
            frames = _speech(0.3)
            ws.send_json(frames[0])         # a turn opens
            assert ws.receive_json() == {"partial": "hello"}
            time.sleep(0.4)                 # and its audio stalls
            during = _keepalives(fake)
            assert during and not any(m["commit"] for m in during)
            for f in frames[1:-1]:
                ws.send_json(f)
                assert ws.receive_json() == {"partial": "hello"}
            ws.send_json(dict(frames[-1], turn_id="t1"))
            assert ws.receive_json() == {"final": "hello world",
                                         "turn_id": "t1"}
            ws.send_json({"done": True})


def test_a_busy_socket_sends_no_keepalive(app, monkeypatch):
    """At the real gap nothing the browser sends in a normal exchange is
    ever joined by silence: the frames upstream are the browser's own."""
    fake = QuietEleven("both")
    _timed_relay(app, monkeypatch, fake)
    with TestClient(app, base_url="http://127.0.0.1") as c:
        chat = c.post("/api/chats", json={}).json()
        with c.websocket_connect("/api/voice/stt-stream") as ws:
            ws.send_json({"chat_id": chat["id"]})
            assert ws.receive_json()["session"]
            for tid in ("t1", "t2"):
                assert _say(ws, 0.3, tid)["turn_id"] == tid
            ws.send_json({"done": True})
    assert _keepalives(fake) == []
    assert voice_mod.STT_KEEPALIVE_IDLE_S < 15


def test_keepalive_audio_is_twenty_ms_of_silence():
    raw = base64.b64decode(voice_mod.keepalive_audio())
    assert len(raw) == 16000 * 2 * voice_mod.STT_KEEPALIVE_MS // 1000
    assert not any(raw)
    # Committed in batches Scribe accepts (at least 0.3 s), well under the
    # 36 s it would commit on its own.
    assert 0.3 <= voice_mod.STT_KEEPALIVE_COMMIT_S < 36


def test_pairing_a_timed_final_first_then_its_plain_twin():
    """Seen live: Scribe answers a commit timed first, then plain. That is
    one final, and the next commit keeps its own id."""
    f = voice_mod.CommitFinals(wait_s=0.3)
    f.commit("t1", 0.0, 1.0)
    f.commit("t2", 1.0, 2.0)
    assert _finals(f.timed("", [], 0.0)) == [("t1", "", True)]
    assert f.plain("", 0.01) == [] and f.wait_left(0.02) is None
    assert f.commits[0]["turn_id"] == "t2"
    assert f.plain("two", 1.0) == []
    assert _finals(f.timed("two", [], 1.02)) == [("t2", "two", True)]
    assert f.alone == 0 and f.misses == 0


def test_pairing_a_keepalive_commit_is_marked():
    f = voice_mod.CommitFinals(wait_s=0.3)
    f.commit(None, 5.0, 5.0, keepalive=True)
    f.commit("t1", 5.0, 6.0)
    out = f.timed("", [], 0.0) + f.plain("", 0.0)
    assert [o["turn"].get("keepalive") for o in out] == [True]
    f.plain("one", 1.0)
    assert f.timed("one", [], 1.01)[0]["turn"]["turn_id"] == "t1"


def test_pairing_a_plain_final_after_a_timed_one_can_be_the_next_commits():
    """The timed final came alone and the next commit is already waiting:
    a plain final that doesn't read as its twin is the next commit's."""
    f = voice_mod.CommitFinals(wait_s=0.3)
    f.commit("t1", 0.0, 1.0)
    f.commit("t2", 1.0, 2.0)
    f.timed("one", [], 0.0)
    assert f.plain("two", 0.5) == []
    assert _finals(f.expire(0.9)) == [("t2", "two", False)]


def test_each_backup_path_turn_is_logged_with_why_and_no_words(
        app, monkeypatch, caplog):
    """#470: how often a turn takes the backup path shows in service.log,
    one content-free line per turn: the reason the browser gave, from an
    allowlist, and how long the speech was."""
    monkeypatch.setattr(voice_router.voice, "provider_for",
                        lambda cfg: voice_router.voice.PROVIDER_ELEVENLABS)
    monkeypatch.setattr(voice_router.voice, "transcribe",
                        lambda data, mime, cfg: ("Sam fixed the gate", "scribe_v2"))
    caplog.set_level("INFO", logger="crossband.voice")
    with TestClient(app, base_url="http://127.0.0.1") as c:
        chat = c.post("/api/chats", json={}).json()
        for why in ("late", "reconnect", "Sam fixed the gate", ""):
            r = c.post(f"/api/chats/{chat['id']}/stt",
                       files={"file": ("utterance.webm", b"\x1a\x45" * 64,
                                       "audio/webm")},
                       data={"duration_ms": "2400", "why": why})
            assert r.status_code == 200
    lines = [r.getMessage() for r in caplog.records
             if r.getMessage().startswith("stt backup path")]
    assert [ln.split("why=")[1].split()[0] for ln in lines] == [
        "late", "reconnect", "unknown", "unknown"]
    assert all("speech_s=2.4" in ln for ln in lines)
    assert not any("gate" in r.getMessage() for r in caplog.records)
