"""Provider-export importer: Claude + ChatGPT exports become chats, idempotent
by import_uuid with incremental merge, and Membro is seeded through the
contract - ingest everything, mine per chat with the summary rebuild deferred
to ONE final call. Long-term recall works from day one."""

import asyncio
import json

import pytest

from backend import db, importer
from backend.app import create_app
from backend.config import Settings

CLAUDE_EXPORT = [
    {"uuid": "c-1", "name": "Woodworking plans", "created_at": "2025-11-02T10:00:00Z",
     "updated_at": "2025-11-02T11:00:00Z",
     "chat_messages": [
         {"uuid": "m-1", "sender": "human", "created_at": "2025-11-02T10:00:00Z",
          "text": "", "content": [{"type": "text", "text": "I want to build a workbench for my shop at 42 Foundry Lane."}]},
         {"uuid": "m-2", "sender": "assistant", "created_at": "2025-11-02T10:01:00Z",
          "text": "fallback", "content": [{"type": "text", "text": "Great — start with the top."}]},
     ]},
    {"uuid": "c-2", "name": "", "created_at": "2025-12-01T09:00:00Z",
     "updated_at": "2025-12-01T09:30:00Z",
     "chat_messages": [
         {"uuid": "m-3", "sender": "human", "created_at": "2025-12-01T09:00:00Z",
          "text": "Untitled chat first line becomes the title", "content": []},
     ]},
]

CHATGPT_EXPORT = [{
    "conversation_id": "g-1", "title": "Trip planning",
    "create_time": 1730500000, "update_time": 1730503600,
    "current_node": "n-2",
    "mapping": {
        "n-1": {"id": "n-1", "parent": None,
                "message": {"id": "n-1", "create_time": 1730500000,
                            "author": {"role": "user"},
                            "content": {"content_type": "text", "parts": ["Plan me a Japan trip"]}}},
        "n-2": {"id": "n-2", "parent": "n-1",
                "message": {"id": "n-2", "create_time": 1730500100,
                            "author": {"role": "assistant"},
                            "content": {"content_type": "text", "parts": ["Start in Tokyo."]}}},
    },
}]


class FakeMemory:
    def __init__(self, reachable=True):
        self.reachable = reachable
        self.ingested = []       # (conversation_id, n_messages)
        self.distilled = []      # (conversation_id, regenerate)
        self.saved_facts = []
        self.summary_rebuilds = 0

    async def probe(self, force=False):
        return self.reachable

    async def ingest(self, conversation_id, messages):
        self.ingested.append((conversation_id, len(messages)))
        return {"ingested": len(messages), "skipped": 0, "attached": 0}

    async def save_fact(self, content, origin_agent, **kw):
        self.saved_facts.append((content, origin_agent))
        return {"id": 1, "quarantined": False}

    async def distill_and_wait(self, conversation_id, regenerate=True):
        self.distilled.append((conversation_id, regenerate))
        return {"status": "ok"}

    async def regenerate_summary_and_wait(self):
        self.summary_rebuilds += 1
        return {"status": "ok"}


@pytest.fixture
def app(tmp_path):
    settings = Settings(data_dir=str(tmp_path / "data"),
                        memory_url="http://127.0.0.1:1")
    return create_app(settings)  # initializes the DB/schema for db.connect()


def _run(data, filename, memory, mine=True):
    async def go():
        return [ev async for ev in importer.import_stream(data, filename, memory, mine=mine)]
    return asyncio.run(go())


def test_claude_import_seeds_memory(app):
    mem = FakeMemory()
    events = _run(json.dumps(CLAUDE_EXPORT).encode(), "conversations.json", mem)
    done = events[-1]
    assert done["type"] == "done" and done["format"] == "claude"
    assert done["chats"] == 2 and done["messages"] == 3
    # everything reached Membro; mining deferred the summary to ONE rebuild
    assert len(mem.ingested) == 2 and done["ingested"] == 3
    assert all(regen is False for _, regen in mem.distilled)
    assert mem.summary_rebuilds == 1
    # local side: chats exist, titles resolved, watermark advanced
    con = db.connect()
    chats = [dict(r) for r in con.execute("SELECT * FROM chats ORDER BY id")]
    assert chats[0]["title"] == "Woodworking plans"
    assert chats[1]["title"].startswith("Untitled chat first line")
    assert all(c["ingested_upto"] > 0 for c in chats)
    assert all(c["title_upto"] == -1 for c in chats)  # imported titles are locked
    con.close()


def test_reimport_is_idempotent_and_merges_growth(app):
    mem = FakeMemory()
    _run(json.dumps(CLAUDE_EXPORT).encode(), "conversations.json", mem, mine=False)
    # unchanged re-import: nothing new anywhere
    events = _run(json.dumps(CLAUDE_EXPORT).encode(), "conversations.json", mem, mine=False)
    done = events[-1]
    assert done["chats"] == 0 and done["messages"] == 0 and done["skipped_existing"] == 2
    # a newer export where c-1 grew by one message: only the delta lands
    grown = json.loads(json.dumps(CLAUDE_EXPORT))
    grown[0]["chat_messages"].append(
        {"uuid": "m-9", "sender": "human", "created_at": "2025-11-03T10:00:00Z",
         "text": "One more question about the vise.", "content": []})
    done2 = _run(json.dumps(grown).encode(), "conversations.json", mem, mine=False)[-1]
    assert done2["updated_chats"] == 1 and done2["messages"] == 1


def test_chatgpt_format_and_memory_absent(app):
    mem = FakeMemory(reachable=False)
    events = _run(json.dumps(CHATGPT_EXPORT).encode(), "conversations.json", mem)
    done = events[-1]
    assert done["format"] == "chatgpt" and done["chats"] == 1 and done["messages"] == 2
    assert done["ingested"] == 0 and mem.distilled == []
    assert any(ev["type"] == "note" for ev in events)  # said so, honestly
    con = db.connect()
    speakers = [r["speaker"] for r in con.execute(
        "SELECT speaker FROM messages ORDER BY id")]
    con.close()
    assert speakers == ["user", "gpt"]


def test_garbage_upload_reports_cleanly(app):
    events = _run(b"not an export", "whatever.json", FakeMemory())
    assert events[-1]["type"] == "error"
