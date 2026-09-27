"""The data directory is owner-only (#542).

A Mac home folder is readable by the `staff` group, which every local
account is in, so a 0644 chat database, snapshot, attachment or log under
it was readable by every other account on the machine. Startup now takes
group and other access off whatever is already there, and every file made
after it is 0600 from the start, folders 0700. Everything here runs on a
throwaway data directory, starting from the common 0o022 umask.
"""

import importlib.util
import os
import sqlite3
import stat
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from backend import db
from backend.app import create_app
from backend.config import Settings

REPO = Path(__file__).resolve().parents[1]


def mode(path) -> int:
    return stat.S_IMODE(os.lstat(path).st_mode)


def loose_file(path: Path, body: bytes = b"x") -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(body)
    os.chmod(path, 0o644)
    return path


@pytest.fixture(autouse=True)
def default_umask():
    """Start each test where a fresh launchd or shell process starts."""
    os.umask(0o022)


def test_tightens_what_is_already_there(tmp_path):
    data = tmp_path / "data"
    data.mkdir()
    os.chmod(data, 0o755)
    sqlite3.connect(data / "chat.db").close()
    os.chmod(data / "chat.db", 0o644)
    loose = [
        loose_file(data / "chat.db-wal", b""),
        loose_file(data / "chat.db-shm", b""),
        loose_file(data / "backups" / "chat-20260101-000000.db"),
        loose_file(data / "backups" / "forensics" / "note.json"),
        loose_file(data / "attachments" / "abc_notes.txt"),
        loose_file(data / "voice_debug" / "voice_debug_x.json"),
        loose_file(data / "service.log"),
        loose_file(data / "service.log.1"),
        loose_file(data / "chat-before-pass-repair-20260101-000000.db"),
        loose_file(data / "repairs.jsonl"),
    ]
    folders = [data / "backups", data / "backups" / "forensics",
               data / "attachments", data / "voice_debug"]
    for d in folders:
        os.chmod(d, 0o755)
    # A symlink out of the data directory is left alone, and so is what it
    # points at: chmod would follow it.
    outside = loose_file(tmp_path / "elsewhere.txt")
    (data / "link.txt").symlink_to(outside)
    # Already private stays exactly as it was.
    anchors = data / "voice_anchors"
    anchors.mkdir(mode=0o700)
    clip = loose_file(anchors / "clip.wav")
    os.chmod(clip, 0o600)

    db.configure(data)
    assert db.secure_data_dir() == len(loose) + len(folders) + 2

    assert mode(data) == 0o700
    assert mode(data / "chat.db") == 0o600
    for f in loose:
        assert mode(f) == 0o600, f
    for d in folders:
        assert mode(d) == 0o700, d
    assert mode(outside) == 0o644
    assert mode(clip) == 0o600
    assert db.secure_data_dir() == 0  # nothing left to tighten


def test_startup_runs_it_before_the_first_snapshot(tmp_path):
    """init() snapshots the database before anything else, so the pass has
    to come first or the snapshot copies an open mode."""
    data = tmp_path / "data"
    data.mkdir()
    os.chmod(data, 0o755)
    sqlite3.connect(data / "chat.db").close()
    os.chmod(data / "chat.db", 0o644)
    log = loose_file(data / "service.log")

    db.configure(data)
    db.init()

    assert mode(data) == 0o700
    assert mode(data / "chat.db") == 0o600
    assert mode(log) == 0o600
    [snapshot] = (data / "backups").glob("chat-*.db")
    assert mode(snapshot) == 0o600
    assert mode(data / "backups") == 0o700


def test_files_made_after_startup_are_owner_only(tmp_path):
    data = tmp_path / "data"  # doesn't exist yet: a first run
    db.configure(data)
    db.init()
    assert mode(data) == 0o700
    assert mode(data / "chat.db") == 0o600

    # WAL mode: the -wal and -shm files exist while a connection is open.
    con = db.connect()
    try:
        con.execute("INSERT INTO chats(title, created_at, updated_at) "
                    "VALUES('T', 0, 0)")
        con.commit()
        assert mode(data / "chat.db-wal") == 0o600
        assert mode(data / "chat.db-shm") == 0o600
    finally:
        con.close()

    snapshot = Path(db.backup_database())
    assert mode(data / "backups") == 0o700
    assert mode(snapshot) == 0o600


def test_an_uploaded_attachment_is_owner_only(tmp_path):
    settings = Settings(data_dir=str(tmp_path / "data"),
                        memory_url="http://127.0.0.1:1")
    app = create_app(settings)
    with TestClient(app, base_url="http://127.0.0.1") as client:
        r = client.post("/api/attachments",
                        files={"file": ("notes.txt", b"hello", "text/plain")})
        assert r.status_code == 200
        stored = Path(db.ATTACH_DIR) / r.json()["stored_name"]
        assert mode(db.LOCK_PATH) == 0o600
    assert mode(stored) == 0o600
    assert mode(db.ATTACH_DIR) == 0o700


def test_the_pass_repair_writes_owner_only_files(tmp_path):
    """scripts/repair_stored_passes.py writes a snapshot and a journal into
    the data directory. It makes them private itself, whatever umask it
    was started with."""
    settings = Settings(data_dir=str(tmp_path / "data"), port=1)
    db.configure(settings.resolved_data_dir())
    db.init(settings)
    con = db.connect()
    con.execute("INSERT INTO chats(id, title, created_at, updated_at) "
                "VALUES(7, 'T', 0, 0)")
    db.insert_message(con, 7, "claude", "[pass]", notify=False)
    con.commit()
    con.close()
    os.umask(0o022)

    spec = importlib.util.spec_from_file_location(
        "repair_stored_passes", REPO / "scripts" / "repair_stored_passes.py")
    script = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(script)
    assert script.main(["--apply"], settings=settings, out=lambda _: None,
                       probe=lambda port: None) == 0

    data = settings.resolved_data_dir()
    written = [data / "repairs.jsonl",
               *data.glob("chat-before-pass-repair-*.db")]
    assert len(written) == 2
    for f in written:
        assert mode(f) == 0o600, f
