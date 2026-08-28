"""A wedged seat can no longer hold a round hostage (#168). The engine
bounds PROGRESS-idleness per stream event: a seat silent past the bound
is errored and the round moves on, with any partial reply kept. Every
yield resets the bound, so a slow healthy reply is never cut - these
tests pin both directions. No SDK, no network: providers.stream_reply is
mocked, same as test_delegation.py."""

import asyncio
import json

import pytest
from fastapi.testclient import TestClient

from backend import engine
from backend.app import create_app
from backend.config import Settings
from roomkit import sse_events


@pytest.fixture
def app(tmp_path):
    settings = Settings(data_dir=str(tmp_path / "data"),
                        memory_url="http://127.0.0.1:1")
    return create_app(settings)


def _round(client, cid, text="what do you both make of this?"):
    with client.stream("POST", f"/api/chats/{cid}/send", json={"text": text}) as r:
        return sse_events("".join(r.iter_text()))


def test_a_silent_seat_is_errored_and_the_round_moves_on(app, monkeypatch):
    monkeypatch.setattr(engine, "SEAT_STALL_TIMEOUT_S", 0.05)

    async def stream_reply(participant, roster, transcript, names, cfg, project,
                           chat_summary, voice_mode, tools=None, memory=None):
        if participant["slug"] == "claude":
            await asyncio.sleep(3600)  # wedged before any output
        yield ("text", "ok from " + participant["slug"])

    monkeypatch.setattr(engine.providers, "stream_reply", stream_reply)
    with TestClient(app, base_url="http://127.0.0.1") as c:
        chat = c.post("/api/chats", json={}).json()
        events = _round(c, chat["id"])
    errors = [e for e in events if e["type"] == "error"]
    assert errors and "stalled" in errors[0]["message"]
    # The round survived the wedge: another seat still answered, and the
    # round reached its normal end.
    assert any(e["type"] == "delta" and "ok from" in e["text"] for e in events)
    assert events[-1]["type"] == "done"


def test_partial_output_survives_a_mid_reply_stall(app, monkeypatch):
    monkeypatch.setattr(engine, "SEAT_STALL_TIMEOUT_S", 0.05)

    async def stream_reply(participant, roster, transcript, names, cfg, project,
                           chat_summary, voice_mode, tools=None, memory=None):
        yield ("text", "half an answer from " + participant["slug"])
        if participant["slug"] == "claude":
            await asyncio.sleep(3600)  # wedged mid-reply

    monkeypatch.setattr(engine.providers, "stream_reply", stream_reply)
    with TestClient(app, base_url="http://127.0.0.1") as c:
        chat = c.post("/api/chats", json={}).json()
        events = _round(c, chat["id"])
        msgs = c.get(f"/api/chats/{chat['id']}").json()["messages"]
    assert any(e["type"] == "error" and "stalled" in e["message"]
               for e in events)
    # The words already streamed persist - same philosophy as the abort
    # path's "[cut off]" marker: the transcript never loses real output.
    assert any("half an answer from claude" in (m["content"] or "")
               for m in msgs)


def test_steady_events_never_trip_the_bound(app, monkeypatch):
    monkeypatch.setattr(engine, "SEAT_STALL_TIMEOUT_S", 0.2)

    async def stream_reply(participant, roster, transcript, names, cfg, project,
                           chat_summary, voice_mode, tools=None, memory=None):
        # Each gap sits under the bound; the total run sits well over it.
        for i in range(4):
            await asyncio.sleep(0.1)
            yield ("text", f"chunk{i} ")

    monkeypatch.setattr(engine.providers, "stream_reply", stream_reply)
    with TestClient(app, base_url="http://127.0.0.1") as c:
        chat = c.post("/api/chats", json={}).json()
        events = _round(c, chat["id"])
    assert not [e for e in events if e["type"] == "error"]
    assert events[-1]["type"] == "done"
