"""The tool list holds still while availability changes (#564).

Every seat call starts with its tool list, and a prompt cache only reuses a
prompt whose start is exactly the same as last time. The list used to
change mid-chat: the summons tool went while a job was claimed, the memory
tools went when the memory probe failed, and an outside server's tools went
when it stopped answering. Each change made every seat write its whole
cached prompt again, and changing back did it twice.

The contract under test:

- the bytes of the tool list a seat is sent don't change when a summons is
  claimed, when memory is down, or when an outside server drops and comes
  back;
- a call to something unavailable right now gets a refusal that says what
  happened and what to do.
"""

import asyncio
import json
import sys
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from backend import db, engine, guest, guestjobs, mcp_client
from backend import tools as tools_mod
from backend.app import create_app
from backend.config import Settings
from backend.mcp_client import McpManager

FAKE = str(Path(__file__).parent / "fake_mcp.py")
SPEC = {"fake": {"command": sys.executable, "args": [FAKE]}}


@pytest.fixture(autouse=True)
def clean_state():
    guest._pending.clear()
    guestjobs._jobs.clear()
    yield
    guest._pending.clear()
    guestjobs._jobs.clear()


@pytest.fixture
def app(tmp_path):
    return create_app(Settings(data_dir=str(tmp_path / "data"),
                               memory_url="http://127.0.0.1:1",
                               code_repos={"demo": str(tmp_path)}))


class FakeMemory:
    def __init__(self, up):
        self.up = up

    async def probe(self, force=False):
        return self.up

    async def get_summary(self):
        return "Alex builds things."

    async def recall(self, query, limit=10, include_superseded=False,
                     origin="http", chat_id=None):
        return []

    def any_write_failed(self):
        return False


class FakeMcp:
    """What McpManager hands the round: its definitions, whether or not the
    server is up right now."""

    def __init__(self, defs):
        self.defs = defs

    def tool_definitions(self):
        return list(self.defs)

    def activity_label(self, server_name):
        return None


def _capture(seen):
    async def stream_reply(participant, roster, transcript, names, cfg,
                           project, chat_summary, voice_mode, tools=None,
                           memory=None):
        # the exact bytes the adapters project from, key order included
        seen.append({"tools": json.dumps(tools), "memory": memory,
                     "names": [t["name"] for t in tools or []]})
        yield ("text", "ok")
    return stream_reply


def _one_round(app, chat_id, memory=None, mcp=None):
    con = db.connect()
    con.execute("INSERT INTO messages(chat_id, speaker, content, created_at) "
                "VALUES(?, 'user', 'hello both', ?)", (chat_id, db.now()))
    con.commit()
    roster = db.get_chat_participants(con, chat_id)
    con.close()

    async def go():
        async for _ in engine.run_round(chat_id, roster, "claude",
                                        app.state.settings, memory=memory,
                                        mcp=mcp):
            pass
    asyncio.run(go())


def _chat(app, **flags):
    with TestClient(app, base_url="http://127.0.0.1") as c:
        chat_id = c.post("/api/chats", json={}).json()["id"]
        if flags:
            c.patch(f"/api/chats/{chat_id}", json=flags)
    return chat_id


def test_memory_going_down_leaves_the_list_as_it_was(app, monkeypatch):
    seen = []
    monkeypatch.setattr(engine.providers, "stream_reply", _capture(seen))
    chat_id = _chat(app)
    _one_round(app, chat_id, memory=FakeMemory(up=True))
    _one_round(app, chat_id, memory=FakeMemory(up=False))
    up, down = seen[0], seen[-1]
    assert "recall_memory" in up["names"]
    assert down["tools"] == up["tools"]
    # the memory client is withheld while it's down, so run_tool refuses
    assert up["memory"] is not None and down["memory"] is None
    out = asyncio.run(tools_mod.run_tool("recall_memory", {"query": "x"},
                                         app.state.settings.as_cfg(),
                                         memory=down["memory"]))
    assert out == tools_mod.MEMORY_DOWN


