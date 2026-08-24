"""#211 wiring: audit findings ride the provider stream as an ("audit", ...)
event, persist on the message row through the one insert path, and reach the
API for the row's quiet chip. The finding CONTENT lives only in the owner's
own database and API - the log side stays content-free (see
test_attribution_audit.py)."""

import json
import time

import pytest
from fastapi.testclient import TestClient

from backend import engine, rounds
from backend.app import create_app
from backend.config import Settings

FINDING = {"kind": "attribution", "who": "Claude",
           "claim": "the deploy was reverted overnight"}


@pytest.fixture
def app(tmp_path):
    return create_app(Settings(data_dir=str(tmp_path / "data"),
                               memory_url="http://127.0.0.1:1"))


def scripted_with_audit(calls):
    async def stream_reply(participant, roster, transcript, names, cfg,
                           project, chat_summary, voice_mode, tools=None,
                           memory=None):
        calls.append(participant["slug"])
        if len(calls) == 1:
            yield ("text", "Claude said the deploy was reverted overnight.")
            yield ("audit", [FINDING])
        else:
            yield ("text", "Nothing to add beyond checking the deploy log.")
    return stream_reply


def _round(c, chat_id, text):
    with c.stream("POST", f"/api/chats/{chat_id}/send",
                  json={"text": text}) as r:
        for _ in r.iter_lines():
            pass
    deadline = time.time() + 5
    while rounds.active(chat_id) is not None and time.time() < deadline:
        time.sleep(0.05)
    return c.get(f"/api/chats/{chat_id}").json()["messages"]


def test_findings_persist_on_the_row_and_reach_the_api(app, monkeypatch):
    calls = []
    monkeypatch.setattr(engine.providers, "stream_reply",
                        scripted_with_audit(calls))
    with TestClient(app, base_url="http://127.0.0.1") as c:
        chat_id = c.post("/api/chats", json={}).json()["id"]
        msgs = _round(c, chat_id, "how did the deploy go?")
        flagged = msgs[1]
        assert json.loads(flagged["audit_flags"]) == [FINDING]
        # the clean reply carries no flags, and the column defaults empty
        assert msgs[2]["audit_flags"] == ""
        assert msgs[0]["audit_flags"] == ""  # the user turn too
