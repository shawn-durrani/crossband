"""The voice model reaching the TTS relay, and the settings around it (#480).

The relay speaks each reply through one of ElevenLabs' two streaming
sockets, chosen by the model: v3 ids ride the dialogue socket, the rest the
text-to-speech socket, which refuses v3 at the handshake. These tests drive
the real handler with a faked ElevenLabs socket and pin:

1. The app setting reaches the upstream URL, and today's default speaks
   byte for byte as before.
2. A v3 model opens the dialogue socket, and the browser's text, flush,
   done and keepalive frames are translated for it.
3. A seat's own choice wins over the app's.
4. A model ElevenLabs refuses at the handshake falls back within the same
   reply, is remembered, and Automatic moves on. Any other handshake
   failure is reported as before.
5. An id that isn't on the list never reaches ElevenLabs.
6. The settings routes: the list, saving a choice to config.local.json,
   refusing an unknown id, and a seat's choice validated on save.
7. The latency trace stores and segments the model that spoke.
8. The benchmark's whole-reply synthesis resolves the same way.

Keyless: every socket and HTTP call is a fake."""

import asyncio
import json

import pytest
import websockets
from fastapi.testclient import TestClient
from websockets.datastructures import Headers
from websockets.http11 import Response

from backend import auth, config, db, tts_models, voice, voice_trace
from backend.app import create_app
from backend.config import Settings
from backend.routers import voice as voice_router

VOICE = "voice123"
REFUSED_BODY = (b'{"detail":{"type":"validation_error","code":"unsupported_model",'
                b'"status":"unsupported_model","param":"model_id"}}')


class FakeUpstream:
    """One ElevenLabs streaming socket: records what the relay sends, answers
    the first real text with one audio chunk and the close with a final
    frame, named the way its socket names it."""

    def __init__(self, url, refuse=None):
        self.url, self.refuse = url, refuse
        self.sent = []
        self.raw = []
        self.queue = asyncio.Queue()
        self.dialogue = "/text-to-dialogue/" in url

    async def __aenter__(self):
        if self.refuse is not None:
            status, body = self.refuse
            raise websockets.InvalidStatus(Response(status, "x", Headers(), body))
        return self

    async def __aexit__(self, *a):
        return False

    async def send(self, raw):
        msg = json.loads(raw)
        self.sent.append(msg)
        self.raw.append(raw)
        spoken = (msg.get("inputs") if self.dialogue
                  else (msg.get("text") or "").strip() and not msg.get("xi_api_key"))
        if spoken and not any("audio" in json.loads(q) for q in self.queue._queue):
            self.queue.put_nowait(json.dumps({"audio": "QUJD"}))
        if self.dialogue and msg.get("close_socket"):
            self.queue.put_nowait(json.dumps({"is_final_audio_for_turn": True}))
            self.queue.put_nowait(json.dumps({"is_final": True}))
        if not self.dialogue and msg == {"text": ""}:
            self.queue.put_nowait(json.dumps({"isFinal": True}))

    def __aiter__(self):
        return self

    async def __anext__(self):
        return await self.queue.get()


class Upstreams:
    """Stands in for websockets.connect: one FakeUpstream per dial, with a
    handshake answer chosen per model id."""

    def __init__(self):
        self.dialled = []
        self.refuse = {}   # model id -> (status, body)

    def __call__(self, url, **kw):
        model = url.split("model_id=")[1].split("&")[0]
        up = FakeUpstream(url, self.refuse.get(model))
        self.dialled.append(up)
        return up

    @property
    def last(self):
        return self.dialled[-1]


@pytest.fixture
def app(tmp_path, monkeypatch):
    local = tmp_path / "config.local.json"
    monkeypatch.setattr(config, "LOCAL_CONFIG_PATH", local)
    monkeypatch.delenv("CROSSBAND_TTS_MODEL", raising=False)
    settings = Settings(data_dir=str(tmp_path / "data"),
                        memory_url="http://127.0.0.1:1")
    a = create_app(settings)
    a.state.allowed_hosts = {"testserver", "127.0.0.1", "localhost", "::1"}
    monkeypatch.setattr(auth, "GATE_LOOPBACK_HOSTS",
                        auth.GATE_LOOPBACK_HOSTS | {"testserver"})
    return a


