"""Async guest execution as a decoupled background job.

The guest no longer speaks as the round's final turn — it runs as a detached
GuestJob whose status lives in the durable guest_jobs table, independent of the
voice/turn lifecycle. These tests drive the lifecycle directly (running →
completed/failed/cancelled), the two completion paths (blocker vs result), the
one-job-per-chat block, and the hand-back timing — no SDK, no network."""

import asyncio
import json

import pytest

from backend import db, guest, guestjobs


@pytest.fixture
def chat(tmp_path):
    db.configure(tmp_path / "data")
    db.init()
    con = db.connect()
    cur = con.execute(
        "INSERT INTO chats(created_at, updated_at, code_enabled) VALUES(?,?,1)",
        (db.now(), db.now()))
    cid = cur.lastrowid
    con.commit()
    con.close()
    guestjobs._jobs.clear()
    yield cid
    guestjobs._jobs.clear()
    guest._pending.clear()


def fake_guest(events):
    async def run_guest(task, repo_key, context, cfg, mode="investigate",
                        resume=None, chat_id=0, model="", effort="",
                        session_key=None, ref=""):
        for ev in events:
            yield ev
    return run_guest


def _job_row(job_id):
    con = db.connect()
    row = db.get_guest_job(con, job_id)
    con.close()
    return row


def _guest_messages(chat_id):
    con = db.connect()
    msgs = [m for m in db.get_chat_messages(con, chat_id)
            if m["speaker"] == guest.SLUG]
    con.close()
    return msgs


# ---------- lifecycle + persistence ----------

def test_job_completes_persists_and_classifies_result(chat, monkeypatch):
    monkeypatch.setattr(guest, "run_guest", fake_guest([
        ("tool", {"tool": "Read", "input": {"file_path": "engine.py"}, "output": "…"}),
        ("text", "Investigated the loop. "),
        ("text", "Everything checks out."),
        ("usage", {"cost": 0.02, "session_id": "sess-1", "output": 5}),
    ]))

    async def go():
        job = guestjobs.start(chat, "explain the loop", "demo", "ctx", {},
                              handback=None)
        await job.task
        return job

    job = asyncio.run(go())
    assert job.status == "completed" and job.kind == "result"
    row = _job_row(job.id)
    assert row["status"] == "completed" and row["kind"] == "result"
    assert row["session_id"] == "sess-1"
    msg = _guest_messages(chat)[0]
    assert msg["content"] == "Investigated the loop. Everything checks out."
    assert row["message_id"] == msg["id"]
    assert msg["tool_events"][0]["tool"] == "Read"
    assert json.loads(msg["usage_json"])["cost"] == 0.02


def test_reply_that_asks_the_group_is_a_blocker(chat, monkeypatch):
    monkeypatch.setattr(guest, "run_guest", fake_guest([
        ("text", "There are two repos matching that name. "),
        ("text", "Which one did you mean — sideband or the memory service?"),
        ("usage", {"cost": 0.0, "session_id": "s"}),
    ]))

    async def go():
        job = guestjobs.start(chat, "do a thing", "demo", "ctx", {}, handback=None)
        await job.task
        return job

    job = asyncio.run(go())
    assert job.kind == "blocker"
    assert _job_row(job.id)["kind"] == "blocker"


def test_shipped_pr_with_trailing_question_stays_result(chat, monkeypatch):
    monkeypatch.setattr(guest, "run_guest", fake_guest([
        ("text", "Shipped: https://github.com/x/y/pull/42. "),
        ("text", "Want me to tackle the follow-up next?"),
        ("usage", {"cost": 0.0, "session_id": "s"}),
    ]))

    async def go():
        job = guestjobs.start(chat, "ship it", "demo", "ctx", {}, handback=None)
        await job.task
        return job

    assert asyncio.run(go()).kind == "result"


def test_failure_marks_job_failed_not_completed(chat, monkeypatch):
    async def boom(*a, **k):
        raise RuntimeError("CLI exploded")
        yield  # pragma: no cover

    monkeypatch.setattr(guest, "run_guest", boom)

    async def go():
        job = guestjobs.start(chat, "t", "demo", "ctx", {}, handback=None)
        await job.task
        return job

    job = asyncio.run(go())
    assert job.status == "failed"
    row = _job_row(job.id)
    assert row["status"] == "failed" and "CLI exploded" in row["error"]
    assert not _guest_messages(chat)  # nothing persisted


def test_cancellation_persists_partial_and_marks_cancelled(chat, monkeypatch):
    gate = asyncio.Event()

    async def slow(*a, **k):
        yield ("text", "partial so far")
        await gate.wait()
        yield ("text", " never reached")  # pragma: no cover

    monkeypatch.setattr(guest, "run_guest", slow)

    async def go():
        job = guestjobs.start(chat, "t", "demo", "ctx", {}, handback=None)
        await asyncio.sleep(0.05)
        job.task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await job.task
        return job

    job = asyncio.run(go())
    row = _job_row(job.id)
    assert row["status"] == "cancelled"
    assert "partial so far" in _guest_messages(chat)[0]["content"]


# ---------- one job per chat (block semantics) ----------

def test_second_summon_blocked_while_one_runs(chat, monkeypatch):
    gate = asyncio.Event()

    async def slow(*a, **k):
        await gate.wait()
        yield ("text", "done")

    monkeypatch.setattr(guest, "run_guest", slow)

    async def go():
        job = guestjobs.start(chat, "first task here", "demo", "ctx", {},
                              handback=None)
        await asyncio.sleep(0.05)
        assert guestjobs.active_for_chat(chat) is not None
        # a fresh summon while it runs is refused with a clear message
        refusal = guest.request(
            chat, {"task": "a second task entirely", "repo": "demo"},
            {"code_repos": {"demo": "/tmp"}, "chat_id": chat})
        # and start() itself is a last-line guard (returns None, no 2nd job)
        assert guestjobs.start(chat, "x", "demo", "c", {}, handback=None) is None
        gate.set()
        await job.task
        return refusal

    refusal = asyncio.run(go())
    assert "already working" in refusal


# ---------- the two hand-back paths ----------

def test_result_waits_for_a_pause_then_hands_back(chat, monkeypatch):
    monkeypatch.setattr(guestjobs, "RESULT_SETTLE_S", 0.0)
    calls = []

    async def handback(chat_id, kind):
        calls.append((chat_id, kind))

    monkeypatch.setattr(guest, "run_guest", fake_guest([
        ("text", "All finished, nothing surprising."),
        ("usage", {"cost": 0.0, "session_id": "s"}),
    ]))

    async def go():
        job = guestjobs.start(chat, "t", "demo", "ctx", {}, handback=handback)
        await job.task

    asyncio.run(go())
    assert calls == [(chat, "result")]


def test_no_handback_when_narrate_false(chat, monkeypatch):
    calls = []

    async def handback(chat_id, kind):
        calls.append(kind)  # pragma: no cover

    monkeypatch.setattr(guest, "run_guest", fake_guest([
        ("text", "done."), ("usage", {"cost": 0.0, "session_id": "s"})]))

    async def go():
        job = guestjobs.start(chat, "t", "demo", "ctx", {}, narrate=False,
                              handback=handback)
        await job.task

    asyncio.run(go())
    assert calls == []  # a guest summoned inside a hand-back round never re-narrates
