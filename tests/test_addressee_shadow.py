"""The addressee shadow (#650): Jev's answer on who a spoken turn was for,
kept and never acted on.

Pins: nothing is asked unless the setting is on and there's a key, and
then only for a spoken turn with words that isn't a `/` message; a spoken
send schedules it and the send never waits; Jev reads the turn, the last
three messages, the seats and the room, and the answer is kept as one row
with no words or names, beside one unpriced utility_usage row; a failed
call keeps a row with its error and no spend; logs carry no words; deleting
the chat removes the row; an older database gains the table; and the
report counts what Jev said against what the seats did, showing words only
when asked.
"""

import asyncio
import importlib.util
import json
import logging
import sqlite3
from pathlib import Path

import httpx
import pytest
from fastapi.testclient import TestClient

from backend import addressee_shadow, db
from backend.app import create_app
from backend.config import Settings

REPO = Path(__file__).resolve().parents[1]
ON = {"addressee_shadow": True, "user_name": "Alex"}


@pytest.fixture
def app(tmp_path):
    return create_app(Settings(data_dir=str(tmp_path / "data"),
                               memory_url="http://127.0.0.1:1",
                               user_name="Alex", addressee_shadow=True))


def _jev(answers=None, status=200, seen=None):
    answers = answers or {"for": {"choice": "someone", "confidence": 0.93},
                          "asks": {"choice": "no"}, "seat": {"choice": "none"}}

    def handler(request):
        if seen is not None:
            seen.append(json.loads(request.content))
        return httpx.Response(status, json={
            "model": "jev-1.13.0", "answers": answers,
            "usage": {"input_tokens": 310, "output_tokens": 3}})
    return httpx.AsyncClient(transport=httpx.MockTransport(handler))


def _rows(sql, *args):
    con = db.connect()
    try:
        return [dict(r) for r in con.execute(sql, args)]
    finally:
        con.close()


def _chat_with_turns(client):
    chat = client.post("/api/chats", json={}).json()
    con = db.connect()
    try:
        seats = db.get_chat_participants(con, chat["id"])
        slug = seats[0]["slug"]
        db.insert_message(con, chat["id"], "user", "what goes in the sauce?",
                          voice_turn_id="t1")
        db.insert_message(con, chat["id"], slug, "Tomatoes, garlic and basil.")
        con.execute("INSERT INTO room_roster(chat_id, name, status, joined_at, "
                    "updated_at) VALUES (?,?,?,?,?)",
                    (chat["id"], "Sam", "present", 0, 0))
        con.commit()
        turn = db.insert_message(con, chat["id"], "user", "Sam, can you grab the basil?",
                                 voice_turn_id="t2")
    finally:
        con.close()
    return chat["id"], turn, [s["name"] for s in seats]


# ---------- when it asks ----------

def test_nothing_is_asked_unless_on_with_a_key_for_a_spoken_turn(monkeypatch):
    called = []

    async def fake(*a, **k):
        called.append(a)

    monkeypatch.setattr(addressee_shadow, "shadow_turn", fake)
    spoken = {"id": 1, "content": "is it ready?", "voice_turn_id": "t"}

    async def go(cfg, msg):
        task = addressee_shadow.schedule(1, msg, cfg)
        if task:
            await task
        return task

    monkeypatch.delenv("TYPESAFE_API_KEY", raising=False)
    assert asyncio.run(go(ON, spoken)) is None
    monkeypatch.setenv("TYPESAFE_API_KEY", "test-key")
    assert asyncio.run(go({}, spoken)) is None
    assert asyncio.run(go(ON, {**spoken, "voice_turn_id": ""})) is None
    assert asyncio.run(go(ON, {**spoken, "content": "  "})) is None
    assert asyncio.run(go(ON, {**spoken, "content": "/deploy crossband"})) is None
    assert called == []
    assert asyncio.run(go(ON, spoken)) is not None
    assert len(called) == 1


