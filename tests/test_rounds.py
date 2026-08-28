"""Detached rounds: generation survives the connection.

A dropped client must not kill a reply (the dead-zone-on-the-highway case);
stopping the models is an explicit abort; a client can re-attach and catch up
from where it left off.
"""

import asyncio
import json
import time

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


def slow_stream(text_chunks, delay=0.05):
    async def stream_reply(participant, roster, transcript, names, cfg, project,
                           chat_summary, voice_mode, tools=None, memory=None):
        for ch in text_chunks:
            await asyncio.sleep(delay)
            yield ("text", ch)
    return stream_reply


def _wait_round_done(c, chat_id, want, timeout=5.0):
    """Poll the persisted chat until `want` messages exist (round finishing
    in the background) - the whole point under test."""
    deadline = time.time() + timeout
    while time.time() < deadline:
        msgs = c.get(f"/api/chats/{chat_id}").json()["messages"]
        if len(msgs) >= want and not rounds.active(chat_id):
            return msgs
        time.sleep(0.05)
    raise AssertionError("round did not finish in the background")


def test_disconnect_no_longer_kills_the_round(app, monkeypatch):
    """Close the stream mid-reply (network drop): the round finishes anyway,
    the full reply persists, and nothing is marked cut off."""
    monkeypatch.setattr(engine.providers, "stream_reply",
                        slow_stream(["part one, ", "part two, ", "part three"]))
    with TestClient(app, base_url="http://127.0.0.1") as c:
        chat_id = c.post("/api/chats", json={}).json()["id"]
        with c.stream("POST", f"/api/chats/{chat_id}/send",
                      json={"text": "hi @claude"}) as r:
            for line in r.iter_lines():
                if '"delta"' in line:
                    break  # tunnel dies mid-first-delta

        msgs = _wait_round_done(c, chat_id, want=2)
        reply = msgs[-1]
        assert reply["content"] == "part one, part two, part three"
        assert "[cut off" not in reply["content"]


def _async_client(app):
    import httpx
    return httpx.AsyncClient(transport=httpx.ASGITransport(app=app),
                             base_url="http://127.0.0.1")


def test_abort_is_the_deliberate_stop(app, monkeypatch):
    """POST /round/abort mid-reply: generation stops NOW and the partial
    persists with the cut-off marker - barge-in semantics, made explicit.
    (Async client: abort must share the event loop the round task lives on.)"""
    monkeypatch.setattr(engine.providers, "stream_reply",
                        slow_stream(["partial answer", " never seen"], delay=0.4))

    async def go():
        async with _async_client(app) as c:
            chat_id = (await c.post("/api/chats", json={})).json()["id"]
            # httpx's ASGITransport buffers whole responses, so the send runs
            # as a task while we watch the round's buffer directly.
            send = asyncio.create_task(
                c.post(f"/api/chats/{chat_id}/send", json={"text": "hi @claude"}))
            await _until(lambda: rounds.active(chat_id) and any(
                '"delta"' in e for e in rounds.active(chat_id).events))
            got = (await c.post(f"/api/chats/{chat_id}/round/abort")).json()
            assert got["aborted"] is True
            await send
            # aborting again: nothing active
            r2 = (await c.post(f"/api/chats/{chat_id}/round/abort")).json()
            assert r2["aborted"] is False
            msgs = (await c.get(f"/api/chats/{chat_id}")).json()["messages"]
            assert msgs[-1]["content"] == "partial answer\n\n[cut off by User]"

    asyncio.run(go())


async def _until(cond, timeout=5.0):
    for _ in range(int(timeout / 0.02)):
        if cond():
            return
        await asyncio.sleep(0.02)
    raise AssertionError("condition not reached in time")


def test_reattach_catches_up_from_where_it_left(app, monkeypatch):
    monkeypatch.setattr(engine.providers, "stream_reply",
                        slow_stream(["alpha ", "beta ", "gamma"]))
    with TestClient(app, base_url="http://127.0.0.1") as c:
        chat_id = c.post("/api/chats", json={}).json()["id"]
        seen = []
        round_id = None
        with c.stream("POST", f"/api/chats/{chat_id}/send",
                      json={"text": "hi @claude"}) as r:
            for line in r.iter_lines():
                if not line.startswith("data: "):
                    continue
                ev = json.loads(line[6:])
                if ev["type"] == "round_start":
                    round_id = ev["round_id"]
                    continue  # not part of the buffer count
                seen.append(ev)
                if ev["type"] == "delta":
                    break  # drop after the first buffered event we saw

        # re-attach from where we left off; the rest of the round replays/streams
        r2 = c.get(f"/api/chats/{chat_id}/round/stream",
                   params={"round_id": round_id, "after": len(seen)})
        rest = sse_events(r2.text)
        texts = [e["text"] for e in rest if e["type"] == "delta"]
        already = [e["text"] for e in seen if e["type"] == "delta"]
        assert "".join(already + texts) == "alpha beta gamma"
        assert rest[-1]["type"] == "done"
        # unknown round id → 404 (client falls back to a chat refresh)
        assert c.get(f"/api/chats/{chat_id}/round/stream",
                     params={"round_id": 999999, "after": 0}).status_code == 404


def test_active_chat_ids_tracks_running_then_clears(app, monkeypatch):
    """Running-task indicator source: a chat with a live round appears in
    active_chat_ids while it generates and drops out once it finishes - the
    signal that lets the sidebar mark a background chat busy and clear it."""
    monkeypatch.setattr(engine.providers, "stream_reply",
                        slow_stream(["working..."], delay=0.4))

    async def go():
        async with _async_client(app) as c:
            chat_id = (await c.post("/api/chats", json={})).json()["id"]
            assert chat_id not in rounds.active_chat_ids()
            send = asyncio.create_task(
                c.post(f"/api/chats/{chat_id}/send", json={"text": "hi @claude"}))
            await _until(lambda: rounds.active(chat_id) is not None)
            assert chat_id in rounds.active_chat_ids()  # busy → advertised
            await send
            assert chat_id not in rounds.active_chat_ids()  # done → cleared

    asyncio.run(go())


