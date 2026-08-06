"""External event ingestion: one generic inbound endpoint —
namespaced speakers (no participant impersonation), idempotent by
(source, dedupe_key), optional bearer token, payload-agnostic."""

import json

import pytest
from fastapi.testclient import TestClient

from backend import engine
from backend.app import create_app
from backend.config import Settings


def make_app(tmp_path, **kw):
    return create_app(Settings(data_dir=str(tmp_path / "data"),
                               memory_url="http://127.0.0.1:1", **kw))


EVENT = {"source": "build-watcher", "target_chat": 1, "dedupe_key": "acme:123",
         "priority": "high",
         "payload": {"title": "Nightly build failed", "body": "New failure",
                     "url": "https://example.com/b/123"}}


def test_ingest_lands_namespaced_and_visible_to_models(tmp_path, monkeypatch):
    app = make_app(tmp_path)
    seen = []

    async def stream_reply(participant, roster, transcript, names, cfg, project,
                           chat_summary, voice_mode, tools=None, memory=None):
        seen.append([(m["speaker"], names.get(m["speaker"])) for m in transcript])
        yield ("text", "ok")
    monkeypatch.setattr(engine.providers, "stream_reply", stream_reply)

    with TestClient(app, base_url="http://127.0.0.1") as c:
        chat = c.post("/api/chats", json={}).json()
        ev = dict(EVENT, target_chat=chat["id"])
        r = c.post("/api/ingest", json=ev).json()
        assert r["deduped"] is False and r["message_id"]
        got = c.get(f"/api/chats/{chat['id']}").json()
        msg = got["messages"][0]
        # namespaced: a producer can never impersonate a participant
        assert msg["speaker"] == "ext:build-watcher"
        assert "❗" in msg["content"] and "Nightly build failed" in msg["content"]
        # models see it, with a readable label
        with c.stream("POST", f"/api/chats/{chat['id']}/send", json={"text": "hi"}) as s:
            "".join(s.iter_text())
        assert ("ext:build-watcher", "build-watcher (external feed)") in seen[0]


def test_ingest_is_idempotent(tmp_path):
    app = make_app(tmp_path)
    with TestClient(app, base_url="http://127.0.0.1") as c:
        chat = c.post("/api/chats", json={}).json()
        ev = dict(EVENT, target_chat=chat["id"])
        first = c.post("/api/ingest", json=ev).json()
        again = c.post("/api/ingest", json=ev).json()
        assert again == {"deduped": True, "message_id": first["message_id"]}
        msgs = c.get(f"/api/chats/{chat['id']}").json()["messages"]
        assert len(msgs) == 1  # retried delivery never duplicates
        # same key from a DIFFERENT source is a different event
        other = c.post("/api/ingest", json=dict(ev, source="calendar")).json()
        assert other["deduped"] is False


def test_ingest_token_gate(tmp_path):
    app = make_app(tmp_path, ingest_token="s3cret")
    with TestClient(app, base_url="http://127.0.0.1") as c:
        chat = c.post("/api/chats", json={}).json()
        ev = dict(EVENT, target_chat=chat["id"])
        assert c.post("/api/ingest", json=ev).status_code == 401
        assert c.post("/api/ingest", json=ev,
                      headers={"Authorization": "Bearer wrong"}).status_code == 401
        assert c.post("/api/ingest", json=ev,
                      headers={"Authorization": "Bearer s3cret"}).status_code == 200


def test_ingest_validation(tmp_path):
    app = make_app(tmp_path)
    with TestClient(app, base_url="http://127.0.0.1") as c:
        chat = c.post("/api/chats", json={}).json()
        ev = dict(EVENT, target_chat=chat["id"])
        assert c.post("/api/ingest", json=dict(ev, source="Claude Code!")).status_code == 400
        assert c.post("/api/ingest", json=dict(ev, priority="urgent")).status_code == 400
        assert c.post("/api/ingest", json=dict(ev, target_chat=999)).status_code == 404
        bad = dict(ev); bad["payload"] = {"title": ""}
        assert c.post("/api/ingest", json=bad).status_code == 422
