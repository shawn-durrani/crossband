"""The echo guard (#210): restatement gets one retry, then suppression.

From the 2026-08-23 field failure (workbench incident record): a seat
delivered a near-copy of content the chat already held, and no live guard
existed. The contract under test:

- a completed text reply that mostly restates the seat's own previous
  message, or a reply already given this round, is dropped (`passed` SSE
  event, same client shape as a refused pass) and the seat re-runs ONCE
  with the guard stated;
- a restatement on that retry is suppressed like an insisted pass, and the
  round survives;
- a [pass] on the echo retry is always accepted, even for the first
  responder to a direct question - the note promises it;
- tool-using replies, repeat requests, short agreements, quoted lines and
  genuine paraphrase are never enforced against;
- voice rounds log a warning and deliver, since by completion the reply
  has already been spoken.
"""

import json
import logging
import time

import pytest
from fastapi.testclient import TestClient

from backend import echo, engine, rounds
from backend.app import create_app
from backend.config import Settings

LONG_A = (
    "The reliable way to repot the fern is to water it the night before, "
    "ease the root ball out sideways, and trim only the dark mushy roots. "
    "Use a pot one size up, fresh mix over a drainage layer, and keep it in "
    "shade for a week so the roots recover before any direct sun."
)

# Same content as LONG_A with light rewording - the shape the guard must
# still catch.
LONG_A_REWORDED = (
    "The reliable way to repot the fern is to water it the evening before, "
    "ease the root ball out sideways, and trim only the dark mushy roots. "
    "Use a pot one size up, fresh mix over a drainage layer, and leave it in "
    "shade for a week so the roots recover before any strong sun."
)

FRESH_B = (
    "One thing nobody mentioned: check the pot for salt crust before "
    "reusing it, because built-up fertiliser salts burn new roots. Soak the "
    "old pot in vinegar water for an hour, scrub the rim, and rinse it "
    "well; that alone prevents most of the leaf-tip browning people blame "
    "on the move."
)

FRESH_C = (
    "Feeding can wait a month: fresh mix already carries nutrients, and "
    "roots cut during the move take up almost nothing at first. After that, "
    "half-strength liquid feed each fortnight through the growing season is "
    "plenty, and nothing at all once the new fronds stop unrolling."
)


@pytest.fixture
def app(tmp_path):
    return create_app(Settings(data_dir=str(tmp_path / "data"),
                               memory_url="http://127.0.0.1:1"))


def scripted(fn, calls):
    """fn(call_index, cfg, calls) -> reply text. Records every provider call
    with the seat, the pass_refused and echo_refused notes it saw, and the
    text it returned."""
    async def stream_reply(participant, roster, transcript, names, cfg,
                           project, chat_summary, voice_mode, tools=None,
                           memory=None):
        calls.append({"slug": participant["slug"],
                      "refused": (cfg.get("pass_refused") or ""),
                      "echoed": (cfg.get("echo_refused") or "")})
        text = fn(len(calls) - 1, cfg, calls)
        calls[-1]["text"] = text
        yield ("text", text)
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


def test_a_copied_reply_is_retried_and_the_fresh_retry_lands(app, monkeypatch):
    calls = []
    monkeypatch.setattr(engine.providers, "stream_reply", scripted(
        lambda i, cfg, _: FRESH_B if cfg.get("echo_refused")
        else LONG_A, calls))
    with TestClient(app, base_url="http://127.0.0.1") as c:
        chat_id = c.post("/api/chats", json={}).json()["id"]
        events, msgs = _round(c, chat_id, "a note for the room, no question")
        assert [m["content"] for m in msgs[1:]] == [LONG_A, FRESH_B]
        # opener once, the copier twice (echo retry), nobody else
        assert [c_["slug"] for c_ in calls][1:] == [calls[1]["slug"]] * 2
        assert calls[1]["echoed"] == "" and calls[2]["echoed"] != ""
        assert "[pass]" in calls[2]["echoed"]
        dropped = [e for e in events if e["type"] == "passed"]
        assert len(dropped) == 1 and dropped[0]["speaker"] == calls[1]["slug"]


