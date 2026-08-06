"""Stopping the app must actually stop it.

The bug: SIGTERM closed the listening socket within a second and then the
process sat there indefinitely. Cause — uvicorn's graceful shutdown waits for
open connections to finish, and `/api/events/stream` is a connection that never
finishes by design (every open browser tab holds one), with uvicorn's
`timeout_graceful_shutdown` defaulting to None: wait forever. Because the
lifespan's `finally` only runs after that drain, the data lock was never
released either, so the next start failed with "another instance is already
running" — naming a pid the operator had just killed. Reproduced with one curl
client on the stream (never exited, lock still held after 20s) and again with no
client attached (exited in ~2s), which is what isolated the cause.

So there are three separable claims to pin, and the first is the fix while the
other two are the reasons the failure was so hard to read:

1. the endless stream ENDS when a stop is requested, so the drain completes;
2. a bounded ceiling exists for connections that don't cooperate, so the
   process always exits even mid-round;
3. the lock message tells the truth about whether anything is still running.

What is deliberately NOT tested here: the real signal path (a test that sends
itself SIGTERM under uvicorn is a flaky way to assert something the two halves
below already pin — `handle_exit` calling begin_shutdown, and begin_shutdown
ending the stream). Proved end to end: with this change,
SIGTERM with a stream client attached exits promptly instead of never.
"""

import asyncio
import fcntl
import threading
import time

import pytest

from backend import db, events
from backend.__main__ import _Server
from backend.app import acquire_lock, create_app
from backend.config import Settings


@pytest.fixture
def db_app(tmp_path):
    """Configure the database the way create_app does, without running the HTTP
    lifespan — these tests bind the loop themselves (see tests/test_events.py)."""
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


# ---------- 1. the endless stream ends on a stop ----------

def test_stream_ends_when_shutdown_begins(db_app):
    """The heart of the fix. A stream that is WAITING, no new messages, nothing to
    yield — must finish when begin_shutdown() fires, without the client
    disconnecting first. The heartbeat is set far longer than the timeout, so
    passing proves the shutdown wake-up ended it rather than a keepalive tick."""
    async def go():
        events.bind_loop(asyncio.get_running_loop())
        _new_chat_id()
        gen = events.stream(since=0, heartbeat_secs=600)

        async def drain():
            async for _ in gen:
                pass

        task = asyncio.create_task(drain())
        await asyncio.sleep(0.05)          # let it reach the wait
        assert not task.done()
        events.begin_shutdown()
        await asyncio.wait_for(task, timeout=1.0)   # ends on its own

    asyncio.run(go())


def test_stream_does_not_start_once_shutdown_has_begun(db_app):
    """A client that connects during the drain window gets a stream that ends
    immediately rather than one more connection for the drain to wait on."""
    async def go():
        events.bind_loop(asyncio.get_running_loop())
        _new_chat_id()
        events.begin_shutdown()
        got = [chunk async for chunk in events.stream(since=0, heartbeat_secs=600)]
        assert got == []

    asyncio.run(go())


def test_bind_loop_clears_the_flag_so_the_next_process_streams(db_app):
    """The flag is module state in a process that hosts many apps in the test
    suite (and one app per real process). A stop left set would make every
    later stream end instantly — a far worse bug than the one being fixed."""
    async def go():
        events.begin_shutdown()
        assert events.shutting_down()
        events.bind_loop(asyncio.get_running_loop())
        assert not events.shutting_down()
        _new_chat_id()
        con = db.connect()
        msg = db.insert_message(con, 1, "system", "after restart")
        con.close()
        gen = events.stream(since=0, heartbeat_secs=600)
        chunk = await asyncio.wait_for(gen.__anext__(), timeout=1.0)
        await gen.aclose()
        assert str(msg["id"]) in chunk

    asyncio.run(go())


def test_begin_shutdown_is_safe_with_no_loop_bound():
    """It runs in a signal handler, so it must never raise — including before
    any lifespan has bound a loop, and when called twice."""
    events.unbind_loop()
    events.begin_shutdown()
    events.begin_shutdown()
    assert events.shutting_down()


# ---------- 2. the bounded ceiling ----------

