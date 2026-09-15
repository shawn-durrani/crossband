"""The running-cost line for an escalated chat (#259): it fires on a message
count and only while a seat is raised, it names each raised seat with its
own metered figure and never a combined total, it anchors on when the depth
was set and not on a one-reply override, the ingest path leaves it out, and
the v26 to v27 migration lands old rows with no anchor. Keyless."""

import asyncio
import json
import time

import pytest
from fastapi.testclient import TestClient

from backend import db, depth, engine, spend_note
from backend.app import create_app
from backend.config import Settings

CFG = {"user_name": "Shawn", "spend_note_every": 4}


@pytest.fixture
def app(tmp_path):
    settings = Settings(data_dir=str(tmp_path / "data"),
                        memory_url="http://127.0.0.1:1")
    return create_app(settings)


def _fill(chat_id, n, speaker="user", usage=None):
    con = db.connect()
    try:
        for i in range(n):
            db.insert_message(con, chat_id, speaker, f"turn {i}",
                              usage_json=json.dumps(usage) if usage else None)
    finally:
        con.close()


def _reflect(chat_id, cfg=CFG):
    con = db.connect()
    try:
        chat = dict(con.execute("SELECT * FROM chats WHERE id=?", (chat_id,)).fetchone())
        return spend_note.maybe_spend_note(con, chat, db.get_chat_messages(con, chat_id), cfg)
    finally:
        con.close()


def _notes(client, chat_id):
    return [m["content"] for m in client.get(f"/api/chats/{chat_id}").json()["messages"]
            if spend_note.is_spend_note(m)]


def test_line_waits_for_the_count_and_needs_a_raised_seat(app):
    with TestClient(app, base_url="http://127.0.0.1") as c:
        chat_id = c.post("/api/chats", json={}).json()["id"]
        _fill(chat_id, 6)
        assert _reflect(chat_id) is False          # nothing raised: never
        depth.apply_depth(chat_id, [{"seat": "Claude", "depth": "deep"}], CFG)
        assert _reflect(chat_id) is False          # the turns before the raise don't count
        _fill(chat_id, 2)                          # plus the raise's own notice: 3 of 4
        assert _reflect(chat_id) is False
        _fill(chat_id, 1)
        assert _reflect(chat_id) is True
        assert _reflect(chat_id) is False          # not again until another 4
        _fill(chat_id, 3)                          # the line itself does not count
        assert _reflect(chat_id) is False
        _fill(chat_id, 1)
        assert _reflect(chat_id) is True
        notes = _notes(c, chat_id)
        assert len(notes) == 2
        assert "Claude at deep thinking since" in notes[0]
        assert "Rate-card estimates, not a bill" in notes[0]
        assert "Anyone can say back to normal" in notes[0]
        # quick is cheaper than default, so it never earns a line
        depth.apply_depth(chat_id, [{"seat": "all", "depth": "quick"}], CFG)
        _fill(chat_id, 8)
        assert _reflect(chat_id) is False
        # and the knob at 0 turns it off outright
        depth.apply_depth(chat_id, [{"seat": "Claude", "depth": "max"}], CFG)
        _fill(chat_id, 8)
        assert _reflect(chat_id, {**CFG, "spend_note_every": 0}) is False


def test_line_prices_each_raised_seat_apart(app):
    with TestClient(app, base_url="http://127.0.0.1") as c:
        chat_id = c.post("/api/chats", json={}).json()["id"]
        con = db.connect()
        try:
            for slug, model, key in (("claude", "claude-sonnet-5", "ANTHROPIC_API_KEY"),
                                     ("gpt", "gpt-5", "OPENAI_API_KEY")):
                con.execute("UPDATE participants SET model=?, api_key_env=? WHERE slug=?",
                            (model, key, slug))
            con.commit()
        finally:
            con.close()
        depth.apply_depth(chat_id, [{"seat": "all", "depth": "deep"}], CFG)
        usage = {"input_tokens": 1000, "output_tokens": 1000, "cost": 0.5,
                 "cost_provenance": "rate_card_estimate", "auth": "api_key"}
        _fill(chat_id, 2, "claude", usage)
        _fill(chat_id, 2, "gpt", {**usage, "cost": 0.25})
        assert _reflect(chat_id) is True
        note = _notes(c, chat_id)[0]
        assert "Claude at deep thinking since" in note
        assert "GPT at deep thinking since" in note
        assert "$1.00 metered" in note and "$0.50 metered" in note
        assert "$1.50" not in note                 # never a combined total
        assert note.count(";") == 1                # one clause per seat


