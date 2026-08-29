"""POST /api/chats/{id}/continue - the untested second copy of the round
choreography (#242).

/continue starts a detached model-to-model round with no user turn: the
full non-trial roster replies, multi-round asks emit progress markers, the
rounds knob clamps instead of rejecting, and the one-active-round lock is
shared with /send. The fakes and idiom are test_rounds.py's.
"""

import asyncio

import pytest
from fastapi.testclient import TestClient

from backend import db, engine, rounds
from backend.app import create_app
from backend.config import Settings
from roomkit import sse_events


@pytest.fixture
def app(tmp_path):
    settings = Settings(data_dir=str(tmp_path / "data"),
                        memory_url="http://127.0.0.1:1")
    return create_app(settings)


def slow_stream(text_chunks, delay=0.0):
    async def stream_reply(participant, roster, transcript, names, cfg,
                           project, chat_summary, voice_mode, tools=None,
                           memory=None):
        for ch in text_chunks:
            await asyncio.sleep(delay)
            yield ("text", ch)
    return stream_reply


def _seeded_chat(c, chat_id, text="hello both"):
    con = db.connect()
    try:
        db.insert_message(con, chat_id, "user", text)
    finally:
        con.close()


def test_continue_runs_a_real_round_without_a_user_turn(app, monkeypatch):
    """A continue starts a detached round: no user message is persisted,
    the full seeded roster replies, and the stream carries round_start then
    the round's events with one done."""
    monkeypatch.setattr(engine.providers, "stream_reply",
                        slow_stream(["more thoughts"]))
    with TestClient(app, base_url="http://127.0.0.1") as c:
        chat_id = c.post("/api/chats", json={}).json()["id"]
        _seeded_chat(c, chat_id)
        r = c.post(f"/api/chats/{chat_id}/continue", json={})
        events = sse_events(r.text)
        assert events[0]["type"] == "round_start"
        kinds = [e["type"] for e in events]
        assert "user_saved" not in kinds
        assert "round" not in kinds          # rounds=1 emits no marker
        assert kinds.count("done") == 1
        msgs = c.get(f"/api/chats/{chat_id}").json()["messages"]
        assert [m["speaker"] for m in msgs[:1]] == ["user"]
        assert {m["speaker"] for m in msgs[1:]} == {"claude", "gpt"}
        assert all(m["content"] == "more thoughts" for m in msgs[1:])
    assert rounds.active(chat_id) is None


def test_multi_round_continue_emits_progress_markers(app, monkeypatch):
    """rounds=2 runs two full rounds in one stream: a marker per round, one
    done per round (the shipped contract the frontend reads), and both
    rounds' replies persist."""
    monkeypatch.setattr(engine.providers, "stream_reply",
                        slow_stream(["again"]))
    with TestClient(app, base_url="http://127.0.0.1") as c:
        chat_id = c.post("/api/chats", json={}).json()["id"]
        _seeded_chat(c, chat_id)
        r = c.post(f"/api/chats/{chat_id}/continue", json={"rounds": 2})
        events = sse_events(r.text)
        markers = [e for e in events if e["type"] == "round"]
        assert markers == [{"type": "round", "n": 1, "total": 2},
                           {"type": "round", "n": 2, "total": 2}]
        assert [e["type"] for e in events].count("done") == 2
        msgs = c.get(f"/api/chats/{chat_id}").json()["messages"]
        assert len(msgs) == 5                # seed + two seats x two rounds


def test_rounds_param_is_clamped_not_rejected(app, monkeypatch):
    """The rounds knob clamps to at least one, silently: an out-of-range
    ask still runs a single round rather than answering 422."""
    monkeypatch.setattr(engine.providers, "stream_reply",
                        slow_stream(["once"]))
    with TestClient(app, base_url="http://127.0.0.1") as c:
        chat_id = c.post("/api/chats", json={}).json()["id"]
        _seeded_chat(c, chat_id)
        r = c.post(f"/api/chats/{chat_id}/continue", json={"rounds": 0})
        assert r.status_code == 200
        assert [e["type"] for e in sse_events(r.text)].count("done") == 1
        assert len(c.get(f"/api/chats/{chat_id}").json()["messages"]) == 3


def test_continue_missing_chat_404(app):
    """Continuing a chat that does not exist is a 404, before any round
    machinery registers anything."""
    with TestClient(app, base_url="http://127.0.0.1") as c:
        assert c.post("/api/chats/999999/continue", json={}).status_code == 404
    assert rounds.active(999999) is None
    assert rounds.latest(999999) is None


def _async_client(app):
    import httpx
    return httpx.AsyncClient(transport=httpx.ASGITransport(app=app),
                             base_url="http://127.0.0.1")


async def _until(cond, timeout=5.0):
    for _ in range(int(timeout / 0.02)):
        if cond():
            return
        await asyncio.sleep(0.02)
    raise AssertionError("condition not reached in time")


def test_second_continue_refused_while_round_active(app, monkeypatch):
    """One active round per chat: a continue during a live round is a 409,
    and the lock is the same one /send checks - the two entrances share
    it."""
    monkeypatch.setattr(engine.providers, "stream_reply",
                        slow_stream(["thinking..."], delay=0.5))

    async def go():
        async with _async_client(app) as c:
            chat_id = (await c.post("/api/chats", json={})).json()["id"]
            first = asyncio.create_task(
                c.post(f"/api/chats/{chat_id}/continue", json={}))
            await _until(lambda: rounds.active(chat_id) is not None)
            second = await c.post(f"/api/chats/{chat_id}/continue", json={})
            assert second.status_code == 409
            crossed = await c.post(f"/api/chats/{chat_id}/send",
                                   json={"text": "hi"})
            assert crossed.status_code == 409
            await c.post(f"/api/chats/{chat_id}/round/abort")
            await first

    asyncio.run(go())