def test_server_bounds_the_drain_and_ends_streams_on_signal(tmp_path):
    """Two things uvicorn cannot do for us: a finite drain ceiling, and telling
    this app's endless streams to stop. Asserted on the Config/Server pair
    rather than by signalling a live server."""
    app = create_app(Settings(data_dir=str(tmp_path / "data"),
                              memory_url="http://127.0.0.1:1"))
    import uvicorn
    config = uvicorn.Config(app, host="127.0.0.1", port=8999,
                            timeout_graceful_shutdown=Settings().shutdown_timeout_s)
    assert config.timeout_graceful_shutdown == 15, \
        "a graceful stop must have a ceiling; None means 'wait forever'"

    server = _Server(config)
    events.unbind_loop()
    import signal
    server.handle_exit(signal.SIGTERM, None)
    assert events.shutting_down(), \
        "the signal handler must end the endless streams before the drain starts"
    assert server.should_exit, "uvicorn's own exit semantics must still run"


def test_shutdown_timeout_is_configurable():
    """An operator who would rather a long round always finish can raise it."""
    assert Settings(shutdown_timeout_s=60).shutdown_timeout_s == 60


# ---------- 3. the lock tells the truth ----------

def test_lock_waits_for_a_predecessor_that_is_still_shutting_down(tmp_path):
    """The deploy case: the old process still holds the lock for a moment. A
    restart one second early should succeed, not fail with a false claim that
    another instance is running."""
    lock_path = str(tmp_path / ".lock")
    held = acquire_lock(lock_path)
    released = threading.Event()

    def release_soon():
        time.sleep(0.4)
        fcntl.flock(held.fileno(), fcntl.LOCK_UN)
        released.set()

    t = threading.Thread(target=release_soon)
    t.start()
    try:
        started = time.monotonic()
        second = acquire_lock(lock_path, wait_s=5.0)
        waited = time.monotonic() - started
        assert released.is_set(), "should have waited for the release, not raced it"
        assert waited < 5.0
        second.close()
    finally:
        t.join()
        held.close()


def test_lock_message_names_the_live_holder_and_what_to_do(tmp_path):
    """When something really IS still running, say so with its pid and the two
    commands that end it — this text is read mid-deploy, when a wrong or vague
    message costs the most."""
    lock_path = str(tmp_path / ".lock")
    held = acquire_lock(lock_path)
    try:
        with pytest.raises(RuntimeError) as err:
            acquire_lock(lock_path, wait_s=0)
        msg = str(err.value)
        import os
        assert str(os.getpid()) in msg, "name the actual holder"
        assert "kill" in msg and "-9" in msg, "say how to stop it, both ways"
        assert "stuck draining" in msg, "say WHY it may refuse to exit"
    finally:
        held.close()


def test_lock_message_does_not_claim_a_running_instance_when_the_pid_is_dead(tmp_path):
    """The failure that sent the operator hunting for a second instance that did
    not exist. If the recorded pid isn't alive, the message must NOT say another
    instance is running — it must point at whoever really holds the file."""
    lock_path = str(tmp_path / ".lock")
    held = acquire_lock(lock_path)
    try:
        with open(lock_path, "r+") as f:      # a pid that cannot be alive
            f.seek(0)
            f.truncate(0)
            f.write("999999999")
            f.flush()
        with pytest.raises(RuntimeError) as err:
            acquire_lock(lock_path, wait_s=0)
        msg = str(err.value)
        assert "already running" not in msg
        assert "lsof" in msg, "tell the operator how to find the real holder"
    finally:
        held.close()


def test_start_script_waits_before_refusing_the_port(tmp_path):
    """start.sh's port guard failed the same way for the same reason, so it gets
    the same treatment: wait for a stopping instance, then name the pid."""
    from pathlib import Path
    script = Path(__file__).resolve().parent.parent / "start.sh"
    text = script.read_text()
    guard = text.split("# python env")[0]
    assert "waiting up to 10s" in guard, "a restart may legitimately be a second early"
    assert "kill -9" in guard, "say what to do when it will not exit"
    assert "stuck draining" in guard, "say WHY it may refuse to exit"
