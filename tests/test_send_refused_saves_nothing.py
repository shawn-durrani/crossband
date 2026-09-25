"""A send refused because a round is running leaves nothing behind (#434).

The client holds a send that gets a 409 and retries it with the same body
when the round ends. The route used to save the message first and refuse
it after, so the retry saved it a second time: two copies in the chat, the
first one no round ever answered, both read by the seats and ingested by
membro. These tests pin the order (refuse, then save), the retry landing
exactly one row, the slash-command side channel staying open mid-round, a
refused voice turn keeping its parked label for the retry, and the claim
that stops another round starting while a send is still saving.
"""

import asyncio
import json
import threading

import pytest

from backend import db, diarize, engine, rounds
from backend.app import create_app
from backend.config import Settings
from roomkit import sse_events


@pytest.fixture
def app(tmp_path):
    settings = Settings(data_dir=str(tmp_path / "data"),
                        memory_url="http://127.0.0.1:1", user_name="Alex")
    return create_app(settings)


def slow_stream(text_chunks, delay=0.05):
    async def stream_reply(participant, roster, transcript, names, cfg, project,
                           chat_summary, voice_mode, tools=None, memory=None):
        for ch in text_chunks:
            await asyncio.sleep(delay)
            yield ("text", ch)
    return stream_reply


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


async def _messages(c, chat_id):
    return (await c.get(f"/api/chats/{chat_id}")).json()["messages"]


def _user_rows(msgs, text):
    return [m for m in msgs if m["speaker"] == "user" and m["content"] == text]


async def _start_round(c, chat_id):
    """Start a real round on a slow fake stream and wait until it's live."""
    task = asyncio.create_task(
        c.post(f"/api/chats/{chat_id}/send", json={"text": "hi @claude"}))
    await _until(lambda: rounds.active(chat_id) is not None)
    return task


def test_send_into_a_running_round_is_refused_and_saves_nothing(app, monkeypatch):
    monkeypatch.setattr(engine.providers, "stream_reply",
                        slow_stream(["thinking..."], delay=0.5))

    async def go():
        async with _async_client(app) as c:
            chat_id = (await c.post("/api/chats", json={})).json()["id"]
            first = await _start_round(c, chat_id)
            before = await _messages(c, chat_id)

            refused = await c.post(f"/api/chats/{chat_id}/send",
                                   json={"text": "Sam is here now"})
            assert refused.status_code == 409

            after = await _messages(c, chat_id)
            assert [m["id"] for m in after] == [m["id"] for m in before]
            assert _user_rows(after, "Sam is here now") == []
            await c.post(f"/api/chats/{chat_id}/round/abort")
            await first

    asyncio.run(go())


def test_the_retry_after_the_round_ends_saves_exactly_one_copy(app, monkeypatch):
    """The whole client loop: refused mid-round, held, sent again with the
    same body once the round is over. One row, and a round answers it."""
    monkeypatch.setattr(engine.providers, "stream_reply",
                        slow_stream(["a reply"], delay=0.2))

    async def go():
        async with _async_client(app) as c:
            chat_id = (await c.post("/api/chats", json={})).json()["id"]
            first = await _start_round(c, chat_id)
            body = {"text": "what about Dave?"}
            assert (await c.post(f"/api/chats/{chat_id}/send",
                                 json=body)).status_code == 409
            await first  # the round ends naturally
            assert rounds.active(chat_id) is None

            retry = await c.post(f"/api/chats/{chat_id}/send", json=body)
            assert retry.status_code == 200
            types = [e["type"] for e in sse_events(retry.text)]
            assert "user_saved" in types and "done" in types

            msgs = await _messages(c, chat_id)
            copies = _user_rows(msgs, "what about Dave?")
            assert len(copies) == 1
            # and the round that ran was for it: a reply follows the one copy
            assert msgs[-1]["speaker"] != "user"
            assert msgs[-1]["id"] > copies[0]["id"]

    asyncio.run(go())


def test_a_slash_command_during_a_round_still_saves_and_streams(app, monkeypatch):
    """The side channel stays open mid-round: persisted, user_saved then
    done, no 409 and no second round."""
    monkeypatch.setattr(engine.providers, "stream_reply",
                        slow_stream(["thinking..."], delay=0.5))

    async def go():
        async with _async_client(app) as c:
            chat_id = (await c.post("/api/chats", json={})).json()["id"]
            first = await _start_round(c, chat_id)
            running = rounds.active(chat_id)

            r = await c.post(f"/api/chats/{chat_id}/send",
                             json={"text": "/deploy crossband"})
            assert r.status_code == 200
            events = sse_events(r.text)
            assert [e["type"] for e in events] == ["user_saved", "done"]
            assert events[0]["message"]["content"] == "/deploy crossband"
            assert rounds.active(chat_id) is running  # untouched

            msgs = await _messages(c, chat_id)
            assert len(_user_rows(msgs, "/deploy crossband")) == 1
            await c.post(f"/api/chats/{chat_id}/round/abort")
            await first

    asyncio.run(go())


