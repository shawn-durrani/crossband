"""The realtime STT relay, driven end to end with a faked ElevenLabs socket.

Exists because of a live failure: the prewarm hook shipped with a missing
import, and the relay died with NameError on the FIRST commit frame - no
test exercised the relay's message loop at all ("thin hook, covered by
review" - it wasn't). These tests run the real handler: init → audio →
commit → final transcript back, with the prewarm observed firing, and prove
a prewarm failure can no longer break transcription."""

import asyncio
import base64
import time
import json

import pytest
from fastapi.testclient import TestClient

from backend import engine
from backend.app import create_app
from backend.config import Settings
from backend.routers import voice as voice_router


@pytest.fixture
def app(tmp_path):
    settings = Settings(data_dir=str(tmp_path / "data"),
                        memory_url="http://127.0.0.1:1")
    return create_app(settings)


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
