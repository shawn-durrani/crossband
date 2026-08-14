"""Owner-discard of a captured voice turn (#106).

Live capture can transcribe audio that was never meant for the chat. The
contract under test:

- the owner can delete their own user turn: the row (and its attachments,
  by cascade) leaves the chat and every future model context;
- honesty rides the response: `ingested` reports whether the memory
  watermark already passed the turn (the append-only ledger copy is never
  touched here);
- nobody else's turns are discardable, unknown ids 404, and a running
  round blocks the delete;
- the audit trail is content-free.
"""

import asyncio

import pytest
from fastapi.testclient import TestClient

from backend import db, rounds
from backend.app import create_app
from backend.config import Settings


@pytest.fixture
def app(tmp_path):
    return create_app(Settings(data_dir=str(tmp_path / "data"),
                               memory_url="http://127.0.0.1:1"))


def _msg(chat_id, speaker="user", text="captured words"):
    con = db.connect()
    try:
        return db.insert_message(con, chat_id, speaker, text)
    finally:
        con.close()


def _ids(chat_id):
    con = db.connect()
    try:
        return [r["id"] for r in con.execute(
            "SELECT id FROM messages WHERE chat_id=? ORDER BY id", (chat_id,))]
    finally:
        con.close()


def test_discard_removes_the_turn_and_reports_not_ingested(app):
    with TestClient(app, base_url="http://127.0.0.1") as c:
        chat = c.post("/api/chats", json={"participant_ids": []}).json()
        m = _msg(chat["id"])
        r = c.post(f"/api/chats/{chat['id']}/messages/{m['id']}/discard")
        assert r.status_code == 200
        assert r.json() == {"ok": True, "ingested": False}
        assert m["id"] not in _ids(chat["id"])


def test_ingested_watermark_is_reported_honestly(app):
    with TestClient(app, base_url="http://127.0.0.1") as c:
        chat = c.post("/api/chats", json={"participant_ids": []}).json()
        m = _msg(chat["id"])
        con = db.connect()
        con.execute("UPDATE chats SET ingested_upto=? WHERE id=?",
                    (m["id"], chat["id"]))
        con.commit()
        con.close()
        r = c.post(f"/api/chats/{chat['id']}/messages/{m['id']}/discard")
        assert r.json() == {"ok": True, "ingested": True}
        assert m["id"] not in _ids(chat["id"])


def test_only_own_user_turns_and_only_real_ids(app):
    with TestClient(app, base_url="http://127.0.0.1") as c:
        chat = c.post("/api/chats", json={"participant_ids": []}).json()
        other = c.post("/api/chats", json={"participant_ids": []}).json()
        model = _msg(chat["id"], speaker="claude")
        system = _msg(chat["id"], speaker="system")
        mine = _msg(chat["id"])

        assert c.post(f"/api/chats/{chat['id']}/messages/{model['id']}/discard"
                      ).status_code == 400
        assert c.post(f"/api/chats/{chat['id']}/messages/{system['id']}/discard"
                      ).status_code == 400
        # right id, wrong chat
        assert c.post(f"/api/chats/{other['id']}/messages/{mine['id']}/discard"
                      ).status_code == 404
        assert c.post(f"/api/chats/{chat['id']}/messages/999999/discard"
                      ).status_code == 404


def test_a_running_round_blocks_the_discard(app):
    from types import SimpleNamespace
    with TestClient(app, base_url="http://127.0.0.1") as c:
        chat = c.post("/api/chats", json={"participant_ids": []}).json()
        m = _msg(chat["id"])
        # A live round is registry state; fabricate a not-done entry rather
        # than racing a real task across test event loops.
        rounds._rounds[chat["id"]] = SimpleNamespace(done=False, round_id=1,
                                                     events=[], task=None)
        try:
            assert c.post(f"/api/chats/{chat['id']}/messages/{m['id']}/discard"
                          ).status_code == 409
        finally:
            rounds._rounds.clear()
        # after the round, the discard goes through
        assert c.post(f"/api/chats/{chat['id']}/messages/{m['id']}/discard"
                      ).status_code == 200
