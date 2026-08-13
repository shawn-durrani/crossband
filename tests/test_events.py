"""Global live-events bus: out-of-band system/deploy notices (and
every normal message) must reach an already-connected client without a page
refresh, survive reconnects/sleep, and lose nothing across a full process
restart because the DB - not the in-memory wake-up bell - is the actual
catch-up buffer. See backend/events.py's module docstring for the full design
and THE LOST-WAKEUP INVARIANT this file's core regression test exercises.

Testing note: GET /api/events/stream is a genuinely PERSISTENT connection -
it never ends on its own. FastAPI's test transport (httpx's ASGITransport,
used by both TestClient and an async httpx.AsyncClient here) fully drains an
ASGI response body before handing anything back to the caller, so pointing it
at a truly infinite generator hangs forever (verified directly; not a guess).
That's a test-harness limitation, not a production one - a real browser/proxy
socket streams incrementally just fine. So: exactly ONE test below exercises
the real HTTP route (with events.stream monkeypatched to a FINITE generator,
just to prove the wiring - param parsing, media type, 404s); every
correctness test (catch-up, ordering, dedup, reconnect, the lost-wakeup race,
the restart scenario) drives backend/events.py's `stream()` and
db.insert_message()/db.get_messages_after() directly, which is also exactly
what a real request handler does under the hood - same code path, just
without a literal socket in the way."""

import asyncio
import json

import pytest
from fastapi.testclient import TestClient

from backend import db, events
from backend.app import create_app
from backend.config import Settings
from backend.routers import events as events_router


def make_app(tmp_path, name="data"):
    return create_app(Settings(data_dir=str(tmp_path / name),
                               memory_url="http://127.0.0.1:1"))


# ---------- HTTP-level: prove the route itself is wired correctly ----------

def test_stream_endpoint_wiring(tmp_path, monkeypatch):
    """`since` is parsed and passed through, the media type is SSE, and the
    events actually reach the HTTP response body - with events.stream()
    swapped for a finite fake so the test transport's full-buffering doesn't
    hang (see module docstring)."""
    captured = {}

    async def fake_stream(since, heartbeat_secs=25):
        captured["since"] = since
        yield 'data: {"type": "new_message", "chat_id": 1, "id": 42, "speaker": "user"}\n\n'

    monkeypatch.setattr(events_router.events, "stream", fake_stream)
    with TestClient(make_app(tmp_path), base_url="http://127.0.0.1") as c:
        r = c.get("/api/events/stream", params={"since": 7})
        assert r.status_code == 200
        assert r.headers["content-type"].startswith("text/event-stream")
        assert captured["since"] == 7
        assert json.loads(r.text.splitlines()[0][6:]) == {
            "type": "new_message", "chat_id": 1, "id": 42, "speaker": "user"}


def test_stream_endpoint_defaults_since_to_zero(tmp_path, monkeypatch):
    captured = {}

    async def fake_stream(since, heartbeat_secs=25):
        captured["since"] = since
        return
        yield  # pragma: no cover - makes this an async generator

    monkeypatch.setattr(events_router.events, "stream", fake_stream)
    with TestClient(make_app(tmp_path), base_url="http://127.0.0.1") as c:
        c.get("/api/events/stream")
    assert captured["since"] == 0


# ---------- messages-after: a normal (non-streaming) endpoint ----------

def test_messages_after_endpoint_shape_and_scope(tmp_path):
    """The per-chat incremental fetch the frontend uses to hydrate content
    after a `new_message` event - full row shape (attachments/tool_events),
    scoped to one chat, never earlier ids."""
    with TestClient(make_app(tmp_path), base_url="http://127.0.0.1") as c:
        chat = c.post("/api/chats", json={}).json()
        other = c.post("/api/chats", json={}).json()
        m1 = c.post(f"/api/chats/{chat['id']}/notice", json={"text": "one"}).json()
        m2 = c.post(f"/api/chats/{chat['id']}/notice", json={"text": "two"}).json()
        c.post(f"/api/chats/{other['id']}/notice", json={"text": "not this chat"})

        got = c.get(f"/api/chats/{chat['id']}/messages", params={"after": 0}).json()
        assert [m["id"] for m in got["messages"]] == [m1["id"], m2["id"]]
        assert got["messages"][0]["attachments"] == []
        assert got["messages"][0]["tool_events"] == []

        got2 = c.get(f"/api/chats/{chat['id']}/messages",
                    params={"after": m1["id"]}).json()
        assert [m["id"] for m in got2["messages"]] == [m2["id"]]

        assert c.get("/api/chats/999999/messages").status_code == 404


