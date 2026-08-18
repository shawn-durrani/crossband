"""view_page screenshots on the tool row (#149): the worker captures a
viewport PNG, it rides the ordinary attachment store keyed to the assistant
message, and the tool event carries the link.

Pinned here:
1. The store half: a worker result carrying shot_b64 lands one attachment
   row (message_id NULL until the message exists) plus the PNG on disk, and
   parks the id on the per-round cfg side-channel; a broken capture stores
   nothing and never costs the view its text.
2. The engine half, end to end: the tool event claims its parked id, the
   SSE event carries it, the persisted assistant message links the
   attachment and the tool_events row records it.
3. The v21-to-v22 migration leaves existing tool rows fileless.

Keyless; the render worker is faked (real-Chromium coverage lives in
test_browse.py's live test, which CI runs)."""

import base64
import json
import sqlite3
import stat

import pytest
from fastapi.testclient import TestClient

from backend import browse, db, egress, engine, tools
from backend.app import create_app
from backend.config import Settings

# A real 1x1 PNG, so mime/size are honest.
PNG_1PX = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR4nGNgYGBg"
    "AAAABQABh6FO1AAAAABJRU5ErkJggg==")


@pytest.fixture
def app(tmp_path):
    settings = Settings(data_dir=str(tmp_path / "data"),
                        memory_url="http://127.0.0.1:1")
    return create_app(settings)


def _cfg(**over):
    c = Settings().as_cfg()
    c.update(over)
    return c


def _fake_worker(tmp_path, monkeypatch, body):
    w = tmp_path / "fake_worker.py"
    w.write_text(body)
    w.chmod(w.stat().st_mode | stat.S_IEXEC)
    monkeypatch.setattr(browse, "_WORKER", w)
    egress.set_proxy_url("http://127.0.0.1:1")
    return w


# ---------- the store half ----------

def test_view_page_stores_the_shot_and_parks_the_id(app, tmp_path, monkeypatch):
    with TestClient(app, base_url="http://127.0.0.1"):
        _fake_worker(tmp_path, monkeypatch, (
            "import json, sys\nsys.stdin.read()\n"
            "print(json.dumps({'final_url': 'https://site.test/page',"
            " 'title': 'T', 'text': 'body text', 'links': [],"
            f" 'shot_b64': {json.dumps(base64.b64encode(PNG_1PX).decode())}"
            "}))\n"))
        monkeypatch.setattr(tools, "_assert_public_url", lambda u: u)
        cfg = _cfg()
        out = tools.view_page({"url": "https://site.test/page"}, cfg)
        assert "body text" in out
        (entry,) = cfg["_tool_attachments"]
        assert entry["tool"] == "view_page"
        assert entry["url"] == "https://site.test/page"
        con = db.connect()
        try:
            row = con.execute("SELECT * FROM attachments WHERE id=?",
                              (entry["attachment_id"],)).fetchone()
        finally:
            con.close()
        assert row["message_id"] is None  # linked later, like an upload
        assert row["mime"] == "image/png" and row["size"] == len(PNG_1PX)
        assert row["filename"].startswith("view-site.test-")
        import os
        assert os.path.exists(os.path.join(db.ATTACH_DIR, row["stored_name"]))
    egress.set_proxy_url(None)


def test_a_missing_or_broken_shot_never_costs_the_view(app, tmp_path, monkeypatch):
    with TestClient(app, base_url="http://127.0.0.1"):
        _fake_worker(tmp_path, monkeypatch, (
            "import json, sys\nsys.stdin.read()\n"
            "print(json.dumps({'final_url': 'https://site.test/x',"
            " 'title': 'T', 'text': 'still readable', 'links': [],"
            " 'shot_b64': '%%%not-base64%%%'}))\n"))
        monkeypatch.setattr(tools, "_assert_public_url", lambda u: u)
        cfg = _cfg()
        out = tools.view_page({"url": "https://site.test/x"}, cfg)
        assert "still readable" in out
        assert not cfg.get("_tool_attachments")
        con = db.connect()
        try:
            n = con.execute("SELECT COUNT(*) c FROM attachments").fetchone()["c"]
        finally:
            con.close()
        assert n == 0
    egress.set_proxy_url(None)


# ---------- the engine half, end to end ----------

def test_round_links_the_shot_to_message_and_tool_row(app, monkeypatch):
    """A tool event whose file was parked on the round cfg ends up: on the
    SSE event, on the persisted tool_events row, and linked to the assistant
    message through the ordinary attachment path."""
    def plant(cfg):
        con = db.connect()
        try:
            cur = con.execute(
                "INSERT INTO attachments(message_id, filename, stored_name, "
                "mime, size, created_at) VALUES(NULL,?,?,?,?,?)",
                ("view-x.png", "stored-x.png", "image/png", 68, db.now()))
            con.commit()
            att_id = cur.lastrowid
        finally:
            con.close()
        cfg.setdefault("_tool_attachments", []).append(
            {"tool": "view_page", "url": "https://x.test/",
             "attachment_id": att_id})
        return att_id

    planted = {}

    async def stream_reply(participant, roster, transcript, names, cfg, project,
                           chat_summary, voice_mode, tools=None, memory=None):
        planted["id"] = plant(cfg)  # what run_tool would have done
        yield ("tool", {"tool": "view_page",
                        "input": {"url": "https://x.test/"},
                        "output": "Viewed: https://x.test/"})
        yield ("text", "seen it")

    monkeypatch.setattr(engine.providers, "stream_reply", stream_reply)
    with TestClient(app, base_url="http://127.0.0.1") as c:
        chat = c.post("/api/chats", json={}).json()
        with c.stream("POST", f"/api/chats/{chat['id']}/send",
                      json={"text": "@claude look at x.test"}) as r:
            body = "".join(r.iter_text())
        events = [json.loads(l[6:]) for l in body.splitlines()
                  if l.startswith("data: ")]
        acts = [e for e in events if e["type"] == "tool_activity"]
        assert acts and acts[0]["attachment_id"] == planted["id"]

        got = c.get(f"/api/chats/{chat['id']}").json()
        reply = next(m for m in got["messages"] if m["speaker"] == "claude")
        (ev,) = [t for t in reply["tool_events"]
                 if t["tool"] == "view_page"]
        assert ev["attachment_id"] == planted["id"]
        assert [a["id"] for a in reply["attachments"]] == [planted["id"]]


# ---------- schema migration (v21 -> v22) ----------

def test_v21_to_v22_migration_leaves_old_tool_rows_fileless(tmp_path):
    data = tmp_path / "data"
    data.mkdir()
    c = sqlite3.connect(data / "chat.db")
    c.executescript(
        "CREATE TABLE tool_events(id INTEGER PRIMARY KEY AUTOINCREMENT,"
        " message_id INTEGER NOT NULL, tool TEXT NOT NULL,"
        " input_json TEXT NOT NULL DEFAULT '{}',"
        " output_text TEXT NOT NULL DEFAULT '', created_at REAL NOT NULL);"
        "INSERT INTO tool_events(message_id, tool, created_at)"
        " VALUES(1, 'web_search', 0);")
    c.execute("PRAGMA user_version = 21")
    c.commit()
    c.close()
    db.configure(data)
    db.init()
    con = db.connect()
    try:
        assert (con.execute("PRAGMA user_version").fetchone()[0]
                == db.SCHEMA_VERSION)
        row = con.execute("SELECT * FROM tool_events").fetchone()
        assert row["attachment_id"] is None
    finally:
        con.close()
