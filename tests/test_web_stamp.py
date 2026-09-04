"""Web provenance, crossband side (#138 slice 4): the untrusted marker above
page content, the round's domain stamp, its persistence on assistant rows,
and the wire fields membro's contract 1.3 holds facts on."""

import asyncio
import json

import httpx
import pytest

from backend import db, egress, memory_client, tools
from backend.config import Settings


# ---------- the untrusted marker ----------

def test_marker_names_the_host():
    m = tools._untrusted_marker("https://a.example/deep/page?q=1")
    assert "a.example" in m
    assert "Untrusted web content" in m


def test_fetch_page_output_carries_the_marker(monkeypatch):
    class _FakeStream:
        def __init__(self, resp):
            self._resp = resp

        def __enter__(self):
            return self._resp

        def __exit__(self, *exc):
            return False

    monkeypatch.setattr(egress.socket, "getaddrinfo",
                        lambda host, port: [(2, 1, 6, "", ("93.184.216.34", 0))])
    monkeypatch.setattr(tools.httpx, "stream", lambda *a, **kw: _FakeStream(
        httpx.Response(200, content=b"<html><body>plain page</body></html>",
                       headers={"content-type": "text/html"},
                       request=httpx.Request("GET", "https://site.example/p"))))
    out = tools.fetch_page({"url": "https://site.example/p"}, Settings().as_cfg())
    assert out.startswith("Fetched: https://site.example/p")
    assert "[Untrusted web content from site.example." in out
    assert "plain page" in out


# ---------- the round's domain stamp ----------

def _cfg(**over):
    c = {"chat_id": 1, "_round_tool_texts": [], "_round_web_domains": set()}
    c.update(over)
    return c


def test_run_tool_collects_domains_per_web_tool(monkeypatch):
    monkeypatch.setitem(tools._RESEARCH_TOOLS, "fetch_page",
                        lambda args, cfg: "Fetched: ok")
    monkeypatch.setitem(tools._RESEARCH_TOOLS, "web_search",
                        lambda args, cfg: "[Tavily]\n1. r")
    from backend import url_ledger
    monkeypatch.setattr(url_ledger, "check", lambda *a, **k: None)
    cfg = _cfg()
    asyncio.run(tools.run_tool("fetch_page", {"url": "https://Docs.Example/g"}, cfg))
    asyncio.run(tools.run_tool("web_search", {"query": "x"}, cfg))
    assert cfg["_round_web_domains"] == {"docs.example", "web-search"}


def test_tool_errors_stamp_nothing(monkeypatch):
    monkeypatch.setitem(tools._RESEARCH_TOOLS, "fetch_page",
                        lambda args, cfg: "Error: nope")
    from backend import url_ledger
    monkeypatch.setattr(url_ledger, "check", lambda *a, **k: None)
    cfg = _cfg()
    asyncio.run(tools.run_tool("fetch_page", {"url": "https://evil.example/x"}, cfg))
    assert cfg["_round_web_domains"] == set()


# ---------- persistence on the assistant row ----------

def test_insert_message_persists_the_stamp(tmp_path):
    db.configure(str(tmp_path / "data"))
    db.init(Settings(data_dir=str(tmp_path / "data")))
    con = db.connect()
    try:
        con.execute("INSERT INTO chats(id, title, created_at, updated_at) "
                    "VALUES (1, 't', 0, 0)")
        con.commit()
        msg = db.insert_message(con, 1, "claude-x", "a reply", notify=False,
                                web_sources={"b.example", "a.example"})
        row = con.execute("SELECT web_sources FROM messages WHERE id=?",
                          (msg["id"],)).fetchone()
        plain = db.insert_message(con, 1, "claude-x", "another", notify=False)
        prow = con.execute("SELECT web_sources FROM messages WHERE id=?",
                           (plain["id"],)).fetchone()
    finally:
        con.close()
    assert json.loads(row["web_sources"]) == ["a.example", "b.example"]  # sorted
    assert prow["web_sources"] == ""


# ---------- the wire ----------

class _FakePost:
    def __init__(self):
        self.bodies = []

    async def post(self, url, json=None, **kw):
        self.bodies.append((url, json))
        return httpx.Response(200, json={"ingested": 1, "skipped": 0,
                                         "attached": 0, "id": 1,
                                         "quarantined": True},
                              request=httpx.Request("POST", url))


def _client(contract="1.3"):
    mc = memory_client.MemoryClient()
    mc._contract_version = contract
    fake = _FakePost()
    mc._client = fake

    async def _probe(force=False):
        return True
    mc.probe = _probe
    return mc, fake


def test_ingest_sends_the_stamp_from_the_row():
    mc, fake = _client()
    asyncio.run(mc.ingest("7", [
        {"id": 1, "speaker": "claude-x", "content": "web-informed reply",
         "created_at": 1755300000.0, "web_sources": '["a.example"]'},
        {"id": 2, "speaker": "user", "content": "plain",
         "created_at": 1755300001.0, "web_sources": ""},
    ]))
    (_, payload), = fake.bodies
    assert payload["messages"][0]["web_sources"] == ["a.example"]
    assert "web_sources" not in payload["messages"][1]


def test_save_fact_sends_the_stamp():
    mc, fake = _client()
    asyncio.run(mc.save_fact("a claim from the web", "claude-x",
                             web_sources=["a.example"]))
    (_, payload), = fake.bodies
    assert payload["web_sources"] == ["a.example"]


