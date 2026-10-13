"""A seat's heard-as names (#663): how the transcriber writes its name.

Pins: a Qwen seat is heard as "Quinn" with no setting, and a seat's own
list adds to that, cleaned, without its real name; "Quinn, ..." addresses
the Qwen seat, so it alone answers and may not pass; no one in the room can
be named Quinn while a Qwen seat exists; Qwen is told the name means her,
and every other seat is told how the transcriber writes Qwen; the routes
take and clean the list, and the app's state hands the page the whole of
it; and an older database gains the column.
"""

import sqlite3

import pytest
from fastapi.testclient import TestClient

from backend import db, engine, introductions, providers, seat_names
from backend.app import create_app
from backend.config import Settings

QWEN = {"slug": "qwen", "name": "Qwen", "heard_as": ""}
CLAUDE = {"slug": "claude", "name": "Claude", "heard_as": ""}
GPT = {"slug": "gpt", "name": "GPT", "heard_as": "GBT, G P T"}


def test_qwen_is_heard_as_quinn_with_no_setting():
    assert seat_names.heard_names(QWEN) == ["Quinn"]
    assert seat_names.heard_names({"slug": "qwen", "name": "Qwen 3"}) == ["Quinn"]
    assert seat_names.heard_names(CLAUDE) == []
    assert seat_names.heard_names(GPT) == ["GBT", "G P T"]


def test_a_seats_own_list_is_cleaned_and_never_holds_its_real_name():
    seat = {"slug": "qwen", "name": "Qwen", "heard_as": " Gwen ,, quinn, Qwen,  Kwen  "}
    assert seat_names.heard_names(seat) == ["Gwen", "quinn", "Kwen"]
    assert seat_names.clean("a, b ,a,, " + "x" * 50) == "a, b, " + "x" * seat_names.MAX_CHARS
    many = ", ".join(f"n{i}" for i in range(20))
    assert len(seat_names.parse(many)) == seat_names.MAX_NAMES


def test_quinn_addresses_the_qwen_seat():
    roster = [CLAUDE, GPT, QWEN]
    picked = engine._vocative_responders("Quinn, how are you feeling?", roster)
    assert [p["slug"] for p in picked] == ["qwen"]
    assert engine.explicitly_addressed("Quinn, how are you feeling?", roster) == {"qwen"}
    assert [p["slug"] for p in engine._vocative_responders("GBT, you first", roster)] == ["gpt"]
    assert engine._vocative_responders("Quinny, hi", roster) == []


def test_every_seat_is_told_how_the_transcriber_writes_qwen():
    cfg = {"user_name": "Alex", "chat_id": 1}
    roster = [CLAUDE, QWEN]
    qwen = " ".join(providers._stable_system_parts(QWEN, roster, cfg, None, ""))
    assert "the transcriber often writes your name as Quinn" in qwen
    assert "Someone who says Quinn means you, Qwen." in qwen
    claude = " ".join(providers._stable_system_parts(CLAUDE, roster, cfg, None, ""))
    assert "Qwen (the transcriber often writes it as Quinn)" in claude
    assert "writes your name" not in claude


@pytest.fixture
def app(tmp_path):
    return create_app(Settings(data_dir=str(tmp_path / "data"),
                               memory_url="http://127.0.0.1:1"))


def _add_qwen(c):
    return c.post("/api/participants", json={
        "name": "Qwen", "provider": "openai", "model": "qwen-local",
        "base_url": "http://127.0.0.1:8080/v1", "heard_as": "  Gwen ,gwen"}).json()


def test_the_routes_take_and_clean_the_list_and_the_state_has_it_all(app):
    with TestClient(app, base_url="http://127.0.0.1") as c:
        qwen = _add_qwen(c)
        assert qwen["heard_as"] == "Gwen"
        r = c.patch(f"/api/participants/{qwen['id']}", json={"heard_as": "Kwen, , kwen"})
        assert r.json()["heard_as"] == "Kwen"
        seats = {p["slug"]: p for p in c.get("/api/state").json()["participants"]}
    assert seats["qwen"]["heard_names"] == ["Kwen", "Quinn"]
    assert seats["claude"]["heard_names"] == []


def test_no_one_in_the_room_can_be_named_quinn(app):
    with TestClient(app, base_url="http://127.0.0.1") as c:
        _add_qwen(c)
        r = c.post("/api/voice/people", json={"name": "Quinn"})
        assert r.status_code == 400
    con = db.connect()
    try:
        assert "Quinn" in introductions._participant_names(con)
    finally:
        con.close()


def test_an_older_database_gains_the_column(tmp_path):
    data = tmp_path / "old"
    data.mkdir()
    db.configure(data)
    db.init()
    con0 = sqlite3.connect(data / "chat.db")
    cols = [r[1] for r in con0.execute("PRAGMA table_info(participants)") if r[1] != "heard_as"]
    con0.execute(f"CREATE TABLE p_old AS SELECT {', '.join(cols)} FROM participants")
    con0.execute("DROP TABLE participants")
    con0.execute("ALTER TABLE p_old RENAME TO participants")
    con0.execute("PRAGMA user_version = 36")
    con0.commit()
    con0.close()
    db.init()
    con = db.connect()
    try:
        assert "heard_as" in {r[1] for r in con.execute("PRAGMA table_info(participants)")}
        assert con.execute("PRAGMA user_version").fetchone()[0] == db.SCHEMA_VERSION
    finally:
        con.close()