@pytest.fixture
def upstreams(monkeypatch):
    fake = Upstreams()
    monkeypatch.setattr(voice_router.websockets, "connect", fake)
    monkeypatch.setattr(voice, "api_key", lambda: "test-key")
    # the relay never fetches the list itself; keep any GET keyless too
    monkeypatch.setattr(voice, "list_models", lambda: (_ for _ in ()).throw(
        RuntimeError("no network in tests")))
    return fake


def _set(app, **over):
    app.state.settings = app.state.settings.model_copy(update=over)


def _speak(c, seat="", text="Hello there, how are you?", chat_id=None):
    """One reply through the relay. Returns every frame the browser got.
    The whole reply is sent before any audio is awaited, because on v3 the
    relay holds a sentence until it ends (#493)."""
    got = []
    with c.websocket_connect("/api/voice/tts") as ws:
        init = {"chat_id": chat_id, "voice_id": VOICE}
        if seat:
            init["seat"] = seat
        ws.send_json(init)
        got.append(ws.receive_json())
        for piece in [text] if isinstance(text, str) else text:
            ws.send_json({"text": piece})
        ws.send_json({"flush": True, "done": True})
        while not (got[-1].get("final") or got[-1].get("error")):
            got.append(ws.receive_json())
    return got


# ---------- 1. the default speaks as before ----------

def test_the_default_speaks_through_the_speech_socket_as_before(app, upstreams):
    with TestClient(app, base_url="http://127.0.0.1") as c:
        got = _speak(c)
    assert got == [{"tts_model": "eleven_flash_v2_5"}, {"audio": "QUJD"}, {"final": True}]
    up = upstreams.last
    assert up.url == voice.TTS_WS_URL.format(voice_id=VOICE, model_id="eleven_flash_v2_5")
    assert up.sent[0] == json.loads(voice.tts_init_message(app.state.settings.as_cfg()))
    assert up.sent[1:] == [{"text": "Hello there, how are you?"},
                           {"text": " ", "flush": True}, {"text": ""}]


def test_the_app_setting_reaches_the_upstream_url(app, upstreams):
    _set(app, tts_model="eleven_multilingual_v2")
    with TestClient(app, base_url="http://127.0.0.1") as c:
        assert _speak(c)[0] == {"tts_model": "eleven_multilingual_v2"}
    assert "model_id=eleven_multilingual_v2&" in upstreams.last.url


# ---------- 2. v3 on the dialogue socket ----------

def test_automatic_speaks_v3_conversational_through_the_dialogue_socket(app, upstreams):
    _set(app, tts_model="auto")
    with TestClient(app, base_url="http://127.0.0.1") as c:
        got = _speak(c)
    assert got == [{"tts_model": "eleven_v3_conversational"}, {"audio": "QUJD"},
                   {"final": True}]
    up = upstreams.last
    assert up.url == voice.TTD_WS_URL.format(model_id="eleven_v3_conversational")
    # #493's defaults: Robust stability, and the sentence held until the
    # flush because nothing followed its question mark.
    assert up.sent[0] == {"voices": [VOICE], "xi_api_key": "test-key",
                          "voice_settings": {"stability": 1.0}}
    assert up.sent[1:] == [
        {"keep_alive": True},
        {"inputs": [{"text": "Hello there, how are you?", "voice_id": VOICE}]},
        {"flush": True}, {"close_socket": True}]


def test_natural_and_no_sentence_chunks_send_what_v3_sent_before_493(app, upstreams):
    _set(app, tts_model="auto", tts_v3_stability="natural",
         tts_v3_sentence_chunks=False)
    with TestClient(app, base_url="http://127.0.0.1") as c:
        _speak(c)
    assert upstreams.last.sent == [
        {"voices": [VOICE], "xi_api_key": "test-key",
         "voice_settings": {"stability": 0.5}},
        {"inputs": [{"text": "Hello there, how are you?", "voice_id": VOICE}]},
        {"flush": True}, {"close_socket": True}]


def test_whitespace_on_the_dialogue_socket_keeps_it_alive_and_keeps_words_apart():
    route, carry = tts_models.ROUTE_DIALOGUE, {}
    frames = [json.loads(f) for msg in ({"text": "Hello"}, {"text": " "},
                                        {"text": "there"})
              for f in voice.tts_upstream_frames(route, msg, VOICE, carry)]
    assert frames == [{"inputs": [{"text": "Hello", "voice_id": VOICE}]},
                      {"keep_alive": True},
                      {"inputs": [{"text": " there", "voice_id": VOICE}]}]
    assert carry == {}