def test_a_spoken_send_schedules_it_and_never_waits(app, monkeypatch):
    monkeypatch.setenv("TYPESAFE_API_KEY", "test-key")
    asked = []

    async def fake(chat_id, message_id, cfg):
        asked.append((chat_id, message_id, cfg["addressee_shadow"]))

    monkeypatch.setattr(addressee_shadow, "shadow_turn", fake)
    with TestClient(app, base_url="http://127.0.0.1") as c:
        chat = c.post("/api/chats", json={"participant_ids": []}).json()
        for body in ({"text": "typed, not spoken"},
                     {"text": "spoken this time", "turn_id": "v1"}):
            with c.stream("POST", f"/api/chats/{chat['id']}/send", json=body) as r:
                assert b'"user_saved"' in b"".join(r.iter_bytes())
    assert len(asked) == 1 and asked[0][0] == chat["id"] and asked[0][2] is True


# ---------- what it sends and keeps ----------

def test_the_answer_is_kept_without_words(app, monkeypatch, caplog):
    monkeypatch.setenv("TYPESAFE_API_KEY", "test-key")
    seen = []
    caplog.set_level(logging.INFO, logger="crossband.addressee_shadow")
    with TestClient(app, base_url="http://127.0.0.1") as c:
        chat_id, turn, seats = _chat_with_turns(c)

        async def go():
            async with _jev(seen=seen) as client:
                return await addressee_shadow.shadow_turn(
                    chat_id, turn["id"], {**ON, "pricing": {}}, client=client)
        got = asyncio.run(go())
    assert got == {"for": "someone", "asks": "no", "seat": "none"}
    state = seen[0]["state"]
    assert 'Now Alex, the owner says: "Sam, can you grab the basil?"' in state
    assert '"what goes in the sauce?"' in state and "Tomatoes, garlic" in state
    assert "Also in the room: Sam." in state
    assert set(seen[0]["questions"]) == {"for", "asks", "seat"}
    assert set(seen[0]["questions"]["seat"]["criteria"]) == {"all", "none", *seats}
    [row] = _rows("SELECT * FROM addressee_shadow")
    assert row["message_id"] == turn["id"] and row["chat_id"] == chat_id
    assert (row["said_for"], row["asks"], row["seat"]) == ("someone", "no", "none")
    assert (row["confidence"], row["speaker"], row["others"]) == (0.93, "owner", 1)
    assert row["model"] == "jev-1.13.0" and row["error"] == ""
    assert "basil" not in json.dumps(row) and "Sam" not in json.dumps(row)
    assert _rows("SELECT kind, model, input_tokens, cost FROM utility_usage "
                 "WHERE kind='addressee_shadow'") == [
        {"kind": "addressee_shadow", "model": "jev-1.13.0", "input_tokens": 310,
         "cost": None}]
    logged = " ".join(r.getMessage() for r in caplog.records)
    assert "addressee shadow:" in logged and "for=someone" in logged
    assert "basil" not in logged and "Sam" not in logged


def test_a_failed_call_keeps_its_error_and_spends_nothing(app, monkeypatch):
    monkeypatch.setenv("TYPESAFE_API_KEY", "test-key")
    with TestClient(app, base_url="http://127.0.0.1") as c:
        chat_id, turn, _ = _chat_with_turns(c)

        async def go():
            async with _jev(status=500) as client:
                return await addressee_shadow.shadow_turn(chat_id, turn["id"], ON,
                                                          client=client)
        asyncio.run(go())
    [row] = _rows("SELECT said_for, error FROM addressee_shadow")
    assert row == {"said_for": "", "error": "HTTPStatusError"}
    assert _rows("SELECT * FROM utility_usage WHERE kind='addressee_shadow'") == []


