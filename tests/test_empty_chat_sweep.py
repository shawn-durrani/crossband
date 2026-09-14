"""The empty-chat sweep (#354): a chat nobody used goes on its own after 48
hours. Anything that says you meant to keep it makes it stay: a message, a
rename, an archive, a person seated in the room, or simply being young."""

import logging

from fastapi.testclient import TestClient

from backend import db
from backend.app import create_app
from backend.config import Settings

TWO_DAYS = 48 * 3600
OLD = 3 * 86400


def _app(tmp_path):
    return create_app(Settings(data_dir=str(tmp_path / "data"),
                               memory_url="http://127.0.0.1:1"))


def _mk_chat(con, *, age_s=OLD, title_upto=0, archived=False):
    t = db.now() - age_s
    cur = con.execute(
        "INSERT INTO chats(title, title_upto, archived_at, created_at, updated_at) "
        "VALUES('New chat', ?, ?, ?, ?)",
        (title_upto, t if archived else None, t, t))
    return cur.lastrowid


def _ids(con):
    return {r["id"] for r in con.execute("SELECT id FROM chats")}


def test_an_old_empty_chat_is_removed(tmp_path):
    _app(tmp_path)
    con = db.connect()
    try:
        cid = _mk_chat(con)
        con.commit()
        assert db.prune_empty_chats(con) == 1
        con.commit()
        assert cid not in _ids(con)
    finally:
        con.close()


def test_each_sign_of_use_keeps_a_chat(tmp_path):
    _app(tmp_path)
    con = db.connect()
    try:
        with_message = _mk_chat(con)
        db.insert_message(con, with_message, "user", "hello", notify=False)
        renamed = _mk_chat(con, title_upto=-1)
        archived = _mk_chat(con, archived=True)
        seated = _mk_chat(con)
        db.add_room_person(con, seated, "Sam")
        young = _mk_chat(con, age_s=TWO_DAYS - 60)
        gone = _mk_chat(con)
        con.commit()

        assert db.prune_empty_chats(con) == 1
        con.commit()

        kept = _ids(con)
        assert {with_message, renamed, archived, seated, young} <= kept
        assert gone not in kept
    finally:
        con.close()


def test_a_seat_that_left_no_longer_keeps_a_chat(tmp_path):
    _app(tmp_path)
    con = db.connect()
    try:
        cid = _mk_chat(con)
        db.add_room_person(con, cid, "Sam")
        con.execute("UPDATE room_roster SET status='left' WHERE chat_id=?", (cid,))
        con.commit()
        assert db.prune_empty_chats(con) == 1
    finally:
        con.close()


def test_auto_titled_chat_still_counts_as_untouched(tmp_path):
    """title_upto > 0 means the app titled it; only -1 is the owner's hand.
    An auto title on a chat with no messages cannot happen in practice, but
    the rule is the marker, not the title text."""
    _app(tmp_path)
    con = db.connect()
    try:
        _mk_chat(con, title_upto=5)
        con.commit()
        assert db.prune_empty_chats(con) == 1
    finally:
        con.close()


def test_startup_runs_the_sweep_and_logs_one_line(tmp_path, caplog):
    app = _app(tmp_path)
    con = db.connect()
    try:
        gone = _mk_chat(con)
        stays = _mk_chat(con, title_upto=-1)
        con.commit()
    finally:
        con.close()
    with caplog.at_level(logging.INFO, logger="crossband"):
        with TestClient(app, base_url="http://127.0.0.1") as c:
            assert c.get(f"/api/chats/{gone}").status_code == 404
            assert c.get(f"/api/chats/{stays}").status_code == 200
    lines = [r.getMessage() for r in caplog.records
             if "empty-chat sweep" in r.getMessage()]
    assert lines == ["empty-chat sweep removed 1 chat(s)"]
