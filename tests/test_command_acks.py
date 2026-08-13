"""The slash-command dead-man and its ack contract (#58).

Crossband stores slash commands and assigns them no meaning; an external
producer consumes them. Before this, a stopped producer was
indistinguishable from a working one - both looked like silence. The
contract under test:

- The producer acks each command it reads (notice route, `ack_command_id`);
  the ack is strict (a user message in THIS chat, nothing else).
- A stored command nobody acks inside `slash_ack_timeout_s` produces ONE
  system line saying nothing picked it up.
- An acked command produces no warning; the command's meaning stays outside
  this repo either way.
- A restart inside the window re-arms the timer (startup sweep).
"""

import time

import pytest
from fastapi.testclient import TestClient

from backend import db
from backend.app import create_app
from backend.config import Settings
from backend.routers import chats as chats_router

TIMEOUT = 0.15


@pytest.fixture
def app(tmp_path):
    return create_app(Settings(data_dir=str(tmp_path / "data"),
                               memory_url="http://127.0.0.1:1",
                               slash_ack_timeout_s=TIMEOUT))


def _messages(chat_id):
    con = db.connect()
    try:
        return [dict(r) for r in con.execute(
            "SELECT speaker, content FROM messages WHERE chat_id=? ORDER BY id",
            (chat_id,))]
    finally:
        con.close()


def _wait_for(pred, timeout=3.0, interval=0.02):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        v = pred()
        if v:
            return v
        time.sleep(interval)
    return pred()


def _warnings(chat_id):
    return [m for m in _messages(chat_id)
            if m["speaker"] == "system" and "acknowledged" in m["content"]]


def _send_slash(client, chat_id, text="/deploy example #1"):
    r = client.post(f"/api/chats/{chat_id}/send", json={"text": text})
    assert r.status_code == 200
    con = db.connect()
    try:
        return con.execute(
            "SELECT id FROM messages WHERE chat_id=? AND speaker='user' "
            "ORDER BY id DESC", (chat_id,)).fetchone()["id"]
    finally:
        con.close()


def test_unacked_command_warns_once(app):
    with TestClient(app, base_url="http://127.0.0.1") as c:
        chat = c.post("/api/chats", json={"participant_ids": []}).json()
        _send_slash(c, chat["id"])
        warnings = _wait_for(lambda: _warnings(chat["id"]))
        assert len(warnings) == 1
        time.sleep(TIMEOUT * 2)             # nothing further fires
        assert len(_warnings(chat["id"])) == 1


def test_acked_command_never_warns(app):
    with TestClient(app, base_url="http://127.0.0.1") as c:
        chat = c.post("/api/chats", json={"participant_ids": []}).json()
        msg_id = _send_slash(c, chat["id"])
        r = c.post(f"/api/chats/{chat['id']}/notice",
                   json={"text": "⏳ request received",
                         "ack_command_id": msg_id})
        assert r.status_code == 200
        time.sleep(TIMEOUT * 3)
        assert _warnings(chat["id"]) == []


def test_ack_is_strict_about_chat_and_speaker(app):
    with TestClient(app, base_url="http://127.0.0.1") as c:
        chat = c.post("/api/chats", json={"participant_ids": []}).json()
        other = c.post("/api/chats", json={"participant_ids": []}).json()
        msg_id = _send_slash(c, chat["id"])
        # right id, wrong chat
        r = c.post(f"/api/chats/{other['id']}/notice",
                   json={"text": "x", "ack_command_id": msg_id})
        assert r.status_code == 400
        # a system message is not ackable
        con = db.connect()
        sys_msg = db.insert_message(con, chat["id"], "system", "not a command")
        con.close()
        r = c.post(f"/api/chats/{chat['id']}/notice",
                   json={"text": "x", "ack_command_id": sys_msg["id"]})
        assert r.status_code == 400
        # an ack without the field is a plain notice, unchanged behaviour
        r = c.post(f"/api/chats/{chat['id']}/notice", json={"text": "plain"})
        assert r.status_code == 200


def test_restart_inside_the_window_still_warns(app, tmp_path):
    """The startup sweep: a slash command stored just before a restart is
    re-armed by the next boot's lifespan and still warns."""
    with TestClient(app, base_url="http://127.0.0.1") as c:
        chat = c.post("/api/chats", json={"participant_ids": []}).json()
        con = db.connect()
        db.insert_message(con, chat["id"], "user", "/deploy example #2")
        con.close()
        # No arm happened (the message bypassed /send, as if the process
        # died right after persisting it). Entering a new lifespan sweeps.
    settings = Settings(data_dir=str(tmp_path / "data"),
                        memory_url="http://127.0.0.1:1",
                        slash_ack_timeout_s=TIMEOUT)
    with TestClient(create_app(settings), base_url="http://127.0.0.1") as c2:
        warnings = _wait_for(lambda: _warnings(chat["id"]))
        assert len(warnings) == 1


def test_feature_off_means_no_timer(tmp_path):
    app = create_app(Settings(data_dir=str(tmp_path / "data"),
                              memory_url="http://127.0.0.1:1",
                              slash_ack_timeout_s=0))
    with TestClient(app, base_url="http://127.0.0.1") as c:
        chat = c.post("/api/chats", json={"participant_ids": []}).json()
        _send_slash(c, chat["id"])
        time.sleep(0.3)
        assert _warnings(chat["id"]) == []
        assert not chats_router._DEADMAN_TASKS
