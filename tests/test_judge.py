"""The interrupt judge (#642): were words said over a paused reply meant for
the AIs?

Pins: the moment is described the way the trial harness measured it, with
the speaker unknown; Jev's request carries one choice of three, and its
answer is read or left blank; the judge is off without a key or when the
setting says so, and the browser is told why; the route fills in the owner,
the seats and who's present, and logs the verdict but never the words;
each answered call is a utility_usage row of kind interrupt_judge,
unpriced until Jev is on the rate card; and a failed or slow call answers
no verdict, so the browser's word rules decide.
"""

import asyncio
import logging

import httpx
import pytest
from fastapi.testclient import TestClient

from backend import db, judge
from backend.app import create_app
from backend.config import Settings


def test_the_moment_reads_as_the_trial_measured_it():
    state = judge.describe("Alex", ["Claude", "GPT"], ["Sam"], "Claude",
                           "Chill them for twenty minutes", judge.SOMEONE,
                           "these are not gonna work")
    assert "Alex is talking out loud with AI assistants (Claude, GPT)" in state
    assert "Also in the room: Sam." in state
    assert "Claude was in the middle of saying: \"Chill them for twenty minutes\"" in state
    assert "someone in the room (the app can't tell who yet) said: " \
           "\"these are not gonna work\"" in state
    assert "Nobody else is in the room." in judge.describe(
        "Alex", ["Claude"], [], "Claude", "", judge.SOMEONE, "hi")


def test_jev_gets_one_choice_of_three_and_its_answer_is_read():
    body = judge.jev_body("the moment", "Claude")
    q = body["questions"]["verdict"]
    assert body["state"] == "the moment" and body["model"] == "jev-latest"
    assert q["type"] == "choice" and set(q["criteria"]) == set(judge.LABELS)
    ok = {"answers": {"verdict": {"choice": "aside", "confidence": 0.9}}}
    assert judge.parse_jev(ok) == ("aside", 0.9)
    assert judge.parse_jev({"answers": {"verdict": {"choice": "nah"}}}) == ("", None)
    assert judge.parse_jev(None) == ("", None)


def test_the_judge_is_off_without_a_key_or_when_set_off(monkeypatch):
    monkeypatch.delenv("TYPESAFE_API_KEY", raising=False)
    assert judge.off_reason({}) == "no_key"
    monkeypatch.setenv("TYPESAFE_API_KEY", "test-key")
    assert judge.off_reason({}) == ""
    assert judge.off_reason({"interrupt_judge": "off"}) == "off"


def _transport(handler):
    return httpx.AsyncClient(transport=httpx.MockTransport(handler))


def test_ask_reads_jevs_answer(monkeypatch):
    monkeypatch.setenv("TYPESAFE_API_KEY", "test-key")
    seen = {}

    def handler(request):
        seen["auth"] = request.headers["authorization"]
        seen["url"] = str(request.url)
        return httpx.Response(200, json={
            "model": "jev-1.13.0", "usage": {"input_tokens": 120, "output_tokens": 1},
            "answers": {"verdict": {"type": "choice", "choice": "interrupt",
                                    "confidence": 0.97}}})

    async def go():
        async with _transport(handler) as client:
            return await judge.ask("state", "Claude", client=client)
    got = asyncio.run(go())
    assert (got["verdict"], got["confidence"], got["model"]) == ("interrupt", 0.97, "jev-1.13.0")
    assert seen == {"auth": "Bearer test-key", "url": judge.JEV_URL}


@pytest.mark.parametrize("handler", [
    lambda r: httpx.Response(500, json={}),
    lambda r: httpx.Response(200, json={"answers": {}}),
    lambda r: (_ for _ in ()).throw(httpx.ReadTimeout("slow")),
])
def test_a_failed_or_empty_answer_is_no_verdict(monkeypatch, handler):
    monkeypatch.setenv("TYPESAFE_API_KEY", "test-key")

    async def go():
        async with _transport(handler) as client:
            return await judge.ask("state", "Claude", client=client)
    assert asyncio.run(go())["verdict"] == ""


# ---------- the route ----------

@pytest.fixture
def app(tmp_path):
    return create_app(Settings(data_dir=str(tmp_path / "data"),
                               memory_url="http://127.0.0.1:1"))