def test_each_socket_names_its_last_frame_its_own_way():
    tts, ttd = tts_models.ROUTE_TTS, tts_models.ROUTE_DIALOGUE
    assert voice.tts_downstream(tts, {"audio": "A", "isFinal": None}) == ({"audio": "A"}, False)
    assert voice.tts_downstream(tts, {"isFinal": True}) == ({"final": True}, True)
    assert voice.tts_downstream(ttd, {"is_final_audio_for_turn": True}) == ({}, False)
    assert voice.tts_downstream(ttd, {"is_final": True}) == ({"final": True}, True)
    assert voice.tts_downstream(ttd, {"error": "bad_voice", "message": "No such voice"}) \
        == ({"error": "No such voice"}, True)


# ---------- 3. a seat's own choice ----------

def test_a_seats_own_choice_wins_over_the_app_setting(app, upstreams):
    with TestClient(app, base_url="http://127.0.0.1") as c:
        con = db.connect()
        pid, slug = con.execute("SELECT id, slug FROM participants LIMIT 1").fetchone()
        con.close()
        r = c.patch(f"/api/participants/{pid}", json={"tts_model": "eleven_v3"})
        assert r.status_code == 200 and r.json()["tts_model"] == "eleven_v3"
        assert _speak(c, seat=slug)[0] == {"tts_model": "eleven_v3"}
        assert "/text-to-dialogue/" in upstreams.last.url
        # another seat, and a seat left blank, follow the app
        assert _speak(c, seat="nobody")[0] == {"tts_model": "eleven_flash_v2_5"}
        c.patch(f"/api/participants/{pid}", json={"tts_model": ""})
        assert _speak(c, seat=slug)[0] == {"tts_model": "eleven_flash_v2_5"}


def test_a_seat_choice_is_validated_on_save(app):
    with TestClient(app, base_url="http://127.0.0.1") as c:
        con = db.connect()
        pid = con.execute("SELECT id FROM participants LIMIT 1").fetchone()[0]
        con.close()
        for bad in ("eleven_v9", "eleven_v3?x=1", "eleven_english_sts_v2"):
            r = c.patch(f"/api/participants/{pid}", json={"tts_model": bad})
            assert r.status_code == 400, bad
        assert c.patch(f"/api/participants/{pid}",
                       json={"tts_model": "auto"}).json()["tts_model"] == "auto"
        r = c.post("/api/participants", json={"name": "Sam", "provider": "openai",
                                              "model": "gpt-5.1", "tts_model": "nope"})
        assert r.status_code == 400
        r = c.post("/api/participants", json={
            "name": "Sam", "provider": "openai", "model": "gpt-5.1",
            "tts_model": "eleven_v3_conversational"})
        assert r.json()["tts_model"] == "eleven_v3_conversational"


# ---------- 4. refusals fall back within the reply ----------

def test_a_refused_model_falls_back_in_the_same_reply_and_is_remembered(app, upstreams):
    _set(app, tts_model="auto")
    upstreams.refuse["eleven_v3_conversational"] = (400, REFUSED_BODY)
    with TestClient(app, base_url="http://127.0.0.1") as c:
        got = _speak(c)
        assert got[0] == {"tts_model": "eleven_v3"}
        assert got[1:] == [{"audio": "QUJD"}, {"final": True}]
        assert tts_models.refused_ids() == {"eleven_v3_conversational"}
        # the next reply goes straight to the next model: no second refusal
        upstreams.dialled.clear()
        assert _speak(c)[0] == {"tts_model": "eleven_v3"}
        assert len(upstreams.dialled) == 1
        body = c.get("/api/voice/models").json()
        assert body["automatic_pick"] == "eleven_v3"
        assert body["refused"] == ["eleven_v3_conversational"]


def test_a_refused_explicit_pick_falls_back_to_flash(app, upstreams):
    _set(app, tts_model="eleven_v3")
    upstreams.refuse["eleven_v3"] = (400, REFUSED_BODY)
    with TestClient(app, base_url="http://127.0.0.1") as c:
        assert _speak(c)[0] == {"tts_model": "eleven_flash_v2_5"}


