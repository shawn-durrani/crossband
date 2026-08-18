"""Roster seat provenance (#84): every roster write records how the seat
happened - `seated_via`, plus the triggering message id when the writer had
one in hand - so the next seat forensic is one query instead of an evening
of correlating log lines with message timestamps. Display-only, never
backfilled: rows written before v20 simply stay empty."""

import sqlite3

from fastapi.testclient import TestClient

from backend import db, introductions
from backend.app import create_app
from backend.config import Settings


def _app(tmp_path):
    return create_app(Settings(data_dir=str(tmp_path / "data"),
                               memory_url="http://127.0.0.1:1"))


def _roster(chat_id):
    con = db.connect()
    try:
        return db.get_room_roster(con, chat_id)
    finally:
        con.close()


def test_introduction_seat_records_message_and_path(tmp_path):
    """The introduction scan has its message in hand: the seat carries it."""
    with TestClient(_app(tmp_path), base_url="http://127.0.0.1") as c:
        chat = c.post("/api/chats", json={"participant_ids": []}).json()
        con = db.connect()
        try:
            mid = db.insert_message(con, chat["id"], "user",
                                    "say hi to Alex")["id"]
        finally:
            con.close()
        introductions.apply_scan(chat["id"],
                                 {"introductions": ["Alex"], "departures": []},
                                 {"user_name": "Sam", "room_roster_max": 6},
                                 message_id=mid)
        row = next(p for p in _roster(chat["id"]) if p["name"] == "Alex")
        assert row["seated_via"] == "introduction"
        assert row["seated_by_message_id"] == mid


def test_reseat_is_a_new_event_but_a_present_row_keeps_its_provenance(tmp_path):
    """Leaving and rejoining is a NEW seating event with the new trigger; an
    idempotent re-write of a still-present row must not drift the original
    answer to "how did this seat happen"."""
    with TestClient(_app(tmp_path), base_url="http://127.0.0.1") as c:
        chat = c.post("/api/chats", json={"participant_ids": []}).json()
        con = db.connect()
        try:
            db.add_room_person(con, chat["id"], "Alex",
                               seated_by_message_id=None,
                               seated_via="introduction")
            # still present: a racing voice-match re-write changes nothing
            db.add_room_person(con, chat["id"], "Alex",
                               seated_via="voice-match")
            row = _roster(chat["id"])[0]
            assert row["seated_via"] == "introduction"

            db.mark_room_person_left(con, chat["id"], "Alex")
            db.add_room_person(con, chat["id"], "Alex",
                               seated_via="voice-match")
            row = _roster(chat["id"])[0]
            assert row["status"] == "present"
            assert row["seated_via"] == "voice-match"
        finally:
            con.close()


# ---------- schema migration (v19 -> v20) ----------

def test_v19_to_v20_migration_leaves_old_rows_unstamped(tmp_path):
    """An upgraded database keeps every pre-v20 roster row exactly as it was:
    no seat trigger is invented for a seat whose trigger was never recorded."""
    data = tmp_path / "data"
    data.mkdir()
    c = sqlite3.connect(data / "chat.db")
    c.executescript(
        "CREATE TABLE room_roster(id INTEGER PRIMARY KEY AUTOINCREMENT,"
        " chat_id INTEGER NOT NULL, name TEXT NOT NULL,"
        " person_id TEXT NOT NULL DEFAULT '',"
        " status TEXT NOT NULL DEFAULT 'present',"
        " joined_at REAL NOT NULL, left_at REAL, updated_at REAL NOT NULL);"
        "INSERT INTO room_roster(chat_id, name, joined_at, updated_at)"
        " VALUES(1, 'Alex', 0, 0);")
    c.execute("PRAGMA user_version = 19")
    c.commit()
    c.close()
    db.configure(data)
    db.init()
    con = db.connect()
    try:
        assert (con.execute("PRAGMA user_version").fetchone()[0]
                == db.SCHEMA_VERSION)
        row = con.execute("SELECT * FROM room_roster").fetchone()
        assert row["seated_via"] == ""
        assert row["seated_by_message_id"] is None
    finally:
        con.close()
