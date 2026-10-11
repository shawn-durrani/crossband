"""The Scribe options a voice turn is transcribed with (#627).

Pins: the realtime connection always names its model, carries the
language only when the setting names one, and never asks for Scribe's
background filter, which Scribe refuses beside word times;
`stt_language` takes a two or three letter code and nothing else; a batch
voice turn sends the name hints and the language and drops sound tags,
while other audio keeps Scribe's defaults; and the backup route asks for a
voice turn with the same name hints the relay uses."""

from urllib.parse import parse_qs, urlparse

import pytest
from fastapi.testclient import TestClient

from backend import auth, voice
from backend.app import create_app
from backend.config import Settings
from backend.routers import voice as voice_router


def _query(url):
    return parse_qs(urlparse(url).query)


def test_the_realtime_url_names_its_model_and_adds_only_what_is_asked():
    q = _query(voice.stt_ws_url(["Alex"], timestamps=True))
    assert q["model_id"] == ["scribe_v2_realtime"]
    assert "language_code" not in q
    q = _query(voice.stt_ws_url(["Alex"], timestamps=True, language="en"))
    assert q["language_code"] == ["en"]
    assert "filter_background_audio" not in q
    assert q["keyterms"] == ["Alex"]
    assert q["include_timestamps"] == ["true"]


@pytest.mark.parametrize("value,expected", [
    ("en", "en"), (" EN ", "en"), ("yue", "yue"),
    ("", ""), (None, ""), ("english", ""), ("en-AU", ""), ("e", ""),
])
def test_stt_language_takes_a_two_or_three_letter_code(value, expected):
    assert voice.stt_language({"stt_language": value}) == expected


class _Ok:
    status_code = 200

    def json(self):
        return {"text": "hello", "words": []}


def _capture_post(monkeypatch):
    sent = []
    monkeypatch.setattr(voice.httpx, "post",
                        lambda *a, **kw: sent.append(kw["data"]) or _Ok())
    monkeypatch.setattr(voice, "_headers", lambda: {})
    return sent


def test_a_voice_turn_sends_hints_and_language_and_no_sound_tags(monkeypatch):
    sent = _capture_post(monkeypatch)
    voice.transcribe(b"x", "audio/webm", {"stt_language": "en"},
                     keyterms=["Alex", "alex", "Sam"], voice_turn=True)
    assert sent == [{"model_id": "scribe_v2", "keyterms": ["Alex", "Sam"],
                     "tag_audio_events": "false", "language_code": "en"}]


def test_a_voice_turn_with_no_language_set_leaves_detection_to_scribe(monkeypatch):
    sent = _capture_post(monkeypatch)
    voice.transcribe(b"x", "audio/webm", {}, voice_turn=True)
    assert sent == [{"model_id": "scribe_v2", "tag_audio_events": "false"}]


def test_other_audio_keeps_scribes_defaults(monkeypatch):
    """A podcast for transcribe_audio_url can be any language, and its
    sounds may matter, so the voice-turn options stay off."""
    sent = _capture_post(monkeypatch)
    voice.transcribe(b"x", "audio/mpeg", {"stt_language": "en"})
    assert sent == [{"model_id": "scribe_v2"}]


@pytest.fixture
def app(tmp_path):
    return create_app(Settings(data_dir=str(tmp_path / "data"),
                               memory_url="http://127.0.0.1:1",
                               stt_language="en"))


def test_the_backup_route_asks_for_a_voice_turn_with_name_hints(app, monkeypatch):
    calls = []

    def fake(data, mime, cfg, with_words=False, **kw):
        calls.append(kw)
        return "hello", "scribe_v2"

    monkeypatch.setattr(voice_router.voice, "provider_for",
                        lambda cfg: voice_router.voice.PROVIDER_ELEVENLABS)
    monkeypatch.setattr(voice_router.voice, "transcribe", fake)
    with TestClient(app, base_url="http://127.0.0.1") as c:
        chat = c.post("/api/chats", json={}).json()
        r = c.post(f"/api/chats/{chat['id']}/stt",
                   files={"file": ("utterance.webm", b"\x1a\x45" * 64,
                                   "audio/webm")},
                   data={"duration_ms": "900", "why": "late"})
    assert r.status_code == 200
    assert calls == [{"keyterms": ["User"], "voice_turn": True}]


def test_the_relay_connects_with_the_language_setting(app, monkeypatch):
    from tests.test_stt_relay import FakeEleven, _frame

    seen = {}

    def connect(url, **kw):
        seen["url"] = url
        return FakeEleven()

    monkeypatch.setattr(voice_router.websockets, "connect", connect)
    monkeypatch.setattr(voice_router.voice, "enabled", lambda: True)
    monkeypatch.setattr(voice_router.voice, "api_key", lambda: "test-key")
    monkeypatch.setattr(voice_router.engine, "prewarm_recall",
                        lambda *a, **kw: None)
    app.state.allowed_hosts = {"testserver", "127.0.0.1", "localhost", "::1"}
    monkeypatch.setattr(auth, "GATE_LOOPBACK_HOSTS",
                        auth.GATE_LOOPBACK_HOSTS | {"testserver"})
    with TestClient(app, base_url="http://127.0.0.1") as c:
        chat = c.post("/api/chats", json={}).json()
        with c.websocket_connect("/api/voice/stt-stream") as ws:
            ws.send_json({"chat_id": chat["id"]})
            assert ws.receive_json()["session"]
            ws.send_json(_frame())
            assert ws.receive_json() == {"partial": "hello"}
            ws.send_json({"done": True})
    q = _query(seen["url"])
    assert q["model_id"] == ["scribe_v2_realtime"]
    assert q["language_code"] == ["en"]
    assert "filter_background_audio" not in q
    assert q["keyterms"] == ["User"]
