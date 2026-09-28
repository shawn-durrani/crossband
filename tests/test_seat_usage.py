"""A seat call that leaves no message still reaches the ledger (#560).

A [pass] is hidden from the chat on purpose (#98), and a refused pass or a
refused restatement throws its first try away (#98, #210). Each of those was
a paid model call, and the app only recorded cost on a saved message, so the
Spend page, the chat's running cost and the prompt cache numbers left them
all out. Measured on the owner's install, that was about a third of Claude
seat calls.

The contract under test:

- each such call writes one seat_usage row: the seat, what became of the
  call, and the priced usage block a message would have carried;
- the row is content-free, and nothing about it reaches the chat;
- accounting counts it as the model turn it was, in every total, and says
  how much of the window went on calls the chat never shows;
- a failure to record never breaks the round.
"""

import json
import sqlite3
import time

import pytest
from fastapi.testclient import TestClient

from backend import accounting, db, engine, rounds
from backend.app import create_app
from backend.config import Settings

USAGE = {"input": 1200, "cache_read": 9000, "cache_creation": 300,
         "output": 4,
         "cache_prefix": {"model": "m", "tools": "0" * 16, "stable": "1" * 16,
                          "volatile": "2" * 16, "transcript": "3" * 16,
                          "changed": ["volatile", "transcript"]}}

# Invented replies for the echo guard: the second restates the first.
OPENER = (
    "Sourdough rises best in a warm spot: aim for about 26 degrees, feed the "
    "starter twice the day before, and let the shaped loaf proof in the "
    "fridge overnight so the crumb opens and the crust blisters in the oven. "
    "Bake it in a lidded pot for twenty minutes, then uncovered until dark."
)
FRESH = (
    "Nobody mentioned the flour: a strong bread flour with twelve percent "
    "protein holds the gas far better than plain flour, and a spoon of rye "
    "in the starter wakes it up faster on a cold morning."
)

# What a seat_usage row's usage block may hold. Counts, prices, provenance
# and fingerprints only: a key outside this set is a new field someone must
# check for content before it lands here.
CONTENT_FREE_KEYS = {"input", "cache_read", "cache_creation", "output",
                     "cache_prefix", "model", "stepped_from", "cost",
                     "cost_provenance"}


@pytest.fixture
def app(tmp_path):
    return create_app(Settings(data_dir=str(tmp_path / "data"),
                               memory_url="http://127.0.0.1:1"))


def scripted(fn, calls):
    """fn(call_index, cfg) -> reply text. Each call streams its text and then
    a usage block, as the real adapters do."""
    async def stream_reply(participant, roster, transcript, names, cfg,
                           project, chat_summary, voice_mode, tools=None,
                           memory=None):
        calls.append({"slug": participant["slug"]})
        yield ("text", fn(len(calls) - 1, cfg))
        yield ("usage", json.loads(json.dumps(USAGE)))
    return stream_reply


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
    msgs = c.get(f"/api/chats/{chat_id}").json()["messages"]
    return events, msgs


def _seat_rows(chat_id):
    con = db.connect()
    try:
        return [dict(r) for r in con.execute(
            "SELECT * FROM seat_usage WHERE chat_id=? ORDER BY id",
            (chat_id,))]
    finally:
        con.close()


def test_a_pass_is_recorded_without_a_message(app, monkeypatch):
    calls = []
    monkeypatch.setattr(engine.providers, "stream_reply", scripted(
        lambda i, cfg: "Something real to add." if i == 0 else "[pass]",
        calls))
    with TestClient(app, base_url="http://127.0.0.1") as c:
        chat_id = c.post("/api/chats", json={}).json()["id"]
        _, msgs = _round(c, chat_id, "a note for the room, no question")
        # the chat still shows only the real reply
        assert [m["content"] for m in msgs[1:]] == ["Something real to add."]
        rows = _seat_rows(chat_id)
        assert [(r["speaker"], r["outcome"]) for r in rows] == [
            (calls[1]["slug"], "pass")]
        u = json.loads(rows[0]["usage_json"])
        # priced and stamped exactly as a reply's usage is
        assert u["input"] == 1200 and u["output"] == 4
        assert u["cost"] is not None and u["cost"] > 0
        assert u["cost_provenance"]["source"] == "rate_card_estimate"