def test_other_handshake_failures_are_reported_and_never_marked(app, upstreams):
    _set(app, tts_model="auto")
    upstreams.refuse["eleven_v3_conversational"] = (401, b'{"detail":"invalid_api_key"}')
    with TestClient(app, base_url="http://127.0.0.1") as c:
        with c.websocket_connect("/api/voice/tts") as ws:
            ws.send_json({"chat_id": None, "voice_id": VOICE})
            msg = ws.receive_json()
    assert "error" in msg and "401" in msg["error"]
    assert len(upstreams.dialled) == 1
    assert tts_models.refused_ids() == set()


# ---------- 5. an unknown id never reaches ElevenLabs ----------

def test_a_hand_edited_unknown_id_speaks_with_flash(app, upstreams):
    _set(app, tts_model="eleven_v9&output_format=pcm_8000")
    with TestClient(app, base_url="http://127.0.0.1") as c:
        assert _speak(c)[0] == {"tts_model": "eleven_flash_v2_5"}
    assert all("eleven_v9" not in up.url for up in upstreams.dialled)


# ---------- 6. the settings routes ----------

def test_the_list_route_offers_the_pinned_list_when_keyless(app):
    with TestClient(app, base_url="http://127.0.0.1") as c:
        body = c.get("/api/voice/models").json()
    assert body["source"] == "pinned"
    assert body["setting"] == "eleven_flash_v2_5"
    assert body["automatic_pick"] == "eleven_v3_conversational"
    assert [o["value"] for o in body["options"]][:2] == ["eleven_v3_conversational",
                                                         "eleven_v3"]


def test_the_list_route_reads_the_live_list_when_voice_is_on(app, monkeypatch):
    monkeypatch.setattr(voice, "api_key", lambda: "test-key")
    listed = [{"model_id": m["id"], "name": m["name"], "can_do_text_to_speech": True}
              for m in tts_models.PINNED]
    listed.append({"model_id": "eleven_v4", "name": "Eleven v4",
                   "can_do_text_to_speech": True})
    monkeypatch.setattr(voice, "list_models", lambda: listed)
    with TestClient(app, base_url="http://127.0.0.1") as c:
        body = c.get("/api/voice/models").json()
        assert body["source"] == "live"
        assert body["automatic_pick"] == "eleven_v4"
        assert c.put("/api/voice/model", json={"model": "eleven_v4"}).status_code == 200


def test_saving_a_choice_writes_config_local_and_applies_to_the_next_reply(
        app, upstreams, tmp_path):
    with TestClient(app, base_url="http://127.0.0.1") as c:
        r = c.put("/api/voice/model", json={"model": "auto"})
        assert r.status_code == 200 and r.json()["setting"] == "auto"
        assert json.loads((tmp_path / "config.local.json").read_text()) == {
            "tts_model": "auto"}
        assert app.state.settings.tts_model == "auto"
        assert _speak(c)[0] == {"tts_model": "eleven_v3_conversational"}
        status = c.get("/api/voice/status").json()
        assert status["tts_model"] == "eleven_v3_conversational"
        assert status["tts_model_setting"] == "auto"


def test_an_unknown_choice_is_refused_and_nothing_is_written(app, tmp_path):
    with TestClient(app, base_url="http://127.0.0.1") as c:
        for bad in ("eleven_v9", "", "auto&x", "eleven_english_sts_v2"):
            r = c.put("/api/voice/model", json={"model": bad})
            assert r.status_code == 400, bad
    assert not (tmp_path / "config.local.json").exists()


def test_an_environment_override_locks_the_choice(app, monkeypatch, tmp_path):
    monkeypatch.setenv("CROSSBAND_TTS_MODEL", "eleven_flash_v2_5")
    with TestClient(app, base_url="http://127.0.0.1") as c:
        assert c.get("/api/voice/models").json()["locked_by_env"] is True
        assert c.put("/api/voice/model", json={"model": "auto"}).status_code == 409
    assert not (tmp_path / "config.local.json").exists()


# ---------- 7. the trace records the model that spoke ----------

