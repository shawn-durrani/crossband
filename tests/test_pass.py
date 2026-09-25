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
  tables;
- a reply cut short while it could still become [pass] (a barge-in, a
  stall, a provider error) is judged as that pass and leaves nothing
  behind, while a cut-off reply that merely starts with "[" is kept as
  before (#456);
- a reply that ENDS with [pass] is judged by what comes before it (#460):
  a remark that only says the seat has nothing to add or is staying quiet
  makes it a pass (not stored, not shown, not spoken, not sent to
  memory, and refused like any pass where the guard applies), and a real
  reply is kept without the token, cut off or not.
"""

import asyncio
import json
import time

import pytest
from fastapi.testclient import TestClient

from backend import db, engine, rounds
from backend.app import create_app
from backend.config import Settings
from backend.engine import explicitly_addressed
from backend.passes import (is_cut_pass, is_pass, is_quiet_remark,
                             may_pass, strip_pass)


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


# ---------- a pass cut short (#456) ----------

def _one_seat(app):
    """A fresh chat and its first seat, for driving run_round directly."""
    with TestClient(app, base_url="http://127.0.0.1") as c:
        chat_id = c.post("/api/chats", json={}).json()["id"]
    con = db.connect()
    roster = db.get_chat_participants(con, chat_id)
    con.close()
    return chat_id, roster[:1]


def _stored(chat_id):
    con = db.connect()
    msgs = db.get_chat_messages(con, chat_id)
    con.close()
    return [m["content"] for m in msgs]


def _barge_in_after(app, monkeypatch, partial):
    """Stream `partial`, then hang until the client walks away: the
    barge-in shape the abort endpoint produces."""
    async def hanging(participant, roster, transcript, names, cfg, project,
                      chat_summary, voice_mode, tools=None, memory=None):
        yield ("text", partial)
        await asyncio.sleep(3600)

    monkeypatch.setattr(engine.providers, "stream_reply", hanging)
    chat_id, seats = _one_seat(app)

    async def go():
        gen = engine.run_round(chat_id, seats, "gpt", app.state.settings,
                               memory=None)
        async for chunk in gen:
            if '"delta"' in chunk:
                break
        await gen.aclose()

    asyncio.run(go())
    return _stored(chat_id)


@pytest.mark.parametrize("partial", [
    "[", "[p", "[pa", "[pas", "[pass", "[pass]", "  [PASS", "[pass\n"])
def test_a_pass_cut_off_by_a_barge_in_is_not_stored(app, monkeypatch,
                                                     partial):
    """The field shape: a seat began its [pass], the owner talked over it,
    and "[pass" plus the cut-off marker landed as a real turn."""
    assert _barge_in_after(app, monkeypatch, partial) == []


def test_a_cut_off_reply_that_only_starts_with_a_bracket_is_stored(
        app, monkeypatch):
    assert _barge_in_after(app, monkeypatch, "[note] something") == [
        "[note] something\n\n[cut off by User]"]


def _error_after(app, monkeypatch, partial):
    """Stream `partial`, then fail the way a provider or a stall does."""
    async def failing(participant, roster, transcript, names, cfg, project,
                      chat_summary, voice_mode, tools=None, memory=None):
        yield ("text", partial)
        raise RuntimeError("upstream went away")

    monkeypatch.setattr(engine.providers, "stream_reply", failing)
    chat_id, seats = _one_seat(app)

    async def go():
        return [chunk async for chunk in engine.run_round(
            chat_id, seats, "gpt", app.state.settings, memory=None)]

    events = [json.loads(ch[6:]) for ch in asyncio.run(go())
              if ch.startswith("data: ")]
    return events, _stored(chat_id)


def test_a_pass_cut_off_by_an_error_is_not_stored(app, monkeypatch):
    events, stored = _error_after(app, monkeypatch, "[pa")
    assert stored == []
    assert [e["type"] for e in events if e["type"] == "error"] == ["error"]


def test_a_real_reply_cut_off_by_an_error_is_stored_as_before(app,
                                                              monkeypatch):
    _, stored = _error_after(app, monkeypatch, "[note] half a thought")
    assert stored == ["[note] half a thought"]


def test_is_cut_pass():
    for text in ("[", "[p", "[pa", "[pas", "[pass", "[pass]", " [PaSs ",
                 "\n[pass]\n"):
        assert is_cut_pass(text), text
    for text in ("", "   ", None, "[note]", "[passed", "[pass] and more",
                 "pass", "[ pass"):
        assert not is_cut_pass(text), text


# ---------- a pass with words in front of it (#460) ----------
#
# The 25 September field test stored seat replies shaped like "<a remark
# that it had nothing to add>  [pass]" and "<a note that the room asked it
# to stay quiet>, passing.  [pass]", token and all. The wording below is
# made up.

QUIET_PASSES = [
    "Nothing to add from me.  [pass]",
    "You two carry on with the timber order, passing.  [pass]",
    "The room asked us to stay quiet, so I'm passing.  [pass]",
    "Staying quiet as asked.\n\n[PASS]",
    "I don\u2019t have anything to add. [pass]",
    "(still listening) [pass]",
    "\u2026 [pass]",
]
REAL_WITH_TOKEN = [
    ("The glue needs 24 hours to cure.  [pass]",
     "The glue needs 24 hours to cure."),
    ("For the record, room mode is still on.  [pass]",
     "For the record, room mode is still on."),
    ("Nothing to add, but Sam's cut list is one leg short. [pass]",
     "Nothing to add, but Sam's cut list is one leg short."),
    ("Yes.  [pass]", "Yes."),
]


def test_the_quiet_remark_rules():
    for text in QUIET_PASSES:
        assert is_pass(text), text
        assert is_cut_pass(text), text
    for text, kept in REAL_WITH_TOKEN:
        assert not is_pass(text), text
        assert strip_pass(text) == kept
    # the token only counts at the very end, and a remark needs the token
    assert not is_pass("[pass] and one more thing")
    assert not is_pass("Nothing to add.")
    assert strip_pass("Nothing to add.") == "Nothing to add."
    # a quiet remark says so, briefly, and asks, counts and turns nothing
    assert is_quiet_remark("Holding back until someone asks me directly.")
    assert is_quiet_remark("You two carry on with the timber order, passing.")
    assert is_quiet_remark("Nothing to add, Mateo covered it.")
    assert not is_quiet_remark("Okay.")
    assert not is_quiet_remark("Same here.")
    assert not is_quiet_remark("Pass the glue.")
    assert not is_quiet_remark("Nothing beats oak.")
    assert not is_quiet_remark("Staying quiet, want me to check the quotes?")
    assert not is_quiet_remark("Staying quiet for 2 minutes.")
    assert not is_quiet_remark("Staying quiet. "
                               + "The plan still stands for the bench. " * 4)
    # trailing punctuation after the token doesn't hide it
    assert is_pass("Still listening. [pass].")
    assert strip_pass("Oil it after sanding. [PASS].") == \
        "Oil it after sanding."
    # a bare token with a tool turn keeps its text, as before
    assert strip_pass("[pass]") == "[pass]"
    # a start of the token counts only on a reply that was cut off
    assert strip_pass("Sand it first. [pa") == "Sand it first. [pa"
    assert strip_pass("Sand it first. [pa", partial=True) == "Sand it first."


@pytest.mark.parametrize("quiet", QUIET_PASSES)
def test_a_quiet_remark_before_pass_is_a_pass(app, monkeypatch, quiet):
    calls = []
    monkeypatch.setattr(engine.providers, "stream_reply", scripted(
        lambda i, cfg: "I have something real to add." if i == 0
        else quiet, calls))
    with TestClient(app, base_url="http://127.0.0.1") as c:
        chat_id = c.post("/api/chats", json={}).json()["id"]
        events, msgs = _round(c, chat_id, "a note for the room, no question")
        assert [m["content"] for m in msgs[1:]] == [
            "I have something real to add."]
        passed = [e for e in events if e["type"] == "passed"]
        assert len(passed) == 1 and passed[0]["speaker"] == calls[1]["slug"]
        # memory reads the stored rows, and none carries the token
        con = db.connect()
        rows = engine.ingest_rows(con, chat_id, 0)
        con.close()
        assert not any("[pass" in r["content"].lower() for r in rows)


@pytest.mark.parametrize("text,kept", REAL_WITH_TOKEN)
def test_a_real_reply_keeps_its_words_without_the_token(app, monkeypatch,
                                                        text, kept):
    calls = []
    monkeypatch.setattr(engine.providers, "stream_reply", scripted(
        lambda i, cfg: "I have something real to add." if i == 0
        else text, calls))
    with TestClient(app, base_url="http://127.0.0.1") as c:
        chat_id = c.post("/api/chats", json={}).json()["id"]
        events, msgs = _round(c, chat_id, "a note for the room, no question")
        assert [m["content"] for m in msgs[1:]] == [
            "I have something real to add.", kept]
        assert not [e for e in events if e["type"] == "passed"]
        ends = [e for e in events if e["type"] == "speaker_end"]
        assert ends[-1]["message"]["content"] == kept


def test_a_quiet_remark_pass_is_refused_where_a_pass_is(app, monkeypatch):
    """The guard reads the widened pass the same way: the first responder
    to a direct question can't pass, with or without words in front."""
    calls = []
    monkeypatch.setattr(engine.providers, "stream_reply", scripted(
        lambda i, cfg: "Fine, the answer is 42."
        if cfg.get("pass_refused") else "Nothing to add from me. [pass]",
        calls))
    with TestClient(app, base_url="http://127.0.0.1") as c:
        chat_id = c.post("/api/chats", json={}).json()["id"]
        _, msgs = _round(c, chat_id, "what is the answer?")
        assert [m["content"] for m in msgs[1:]] == ["Fine, the answer is 42."]
        assert calls[0]["refused"] == "" and calls[1]["refused"] != ""


def test_a_quiet_remark_cut_off_mid_token_is_not_stored(app, monkeypatch):
    assert _barge_in_after(app, monkeypatch, "Nothing to add. [pa") == []


def test_a_real_reply_cut_off_mid_token_keeps_its_words(app, monkeypatch):
    assert _barge_in_after(app, monkeypatch, "Sand it first. [pa") == [
        "Sand it first.\n\n[cut off by User]"]


def test_a_real_reply_that_errors_mid_token_keeps_its_words(app,
                                                            monkeypatch):
    _, stored = _error_after(app, monkeypatch, "Sand it first. [pa")
    assert stored == ["Sand it first."]


def test_a_quiet_remark_fits_inside_the_first_tts_chunk():
    """The voice holds a reply's text while it could still end as a pass
    (frontend/src/passView.js PassSpeechGate). That hold costs no time to
    first audio only while a quiet remark is shorter than the text TTS
    waits for before it makes any audio: the gate lets a reply go once it
    is longer than QUIET_MAX_CHARS, before TTS could have started."""
    from backend import passes, voice
    init = json.loads(voice.tts_init_message({"tts_speed": 1.0}))
    first_chunk = init["generation_config"]["chunk_length_schedule"][0]
    assert passes.QUIET_MAX_CHARS < first_chunk