def test_a_claimed_summons_leaves_the_list_as_it_was(app, monkeypatch):
    seen = []
    monkeypatch.setattr(engine.providers, "stream_reply", _capture(seen))
    monkeypatch.setattr(engine.guest, "available", lambda cfg: True)
    chat_id = _chat(app, code_enabled=True)
    _one_round(app, chat_id)
    before = seen[-1]
    assert "summon_claude_code" in before["names"]
    # a job left running from an earlier round
    con = db.connect()
    row = db.insert_guest_job(con, chat_id, "look into the flaky test",
                              "demo", "investigate", "claude")
    con.close()
    job = guestjobs.GuestJob(row)
    guestjobs._jobs[job.id] = job
    assert guest.claimed(chat_id) is not None
    _one_round(app, chat_id)
    assert seen[-1]["tools"] == before["tools"]
    refused = guest.request(chat_id, {"task": "look again"},
                            app.state.settings.as_cfg(), requested_by="gpt")
    assert "already working on an earlier task" in refused
    assert "wait for it to report back" in refused


def test_an_outside_server_dropping_leaves_the_list_as_it_was(app,
                                                              monkeypatch):
    seen = []
    monkeypatch.setattr(engine.providers, "stream_reply", _capture(seen))
    defs = [{"name": "mcp__fake__echo", "description": "Echo (external)",
             "input_schema": {"type": "object", "properties": {}}}]
    chat_id = _chat(app)
    _one_round(app, chat_id, mcp=FakeMcp(defs))
    _one_round(app, chat_id, mcp=FakeMcp(defs))
    assert "mcp__fake__echo" in seen[0]["names"]
    assert seen[-1]["tools"] == seen[0]["tools"]


class _BrokenSession:
    async def call_tool(self, *a, **k):
        raise ConnectionError("pipe closed")


def test_the_manager_keeps_a_dropped_servers_tools(monkeypatch):
    """The real manager against the real fake server: a failed call marks
    the server down without touching the list, a call while it's down is
    refused with a reason, and the reconnect leaves the list byte for byte
    as it was."""
    monkeypatch.setattr(mcp_client, "RETRY_S", 0.2)

    async def go():
        mgr = McpManager(SPEC)
        task = asyncio.get_event_loop().create_task(mgr.run())
        try:
            for _ in range(100):
                if mgr.tool_definitions():
                    break
                await asyncio.sleep(0.1)
            before = json.dumps(mgr.tool_definitions())
            assert "mcp__fake__echo" in before
            mgr.sessions["fake"] = _BrokenSession()
            out = await tools_mod.run_tool(
                "mcp__fake__echo", {"text": "hi"},
                {"_mcp": mgr, "max_tool_output": 8000})
            assert out.startswith("Error: external server fake failed")
            assert json.dumps(mgr.tool_definitions()) == before
            assert mgr.status()["fake"] == {
                "connected": False, "tools": [],
                "error": mgr.status()["fake"]["error"]}
            # a call while it's down is refused, and says what to do
            mgr.sessions.pop("fake", None)
            out = await tools_mod.run_tool(
                "mcp__fake__echo", {"text": "hi"},
                {"_mcp": mgr, "max_tool_output": 8000})
            assert "disconnected right now" in out
            assert "rather than guessing" in out
            # the retry loop reconnects it; the list is what it was
            for _ in range(100):
                if "fake" in mgr.sessions:
                    break
                await asyncio.sleep(0.1)
            assert "fake" in mgr.sessions
            assert json.dumps(mgr.tool_definitions()) == before
            out = await tools_mod.run_tool(
                "mcp__fake__echo", {"text": "hi"},
                {"_mcp": mgr, "max_tool_output": 8000})
            assert out == "echo:hi"
        finally:
            await mgr.stop()
            await task
    asyncio.run(go())