def _judge_rows():
    con = db.connect()
    try:
        return [dict(r) for r in con.execute(
            "SELECT kind, model, input_tokens, cost FROM utility_usage "
            "WHERE kind='interrupt_judge'")]
    finally:
        con.close()


def test_without_a_key_the_browser_is_told_and_nothing_is_sent(app, monkeypatch):
    monkeypatch.delenv("TYPESAFE_API_KEY", raising=False)
    called = []
    monkeypatch.setattr(judge, "ask", lambda *a, **k: called.append(a))
    with TestClient(app, base_url="http://127.0.0.1") as c:
        chat_id = c.post("/api/chats", json={}).json()["id"]
        got = c.post("/api/voice/judge", json={"chat_id": chat_id, "seat": "claude",
                                               "ai_said": "x", "words": "hello there"}).json()
    assert got == {"verdict": "", "reason": "no_key"}
    assert called == []


def test_the_route_asks_with_the_chats_people_and_records_the_call(app, monkeypatch, caplog):
    monkeypatch.setenv("TYPESAFE_API_KEY", "test-key")
    asked = {}

    async def fake_ask(state, seat, **kw):
        asked["state"], asked["seat"] = state, seat
        return {"verdict": "aside", "confidence": 0.93, "model": "jev-1.13.0",
                "usage": {"input_tokens": 150, "output_tokens": 1}, "ms": 240}
    monkeypatch.setattr(judge, "ask", fake_ask)
    caplog.set_level(logging.INFO, logger="crossband.voice")
    with TestClient(app, base_url="http://127.0.0.1") as c:
        chat_id = c.post("/api/chats", json={}).json()["id"]
        got = c.post("/api/voice/judge", json={
            "chat_id": chat_id, "seat": "claude",
            "ai_said": "Chill them for twenty minutes", "words": "these are not gonna work"}).json()
    assert got == {"verdict": "aside", "confidence": 0.93, "ms": 240}
    assert "these are not gonna work" in asked["state"]
    assert "Chill them for twenty minutes" in asked["state"]
    assert "User is talking out loud with AI assistants (" in asked["state"]
    assert asked["seat"] == "Claude"
    assert _judge_rows() == [{"kind": "interrupt_judge", "model": "jev-1.13.0",
                              "input_tokens": 150, "cost": None}]
    logged = " ".join(r.getMessage() for r in caplog.records)
    assert "interrupt judge:" in logged and "verdict=aside" in logged
    assert "gonna" not in logged and "Chill" not in logged


def test_no_answer_is_no_verdict_and_no_spend_row(app, monkeypatch):
    monkeypatch.setenv("TYPESAFE_API_KEY", "test-key")

    async def fake_ask(state, seat, **kw):
        return {"verdict": "", "confidence": None, "model": "jev-latest", "usage": {},
                "ms": 1000, "error": "ReadTimeout"}
    monkeypatch.setattr(judge, "ask", fake_ask)
    with TestClient(app, base_url="http://127.0.0.1") as c:
        chat_id = c.post("/api/chats", json={}).json()["id"]
        got = c.post("/api/voice/judge", json={"chat_id": chat_id, "seat": "claude",
                                               "words": "is it ready"}).json()
    assert got["verdict"] == "" and got["reason"] == "no_answer"
    assert _judge_rows() == []


def test_long_text_is_cut_to_size_before_it_leaves(app, monkeypatch):
    monkeypatch.setenv("TYPESAFE_API_KEY", "test-key")
    asked = {}

    async def fake_ask(state, seat, **kw):
        asked["state"] = state
        return {"verdict": "aside", "confidence": 0.5, "model": "jev-latest",
                "usage": {}, "ms": 1}
    monkeypatch.setattr(judge, "ask", fake_ask)
    with TestClient(app, base_url="http://127.0.0.1") as c:
        chat_id = c.post("/api/chats", json={}).json()["id"]
        c.post("/api/voice/judge", json={"chat_id": chat_id, "seat": "claude",
                                         "ai_said": "a" * 3000, "words": "b" * 3000})
    assert "a" * judge.MAX_SAID_CHARS in asked["state"]
    assert "a" * (judge.MAX_SAID_CHARS + 1) not in asked["state"]
    assert "b" * (judge.MAX_WORDS_CHARS + 1) not in asked["state"]
