"""Global live-message notification bus - the backend half of the live-events
push channel.

The problem this fixes: Crossband's per-round SSE (rounds.py) only exists for
the lifetime of a round the client itself started via `/send` or `/continue`.
Out-of-band inserts - deploy tooling's `/api/chats/{id}/notice`, external
`/api/ingest` events - wrote straight to the database with no channel to an
idle, already-connected client. The message was always there (a refresh
proved it); it just never got PUSHED.

This module is that channel: ONE process-wide, in-memory wake-up bell
(`notify_new_message`) plus a DB-query catch-up (`db.get_messages_after`) that
a client replays from on every connect AND every reconnect. The database, not
this in-memory bell, is the actual catch-up buffer - so a full process
restart (a deploy!) between two client requests loses nothing: the client
just reconnects with the last message id it saw, and the very first thing a
fresh connection does is query for everything with a higher id, before it
ever starts waiting on the bell.

THE LOST-WAKEUP INVARIANT - read this before touching the loop in `stream()`:

A naive "check condition, then wait" pattern has a race: if the condition
becomes true in the gap between the check and the start of the wait, the
waiter sleeps through it, undetected, for however long its timeout is. We
close that gap with a monotonic generation counter (`_generation`), bumped
every time `notify_new_message()` fires, sampled BEFORE the DB query and
compared AFTER:

  - if `_generation` changed while (or immediately after) we were querying,
    some notify() landed concurrently with our read, so that read cannot be
    trusted as fully caught-up - loop again immediately, without waiting.
  - only once a full query→check cycle sees no generation change do we
    actually wait.

The wakeup itself is NEVER load-bearing for correctness, only for latency:
every wait - whether it ends via a real notify or via the heartbeat timeout -
is followed by a fresh `id > last` DB query. So even in a pathological
scenario where the generation check somehow missed a notification, the
heartbeat forces a re-query at worst ~HEARTBEAT_SECS later; nothing is ever
invisible indefinitely. See tests/test_events.py for a regression test that
drives this race deterministically.

Single-process assumption: like rounds.py's `_rounds` dict, this in-memory
bell only coordinates within ONE process. `start.sh` runs a single
`python -m backend` (no multi-worker uvicorn), which is what makes a
module-level dict/list a valid coordination point at all. If Crossband ever
runs multiple worker processes this stops being sufficient - call that out
explicitly rather than silently, should it come up.
"""

import asyncio
import json
import logging

from . import db

log = logging.getLogger("crossband.events")

HEARTBEAT_SECS = 25

_loop: asyncio.AbstractEventLoop | None = None
_waiters: list[asyncio.Future] = []
_generation = 0
_shutting_down = False


def begin_shutdown() -> None:
    """Stop the never-ending watcher streams because the process was asked to
    stop. Called from the signal handler in `backend/__main__.py`, BEFORE
    uvicorn starts draining connections.

    Why this has to exist: uvicorn's graceful shutdown waits for open
    connections to finish, and `stream()` below is BY DESIGN a connection that
    never finishes - every open browser tab holds one. So a plain SIGTERM
    waited forever; and because the lifespan's `finally` only runs after that
    drain, the data lock was never released either, so the next start died with
    "another instance is already running" while the operator watched the old one
    refuse to die. Ending our own endless streams lets the drain finish in
    milliseconds.

    Dropping these streams costs nothing: the DATABASE is the catch-up buffer
    (see the module docstring) and the client reconnects with its last-seen id,
    which is exactly the deploy case that has always been supported.

    Runs in a signal handler, so it touches no asyncio object directly - it
    sets a flag and reuses notify_new_message()'s thread-safe wake-up so every
    waiting stream re-checks the flag immediately instead of at its next
    heartbeat."""
    global _shutting_down
    _shutting_down = True
    notify_new_message()


def shutting_down() -> bool:
    """True once begin_shutdown() has fired. Read by stream()'s loop; exposed
    for any future long-lived handler that needs the same courtesy."""
    return _shutting_down


def bind_loop(loop: asyncio.AbstractEventLoop) -> None:
    """Call once, from the app's lifespan startup, ON the running event loop.
    Without this, notify_new_message() is a safe no-op (nothing is listening
    yet - true of most unit tests that never open the stream endpoint).

    Also resets generation/waiters: a fresh lifespan start IS a fresh process
    boundary (or, in tests, a fresh `create_app()` in the same process) - old
    waiters from a previous loop can never be woken safely anyway, so there's
    nothing to preserve. The real catch-up cursor is the DB message id, not
    this counter, so resetting it changes nothing about correctness.

    Clears the shutdown flag for the same reason: a fresh lifespan is a fresh
    process, and the test suite builds many apps in one process - a flag left
    set by one test's shutdown would make the next test's stream end instantly
    (which is how this line earned its own regression test)."""
    global _loop, _generation, _shutting_down
    _loop = loop
    _generation = 0
    _shutting_down = False
    _waiters.clear()


def unbind_loop() -> None:
    """Call from lifespan shutdown. Prevents notify_new_message() from holding
    a reference to a loop that's about to close - belt-and-suspenders on top
    of notify_new_message()'s own defensive try/except below (which exists
    because the test suite builds many short-lived apps/loops in one process:
    a raw ASGITransport client with no lifespan at all leaves `_loop` pointing
    at whatever a PREVIOUS test's TestClient last bound, and that loop can be
    closed by the time this one's insert runs)."""
    global _loop
    _loop = None


def notify_guest_job() -> None:
    """Wake connected clients after a guest job's status changes. Shares
    the SAME wake-up machinery as notify_new_message - the guest chip and the
    message feed ride one live channel, so voice and text/mobile clients get
    the identical signal (no second implementation). Like the message path, the
    DATABASE is the real catch-up buffer (guest_jobs, queried by updated_at in
    stream()), so this in-memory bell is a latency optimization, never
    load-bearing for correctness."""
    notify_new_message()


