"""Slash commands: a user message starting with "/" is a note
to tooling - persisted like any message, but no round runs, no model replies,
and it never enters the transcript the models read."""

import json

import pytest
from fastapi.testclient import TestClient

from backend import engine
from backend.app import create_app
from backend.config import Settings


@pytest.fixture
def app(tmp_path):
    settings = Settings(data_dir=str(tmp_path / "data"),
                        memory_url="http://127.0.0.1:1")
    return create_app(settings)


def sse_events(body):
    return [json.loads(line[6:]) for line in body.splitlines()
            if line.startswith("data: ")]


def fake_stream(text, seen_transcripts):
    async def stream_reply(participant, roster, transcript, names, cfg, project,
                           chat_summary, voice_mode, tools=None, memory=None):
        seen_transcripts.append([m["content"] for m in transcript])
        yield ("text", text)
    return stream_reply


def test_slash_message_persists_but_runs_no_round(app):
    with TestClient(app, base_url="http://127.0.0.1") as c:
        chat = c.post("/api/chats", json={}).json()
        with c.stream("POST", f"/api/chats/{chat['id']}/send",
                      json={"text": "/deploy sideband #37"}) as r:
            assert r.status_code == 200
            events = sse_events("".join(r.iter_text()))
        assert [e["type"] for e in events] == ["user_saved", "done"]  # no speakers
        got = c.get(f"/api/chats/{chat['id']}").json()
        assert [m["speaker"] for m in got["messages"]] == ["user"]
        assert got["messages"][0]["content"] == "/deploy sideband #37"


def test_slash_messages_hidden_from_model_transcript(app, monkeypatch):
    seen = []
    monkeypatch.setattr(engine.providers, "stream_reply", fake_stream("ok", seen))
    with TestClient(app, base_url="http://127.0.0.1") as c:
        chat = c.post("/api/chats", json={}).json()
        with c.stream("POST", f"/api/chats/{chat['id']}/send",
                      json={"text": "/ship it #34"}) as r:
            "".join(r.iter_text())
        with c.stream("POST", f"/api/chats/{chat['id']}/send",
                      json={"text": "hello models"}) as r:
            "".join(r.iter_text())
    assert seen, "round should have run for the normal message"
    for transcript in seen:
        assert "hello models" in transcript
        assert all("/ship" not in t for t in transcript)


def test_leading_whitespace_still_counts_as_slash(app):
    with TestClient(app, base_url="http://127.0.0.1") as c:
        chat = c.post("/api/chats", json={}).json()
        with c.stream("POST", f"/api/chats/{chat['id']}/send",
                      json={"text": "  /deploy #9"}) as r:
            events = sse_events("".join(r.iter_text()))
        assert [e["type"] for e in events] == ["user_saved", "done"]


def test_slash_suggestions_are_config_driven(tmp_path):
    """The composer's predictive chips come from config; default is none -
    core assigns no meaning to any command."""
    settings = Settings(data_dir=str(tmp_path / "data2"),
                        memory_url="http://127.0.0.1:1",
                        slash_commands=[{"insert": "/deploy sideband #",
                                         "label": "/deploy sideband"}])
    with TestClient(create_app(settings), base_url="http://127.0.0.1") as c:
        cfg = c.get("/api/state").json()["config"]
        assert cfg["slash_commands"][0]["insert"] == "/deploy sideband #"
    settings = Settings(data_dir=str(tmp_path / "data3"),
                        memory_url="http://127.0.0.1:1")
    with TestClient(create_app(settings), base_url="http://127.0.0.1") as c:
        assert c.get("/api/state").json()["config"]["slash_commands"] == []


def test_tooling_notice_persists_without_round(app):
    """POST /notice: machine-side tooling reports status into a chat as a
    'system' message - no round, models see it next time, UI renders muted."""
    with TestClient(app, base_url="http://127.0.0.1") as c:
        chat = c.post("/api/chats", json={}).json()
        msg = c.post(f"/api/chats/{chat['id']}/notice",
                     json={"text": "🚀 deployed sideband #61 (80fa874)"}).json()
        assert msg["speaker"] == "system"
        got = c.get(f"/api/chats/{chat['id']}").json()
        assert [m["speaker"] for m in got["messages"]] == ["system"]
        assert c.post(f"/api/chats/{chat['id']}/notice",
                      json={"text": "  "}).status_code == 400
        assert c.post("/api/chats/999/notice",
                      json={"text": "hello"}).status_code == 404