# ---------- events.stream() driven directly: the real correctness tests ----------
#
# Full control over concurrency, no HTTP transport in the way - the same
# function GET /api/events/stream calls, exercised with heartbeats far longer
# than any test timeout, so passing proves the LIVE/DB-catch-up paths did the
# work, not a heartbeat fallback quietly bailing the test out.

@pytest.fixture
def db_app(tmp_path):
    """Configure the database (create_app's side effect) without starting the
    real HTTP lifespan - these tests bind the loop themselves, on the loop
    they actually run under (asyncio.run's own), which is what a real
    lifespan would do too."""
    return create_app(Settings(data_dir=str(tmp_path / "data"),
                               memory_url="http://127.0.0.1:1"))


def _new_chat_id():
    con = db.connect()
    cur = con.execute(
        "INSERT INTO chats(title, created_at, updated_at) VALUES('t', 0, 0)")
    con.commit()
    chat_id = cur.lastrowid
    con.close()
    return chat_id


def test_initial_catchup_replays_existing_rows(db_app):
    """since=0 on a chat with pre-existing messages: everything already
    committed is replayed before the generator ever waits."""
    async def go():
        events.bind_loop(asyncio.get_running_loop())
        chat_id = _new_chat_id()
        con = db.connect()
        m1 = db.insert_message(con, chat_id, "system", "one")
        m2 = db.insert_message(con, chat_id, "system", "two")
        con.close()

        gen = events.stream(since=0, heartbeat_secs=25)
        got = [await asyncio.wait_for(gen.__anext__(), timeout=1.0) for _ in range(2)]
        await gen.aclose()
        assert [json.loads(g[6:])["id"] for g in got] == [m1["id"], m2["id"]]

    asyncio.run(go())


def test_reconnect_from_watermark_skips_already_seen(db_app):
    """A stream opened with since=<last id already seen> never re-yields
    that row - the exact shape of a reconnect after a drop/sleep."""
    async def go():
        events.bind_loop(asyncio.get_running_loop())
        chat_id = _new_chat_id()
        con = db.connect()
        first = db.insert_message(con, chat_id, "system", "first")
        second = db.insert_message(con, chat_id, "system", "second")
        con.close()

        gen = events.stream(since=first["id"], heartbeat_secs=25)
        got = await asyncio.wait_for(gen.__anext__(), timeout=1.0)
        await gen.aclose()
        assert json.loads(got[6:]) == {
            "type": "new_message", "chat_id": chat_id, "id": second["id"],
            "speaker": second["speaker"]}

    asyncio.run(go())


def test_global_channel_never_carries_content(db_app):
    """Design decision: chat_id + id only, never message text."""
    async def go():
        events.bind_loop(asyncio.get_running_loop())
        chat_id = _new_chat_id()
        con = db.connect()
        db.insert_message(con, chat_id, "system", "🚀 deployed sideband — secret sauce")
        con.close()

        gen = events.stream(since=0, heartbeat_secs=25)
        raw = await asyncio.wait_for(gen.__anext__(), timeout=1.0)
        got = json.loads(raw[6:])
        await gen.aclose()
        # speaker joined the payload (#64): a slug is metadata, not content -
        # the guard's real teeth are that no TEXT ever rides this stream.
        assert set(got) == {"type", "chat_id", "id", "speaker"}
        assert got["speaker"] == "system"
        assert "secret sauce" not in raw and "deployed" not in raw

    asyncio.run(go())


def test_ordered_dedup_across_chats(db_app):
    """Messages from different chats interleaved are delivered exactly once
    each, strictly in global id order - the id IS the order, and the
    generation-check fast path never double-delivers a row it already
    yielded."""
    async def go():
        events.bind_loop(asyncio.get_running_loop())
        chat_a = _new_chat_id()
        chat_b = _new_chat_id()
        con = db.connect()
        ids = []
        for i, cid in enumerate([chat_a, chat_b, chat_a, chat_b]):
            msg = db.insert_message(con, cid, "system", f"m{i}")
            ids.append((cid, msg["id"]))
        con.close()

        gen = events.stream(since=0, heartbeat_secs=25)
        got = []
        for _ in range(len(ids)):
            ev = json.loads((await asyncio.wait_for(gen.__anext__(), timeout=1.0))[6:])
            got.append((ev["chat_id"], ev["id"]))
        await gen.aclose()
        assert got == ids  # exact order, no dupes, no drops

    asyncio.run(go())