def test_a_reworded_copy_on_the_retry_is_suppressed(app, monkeypatch):
    calls = []
    monkeypatch.setattr(engine.providers, "stream_reply", scripted(
        lambda i, cfg, _: LONG_A_REWORDED if cfg.get("echo_refused")
        else LONG_A, calls))
    with TestClient(app, base_url="http://127.0.0.1") as c:
        chat_id = c.post("/api/chats", json={}).json()["id"]
        events, msgs = _round(c, chat_id, "a note for the room, no question")
        # only the original survives; the insisting copier left nothing
        assert [m["content"] for m in msgs[1:]] == [LONG_A]
        assert len(calls) == 3
        assert len([e for e in events if e["type"] == "passed"]) == 2


def test_a_pass_on_the_echo_retry_is_accepted_for_a_first_responder(
        app, monkeypatch):
    def script(i, cfg, calls):
        if cfg.get("echo_refused"):
            return "[pass]"
        prior = [c_["text"] for c_ in calls[:-1]
                 if c_["slug"] == calls[-1]["slug"]]
        if prior:
            return prior[-1]        # round 2 opener repeats its own answer
        return LONG_A if len(calls) == 1 else FRESH_B
    calls = []
    monkeypatch.setattr(engine.providers, "stream_reply",
                        scripted(script, calls))
    with TestClient(app, base_url="http://127.0.0.1") as c:
        chat_id = c.post("/api/chats", json={}).json()["id"]
        _round(c, chat_id, "how do I repot the fern?")
        _, msgs = _round(c, chat_id, "and what about feeding it?")
        # Round 2 (calls 2-5): each seat echoed its own round-1 reply, was
        # retried, and passed. The opener's pass was accepted even though it
        # was first responder to a direct question - the echo retry promised
        # that - so no seat ran a third time and the round ended silent.
        opener2 = calls[2]["slug"]
        assert [c_["slug"] for c_ in calls[2:4]] == [opener2, opener2]
        assert calls[3]["echoed"] != "" and calls[3]["text"] == "[pass]"
        assert len(calls) == 6
        # round 1's two answers stand; round 2 delivered nothing new
        assert [m["content"] for m in msgs[1:3]] == [LONG_A, FRESH_B]
        assert len(msgs) == 4


def test_tool_rounds_are_exempt(app, monkeypatch):
    calls = []

    def tool_stream(fn):
        async def stream_reply(participant, roster, transcript, names, cfg,
                               project, chat_summary, voice_mode, tools=None,
                               memory=None):
            calls.append({"slug": participant["slug"],
                          "echoed": (cfg.get("echo_refused") or "")})
            if len(calls) == 2:
                yield ("tool", {"tool": "web_search",
                                "input": {"query": "fern"}, "output": "ok"})
            yield ("text", fn(len(calls) - 1))
        return stream_reply

    monkeypatch.setattr(engine.providers, "stream_reply",
                        tool_stream(lambda i: LONG_A))
    with TestClient(app, base_url="http://127.0.0.1") as c:
        chat_id = c.post("/api/chats", json={}).json()["id"]
        events, msgs = _round(c, chat_id, "a note for the room, no question")
        # the copy rode a tool call, so it was delivered, not retried
        assert [m["content"] for m in msgs[1:]] == [LONG_A, LONG_A]
        assert len(calls) == 2
        assert not [e for e in events if e["type"] == "passed"]


