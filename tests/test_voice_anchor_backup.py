"""Learned voices ride the backup cycle (#33).

voice_anchors/ was in no backup: losing the data directory forgot every
learned voice while the chats survived. The contract under test:

- every DB snapshot is accompanied by a voices-<same stamp>.tar of the
  whole anchors directory, owner-only;
- the tar restores to a working store (index + clip files intact);
- retention prunes the voices family like the chat family;
- an empty or absent anchors directory adds nothing and breaks nothing.
"""

import struct
import tarfile

import pytest
from fastapi.testclient import TestClient

from backend import anchors, db
from backend.app import create_app
from backend.config import Settings


@pytest.fixture
def app(tmp_path):
    return create_app(Settings(data_dir=str(tmp_path / "data"),
                               memory_url="http://127.0.0.1:1"))


def _pcm(seconds=2.0, rate=16000, amp=6000):
    n = int(seconds * rate)
    return struct.pack(f"<{n}h", *([amp, -amp] * (n // 2)))


def _voices_tars():
    return sorted(f for f in db.BACKUP_DIR.iterdir()
                  if f.name.startswith("voices-") and f.name.endswith(".tar"))


def test_snapshot_carries_the_anchor_store(app, tmp_path):
    with TestClient(app, base_url="http://127.0.0.1"):
        store = anchors.store()
        pid = store.ensure_person("Alex")
        assert store.add_clip(pid, _pcm(), 16000, source="accumulated")

        db.backup_database()
        tars = _voices_tars()
        assert tars, "no voices tar beside the DB snapshot"
        newest = tars[-1]
        assert (newest.stat().st_mode & 0o777) == 0o600

        # restores to a working store: index plus the clip file
        restore = tmp_path / "restore"
        with tarfile.open(newest) as tar:
            tar.extractall(restore, filter="data")
        restored = anchors.AnchorStore(restore / "voice_anchors")
        person = [p for p in restored.people() if p["person_id"] == pid][0]
        assert person["clip_count"] == 1
        fname = restored.clips_of(pid)[0]["file"]
        assert restored.clip_path(pid, fname) is not None


def test_retention_prunes_the_voices_family(app):
    with TestClient(app, base_url="http://127.0.0.1"):
        store = anchors.store()
        pid = store.ensure_person("Alex")
        assert store.add_clip(pid, _pcm(), 16000, source="accumulated")
        # more snapshots than the keep limit (distinct stamps via mtime-free
        # naming: stamp comes from the DB snapshot name, so call the private
        # helper directly with synthetic stamps)
        for i in range(db.BACKUP_KEEP + 3):
            db._backup_voice_anchors(f"20990101-{i:06d}")
        assert len(_voices_tars()) <= db.BACKUP_KEEP


def test_empty_or_absent_store_adds_nothing(app):
    with TestClient(app, base_url="http://127.0.0.1"):
        db.backup_database()          # store exists but is empty, or absent
        assert _voices_tars() == []