def test_no_lost_wakeup_between_query_and_wait(db_app, monkeypatch):
    """THE regression test for the invariant in events.py's module docstring.
    Simulates the exact race: a message is committed (and notifies) in the
    gap between the stream's DB query returning and it checking the
    generation counter - by inserting AS A SIDE EFFECT of the first (empty)
    query call. Uses a deliberately huge heartbeat (25s, the real default):
    if the generation check did NOT catch this, the only way the message
    could ever arrive is the heartbeat timing out - which a 1s test timeout
    will never survive. Passing proves the fast path, not the fallback,
    delivered it."""
    async def go():
        events.bind_loop(asyncio.get_running_loop())
        chat_id = _new_chat_id()
        target_chat_id = chat_id  # captured under a different name - racing_query's
                                   # own `chat_id` kwarg (matching get_messages_after's
                                   # signature) would otherwise shadow this

        call_count = 0
        original = db.get_messages_after

        def racing_query(con, since, chat_id=None):
            nonlocal call_count
            call_count += 1
            result = original(con, since, chat_id=chat_id)
            if call_count == 1:
                con2 = db.connect()
                db.insert_message(con2, target_chat_id, "system", "deploy done")
                con2.close()
            return result

        monkeypatch.setattr(db, "get_messages_after", racing_query)

        gen = events.stream(since=0, heartbeat_secs=25)
        got = json.loads((await asyncio.wait_for(gen.__anext__(), timeout=1.0))[6:])
        await gen.aclose()
        assert got["chat_id"] == chat_id

    asyncio.run(go())


def test_live_wakeup_faster_than_heartbeat(db_app):
    """Without any artificial race: insert shortly after the stream starts,
    with a long heartbeat - delivery must come from the live notify, not the
    heartbeat timeout (proven by the tight 1s wait_for against a 25s
    heartbeat)."""
    async def go():
        events.bind_loop(asyncio.get_running_loop())
        chat_id = _new_chat_id()

        async def insert_soon():
            await asyncio.sleep(0.05)
            con = db.connect()
            db.insert_message(con, chat_id, "system", "hello")
            con.close()

        task = asyncio.create_task(insert_soon())
        gen = events.stream(since=0, heartbeat_secs=25)
        got = json.loads((await asyncio.wait_for(gen.__anext__(), timeout=1.0))[6:])
        await task
        await gen.aclose()
        assert got["chat_id"] == chat_id

    asyncio.run(go())


def test_heartbeat_timeout_path_is_real(db_app):
    """The other half of the invariant: absent any notify at all, a waiter
    still returns (heartbeat), it just takes ~`heartbeat_secs` - proving the
    fallback path itself is reachable and not silently broken."""
    async def go():
        events.bind_loop(asyncio.get_running_loop())
        timed_out = await events._wait_for_wakeup(0.05)
        assert timed_out is True

    asyncio.run(go())


def test_deploy_restart_scenario(tmp_path):
    """The scenario this was actually filed about: a notice lands, the
    PROCESS RESTARTS (killing every connection, tearing down the events bus's
    bound loop via lifespan shutdown), and a second notice lands while
    nothing is connected at all. A client reconnecting afterward with its old
    watermark must receive exactly the messages it missed - proving catch-up
    is DB-backed, not dependent on any in-memory buffer a restart would wipe."""
    settings = Settings(data_dir=str(tmp_path / "data"), memory_url="http://127.0.0.1:1")

    app1 = create_app(settings)
    with TestClient(app1, base_url="http://127.0.0.1") as c1:
        chat = c1.post("/api/chats", json={}).json()
        restarting = c1.post(f"/api/chats/{chat['id']}/notice",
                             json={"text": "⏳ deploy request received"}).json()
    # app1's lifespan has now torn down (process "restarted"): events.unbind_loop()
    # ran, no server, no open connection - nothing in memory survives this point.

    # A brand new process (app2) comes up against the SAME data directory and
    # the deploy tooling's final notice lands on it - a real webhook call
    # arriving after the restart, on the new process, same as production.
    app2 = create_app(settings)
    with TestClient(app2, base_url="http://127.0.0.1") as c2:
        deployed = c2.post(f"/api/chats/{chat['id']}/notice",
                           json={"text": "🚀 deployed"}).json()

        # The client reconnects with the watermark it had BEFORE the restart -
        # driving the same events.stream() the endpoint calls, under app2's
        # now-current loop binding.
        async def reconnect():
            gen = events.stream(since=restarting["id"], heartbeat_secs=25)
            got = json.loads((await asyncio.wait_for(gen.__anext__(), timeout=1.0))[6:])
            await gen.aclose()
            return got

        got = asyncio.run(reconnect())
        assert got == {"type": "new_message", "chat_id": chat["id"], "id": deployed["id"],
                       "speaker": "system"}