def test_the_row_holds_no_text(app, monkeypatch):
    monkeypatch.setattr(engine.providers, "stream_reply", scripted(
        lambda i, cfg: "[pass]", []))
    with TestClient(app, base_url="http://127.0.0.1") as c:
        chat_id = c.post("/api/chats", json={}).json()["id"]
        _round(c, chat_id, "@gpt are you there?")
        rows = _seat_rows(chat_id)
        assert rows
        for r in rows:
            assert set(r) == {"id", "chat_id", "speaker", "outcome",
                              "usage_json", "created_at"}
            assert set(json.loads(r["usage_json"])) <= CONTENT_FREE_KEYS
            assert "[pass]" not in r["usage_json"]


def test_a_refused_pass_records_its_first_try(app, monkeypatch):
    calls = []
    monkeypatch.setattr(engine.providers, "stream_reply", scripted(
        lambda i, cfg: "Alright, my honest take."
        if cfg.get("pass_refused") else "[pass]", calls))
    with TestClient(app, base_url="http://127.0.0.1") as c:
        chat_id = c.post("/api/chats", json={}).json()["id"]
        _, msgs = _round(c, chat_id, "@gpt give us your take on this")
        assert [m["speaker"] for m in msgs[1:]] == ["gpt"]
        # the retry is a message with its own usage; the refused first try
        # is a seat_usage row, so the seat's two calls are both counted
        assert json.loads(msgs[-1]["usage_json"])["input"] == 1200
        assert [(r["speaker"], r["outcome"]) for r in _seat_rows(chat_id)] \
            == [("gpt", "pass_retried")]


def test_a_seat_that_insists_records_both_calls(app, monkeypatch):
    monkeypatch.setattr(engine.providers, "stream_reply", scripted(
        lambda i, cfg: "[pass]", []))
    with TestClient(app, base_url="http://127.0.0.1") as c:
        chat_id = c.post("/api/chats", json={}).json()["id"]
        _, msgs = _round(c, chat_id, "@gpt are you there?")
        assert len(msgs) == 1
        assert [r["outcome"] for r in _seat_rows(chat_id)] == [
            "pass_retried", "pass"]


def test_a_refused_restatement_records_its_first_try(app, monkeypatch):
    calls = []
    monkeypatch.setattr(engine.providers, "stream_reply", scripted(
        lambda i, cfg: FRESH if cfg.get("echo_refused") else OPENER, calls))
    with TestClient(app, base_url="http://127.0.0.1") as c:
        chat_id = c.post("/api/chats", json={}).json()["id"]
        _, msgs = _round(c, chat_id, "a note for the room, no question")
        assert [m["content"] for m in msgs[1:]] == [OPENER, FRESH]
        assert [(r["speaker"], r["outcome"]) for r in _seat_rows(chat_id)] \
            == [(calls[1]["slug"], "echo_retried")]


def test_a_dropped_restatement_is_recorded(app, monkeypatch):
    monkeypatch.setattr(engine.providers, "stream_reply", scripted(
        lambda i, cfg: OPENER, []))
    with TestClient(app, base_url="http://127.0.0.1") as c:
        chat_id = c.post("/api/chats", json={}).json()["id"]
        _, msgs = _round(c, chat_id, "a note for the room, no question")
        assert [m["content"] for m in msgs[1:]] == [OPENER]
        assert [r["outcome"] for r in _seat_rows(chat_id)] == [
            "echo_retried", "echo_dropped"]


