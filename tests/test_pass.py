"""The sanctioned pass (#98): invisible, honourable, guarded.

The July diagnosis: prompted to stay silent when redundant AND to be
helpful, models invent angles instead of obeying the silence rule - a
prompt tension that needs a structural outlet. The contract under test:

- a bare [pass] reply is suppressed entirely: nothing persisted, a
  `passed` SSE event tells the client to drop the streamed bubble, and
  the round moves on;
- the guard (owner decision): the FIRST responder to a direct user
  question may not pass, and neither may a seat addressed by name - the
  pass is refused and the seat re-runs ONCE with the guard stated;
- a seat that insists after refusal is suppressed anyway (it has
  nothing); the round survives with zero crashes;
- the pure rules (is_pass, may_pass, addressed_slugs) hold their truth
  tables.
"""

import json
import time

import pytest
from fastapi.testclient import TestClient

from backend import engine, rounds
from backend.app import create_app
from backend.config import Settings
from backend.engine import explicitly_addressed
from backend.passes import is_pass, may_pass


@pytest.fixture
def app(tmp_path):
    return create_app(Settings(data_dir=str(tmp_path / "data"),
                               memory_url="http://127.0.0.1:1"))


def scripted(script, calls):
    """script(call_index, cfg) -> reply text. Records every provider call
    with the seat and the pass_refused note it saw."""
    async def stream_reply(participant, roster, transcript, names, cfg,
                           project, chat_summary, voice_mode, tools=None,
                           memory=None):
        calls.append({"slug": participant["slug"],
                      "refused": (cfg.get("pass_refused") or "")})
        yield ("text", script(len(calls) - 1, cfg))
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


def test_a_pass_is_invisible(app, monkeypatch):
    calls = []
    monkeypatch.setattr(engine.providers, "stream_reply", scripted(
        lambda i, cfg: "I have something real to add." if i == 0
        else "[pass]", calls))
    with TestClient(app, base_url="http://127.0.0.1") as c:
        chat_id = c.post("/api/chats", json={}).json()["id"]
        events, msgs = _round(c, chat_id, "a note for the room, no question")
        # one substantive reply persisted; the pass left NOTHING behind
        assert [m["content"] for m in msgs[1:]] == [
            "I have something real to add."]
        assert not any("[pass]" in m["content"] for m in msgs)
        passed = [e for e in events if e["type"] == "passed"]
        assert len(passed) == 1 and passed[0]["speaker"] == calls[1]["slug"]


def test_first_responder_may_not_pass_on_a_direct_question(app, monkeypatch):
    calls = []
    monkeypatch.setattr(engine.providers, "stream_reply", scripted(
        lambda i, cfg: "Fine - the answer is 42."
        if cfg.get("pass_refused") else "[pass]", calls))
    with TestClient(app, base_url="http://127.0.0.1") as c:
        chat_id = c.post("/api/chats", json={}).json()["id"]
        events, msgs = _round(c, chat_id, "what is the answer?")
        # the first seat's pass was refused; its retry answered; the second
        # seat's pass was allowed and suppressed
        assert [m["content"] for m in msgs[1:]] == ["Fine - the answer is 42."]
        first = calls[0]["slug"]
        assert [c_["slug"] for c_ in calls][:2] == [first, first]
        assert calls[0]["refused"] == "" and calls[1]["refused"] != ""
        assert len(calls) == 3          # seat1 x2, seat2 x1
        assert len([e for e in events if e["type"] == "passed"]) == 2


def test_an_addressed_seat_may_not_pass_even_without_a_question(app,
                                                                monkeypatch):
    calls = []
    monkeypatch.setattr(engine.providers, "stream_reply", scripted(
        lambda i, cfg: "Alright - my honest take."
        if cfg.get("pass_refused") else "[pass]", calls))
    with TestClient(app, base_url="http://127.0.0.1") as c:
        chat_id = c.post("/api/chats", json={}).json()["id"]
        _, msgs = _round(c, chat_id, "@gpt give us your take on this")
        assert [m["speaker"] for m in msgs[1:]] == ["gpt"]
        assert msgs[-1]["content"] == "Alright - my honest take."
        assert [c_["slug"] for c_ in calls] == ["gpt", "gpt"]