def test_abort_force_clears_an_uncooperative_round(monkeypatch):
    """A round whose task refuses to die (a wedged Claude Code subprocess
    that swallows cancellation) must not latch the running lock forever. Abort
    force-finishes it after the bounded wait, so `active()` clears and the chat
    unblocks - and abort itself returns instead of hanging."""
    monkeypatch.setattr(rounds, "ABORT_SETTLE_S", 0.3)

    async def go():
        chat_id = 424242
        started = asyncio.Event()
        release = asyncio.Event()

        async def wedged():
            started.set()
            while not release.is_set():
                try:
                    await asyncio.sleep(30)
                except asyncio.CancelledError:
                    if release.is_set():
                        raise
                    continue  # pathological: ignore the abort's cancellation
            return
            yield  # pragma: no cover - makes this an async generator

        r = rounds.start(chat_id, wedged())
        await asyncio.wait_for(started.wait(), 2)
        assert rounds.active(chat_id) is not None

        t0 = time.monotonic()
        aborted = await rounds.abort(chat_id)
        elapsed = time.monotonic() - t0
        assert aborted is True
        assert elapsed < 3.0            # bounded - did NOT hang on the wedged task
        assert rounds.active(chat_id) is None   # force-cleared → lock released
        assert chat_id not in rounds.active_chat_ids()

        # let the orphaned task exit cleanly so it doesn't leak into other tests
        release.set()
        if r.task:
            r.task.cancel()
            try:
                await r.task
            except (asyncio.CancelledError, Exception):
                pass

    asyncio.run(go())


def test_guest_teardown_is_bounded(tmp_path, monkeypatch):
    """The root cause: the guest's finally teardown (SDK iterator close +
    worktree removal) must not be able to wedge the round. Even when the SDK
    iterator's aclose() never returns, run_guest still finishes within the
    teardown budget and yields its turn - so the round completes and the
    running lock clears."""
    import claude_agent_sdk as sdk
    from backend import guest

    monkeypatch.setattr(guest, "GUEST_TEARDOWN_S", 0.4)

    class HangingCloseIterator:
        """Streams one ResultMessage, then hangs forever on aclose()."""
        def __init__(self, messages):
            self._it = iter(messages)

        def __aiter__(self):
            return self

        async def __anext__(self):
            try:
                return next(self._it)
            except StopIteration:
                raise StopAsyncIteration

        async def aclose(self):
            await asyncio.Event().wait()  # never returns - the wedge under test

    def fake_query(*, prompt, options=None, transport=None):
        return HangingCloseIterator([sdk.ResultMessage(
            subtype="success", duration_ms=1, duration_api_ms=1, is_error=False,
            num_turns=1, session_id="s-1", total_cost_usd=0.01,
            usage={"input_tokens": 5, "output_tokens": 2}, result="done")])

    class FakeSdk:
        ClaudeAgentOptions = sdk.ClaudeAgentOptions
        AssistantMessage = sdk.AssistantMessage
        UserMessage = sdk.UserMessage
        ResultMessage = sdk.ResultMessage
        StreamEvent = sdk.StreamEvent
        TextBlock = sdk.TextBlock
        ToolUseBlock = sdk.ToolUseBlock
        ToolResultBlock = sdk.ToolResultBlock
        query = staticmethod(fake_query)

    monkeypatch.setattr(guest, "_sdk", lambda: FakeSdk)
    _git_repo(tmp_path)
    cfg = {"code_repos": {"demo": str(tmp_path)}}

    async def go():
        t0 = time.monotonic()
        events = await asyncio.wait_for(
            _drain(guest.run_guest("t", "demo", "ctx", cfg)), timeout=5.0)
        return time.monotonic() - t0, events

    elapsed, events = asyncio.run(go())
    assert elapsed < 4.0                         # bounded - aclose didn't wedge it
    assert any(k == "usage" for k, _ in events)  # the turn still produced its result


async def _drain(agen):
    return [ev async for ev in agen]


def _git_repo(path):
    """A tiny real git repo so the worktree add/remove in run_guest is honest."""
    import os
    import subprocess
    env = {**os.environ, "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@t",
           "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@t"}
    subprocess.run(["git", "init", "-q", "-b", "main", str(path)], check=True)
    (path / "README.md").write_text("hi")
    subprocess.run(["git", "-C", str(path), "add", "-A"], check=True, env=env)
    subprocess.run(["git", "-C", str(path), "commit", "-qm", "init"], check=True,
                   env=env)


def test_concurrent_send_refused_while_round_active(app, monkeypatch):
    monkeypatch.setattr(engine.providers, "stream_reply",
                        slow_stream(["thinking..."], delay=0.5))

    async def go():
        async with _async_client(app) as c:
            chat_id = (await c.post("/api/chats", json={})).json()["id"]
            send = asyncio.create_task(
                c.post(f"/api/chats/{chat_id}/send", json={"text": "hi @claude"}))
            await _until(lambda: rounds.active(chat_id) is not None)
            second = await c.post(f"/api/chats/{chat_id}/send",
                                  json={"text": "again"})
            assert second.status_code == 409
            await c.post(f"/api/chats/{chat_id}/round/abort")
            await send

    asyncio.run(go())