def test_old_contract_warns_once(caplog):
    mc, fake = _client(contract="1.2")
    with caplog.at_level("WARNING"):
        asyncio.run(mc.save_fact("first", "claude-x", web_sources=["a.example"]))
        asyncio.run(mc.save_fact("second", "claude-x", web_sources=["a.example"]))
    hits = [r for r in caplog.records if "predates web_sources" in r.message]
    assert len(hits) == 1
    # The stamp still rides: ignoring it is the service's (baseline) call.
    assert all(b[1].get("web_sources") for b in fake.bodies)


def test_current_contract_stays_quiet(caplog):
    mc, _ = _client(contract="1.3")
    with caplog.at_level("WARNING"):
        asyncio.run(mc.save_fact("fine", "claude-x", web_sources=["a.example"]))
    assert not [r for r in caplog.records if "predates" in r.message]


# ---------- the guest stamp on the wire (contract 1.5) ----------

def test_save_fact_sends_guest_speakers_only_when_present():
    mc, fake = _client(contract="1.5")
    asyncio.run(mc.save_fact("a claim heard in the room", "claude-x",
                             guest_speakers=["guest:Sam", "guest:unknown"]))
    asyncio.run(mc.save_fact("the owner alone", "claude-x",
                             guest_speakers=[]))
    asyncio.run(mc.save_fact("no stamp given", "claude-x"))
    (_, stamped), (_, empty), (_, absent) = fake.bodies
    assert stamped["guest_speakers"] == ["guest:Sam", "guest:unknown"]
    assert "guest_speakers" not in empty
    assert "guest_speakers" not in absent
    # SOURCE_APP is untouched by the new field
    assert stamped["source_app"] == memory_client.SOURCE_APP


def test_save_fact_caps_guest_speakers_at_twelve():
    mc, fake = _client(contract="1.5")
    many = [f"guest:G{i}" for i in range(20)]
    asyncio.run(mc.save_fact("a crowded room", "claude-x", guest_speakers=many))
    (_, payload), = fake.bodies
    assert payload["guest_speakers"] == many[:memory_client.MAX_GUEST_SPEAKERS]
    assert memory_client.MAX_GUEST_SPEAKERS == 12


def test_guest_stamp_on_a_1_4_service_warns_once_and_still_sends(caplog):
    mc, fake = _client(contract="1.4")
    with caplog.at_level("WARNING"):
        asyncio.run(mc.save_fact("first", "claude-x",
                                 guest_speakers=["guest:Sam"]))
        asyncio.run(mc.save_fact("second", "claude-x",
                                 guest_speakers=["guest:Sam"]))
    hits = [r for r in caplog.records if "predates guest_speakers" in r.message]
    assert len(hits) == 1
    # The stamp still rides: ignoring it is the older service's call.
    assert all(b[1].get("guest_speakers") for b in fake.bodies)


def test_guest_stamp_on_a_1_5_service_stays_quiet(caplog):
    mc, _ = _client(contract="1.5")
    with caplog.at_level("WARNING"):
        asyncio.run(mc.save_fact("fine", "claude-x",
                                 guest_speakers=["guest:Sam"]))
    assert not [r for r in caplog.records if "predates" in r.message]


# ---------- the row-to-wire seam ----------

class _FakeHandoffHttp:
    """Healthy service, every POST recorded - the leave hook needs get()
    for the probe and post() for /ingest and /distill."""

    def __init__(self):
        self.posts = []

    async def get(self, url):
        req = httpx.Request("GET", url)
        return httpx.Response(200, json={"status": "ok",
                                         "contract_version": "1.3"},
                              request=req)

    async def post(self, url, json=None):
        self.posts.append((url, json))
        req = httpx.Request("POST", url)
        return httpx.Response(200, json={"ok": True, "ingested": 1,
                                         "skipped": 0, "attached": 0},
                              request=req)


def test_handoff_carries_the_stamp_from_row_to_wire(tmp_path):
    """End to end across the seam that broke: a stamped ROW must arrive at
    /ingest still stamped. The row write and the payload mapper each had a
    test; the query between them selected a fixed column list and silently
    dropped the stamp, so every mined fact skipped the web-derived hold."""
    from backend import engine
    from backend.app import create_app

    create_app(Settings(data_dir=str(tmp_path / "data"),
                        memory_url="http://127.0.0.1:1"))
    con = db.connect()
    cur = con.execute(
        "INSERT INTO chats(title, created_at, updated_at) VALUES('t', 0, 0)")
    con.commit()
    chat_id = cur.lastrowid
    stamped = db.insert_message(con, chat_id, "claude-x", "web-informed",
                                notify=False, web_sources={"a.example"})
    plain = db.insert_message(con, chat_id, "user", "typed", notify=False)
    con.close()

    mc = memory_client.MemoryClient(base_url="http://127.0.0.1:1")
    fake = _FakeHandoffHttp()
    mc._client = fake
    asyncio.run(engine.leave_chat_job(chat_id, {"user_name": "Owner"}, mc))

    ingests = [body for url, body in fake.posts if url.endswith("/ingest")]
    assert len(ingests) == 1
    by_id = {int(m["external_id"]): m for m in ingests[0]["messages"]}
    assert by_id[stamped["id"]]["web_sources"] == ["a.example"]
    assert "web_sources" not in by_id[plain["id"]]
