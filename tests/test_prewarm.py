"""Ambient recall fires at speech-end, the round adopts it only on a match.

The STT relay starts the recall the instant the commit frame passes through,
using the freshest partial transcript; the round later adopts that task only
when it is fresh and its text matches the final transcript closely enough.
These tests pin the matcher's bounds, the store's replace-and-cancel
behavior, and both adoption outcomes (adopted → no fresh recall; mismatch →
fresh recall exactly as before)."""

import asyncio
import json
import time

import pytest
from fastapi.testclient import TestClient

from backend import db, engine
from backend.app import create_app
from backend.config import Settings

from tests.test_engine import fake_stream  # same fixture style as the engine suite


@pytest.fixture
def app(tmp_path):
    settings = Settings(data_dir=str(tmp_path / "data"),
                        memory_url="http://127.0.0.1:1")
    return create_app(settings)


@pytest.fixture(autouse=True)
def _clean_store():
    engine._recall_prewarm.clear()
    yield
    engine._recall_prewarm.clear()


# ---------- the matcher ----------

def test_norm_and_match_bounds():
    n = engine._norm_query
    assert n("What did we DO, about the cache?") == "what did we do about the cache"
    # punctuation/case-only differences: equal → match
    assert engine._prewarm_matches(n("Hey there!"), n("hey, THERE"))
    # partial trails the final by a word: prefix with enough overlap → match
    assert engine._prewarm_matches(n("what did we do about the"),
                                   n("what did we do about the cache"))
    # too little overlap → no match
    assert not engine._prewarm_matches(n("what"), n("what did we do about the cache"))
    # different sentence entirely → no match
    assert not engine._prewarm_matches(n("play some music"), n("what's my schedule"))
    assert not engine._prewarm_matches("", n("anything"))


# ---------- the store ----------

def test_prewarm_replaces_and_cancels_the_previous_task(app):
    class SlowMemory:
        async def recall(self, q, limit=10, origin="http", **kw):
            await asyncio.sleep(30)
            return []

    async def go():
        with TestClient(app, base_url="http://127.0.0.1") as c:
            chat = c.post("/api/chats", json={}).json()
            engine.prewarm_recall(chat["id"], "first utterance text", SlowMemory())
            first = engine._recall_prewarm[chat["id"]]["task"]
            await asyncio.sleep(0.01)
            engine.prewarm_recall(chat["id"], "second utterance text", SlowMemory())
            await asyncio.sleep(0.01)
            assert first.cancelled()
            assert engine._recall_prewarm[chat["id"]]["norm"] == "second utterance text"
    asyncio.run(go())


def test_prewarm_respects_memory_disabled_chats(app, monkeypatch):
    calls = {"n": 0}

    class Memory:
        async def recall(self, q, limit=10, origin="http", **kw):
            calls["n"] += 1
            return []

    async def go():
        with TestClient(app, base_url="http://127.0.0.1") as c:
            chat = c.post("/api/chats", json={}).json()
            con = db.connect()
            con.execute("UPDATE chats SET memory_enabled=0 WHERE id=?", (chat["id"],))
            con.commit(); con.close()
            engine.prewarm_recall(chat["id"], "some words", Memory())
            await engine._recall_prewarm[chat["id"]]["task"]
            assert calls["n"] == 0  # gate ran before any service call
    asyncio.run(go())


# ---------- adoption in the round ----------

def _round_with_prewarm(app, monkeypatch, send_text, prewarm_text, *, age=0.0):
    """Run one round with a completed prewarm planted; return (fresh_recall
    _count, the memory_ambient each speaker received)."""
    fresh = {"n": 0}

    class Memory:
        async def probe(self, force=False):
            return True

        def any_write_failed(self):
            return False

        async def get_summary(self):
            return "summary"

        async def recall(self, q, limit=10, origin="http", **kw):
            fresh["n"] += 1
            return [{"content": "FRESH-FACT"}]

    ambients = {}

    async def capturing_stream(participant, roster, transcript, names, cfg,
                               project, chat_summary, voice_mode,
                               tools=None, memory=None):
        ambients[participant["slug"]] = cfg.get("memory_ambient") or ""
        yield ("text", "ok")
        yield ("usage", {"input": 1, "cache_read": 0, "cache_creation": 0, "output": 1})

    monkeypatch.setattr(engine.providers, "stream_reply", capturing_stream)

    with TestClient(app, base_url="http://127.0.0.1") as c:
        chat = c.post("/api/chats", json={}).json()
        c.app.state.memory = Memory()

        async def planted():
            return [{"content": "PREWARMED-FACT"}]

        async def plant_and_send():
            task = asyncio.create_task(planted())
            await task
            engine._recall_prewarm[chat["id"]] = {
                "norm": engine._norm_query(prewarm_text),
                "task": task,
                "at": time.monotonic() - age,
            }
        # TestClient runs the app loop per-request; plant via a route call
        # context - simplest: plant synchronously with a pre-resolved task
        loop = asyncio.new_event_loop()
        try:
            loop.run_until_complete(plant_and_send())
        finally:
            loop.close()
        with c.stream("POST", f"/api/chats/{chat['id']}/send",
                      json={"text": send_text}) as r:
            "".join(r.iter_text())
    return fresh["n"], ambients


def test_matching_prewarm_is_adopted_no_fresh_recall(app, monkeypatch):
    fresh_n, ambients = _round_with_prewarm(
        app, monkeypatch,
        send_text="What did we do about the cache?",
        prewarm_text="what did we do about the")
    assert fresh_n == 0
    assert all("PREWARMED-FACT" in a for a in ambients.values())


def test_mismatched_prewarm_falls_back_to_fresh_recall(app, monkeypatch):
    fresh_n, ambients = _round_with_prewarm(
        app, monkeypatch,
        send_text="What did we do about the cache?",
        prewarm_text="play some music please")
    assert fresh_n == 1
    assert all("FRESH-FACT" in a for a in ambients.values())


def test_stale_prewarm_falls_back_to_fresh_recall(app, monkeypatch):
    fresh_n, ambients = _round_with_prewarm(
        app, monkeypatch,
        send_text="What did we do about the cache?",
        prewarm_text="what did we do about the cache",
        age=engine.PREWARM_TTL_S + 1)
    assert fresh_n == 1
    assert all("FRESH-FACT" in a for a in ambients.values())