def test_a_repeat_request_disables_the_guard_for_the_round(app, monkeypatch):
    def script(i, cfg, calls):
        prior = [c_["text"] for c_ in calls[:-1]
                 if c_["slug"] == calls[-1]["slug"]]
        if prior:
            return prior[-1]        # round 2: each seat repeats itself
        return LONG_A if len(calls) == 1 else FRESH_B
    calls = []
    monkeypatch.setattr(engine.providers, "stream_reply",
                        scripted(script, calls))
    with TestClient(app, base_url="http://127.0.0.1") as c:
        chat_id = c.post("/api/chats", json={}).json()["id"]
        _round(c, chat_id, "how do I repot the fern?")
        _, msgs = _round(c, chat_id, "please repeat that for me")
        # restating was the job this round: both repeats delivered, no
        # retries anywhere
        assert len(calls) == 4 and len(msgs) == 6
        assert {msgs[4]["content"], msgs[5]["content"]} == {LONG_A, FRESH_B}


def test_a_voice_round_logs_and_delivers(app, monkeypatch, caplog):
    calls = []
    monkeypatch.setattr(engine.providers, "stream_reply", scripted(
        lambda i, cfg, _: LONG_A, calls))
    caplog.set_level(logging.WARNING, logger="crossband.engine")
    with TestClient(app, base_url="http://127.0.0.1") as c:
        chat_id = c.post("/api/chats", json={}).json()["id"]
        c.patch(f"/api/chats/{chat_id}", json={"voice_mode": True})
        _, msgs = _round(c, chat_id, "a note for the room, no question")
        # the copy was already spoken by completion: delivered, logged
        assert [m["content"] for m in msgs[1:]] == [LONG_A, LONG_A]
        assert len(calls) == 2
        hits = [r for r in caplog.records if "echo_guard" in r.getMessage()]
        assert len(hits) == 1 and "action=logged" in hits[0].getMessage()


def test_the_pure_rules():
    # verbatim and lightly reworded copies are hits; fresh content is not
    assert echo.find_restated(LONG_A, [("ref", LONG_A)]) == "ref"
    assert echo.find_restated(LONG_A_REWORDED, [("ref", LONG_A)]) == "ref"
    assert echo.find_restated(FRESH_B, [("ref", LONG_A)]) is None
    # first matching reference wins, judged one reference at a time
    assert echo.find_restated(
        LONG_A, [("other", FRESH_B), ("own", LONG_A)]) == "own"
    # short agreements are never judged
    assert echo.find_restated("Yep - what Claude said.",
                              [("ref", LONG_A)]) is None
    # quoting a line to answer it is not restating it
    quoted = (f'Claude wrote "{LONG_A}" - but that soaks the mix too early. '
              + FRESH_B)
    assert echo.find_restated(quoted, [("ref", LONG_A)]) is None
    # a repeat request disables enforcement for the round
    assert echo.requested_repeat("please repeat that")
    assert echo.requested_repeat("say it once more?")
    assert echo.requested_repeat("can you post that AGAIN")
    assert not echo.requested_repeat("what should I do next?")
    # the retry note names the promise the engine keeps
    assert "[pass]" in echo.RETRY_NOTE


def test_references_are_own_last_message_plus_this_rounds_replies():
    transcript = [
        {"id": 1, "speaker": "user", "content": "first question"},
        {"id": 2, "speaker": "claude", "content": "claude round one"},
        {"id": 3, "speaker": "gpt", "content": "gpt round one"},
        {"id": 4, "speaker": "user", "content": "second question"},
        {"id": 5, "speaker": "claude", "content": "claude round two"},
        {"id": 6, "speaker": "ext:build-watcher", "content": "a notice"},
    ]
    refs = echo.references_for(transcript, "gpt", {"claude", "gpt"},
                               {"claude": "Claude", "gpt": "GPT"})
    assert refs == [(echo.OWN_LABEL, "gpt round one"),
                    ("Claude's reply just above", "claude round two")]
    # no prior message and nobody spoken yet: nothing to judge against
    assert echo.references_for([{"id": 1, "speaker": "user", "content": "hi"}],
                               "gpt", {"claude", "gpt"}, {}) == []
