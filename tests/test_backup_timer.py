"""The backup timer goes by the wall clock (#447).

The periodic snapshot used to wait on asyncio.sleep(6h). asyncio's clock
is monotonic, and it stops while macOS sleeps, so a laptop that slept most
of the day went days without a restore point. The timer now wakes every few
minutes and runs a cycle once the newest snapshot, and its own last try,
are an interval old by the wall clock. The cycle keeps its content dedupe.

Nothing here sleeps: ticks run with explicit times, and the loop runs on an
injected clock.
"""

import asyncio
import os
import threading
import time
from pathlib import Path

import pytest

from backend import db
from backend.app import create_app
from backend.config import Settings

HOUR = 3600.0
DAY = 24 * HOUR
SIX_H = 6 * HOUR


@pytest.fixture
def app(tmp_path):
    return create_app(Settings(data_dir=str(tmp_path / "data"),
                               memory_url="http://127.0.0.1:1"))


def _snaps():
    return sorted(db.BACKUP_DIR.glob("chat-*.db"))


def _write():
    con = db.connect()
    con.execute("INSERT INTO chats(title, created_at, updated_at) "
                "VALUES('t', 0, 0)")
    con.commit()
    con.close()


def _standing_snapshot(taken_at):
    """A restore point taken at `taken_at`, renamed to an older stamp so a
    fresh snapshot in the same second can't land on its file name."""
    snap = Path(db.backup_database())
    old = snap.with_name("chat-20200101-000000.db")
    snap.rename(old)
    os.utime(old, (taken_at, taken_at))
    return old


def test_an_overdue_snapshot_is_taken_on_the_first_tick_after_sleep(app):
    """The Mac sleeps for three days: the wall clock jumps and awake time
    barely moves. The first tick after waking takes the overdue snapshot."""
    t0 = time.time()
    _standing_snapshot(t0 - HOUR)
    _write()                                           # chats changed after it
    last = db.backup_tick(t0 + 300, SIX_H, t0)
    assert len(_snaps()) == 1                          # five minutes on: not due
    last = db.backup_tick(t0 + 3 * DAY, SIX_H, last)
    assert len(_snaps()) == 2
    assert last == t0 + 3 * DAY


def test_a_snapshot_that_is_not_overdue_is_not_taken(app):
    t0 = time.time()
    _standing_snapshot(t0 - HOUR)
    _write()
    assert db.backup_tick(t0 + 5 * HOUR - 1, SIX_H, None) is None
    assert len(_snaps()) == 1


def test_an_unchanged_database_takes_no_snapshot(app, monkeypatch):
    """An overdue cycle over an unchanged database is copied, found
    identical and dropped, so the old snapshot stands. The try itself
    restarts the interval, or every tick would copy the database again."""
    t0 = time.time()
    _write()
    _standing_snapshot(t0 - DAY)
    last = db.backup_tick(t0, SIX_H, None)
    assert last == t0
    assert len(_snaps()) == 1                          # identical copy dropped
    calls = []
    monkeypatch.setattr(db, "backup_database", lambda: calls.append(1))
    assert db.backup_tick(t0 + 300, SIX_H, last) == t0
    assert calls == []
    db.backup_tick(t0 + SIX_H, SIX_H, last)
    assert calls == [1]


def test_times_ahead_of_the_clock_are_ignored(app):
    """A clock set back must not stall backups until it catches up."""
    t0 = time.time()
    _standing_snapshot(t0 + 30 * DAY)
    assert db.snapshot_due(t0, SIX_H, t0 + 30 * DAY)
    assert not db.snapshot_due(t0, SIX_H, t0 - HOUR)


def test_the_loop_runs_on_the_wall_clock(app, monkeypatch):
    """The loop waits on asyncio's monotonic clock, so a jump in the
    injected wall clock alone must be enough for the next tick to back up."""
    t0 = time.time()
    _standing_snapshot(t0 - HOUR)
    wall, reads = [t0], []
    took = threading.Event()
    monkeypatch.setattr(db, "backup_database", took.set)

    async def go():
        ticking = asyncio.Event()

        def clock():
            reads.append(wall[0])
            if len(reads) >= 3:                        # the seed, then two ticks
                ticking.set()
            return wall[0]

        task = asyncio.create_task(db.backup_loop(6, clock=clock, tick_s=0.001))
        await asyncio.wait_for(ticking.wait(), 5)
        assert not took.is_set()                       # awake, nothing due yet
        wall[0] = t0 + 3 * DAY                         # asleep for three days
        assert await asyncio.to_thread(took.wait, 5)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await asyncio.wait_for(task, 1)

    asyncio.run(go())


def test_cancelling_stops_the_loop_promptly(app):
    async def go():
        task = asyncio.create_task(db.backup_loop(6))  # the five-minute tick
        await asyncio.sleep(0)                          # let it start waiting
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await asyncio.wait_for(task, 1)
        return task.cancelled()

    assert asyncio.run(go())


def test_an_interval_of_zero_turns_the_timer_off(app, monkeypatch):
    """0 used to make the loop copy the database back to back."""
    monkeypatch.setattr(db, "backup_database",
                        lambda: pytest.fail("the timer is off"))
    assert asyncio.run(asyncio.wait_for(db.backup_loop(0, tick_s=0), 1)) is None