def test_a_seat_that_insists_is_suppressed_and_the_round_survives(
        app, monkeypatch):
    calls = []
    monkeypatch.setattr(engine.providers, "stream_reply",
                        scripted(lambda i, cfg: "[pass]", calls))
    with TestClient(app, base_url="http://127.0.0.1") as c:
        chat_id = c.post("/api/chats", json={}).json()["id"]
        events, msgs = _round(c, chat_id, "@gpt are you there?")
        assert len(msgs) == 1                      # only the user turn
        assert [c_["slug"] for c_ in calls] == ["gpt", "gpt"]
        assert [e["type"] for e in events if e["type"] in
                ("passed", "done")] == ["passed", "passed", "done"]


def test_the_pure_rules(app):
    assert is_pass("[pass]") and is_pass("  [PASS]\n")
    assert not is_pass("[pass] but also...") and not is_pass("pass")
    assert not is_pass("") and not is_pass(None)

    assert may_pass(1, False, "what time is it?")
    assert not may_pass(0, False, "what time is it?")
    assert may_pass(0, False, "just a statement")
    assert not may_pass(2, True, "no question at all")

    roster = [{"slug": "claude", "name": "Claude"},
              {"slug": "gpt", "name": "GPT"}]
    assert explicitly_addressed("@gpt what do you think", roster) == {"gpt"}
    assert explicitly_addressed("Claude and GPT, thoughts?", roster) == {
        "claude", "gpt"}
    assert explicitly_addressed("nothing for anyone here", roster) == set()
    # a name deep in the sentence is a mention, not a summons
    assert explicitly_addressed("earlier claude said something",
                                roster) == set()


# ---------- the extracted judgement table (#241) ----------

def test_judge_reply_pass_actions():
    """The pure decision behind the pass guard, extracted so the four
    interacting flags are discoverable without reading the whole loop. A
    first responder to a direct question retries with the guard stated; a
    prior note of either kind means the seat already had its retry, so an
    insisted pass suppresses."""
    from backend.engine import _judge_reply
    common = dict(echo_note="", echo_refs={}, voice_mode=False,
                  echo_guard=True, user_name="Alex")
    action, note, _ = _judge_reply("[pass]", [], pass_note="", idx=0,
                                   addressed=False,
                                   user_text="what's the weather?", **common)
    assert action == "retry_pass" and "Alex" in note
    action, _, _ = _judge_reply("[pass]", [], pass_note="", idx=1,
                                addressed=False,
                                user_text="what's the weather?", **common)
    assert action == "suppress_pass"
    action, _, _ = _judge_reply("[pass]", [], pass_note="already refused",
                                idx=0, addressed=False,
                                user_text="what's the weather?", **common)
    assert action == "suppress_pass"


def test_judge_reply_echo_actions():
    """The echo half: voice logs only (the reply is already spoken), a
    fresh restatement retries once, a restatement on its retry suppresses,
    and a tool round is exempt."""
    from backend import echo
    from backend.engine import _judge_reply
    prior = ("The plan is settled: we sand the bench top first, then fit "
             "the vice on the left, drill the dog holes on a 96mm grid, "
             "and finish the whole thing with two coats of hard wax oil "
             "before bolting the frame to the wall so nothing racks when "
             "you plane against the stop on the far end of the top.")
    refs = echo.references_for(
        [{"id": 1, "speaker": "claude", "content": prior}],
        "claude", {"claude"}, {"claude": "Claude"})
    common = dict(pass_note="", echo_refs=refs, idx=1, addressed=False,
                  user_text="anything else?", echo_guard=True,
                  user_name="Alex")
    restating = prior
    action, note, ref = _judge_reply(restating, [], echo_note="",
                                     voice_mode=False, **common)
    assert action == "retry_echo" and note and ref == "own"
    action, _, ref = _judge_reply(restating, [], echo_note="had its retry",
                                  voice_mode=False, **common)
    assert action == "suppress_echo" and ref == "own"
    action, _, _ = _judge_reply(restating, [], echo_note="",
                                voice_mode=True, **common)
    assert action == "log_echo"
    action, _, _ = _judge_reply(restating, [{"tool": "web_search"}],
                                echo_note="", voice_mode=False, **common)
    assert action == "accept"


def test_judge_reply_accepts_an_ordinary_reply():
    from backend.engine import _judge_reply
    action, note, ref = _judge_reply(
        "Here's a fresh thought.", [], pass_note="", echo_note="",
        echo_refs={}, idx=0, addressed=False, user_text="hi",
        voice_mode=False, echo_guard=True, user_name="Alex")
    assert (action, note, ref) == ("accept", "", "")
