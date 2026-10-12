"""The AIs' and apps' names as the transcriber's spelling hints (#652).

Pins: the apps are crossband, its sibling apps and the configured MCP
apps, spoken as words and capitalised when the key is all lower case; the
hints run in priority order (the owner, this chat's seats, the people in
the room, the apps, everyone remembered), so the 50-term cap drops
remembered people before a seat; only this chat's own seats are hinted;
and the backup transcriber gets the same list as the live one.
"""

from urllib.parse import parse_qs, urlparse

import pytest
from fastapi.testclient import TestClient

from backend import anchors, db, voice
from backend.app import create_app
from backend.config import Settings
from backend.routers import voice as voice_router
from tests.test_stt_relay import _capturing_relay, _frame


def test_apps_are_spoken_as_words_and_named_properly():
    cfg = {"sibling_apps": {"membro": "x", "spendglass": "y"},
           "mcp_servers": {"build-watcher": {}, "woodchuck": {}},
           "code_mcp": {"GitHubTools": {}}}
    assert voice.app_terms(cfg) == ["Crossband", "Membro", "Spendglass",
                                    "Build Watcher", "Woodchuck", "GitHubTools"]
    assert voice.app_terms({"sibling_apps": {}}) == ["Crossband"]
    assert voice.app_terms({}) == ["Crossband"]


@pytest.fixture
def app(tmp_path):
    return create_app(Settings(data_dir=str(tmp_path / "data"),
                               memory_url="http://127.0.0.1:1",
                               sibling_apps={"membro": "http://127.0.0.1:1"},
                               mcp_servers={"woodchuck": {"command": "true"}}))


def _relay_terms(c, chat_id):
    with c.websocket_connect("/api/voice/stt-stream") as ws:
        ws.send_json({"chat_id": chat_id})
        assert ws.receive_json()["session"]
        ws.send_json(_frame())
        assert ws.receive_json() == {"partial": "hello"}
        ws.send_json({"done": True})


def test_seats_come_before_the_room_and_survive_the_cap(app, monkeypatch):
    seen = _capturing_relay(app, monkeypatch)
    store = anchors.store()
    for i in range(30):
        pid = store.ensure_person(f"Person{i}")
        store.set_preferred_name(pid, f"Pal{i}")
    with TestClient(app, base_url="http://127.0.0.1") as c:
        chat = c.post("/api/chats", json={}).json()
        con = db.connect()
        db.add_room_person(con, chat["id"], "Sam")
        con.close()
        _relay_terms(c, chat["id"])
    terms = parse_qs(urlparse(seen["url"]).query)["keyterms"]
    assert terms[:6] == ["User", "Claude", "GPT", "Sam", "Crossband", "Membro"]
    assert terms[6] == "Woodchuck"
    assert len(terms) == voice.KEYTERMS_MAX
    # the cap dropped remembered people from the end, never a seat or an app
    assert "Pal0" in terms and "Person29" not in terms


def test_only_this_chats_seats_are_hinted(app, monkeypatch):
    seen = _capturing_relay(app, monkeypatch)
    with TestClient(app, base_url="http://127.0.0.1") as c:
        con = db.connect()
        seats = {p["name"]: p["id"] for p in db.get_participants(con, enabled_only=True)}
        con.close()
        chat = c.post("/api/chats", json={"participant_ids": [seats["GPT"]]}).json()
        _relay_terms(c, chat["id"])
    terms = parse_qs(urlparse(seen["url"]).query)["keyterms"]
    assert terms == ["User", "GPT", "Crossband", "Membro", "Woodchuck"]


def test_the_backup_transcriber_gets_the_same_hints(app, monkeypatch):
    calls = []

    def fake(data, mime, cfg, with_words=False, **kw):
        calls.append(kw["keyterms"])
        return "hello", "scribe_v2"

    monkeypatch.setattr(voice_router.voice, "provider_for",
                        lambda cfg: voice_router.voice.PROVIDER_ELEVENLABS)
    monkeypatch.setattr(voice_router.voice, "transcribe", fake)
    with TestClient(app, base_url="http://127.0.0.1") as c:
        chat = c.post("/api/chats", json={}).json()
        r = c.post(f"/api/chats/{chat['id']}/stt",
                   files={"file": ("utterance.webm", b"\x1a\x45" * 64, "audio/webm")},
                   data={"duration_ms": "900", "why": "late"})
    assert r.status_code == 200
    assert calls == [["User", "Claude", "GPT", "Crossband", "Membro", "Woodchuck"]]
