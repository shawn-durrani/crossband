"""Voice playback regression guards for the change that decoupled Claude Code
guest execution from the turn lifecycle.

Two backend halves of the fix:

  1. `GET /api/chats/{id}/active_round` — a voice-active client that DIDN'T start
     a round (a Claude Code hand-back) polls this to learn the round id so it can
     attach and actually SPEAK the narration. Before rounds were detached, that
     round reached the client only as persisted text; audio never played.

  2. A hand-back (`engine.make_handback`) must spin up a DISCOVERABLE active round
     that streams the same speaker_start/delta/speaker_end vocabulary the voice
     pipeline consumes — otherwise the narration is silent by construction.

Providers are mocked; no network.
"""

import asyncio
import json

import pytest
from fastapi.testclient import TestClient

from backend import db, engine, rounds
from backend.app import create_app
from backend.config import Settings
from backend.routers import chats as chats_router


@pytest.fixture
def app(tmp_path):
    settings = Settings(data_dir=str(tmp_path / "data"),
                        memory_url="http://127.0.0.1:1")
    yield create_app(settings)
    rounds._rounds.clear()  # module global — don't leak a round into the next test


def fake_stream(text_chunks):
    async def stream_reply(participant, roster, transcript, names, cfg, project,
                           chat_summary, voice_mode, tools=None, memory=None):
        for ch in text_chunks:
            yield ("text", ch)
        yield ("usage", {"input": 1, "cache_read": 0, "cache_creation": 0, "output": 1})
    return stream_reply


def _event_types(round_):
    out = []
    for e in round_.events:
        for line in e.splitlines():
            if line.startswith("data: "):
                out.append(json.loads(line[6:])["type"])
    return out


def test_active_round_endpoint_reports_the_running_round_then_clears():
    async def go():
        # idle chat → nothing to attach to
        assert (await chats_router.active_round(1))["round_id"] is None

        gate = asyncio.Event()

        async def gen():
            yield engine.sse({"type": "speaker_start", "speaker": "claude"})
            await gate.wait()
            yield engine.sse({"type": "speaker_end", "speaker": "claude"})

        r = rounds.start(1, gen())
        await asyncio.sleep(0.01)  # let the task buffer its first event
        res = await chats_router.active_round(1)
        assert res["round_id"] == r.round_id
        assert res["count"] >= 1   # a voice client can attach and tail from here

        gate.set()
        await r.task
        # round done → the endpoint reports idle again, so no stale re-attach
        assert (await chats_router.active_round(1))["round_id"] is None

    asyncio.run(go())
    rounds._rounds.clear()


def test_handback_round_is_discoverable_and_streams_voiceable_events(app, monkeypatch):
    monkeypatch.setattr(engine.providers, "stream_reply",
                        fake_stream(["Claude Code ", "finished the task."]))
    with TestClient(app, base_url="http://127.0.0.1") as c:
        cid = c.post("/api/chats", json={}).json()["id"]

    handback = engine.make_handback(app.state.settings, app.state.memory, None)

    async def go():
        assert rounds.active(cid) is None
        await handback(cid, "result")     # a completed guest hands the chat back
        r = rounds.active(cid)
        assert r is not None              # ...as a DISCOVERABLE detached round
        await r.task                      # drain it
        return r

    r = asyncio.run(go())
    types = _event_types(r)
    # the narrator's turn streams the exact vocabulary voice.js taps — so a
    # voice client attaching via /round/stream will actually speak it
    assert "speaker_start" in types
    assert "delta" in types
    assert "speaker_end" in types

    # and the endpoint the voice client polls would have surfaced this round id
    async def endpoint():
        return await chats_router.active_round(cid)
    # (round already drained above → now idle, proving it clears cleanly)
    assert asyncio.run(endpoint())["round_id"] is None