def test_an_empty_reply_is_recorded(app, monkeypatch):
    monkeypatch.setattr(engine.providers, "stream_reply", scripted(
        lambda i, cfg: "", []))
    with TestClient(app, base_url="http://127.0.0.1") as c:
        chat_id = c.post("/api/chats", json={}).json()["id"]
        _, msgs = _round(c, chat_id, "hello both")
        assert len(msgs) == 1
        assert {r["outcome"] for r in _seat_rows(chat_id)} == {"empty"}
        assert len(_seat_rows(chat_id)) == 2


def test_the_spend_surfaces_count_it(app, monkeypatch):
    monkeypatch.setattr(engine.providers, "stream_reply", scripted(
        lambda i, cfg: "Something real to add." if i == 0 else "[pass]",
        []))
    with TestClient(app, base_url="http://127.0.0.1") as c:
        chat_id = c.post("/api/chats", json={}).json()["id"]
        _round(c, chat_id, "a note for the room, no question")
        con = db.connect()
        events = list(accounting.iter_cost_events(con, chat_id=chat_id))
        con.close()
        reply, passed = (sorted(events, key=lambda e: e.unposted))
        assert not reply.unposted and passed.unposted
        assert passed.source == accounting.SOURCE_NORMAL
        assert passed.category == accounting.CAT_METERED
        assert passed.cache_read == 9000 and passed.cache_creation == 300
        s = accounting.summarize(events)
        assert s["totals"]["metered"] == pytest.approx(reply.cost + passed.cost)
        assert s["unposted"] == {"events": 1, "cost": passed.cost,
                                 "tokens": passed.tokens}
        # the Spend page and the chat header read the same ledger
        spend = c.get("/api/usage/summary?window=all").json()["breakdown"]
        assert spend["unposted"]["events"] == 1
        assert spend["totals"]["metered"] == pytest.approx(
            reply.cost + passed.cost)
        header = c.get(f"/api/chats/{chat_id}").json()["chat"]["cost"]
        assert header["metered"] == pytest.approx(reply.cost + passed.cost)


def test_a_failed_record_never_breaks_the_round(app, monkeypatch):
    def boom(*a, **k):
        raise sqlite3.OperationalError("disk I/O error")
    monkeypatch.setattr(engine.db, "log_seat_usage", boom)
    monkeypatch.setattr(engine.providers, "stream_reply", scripted(
        lambda i, cfg: "Something real to add." if i == 0 else "[pass]",
        []))
    with TestClient(app, base_url="http://127.0.0.1") as c:
        chat_id = c.post("/api/chats", json={}).json()["id"]
        events, msgs = _round(c, chat_id, "a note for the room, no question")
        assert [m["content"] for m in msgs[1:]] == ["Something real to add."]
        assert events[-1]["type"] == "done"


def test_deleting_a_chat_takes_its_rows(app, monkeypatch):
    monkeypatch.setattr(engine.providers, "stream_reply", scripted(
        lambda i, cfg: "[pass]", []))
    with TestClient(app, base_url="http://127.0.0.1") as c:
        chat_id = c.post("/api/chats", json={}).json()["id"]
        _round(c, chat_id, "@gpt are you there?")
        assert _seat_rows(chat_id)
        c.delete(f"/api/chats/{chat_id}")
        assert _seat_rows(chat_id) == []


def test_an_older_database_gains_the_table(tmp_path):
    data = tmp_path / "old"
    data.mkdir()
    con = sqlite3.connect(data / "chat.db")
    con.execute("PRAGMA user_version = 32")
    con.close()
    db.configure(data)
    db.init()
    con = db.connect()
    try:
        assert con.execute("PRAGMA user_version").fetchone()[0] == 33
        cols = {r[1] for r in con.execute("PRAGMA table_info(seat_usage)")}
        assert cols == {"id", "chat_id", "speaker", "outcome", "usage_json",
                        "created_at"}
    finally:
        con.close()
