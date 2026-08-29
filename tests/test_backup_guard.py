"""Content-deduped snapshots: the crash-loop guard.

Retention keeps the newest BACKUP_KEEP snapshots by count, launchd
restarts a crashing service every ~10 seconds, and every startup takes a
pre-init snapshot. Unguarded, BACKUP_KEEP restarts - about two minutes -
evicted the entire pre-crash history at exactly the moment it was needed.
A copy byte-identical to the newest snapshot (voice store also unchanged)
is now discarded: it protects nothing the standing snapshot does not.
"""

import time

import pytest

from backend import anchors, db
from backend.app import create_app
from backend.config import Settings


@pytest.fixture
def app(tmp_path):
    return create_app(Settings(data_dir=str(tmp_path / "data"),
                               memory_url="http://127.0.0.1:1"))


def _snaps():
    return [(f.name, f.stat().st_mtime_ns) for f in
            sorted(db.BACKUP_DIR.glob("chat-*.db"))]


def _touch_db():
    con = db.connect()
    con.execute("INSERT INTO chats(title, created_at, updated_at) "
                "VALUES('t', 0, 0)")
    con.commit()
    con.close()


def test_identical_content_takes_no_new_snapshot(app):
    first = db.backup_database()
    again = db.backup_database()
    assert again == first          # the standing snapshot is the answer
    assert len(_snaps()) == 1


def test_a_write_takes_a_new_snapshot(app):
    db.backup_database()
    time.sleep(0.05)
    _touch_db()
    before = _snaps()
    db.backup_database()
    assert _snaps() != before


def test_a_voice_change_alone_still_takes_the_cycle(app):
    """A learned clip must reach a voices tar even when the DB is
    byte-identical - the voices ride the DB snapshot cycle."""
    db.backup_database()
    time.sleep(0.05)
    anchors.store().ensure_person("Alex")   # index write, no DB write
    before = _snaps()
    db.backup_database()
    assert _snaps() != before


def test_a_crash_loop_no_longer_shreds_the_history(app, tmp_path):
    """Repeated init() with nothing changing must not add, evict, or even
    rewrite a snapshot."""
    settings = Settings(data_dir=str(tmp_path / "data"),
                        memory_url="http://127.0.0.1:1")
    _touch_db()
    db.init(settings)          # one changed startup: takes the snapshot
    before = _snaps()
    assert before
    time.sleep(0.05)
    for _ in range(5):         # the crash loop: restart, restart, restart
        db.init(settings)
    assert _snaps() == before