def test_deleting_the_chat_removes_the_row(app, monkeypatch):
    monkeypatch.setenv("TYPESAFE_API_KEY", "test-key")
    with TestClient(app, base_url="http://127.0.0.1") as c:
        chat_id, turn, _ = _chat_with_turns(c)

        async def go():
            async with _jev() as client:
                await addressee_shadow.shadow_turn(chat_id, turn["id"], ON, client=client)
        asyncio.run(go())
        assert len(_rows("SELECT * FROM addressee_shadow")) == 1
        assert c.delete(f"/api/chats/{chat_id}").status_code == 200
    assert _rows("SELECT * FROM addressee_shadow") == []


def test_an_older_database_gains_the_table(tmp_path):
    data = tmp_path / "old"
    data.mkdir()
    db.configure(data)
    db.init()
    con0 = sqlite3.connect(data / "chat.db")
    con0.execute("DROP TABLE addressee_shadow")
    con0.execute("PRAGMA user_version = 34")
    con0.commit()
    con0.close()
    db.init()
    con = db.connect()
    try:
        assert con.execute("PRAGMA user_version").fetchone()[0] == db.SCHEMA_VERSION
        cols = {r[1] for r in con.execute("PRAGMA table_info(addressee_shadow)")}
    finally:
        con.close()
    assert {"message_id", "said_for", "asks", "seat", "speaker", "others"} <= cols


# ---------- the report ----------

def _script():
    spec = importlib.util.spec_from_file_location(
        "addressee_shadow_report", REPO / "scripts" / "addressee_shadow_report.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def test_the_report_counts_what_jev_said_against_what_the_seats_did(app, monkeypatch):
    rep = _script()
    with TestClient(app, base_url="http://127.0.0.1") as c:
        chat = c.post("/api/chats", json={}).json()
    con = db.connect()
    try:
        slug = db.get_chat_participants(con, chat["id"])[0]["slug"]

        def turn(text, said_for, asks, seat_reply=False, passed=False, others=1):
            m = db.insert_message(con, chat["id"], "user", text, voice_turn_id="v")
            if seat_reply:
                db.insert_message(con, chat["id"], slug, "a reply")
            if passed:
                db.log_seat_usage(con, chat["id"], slug, "pass", "{}")
            con.execute("INSERT INTO addressee_shadow(message_id, chat_id, created_at, "
                        "said_for, asks, seat, speaker, others, ms) "
                        "VALUES (?,?,?,?,?,?,?,?,?)",
                        (m["id"], chat["id"], m["created_at"], said_for, asks, "none",
                         "owner", others, 300))
            con.commit()
        turn("can you pass the salt", "someone", "no", seat_reply=True)
        turn("the oven's on, love", "someone", "no", passed=True)
        turn("what's next in the recipe?", "ais", "yes", passed=True)
        turn("how long to rest it?", "ais", "yes", seat_reply=True, others=0)
    finally:
        con.close()
    settings = Settings(data_dir=app.state.settings.data_dir)
    out = []
    rep.main(["--days", "1"], settings=settings, out=out.append)
    text = "\n".join(out)
    assert "Spoken turns Jev judged" in text and ": 4" in out[0]
    assert "With others in the room: 3 turns" in text
    assert "Jev said someone: answered 1, all passed 1, no reply 0" in text
    assert "not for the AIs, but answered: 1 of 2" in text
    assert "not for the AIs, and all passed: 1 of 2" in text
    assert "for the AIs, but all passed: 1" in text
    assert "a reply owed, but all passed: 1" in text
    assert "Alone: 1 turns" in text and "median 300 ms" in text
    assert "salt" not in text and "recipe" not in text
    out = []
    rep.main(["--days", "1", "--show-text"], settings=settings, out=out.append)
    text = "\n".join(out)
    assert "can you pass the salt" in text and "what's next in the recipe?" in text
    assert "how long to rest it" not in text


def test_the_report_runs_on_a_database_without_the_table(tmp_path):
    rep = _script()
    data = tmp_path / "data"
    data.mkdir()
    sqlite3.connect(data / "chat.db").close()
    out = []
    rep.main([], settings=Settings(data_dir=str(data)), out=out.append)
    assert out[0].endswith(": 0")