def test_a_send_with_no_round_running_is_unchanged(app, monkeypatch):
    monkeypatch.setattr(engine.providers, "stream_reply",
                        slow_stream(["hello Mateo"], delay=0.0))

    async def go():
        async with _async_client(app) as c:
            chat_id = (await c.post("/api/chats", json={})).json()["id"]
            r = await c.post(f"/api/chats/{chat_id}/send",
                             json={"text": "hi @claude"})
            assert r.status_code == 200
            types = [e["type"] for e in sse_events(r.text)]
            assert types[0] == "round_start"
            assert types[1] == "user_saved"
            assert types[-1] == "done"
            msgs = await _messages(c, chat_id)
            assert len(_user_rows(msgs, "hi @claude")) == 1
            assert msgs[-1]["content"] == "hello Mateo"
            assert not rounds.busy(chat_id)

    asyncio.run(go())


def test_a_refused_voice_turn_keeps_its_label_for_the_retry(app, monkeypatch):
    """The identity check parks a finished label for the turn, and the save
    claims it (single use). A refused send never reaches the save, so the
    label is still parked, and the retry's one row carries it."""
    monkeypatch.setattr(engine.providers, "stream_reply",
                        slow_stream(["a reply"], delay=0.2))
    label = {"labels": ["Sam"], "uncertain": []}

    async def go():
        async with _async_client(app) as c:
            chat_id = (await c.post("/api/chats", json={})).json()["id"]
            first = await _start_round(c, chat_id)
            diarize.park_label("turn-sam-1", label)
            body = {"text": "it's Sam", "turn_id": "turn-sam-1"}

            assert (await c.post(f"/api/chats/{chat_id}/send",
                                 json=body)).status_code == 409
            assert "turn-sam-1" in diarize._PENDING_LABELS
            await first

            assert (await c.post(f"/api/chats/{chat_id}/send",
                                 json=body)).status_code == 200
            copies = _user_rows(await _messages(c, chat_id), "it's Sam")
            assert len(copies) == 1
            assert copies[0]["voice_turn_id"] == "turn-sam-1"
            assert json.loads(copies[0]["voice_labels"]) == label
            assert "turn-sam-1" not in diarize._PENDING_LABELS

    asyncio.run(go())


def test_nothing_else_starts_a_round_while_a_send_is_saving(app, monkeypatch):
    """The save runs on a worker thread. While it's in flight the send holds
    the chat's claim, so a second send is refused before it saves, a
    continue is refused, and a guest hand-back stands down. Without the
    claim the second send would save too, and one of the two would then be
    refused with its row already written."""
    monkeypatch.setattr(engine.providers, "stream_reply",
                        slow_stream(["a reply"], delay=0.0))
    entered, gate = threading.Event(), threading.Event()
    real_insert = db.insert_message

    def slow_insert(con, chat_id, speaker, content, *a, **kw):
        if speaker == "user" and content == "first from Alex":
            entered.set()
            assert gate.wait(5.0), "test never opened the gate"
        return real_insert(con, chat_id, speaker, content, *a, **kw)

    monkeypatch.setattr(db, "insert_message", slow_insert)

    async def go():
        async with _async_client(app) as c:
            chat_id = (await c.post("/api/chats", json={})).json()["id"]
            first = asyncio.create_task(
                c.post(f"/api/chats/{chat_id}/send",
                       json={"text": "first from Alex"}))
            try:
                await _until(entered.is_set)  # first send is mid-save
                assert rounds.active(chat_id) is None

                second = await c.post(f"/api/chats/{chat_id}/send",
                                      json={"text": "second from Alex"})
                assert second.status_code == 409
                cont = await c.post(f"/api/chats/{chat_id}/continue", json={})
                assert cont.status_code == 409
                s = app.state
                await engine.make_handback(s.settings, s.memory, s.mcp)(
                    chat_id, "blocker")
                assert rounds.active(chat_id) is None
            finally:
                gate.set()

            r = await first
            assert r.status_code == 200
            assert not rounds.busy(chat_id)
            msgs = await _messages(c, chat_id)
            assert len(_user_rows(msgs, "first from Alex")) == 1
            assert _user_rows(msgs, "second from Alex") == []

    asyncio.run(go())


def test_a_failed_send_releases_its_claim(app):
    """A 404 or 400 from inside the save must not leave the chat claimed,
    or every later send to it would be refused until a restart."""

    async def go():
        async with _async_client(app) as c:
            missing = await c.post("/api/chats/999999/send", json={"text": "hi"})
            assert missing.status_code == 404
            assert not rounds.busy(999999)

            chat_id = (await c.post("/api/chats", json={})).json()["id"]
            empty = await c.post(f"/api/chats/{chat_id}/send", json={"text": "  "})
            assert empty.status_code == 400
            assert not rounds.busy(chat_id)

    asyncio.run(go())
