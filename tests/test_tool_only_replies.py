"""A reply of tool calls and no words keeps its tool record (#575).

A seat that used a tool (a web search, a memory save, a GitHub lookup) and
then wrote nothing left no message, so the record of what its tools did
went with it: the chat showed the tool activity live and lost it on a
reload, and later turns never saw the results. The same went for a call
that failed, or was talked over, after its tools ran.

The contract under test:

- such a reply is saved as a message with no words, its tool events and
  its cost, and nothing goes to the seat_usage ledger for it;
- tool calls followed by a pass save the tools and never the pass;
- a call that fails or is cut off after its tools ran keeps them too;
- the models read the turn through its research log alone: the seat's
  own empty turn is never sent (the providers refuse one), and another
  seat's isn't sent as an empty line;
- a wordless turn doesn't count as a reply in the round note, and the
  echo guard never judges against it.
"""

import asyncio
import json
import time

import pytest
from fastapi.testclient import TestClient

from backend import db, echo, engine, providers, rounds
from backend.app import create_app
from backend.config import Settings

USAGE = {"input": 900, "cache_read": 0, "cache_creation": 0, "output": 30}
SEARCH = {"tool": "web_search", "input": {"query": "oak finishing oil"},
          "output": "Three results about oil finishes."}


@pytest.fixture
def app(tmp_path):
    return create_app(Settings(data_dir=str(tmp_path / "data"),
                               memory_url="http://127.0.0.1:1"))


def _round(c, chat_id, text):
    events = []
    with c.stream("POST", f"/api/chats/{chat_id}/send",
                  json={"text": text}) as r:
        for line in r.iter_lines():
            if line.startswith("data: "):
                events.append(json.loads(line[6:]))
    deadline = time.time() + 5
    while rounds.active(chat_id) is not None and time.time() < deadline:
        time.sleep(0.05)
    return events, c.get(f"/api/chats/{chat_id}").json()["messages"]


def _seat_rows(chat_id):
    con = db.connect()
    try:
        return [dict(r) for r in con.execute(
            "SELECT * FROM seat_usage WHERE chat_id=?", (chat_id,))]
    finally:
        con.close()


def _first_seat_uses_a_tool(text, seen=None):
    """The round's first seat runs a search and then writes `text`; the
    second writes a real reply. `seen` collects each call's round note."""
    async def stream_reply(participant, roster, transcript, names, cfg,
                           project, chat_summary, voice_mode, tools=None,
                           memory=None):
        if seen is not None:
            seen.append(list(cfg.get("round_predecessors") or []))
        if seen is None or len(seen) == 1:
            yield ("tool", dict(SEARCH))
            if text:
                yield ("text", text)
        else:
            yield ("text", "Danish oil suits oak best.")
        yield ("usage", dict(USAGE))
    return stream_reply


@pytest.mark.parametrize("text", ["", "[pass]", "Nothing to add. [pass]"])
def test_tool_calls_with_no_words_are_saved_with_them(app, monkeypatch, text):
    monkeypatch.setattr(engine.providers, "stream_reply",
                        _first_seat_uses_a_tool(text))
    with TestClient(app, base_url="http://127.0.0.1") as c:
        chat_id = c.post("/api/chats", json={}).json()["id"]
        events, msgs = _round(c, chat_id, "what oil for oak, anyone")
    seats = [m for m in msgs if m["speaker"] != "user"]
    assert seats, "the tool record was dropped"
    for m in seats:
        assert m["content"] == ""
        assert [(t["tool"], t["output_text"]) for t in m["tool_events"]] == [
            ("web_search", SEARCH["output"])]
        # the cost rides the message, as any reply's does
        assert json.loads(m["usage_json"])["cost"] > 0
    assert _seat_rows(chat_id) == []
    ends = [e for e in events if e["type"] == "speaker_end"]
    assert len(ends) == len(seats)
    assert not [e for e in events if e["type"] in ("passed", "error")]


def test_a_wordless_turn_is_not_named_in_the_round_note(app, monkeypatch):
    seen = []
    monkeypatch.setattr(engine.providers, "stream_reply",
                        _first_seat_uses_a_tool("", seen))
    with TestClient(app, base_url="http://127.0.0.1") as c:
        chat_id = c.post("/api/chats", json={}).json()["id"]
        _, msgs = _round(c, chat_id, "what oil for oak, anyone")
    assert seen == [[], []]
    assert [m["content"] for m in msgs[1:]] == ["", "Danish oil suits oak best."]


