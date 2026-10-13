"""The room list, measured beside room mode (#669, #670).

Pins: the open voice session's named voices are read by a tap's name
first, the matcher's next, and never the TV; the room list is the roster
still present plus the session's names, without the owner; an unnamed
spoken turn is the owner's by the room list while no one else is in it
and no "who's this?" is open, and not yet known otherwise, beside what
room mode says; a typed turn isn't measured; each spoken turn's row holds
flags and counts and no names; and the report counts where the two
credits disagree. Names are the synthetic roster.
"""

import asyncio
import importlib.util
import json
import sqlite3
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from backend import db, room_now, voice_sessions
from backend.app import create_app
from backend.config import Settings

CFG = {"user_name": "Alex"}
REPO = Path(__file__).resolve().parents[1]


@pytest.fixture
def app(tmp_path):
    return create_app(Settings(data_dir=str(tmp_path / "data"),
                               memory_url="http://127.0.0.1:1", user_name="Alex"))


@pytest.fixture
def session(monkeypatch):
    sessions = {}
    monkeypatch.setattr(voice_sessions, "_sessions", sessions)
    return sessions


def test_the_sessions_named_voices_are_read_tap_first_and_never_the_tv(session):
    session[1] = {
        "voices": {0: {}, 1: {"human": {"name": "Sam", "pid": "p1"}},
                   2: {}, 3: {voice_sessions.MEDIA: True}, 4: {}},
        "named": {0: {"state": "named", "name": "Alex"},
                  1: {"state": "named", "name": "Samantha"},
                  2: {"state": "named", "name": "Dave"},
                  3: {"state": "named", "name": "Newsreader"},
                  4: {"state": "new"}}}
    assert voice_sessions.named_people(1) == ["Alex", "Sam", "Dave"]
    assert voice_sessions.named_people(2) == []


def _chat(c, room_mode=0, solo=0):
    chat_id = c.post("/api/chats", json={"participant_ids": []}).json()["id"]
    con = db.connect()
    try:
        con.execute("UPDATE chats SET room_mode=?, ambient_off=? WHERE id=?",
                    (room_mode, solo, chat_id))
        con.commit()
    finally:
        con.close()
    return chat_id


def _turn(chat_id, labels=None):
    con = db.connect()
    try:
        return db.insert_message(con, chat_id, "user", "pass the salt",
                                 voice_turn_id="t1", voice_labels=labels)["id"]
    finally:
        con.close()


def test_the_room_list_is_the_roster_and_the_session_without_the_owner(app, session):
    with TestClient(app, base_url="http://127.0.0.1") as c:
        chat_id = _chat(c, room_mode=1)
        con = db.connect()
        db.add_room_person(con, chat_id, "Dave")
        db.add_room_person(con, chat_id, "Alex")
        session[chat_id] = {"voices": {0: {}, 1: {}},
                            "named": {0: {"state": "named", "name": "Sam"},
                                      1: {"state": "named", "name": "Dave"}}}
        room = room_now.people(con, chat_id, "Alex")
        con.close()
    assert room == {"roster": ["Dave"], "session": ["Sam", "Dave"],
                    "room": ["Dave", "Sam"]}


@pytest.mark.parametrize("room_mode,solo,heard,ask,labels,old,new", [
    (0, 0, [], False, None, "owner", "owner"),
    (0, 0, ["Sam"], False, None, "owner", "pending"),     # someone heard, mode off
    (0, 1, ["Sam"], False, None, "owner", "pending"),     # ...and in solo
    (1, 0, [], False, None, "pending", "owner"),          # mode on, nobody else
    (1, 0, [], True, None, "pending", "pending"),         # a "who's this?" is open
    (1, 0, ["Sam"], False, None, "pending", "pending"),
    (0, 0, ["Sam"], False, {"labels": ["Alex"]}, "owner", "owner"),
    (0, 0, [], False, {"labels": ["Sam"]}, "guest", "guest"),
])
def test_an_unnamed_turn_is_credited_by_who_has_been_heard(
        app, session, monkeypatch, room_mode, solo, heard, ask, labels, old, new):
    monkeypatch.setattr(room_now, "asking", lambda con, chat_id: ask)
    with TestClient(app, base_url="http://127.0.0.1") as c:
        chat_id = _chat(c, room_mode, solo)
        session[chat_id] = {"voices": {i: {} for i in range(len(heard))},
                            "named": {i: {"state": "named", "name": n}
                                      for i, n in enumerate(heard)}}
        row = room_now.compare(chat_id, _turn(chat_id, labels), CFG)
    assert (row["old"], row["new"]) == (old, new)
    assert (row["room_mode"], row["solo"], row["others"]) == (room_mode, solo, len(heard))


def test_each_spoken_turn_gets_a_row_with_no_names_and_typed_ones_none(app, session):
    with TestClient(app, base_url="http://127.0.0.1") as c:
        chat_id = _chat(c)
        session[chat_id] = {"voices": {0: {}},
                            "named": {0: {"state": "named", "name": "Sam"}}}
        message_id = _turn(chat_id)

        async def go():
            typed = room_now.schedule(chat_id, {"id": 1, "voice_turn_id": ""}, CFG)
            return typed, await room_now.shadow_turn(chat_id, message_id, CFG)
        typed, row = asyncio.run(go())
        con = db.connect()
        rows = [dict(r) for r in con.execute("SELECT * FROM room_shadow")]
        con.close()
    assert typed is None and row["new"] == "pending"
    assert len(rows) == 1 and rows[0]["message_id"] == message_id
    assert (rows[0]["old"], rows[0]["new"], rows[0]["session_named"]) == ("owner", "pending", 1)
    assert "Sam" not in json.dumps(rows)


def test_the_report_counts_where_the_credits_differ(tmp_path):
    spec = importlib.util.spec_from_file_location(
        "room_shadow_report", REPO / "scripts" / "room_shadow_report.py")
    rep = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(rep)
    data = tmp_path / "data"
    data.mkdir()
    con = sqlite3.connect(data / "chat.db")
    con.execute("CREATE TABLE room_shadow(message_id INTEGER PRIMARY KEY, chat_id INTEGER, "
                "created_at REAL, room_mode INTEGER, solo INTEGER, roster INTEGER, "
                "session_named INTEGER, others INTEGER, asking INTEGER, labelled INTEGER, "
                "old TEXT, new TEXT)")
    rows = [(0, 0, 1, 0, 0, "owner", "pending"), (0, 1, 1, 0, 0, "owner", "pending"),
            (1, 0, 0, 0, 0, "pending", "owner"), (0, 0, 0, 0, 1, "owner", "owner"),
            (1, 0, 2, 0, 1, "guest", "guest")]
    for i, (mode, solo, others, ask, labelled, old, new) in enumerate(rows):
        con.execute("INSERT INTO room_shadow VALUES (?,1,?,?,?,0,?,?,?,?,?,?)",
                    (i, 1_700_000_000.0 + i, mode, solo, others, others, ask,
                     labelled, old, new))
    con.commit()
    con.close()
    out = []
    rep.main(["--since", "2023-11-01", "--until", "2023-11-30"],
             settings=Settings(data_dir=str(data)), out=out.append)
    text = "\n".join(out)
    assert out[0].endswith(": 5")
    assert "Room mode when they were said: off 2, on 2, solo 1" in text
    assert "Credited differently: 3" in text
    assert "someone else heard, mode off: 2 (1 of them in solo)" in text
    assert "mode on, nobody else: 1" in text
    assert "mode off while someone else was in the room list: 2" in text