def test_seat_line_names_subscription_and_unpriced_use_apart():
    line = spend_note.seat_line("Claude", "high", 0,
                                {"metered": 0.0, "subscription_equiv": 0.4, "unknown": 0.1})
    assert "since it was set" in line
    assert "$0.00 metered" in line
    assert "subscription-covered use apart" in line
    assert "some unpriced use apart" in line
    assert "0.4" not in line and "0.5" not in line


def test_anchor_is_the_raise_and_survives_a_one_reply_override(app):
    with TestClient(app, base_url="http://127.0.0.1") as c:
        chat_id = c.post("/api/chats", json={}).json()["id"]
        depth.apply_depth(chat_id, [{"seat": "Claude", "depth": "deep"}], CFG)
        con = db.connect()
        try:
            raised = db.get_chat_seat_escalations(con, chat_id)
            assert [r["slug"] for r in raised] == ["claude"]
            set_at = raised[0]["set_at"]
            assert time.time() - set_at < 5
            # a parked override moves updated_at and not the anchor
            db.set_chat_seat_once(con, chat_id, "claude", "low")
            assert db.get_chat_seat_escalations(con, chat_id)[0]["set_at"] == set_at
            # clearing drops the seat from the raised list
            db.set_chat_seat_depth(con, chat_id, "claude", "", keep_once=True)
            assert db.get_chat_seat_escalations(con, chat_id) == []
        finally:
            con.close()


def test_reflect_job_posts_the_line_and_ingest_skips_it(app):
    with TestClient(app, base_url="http://127.0.0.1") as c:
        chat_id = c.post("/api/chats", json={}).json()["id"]
        depth.apply_depth(chat_id, [{"seat": "gpt", "depth": "max"}], CFG)
        _fill(chat_id, 3)
        cfg = {**app.state.settings.as_cfg(), **CFG}
        asyncio.run(engine.post_round_reflect_job(chat_id, cfg))
        assert len(_notes(c, chat_id)) == 1
        con = db.connect()
        try:
            rows = engine.ingest_rows(con, chat_id, 0)
        finally:
            con.close()
    assert [m["speaker"] for m in rows if m["speaker"] == "user"], "user turns still ship"
    assert not any(spend_note.is_spend_note(m) for m in rows)
    # the raise's own notice is an ordinary system row and still ships
    assert any(m["speaker"] == "system" for m in rows)


def test_v26_to_v27_migration_leaves_old_rows_without_an_anchor(tmp_path):
    import sqlite3
    data = tmp_path / "data4"
    data.mkdir()
    con0 = sqlite3.connect(data / "chat.db")
    con0.executescript(
        "CREATE TABLE chats(id INTEGER PRIMARY KEY, title TEXT NOT NULL DEFAULT '',"
        " created_at REAL NOT NULL, updated_at REAL NOT NULL);"
        "INSERT INTO chats VALUES(1, 'old', 0, 0);"
        "CREATE TABLE chat_seat_state(chat_id INTEGER NOT NULL,"
        " slug TEXT NOT NULL, reasoning_effort TEXT NOT NULL DEFAULT '',"
        " once_effort TEXT NOT NULL DEFAULT '', set_by TEXT NOT NULL DEFAULT '',"
        " once_by TEXT NOT NULL DEFAULT '',"
        " updated_at REAL NOT NULL, PRIMARY KEY (chat_id, slug));"
        "INSERT INTO chat_seat_state VALUES(1, 'claude', 'high', '', 'Dai', '', 7);")
    con0.execute("PRAGMA user_version = 26")
    con0.commit()
    con0.close()
    db.configure(data)
    db.init()
    con = db.connect()
    try:
        assert con.execute("PRAGMA user_version").fetchone()[0] == db.SCHEMA_VERSION
        assert con.execute("SELECT spend_note_upto FROM chats").fetchone()[0] == 0
        raised = db.get_chat_seat_escalations(con, 1)
        assert raised == [{"slug": "claude", "effort": "high", "set_by": "Dai",
                           "set_at": 0}]
        assert "since it was set" in spend_note.seat_line("Claude", "high", 0, None)
    finally:
        con.close()