def _one_seat(app):
    with TestClient(app, base_url="http://127.0.0.1") as c:
        chat_id = c.post("/api/chats", json={}).json()["id"]
    con = db.connect()
    roster = db.get_chat_participants(con, chat_id)
    con.close()
    return chat_id, roster[:1]


def _stored(chat_id):
    con = db.connect()
    try:
        return db.get_chat_messages(con, chat_id)
    finally:
        con.close()


@pytest.mark.parametrize("text", ["", "[pa"])
def test_a_call_that_fails_after_its_tools_keeps_them(app, monkeypatch, text):
    async def failing(participant, roster, transcript, names, cfg, project,
                      chat_summary, voice_mode, tools=None, memory=None):
        yield ("tool", dict(SEARCH))
        if text:
            yield ("text", text)
        raise RuntimeError("upstream went away")

    monkeypatch.setattr(engine.providers, "stream_reply", failing)
    chat_id, seats = _one_seat(app)

    async def go():
        return [chunk async for chunk in engine.run_round(
            chat_id, seats, "gpt", app.state.settings, memory=None)]

    events = [json.loads(ch[6:]) for ch in asyncio.run(go())
              if ch.startswith("data: ")]
    stored = _stored(chat_id)
    assert [(m["content"], [t["tool"] for t in m["tool_events"]])
            for m in stored] == [("", ["web_search"])]
    assert [e["type"] for e in events].count("error") == 1


@pytest.mark.parametrize("text", ["", "[pa"])
def test_a_call_cut_off_after_its_tools_keeps_them(app, monkeypatch, text):
    async def hanging(participant, roster, transcript, names, cfg, project,
                      chat_summary, voice_mode, tools=None, memory=None):
        yield ("tool", dict(SEARCH))
        if text:
            yield ("text", text)
        await asyncio.sleep(3600)

    monkeypatch.setattr(engine.providers, "stream_reply", hanging)
    chat_id, seats = _one_seat(app)
    last = '"delta"' if text else '"tool_activity"'

    async def go():
        gen = engine.run_round(chat_id, seats, "gpt", app.state.settings,
                               memory=None)
        async for chunk in gen:
            if last in chunk:
                break
        await gen.aclose()

    asyncio.run(go())
    stored = _stored(chat_id)
    assert [(m["content"], [t["tool"] for t in m["tool_events"]])
            for m in stored] == [("[cut off by User]", ["web_search"])]


def _wordless(mid, speaker, attachments=None):
    return {"id": mid, "chat_id": 1, "speaker": speaker, "content": "",
            "created_at": 1751600000.0, "usage_json": None,
            "attachments": attachments or [],
            "tool_events": [{"tool": "web_search",
                             "input_json": json.dumps(SEARCH["input"]),
                             "output_text": SEARCH["output"]}]}


def _texts(content):
    if isinstance(content, str):
        return [content]
    return [b.get("text") for b in content if b.get("text") is not None]


@pytest.mark.parametrize("build", [providers.build_anthropic_messages,
                                   providers.build_openai_input])
def test_the_models_read_a_wordless_turn_as_its_research_log(build, cfg,
                                                              names):
    user = {"id": 1, "chat_id": 1, "speaker": "user", "content": "oak oil?",
            "created_at": 1751600000.0, "usage_json": None,
            "attachments": [], "tool_events": []}
    transcript = [user, _wordless(2, "claude")]
    for self_slug in ("claude", "gpt"):
        out = build(self_slug, transcript, names, cfg)
        texts = [t for item in out for t in _texts(item["content"])]
        assert not [item for item in out if item["role"] == "assistant"]
        assert all(t.strip() for t in texts)
        assert any("[research log] Claude" in t and SEARCH["output"] in t
                   for t in texts)
        assert not any(t.startswith("[Claude") for t in texts)


def test_another_seats_wordless_turn_with_a_file_still_shows_it(cfg, names):
    turn = _wordless(2, "claude", attachments=[{"id": 1}])
    assert not providers._wordless_seat_turn(turn, "gpt")
    assert providers._wordless_seat_turn(turn, "claude")


def test_the_echo_guard_judges_against_the_last_turn_with_words():
    said = {"id": 1, "speaker": "claude",
            "content": "Danish oil suits oak best, three coats a day apart."}
    refs = echo.references_for([said, _wordless(2, "claude")], "claude",
                               {"claude", "gpt"}, {"claude": "Claude"})
    assert refs == [(echo.OWN_LABEL, said["content"], "claude")]