def test_the_trace_stores_and_segments_the_voice_model(app):
    with TestClient(app, base_url="http://127.0.0.1") as c:
        r = c.post("/api/voice/trace", json={"turn_id": "t1", "stages": [
            {"stage": "first_token_to_first_audio", "ms": 300, "model": "gpt-5.1",
             "tts_provider": "elevenlabs", "tts_model": "eleven_v3_conversational"},
            {"stage": "first_token_to_first_audio", "ms": 120, "model": "gpt-5.1",
             "tts_provider": "elevenlabs", "tts_model": "eleven_flash_v2_5"},
        ]})
        assert r.json() == {"stored": 2}
        con = db.connect()
        rows = db.get_voice_traces(con)
        con.close()
        assert {r["tts_model"] for r in rows} == {"eleven_v3_conversational",
                                                  "eleven_flash_v2_5"}
    seg = voice_trace.aggregate(rows)["stages"]["first_token_to_first_audio"]
    assert seg["by_tts_model"]["eleven_v3_conversational"]["p50"] == 300
    assert seg["by_tts_model"]["eleven_flash_v2_5"]["p50"] == 120


def test_v29_to_v30_migration_lands_old_rows_following_the_app(tmp_path):
    import sqlite3
    data = tmp_path / "data30"
    data.mkdir()
    con0 = sqlite3.connect(data / "chat.db")
    con0.executescript(
        "CREATE TABLE participants(id INTEGER PRIMARY KEY, slug TEXT NOT NULL UNIQUE,"
        " name TEXT NOT NULL, provider TEXT NOT NULL, model TEXT NOT NULL,"
        " base_url TEXT, api_key_env TEXT, system_prompt TEXT NOT NULL DEFAULT '',"
        " color TEXT NOT NULL DEFAULT '#a1a1aa', voice_id TEXT NOT NULL DEFAULT '',"
        " voice_gain REAL NOT NULL DEFAULT 1.0,"
        " reasoning_effort TEXT NOT NULL DEFAULT '',"
        " thinking_control TEXT NOT NULL DEFAULT '', keep_alive TEXT NOT NULL DEFAULT '',"
        " enabled INTEGER NOT NULL DEFAULT 1, position INTEGER NOT NULL DEFAULT 0,"
        " lifecycle TEXT NOT NULL DEFAULT 'trial', created_at REAL NOT NULL);"
        "INSERT INTO participants(slug, name, provider, model, created_at)"
        " VALUES('claude', 'Claude', 'anthropic', 'claude-opus-4-8', 1);"
        "CREATE TABLE voice_turn_traces(id INTEGER PRIMARY KEY AUTOINCREMENT,"
        " turn_id TEXT NOT NULL, chat_id INTEGER, stage TEXT NOT NULL,"
        " ms REAL NOT NULL, provider TEXT NOT NULL DEFAULT '',"
        " model TEXT NOT NULL DEFAULT '', tts_provider TEXT NOT NULL DEFAULT '',"
        " speaker TEXT NOT NULL DEFAULT '', created_at REAL NOT NULL);"
        "INSERT INTO voice_turn_traces(turn_id, stage, ms, created_at)"
        " VALUES('t', 'end_to_end_first_audio', 900, 1);")
    con0.execute("PRAGMA user_version = 29")
    con0.commit()
    con0.close()
    db.configure(data)
    db.init()
    con = db.connect()
    try:
        assert con.execute("PRAGMA user_version").fetchone()[0] == db.SCHEMA_VERSION
        assert con.execute("SELECT tts_model FROM participants").fetchone()[0] == ""
        assert con.execute("SELECT tts_model FROM voice_turn_traces").fetchone()[0] == ""
    finally:
        con.close()


# ---------- 8. the benchmark resolves the same way ----------

def test_whole_reply_synthesis_resolves_the_model_before_any_call(monkeypatch):
    monkeypatch.setattr(voice, "api_key", lambda: "test-key")
    posts, dialogue = [], []

    class _R:
        status_code, content, text = 200, b"mp3", ""

    monkeypatch.setattr(voice.httpx, "post",
                        lambda url, **kw: posts.append(kw["json"]["model_id"]) or _R())
    monkeypatch.setattr(voice, "_synthesize_dialogue",
                        lambda text, vid, model, cfg: dialogue.append(model) or b"mp3")
    voice.synthesize("Hi.", VOICE, {"tts_model": "eleven_multilingual_v2"})
    voice.synthesize("Hi.", VOICE, {"tts_model": "auto"})
    voice.synthesize("Hi.", VOICE, {"tts_model": "made_up_model"})
    voice.synthesize("Hi.", VOICE, {})
    assert posts == ["eleven_multilingual_v2", "eleven_flash_v2_5", "eleven_flash_v2_5"]
    assert dialogue == ["eleven_v3_conversational"]