def guest_job_event(job: dict) -> dict:
    """The wire shape for a guest-job status push. Deliberately small - enough
    to drive the collapsed status chip (running/completed/failed/cancelled + the
    two completion paths) without shipping the guest's reasoning; the reply
    itself is a normal message the narrating models hand back.

    status_label/status_at are the periodic "still working" check-in -
    fully ephemeral, carried on THIS row rather than a persisted chat message,
    so a reconnecting client re-reads current state instead of replaying a
    fake message that never happened in the conversation."""
    return {"type": "guest_job", "chat_id": job["chat_id"], "id": job["id"],
            "status": job["status"], "kind": job["kind"], "task": job["task"],
            "repo": job["repo"], "mode": job["mode"],
            "step_count": job["step_count"], "updated_at": job["updated_at"],
            "status_label": job.get("status_label") or "",
            "status_at": job.get("status_at") or 0}


def notify_new_message() -> None:
    """Fire-and-forget wake-up call for ANY newly-committed message, from
    ANY insert site (see db.insert_message - the only caller). Safe to call
    from ANY thread: a synchronous (threadpooled) FastAPI path operation
    calling this right after its own `con.commit()` never touches asyncio
    objects off the event-loop thread, because the actual waiter release is
    marshalled onto the loop via call_soon_threadsafe.

    Also safe if the bound loop is closed or stale (see unbind_loop's
    docstring for why that happens in tests): a wake-up hook must NEVER be
    able to break the insert it's reporting on, so any RuntimeError here is
    swallowed, not raised. This is deliberately defensive, not hand-waved -
    an earlier version of this function let exactly this race crash normal
    message sends in tests that don't run the app's lifespan."""
    loop = _loop
    if loop is None or loop.is_closed():
        return
    try:
        loop.call_soon_threadsafe(_release_waiters)
    except RuntimeError:
        log.debug("notify_new_message: event loop unavailable, skipping wake-up")


def _release_waiters() -> None:
    global _generation
    _generation += 1
    waiters, _waiters[:] = list(_waiters), []
    for fut in waiters:
        if not fut.done():
            fut.set_result(None)


async def _wait_for_wakeup(timeout: float) -> bool:
    """Block until notify_new_message() fires or `timeout` elapses. Returns
    True if the wait timed out (a heartbeat is due), False if woken by a real
    notification (the next loop iteration's DB query will find it)."""
    loop = asyncio.get_running_loop()
    fut = loop.create_future()
    _waiters.append(fut)
    try:
        await asyncio.wait_for(asyncio.shield(fut), timeout)
        return False
    except asyncio.TimeoutError:
        return True
    finally:
        # Runs on cancellation too (client disconnect) - a dropped connection
        # can never leak a stale waiter into the next notify's release list.
        if fut in _waiters:
            _waiters.remove(fut)


def _sse(payload: dict) -> str:
    # Deliberately NOT importing engine.sse: events.py must stay independent
    # of engine's heavier import graph (providers/tools/guest) to keep this a
    # small, leaf-ish module - same one-line format, defined locally.
    return f"data: {json.dumps(payload)}\n\n"


async def stream(since: int, heartbeat_secs: float = HEARTBEAT_SECS):
    """The global SSE generator behind GET /api/events/stream. Yields
    `{"type": "new_message", "chat_id", "id"}` - never message content, by
    deliberate design - for every message inserted anywhere with id > `since`. Runs forever until the client disconnects (Starlette cancels
    this generator; `_wait_for_wakeup`'s finally cleans up the waiter either
    way - nothing to catch here).

    since=0 replays the entire message table's ids on first connect; a real
    client always passes its own last-seen id, so in practice this is a small
    "what did I miss" tail, not a full-table dump (message rows here carry
    only id + chat_id, never content, keeping even a since=0 replay cheap).

    Guest-job status rides the SAME stream, keyed on its own updated_at
    cursor. That cursor starts at connect time (`job_cursor`), so only changes
    AFTER connect are pushed live; a client seeds its initial view from
    GET /api/chats/{id}/guest_jobs, exactly as it seeds messages from the chat
    fetch. One channel, one mechanism, both modalities."""
    last = since
    job_cursor = db.now()
    # `not _shutting_down`, not `True`: when the process is stopping, this
    # generator has to END, or uvicorn's connection drain waits on it forever
    # (begin_shutdown() above has the full story). Checked at the top of
    # every iteration, so a stream woken by the shutdown wake-up returns on the
    # spot rather than after one more heartbeat.
    while not _shutting_down:
        gen = _generation
        con = db.connect()
        try:
            rows = db.get_messages_after(con, last)
            jobs = db.get_guest_jobs_after(con, job_cursor)
        finally:
            con.close()
        for r in rows:
            last = r["id"]
            yield _sse({"type": "new_message", "chat_id": r["chat_id"], "id": r["id"]})
        for j in jobs:
            job_cursor = max(job_cursor, j["updated_at"])
            yield _sse(guest_job_event(j))
        if _generation != gen:
            # A notify() landed while (or just after) we queried - see THE
            # LOST-WAKEUP INVARIANT above. That read is stale by definition;
            # loop again immediately rather than waiting.
            continue
        timed_out = await _wait_for_wakeup(heartbeat_secs)
        if timed_out:
            yield ": ping\n\n"  # keepalive comment - keeps Tailscale/reverse-proxy
                                 # idle timeouts from silently closing the pipe;
                                 # ignored by any SSE parser (comment lines start with ':')
