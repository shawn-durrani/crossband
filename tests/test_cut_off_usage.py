"""A model call cut off partway counts what the provider had reported (#576).

The adapters report a call's usage when it ends, so a call that never
ended (a barge-in, a stall, a provider error) counted nothing, though the
provider had read all of its input. Anthropic reports that input as each
reply starts. OpenAI reports nothing until a call ends.

The contract under test:

- the call meter holds the tool rounds that finished, plus, for an
  Anthropic round in flight, what its message snapshot reported;
- a cut-off reply's saved message carries that usage, priced and marked
  partial, and a call cut off with nothing to save leaves a seat_usage row
  with the outcome cut_off;
- a call that did report in full is never counted again;
- accounting counts partial calls like any other and says how much of the
  total they are.
"""

import asyncio
import json
import time

import pytest
from fastapi.testclient import TestClient

from backend import accounting, db, engine, providers
from backend.app import create_app
from backend.config import Settings

PARTICIPANT = {"name": "Claude", "slug": "claude", "model": "claude-opus-4-8",
               "provider": "anthropic", "system_prompt": ""}
ROSTER = [{"name": "Claude", "slug": "claude"}, {"name": "GPT", "slug": "gpt"}]


class _Usage:
    def __init__(self, input_tokens, cache_read=0, cache_creation=0,
                 output_tokens=1):
        self.input_tokens = input_tokens
        self.cache_read_input_tokens = cache_read
        self.cache_creation_input_tokens = cache_creation
        self.output_tokens = output_tokens
        self.cache_creation = None


class _Snapshot:
    def __init__(self, usage):
        self.usage = usage


class _ToolUse:
    type = "tool_use"
    id = "tu_1"
    name = "web_search"
    input = {"query": "oak oil"}


class _Final:
    def __init__(self, usage, stop_reason="end_turn", content=None):
        self.usage = usage
        self.stop_reason = stop_reason
        self.content = content or []


class _Round:
    """One Anthropic round. `start` is what message_start reported (None:
    the reply hadn't started). `final` ends it; without one it hangs after
    its text, as a reply you talk over does."""

    def __init__(self, start, text="", final=None):
        self._start, self._text, self._final = start, text, final

    async def __aenter__(self):
        return self

    async def __aexit__(self, *a):
        return False

    @property
    def current_message_snapshot(self):
        assert self._start is not None
        return _Snapshot(self._start)

    @property
    def text_stream(self):
        async def gen():
            if self._text:
                yield self._text
            if self._final is None:
                await asyncio.sleep(3600)
        return gen()

    async def get_final_message(self):
        return self._final


def _client(rounds):
    class _Messages:
        @staticmethod
        def stream(**kwargs):
            return rounds.pop(0)

    class _Client:
        messages = _Messages()
    return _Client()


def _cut_after(monkeypatch, cfg, rounds, stop_after):
    """Drive the Anthropic adapter until its `stop_after`th piece of text,
    then close it the way a barge-in does. Returns the meter."""
    monkeypatch.setattr(providers, "_anthropic_client",
                        lambda p: _client(rounds))

    async def fake_tool(name, args, cfg, origin_agent=None, memory=None):
        return "three results"

    monkeypatch.setattr(providers, "run_tool", fake_tool)
    meter = providers.CallMeter()
    live = dict(cfg, chat_id=5, _call_meter=meter)

    async def go():
        gen = providers.stream_reply(PARTICIPANT, ROSTER, [], {}, live, None,
                                     "", False, tools=[{
                                         "name": "web_search",
                                         "description": "search",
                                         "input_schema": {"type": "object"}}])
        seen = 0
        async for kind, _ in gen:
            seen += kind == "text"
            if seen >= stop_after:
                break
        await gen.aclose()

    asyncio.run(go())
    return meter


def test_a_reply_cut_off_counts_the_input_it_had_read(cfg, monkeypatch):
    meter = _cut_after(monkeypatch, cfg, [
        _Round(_Usage(4000, cache_read=20000, cache_creation=300), "Oak")], 1)
    u = meter.partial()
    assert (u["input"], u["cache_read"], u["cache_creation"], u["output"]) \
        == (4000, 20000, 300, 1)
    assert u["partial"] is True
    # the cache fingerprints ride along, as on any call's usage
    assert u["cache_prefix"]["model"] == "claude-opus-4-8"


def test_a_cut_after_a_tool_round_counts_the_round_and_the_start(cfg,
                                                                  monkeypatch):
    first = _Final(_Usage(1000, cache_read=9000, cache_creation=50,
                          output_tokens=40),
                   stop_reason="tool_use", content=[_ToolUse()])
    meter = _cut_after(monkeypatch, cfg, [
        _Round(_Usage(1000), "", first),
        _Round(_Usage(1300, cache_read=10000), "It's")], 1)
    u = meter.partial()
    assert (u["input"], u["cache_read"], u["cache_creation"], u["output"]) \
        == (2300, 19000, 50, 41)


def test_a_reply_that_never_started_counts_only_what_finished(cfg,
                                                               monkeypatch):
    meter = providers.CallMeter()
    assert meter.partial() is None                    # nothing handed over
    meter.done = {"input": 0, "cache_read": 0, "cache_creation": 0,
                  "output": 0}
    meter.stream = _Round(None)                       # no message_start yet
    assert meter.partial() is None                    # nothing known at all
    meter.done["input"] = 700
    assert meter.partial() == {"input": 700, "cache_read": 0,
                               "cache_creation": 0, "output": 0,
                               "partial": True}


def test_a_finished_round_leaves_the_meter_by_its_total(cfg, monkeypatch):
    """Once a round's usage is in the total, its stream isn't counted
    again."""
    first = _Final(_Usage(1000, output_tokens=40), stop_reason="tool_use",
                   content=[_ToolUse()])
    meter = _cut_after(monkeypatch, cfg, [
        _Round(_Usage(1000), "", first),
        _Round(_Usage(1300), "", _Final(_Usage(1300, output_tokens=9)))], 99)
    assert meter.stream is None
    assert meter.done["input"] == 2300 and meter.done["output"] == 49


def test_accounting_counts_a_partial_call_and_says_so():
    def event(partial, cost):
        return accounting.CostEvent(
            chat_id=1, ts=time.time(), source=accounting.SOURCE_NORMAL,
            category=accounting.CAT_METERED, provenance="rate_card_estimate",
            provider="anthropic", model="m", speaker="claude", cost=cost,
            tokens=100, has_cost=True, partial=partial)

    s = accounting.summarize([event(False, 0.5), event(True, 0.02)])
    assert s["totals"]["metered"] == pytest.approx(0.52)
    assert s["partial"] == {"events": 1, "cost": 0.02, "tokens": 100}


def test_a_usage_block_marked_partial_becomes_a_partial_event():
    u = {"input": 10, "output": 1, "cost": 0.01, "partial": True}
    e = accounting._seat_event(1, 0.0, "claude", u, {}, {})
    assert e.partial and e.cost == 0.01
    assert not accounting._seat_event(1, 0.0, "claude", {"input": 1}, {},
                                      {}).partial


# ---------- through a round ----------

KNOWN = {"input": 3000, "cache_read": 12000, "cache_creation": 200,
         "output": 0}


@pytest.fixture
def app(tmp_path):
    return create_app(Settings(data_dir=str(tmp_path / "data"),
                               memory_url="http://127.0.0.1:1"))


def _seats(app, n=1):
    with TestClient(app, base_url="http://127.0.0.1") as c:
        chat_id = c.post("/api/chats", json={}).json()["id"]
    con = db.connect()
    roster = db.get_chat_participants(con, chat_id)
    con.close()
    return chat_id, roster[:n]


def _stored(chat_id):
    con = db.connect()
    try:
        msgs = db.get_chat_messages(con, chat_id)
        rows = [dict(r) for r in con.execute(
            "SELECT speaker, outcome, usage_json FROM seat_usage "
            "WHERE chat_id=? ORDER BY id", (chat_id,))]
    finally:
        con.close()
    return msgs, rows


def _metered(before_cut, known=KNOWN, then="hang"):
    """A seat whose provider had reported `known` when the call stopped.
    `before_cut` is what streams first; `then` is how it stops."""
    async def stream_reply(participant, roster, transcript, names, cfg,
                           project, chat_summary, voice_mode, tools=None,
                           memory=None):
        cfg["_call_meter"].done = dict(known)
        for ev in before_cut:
            yield ev
        if then == "fail":
            raise RuntimeError("upstream went away")
        await asyncio.sleep(3600)
    return stream_reply


def _barge_in(app, monkeypatch, stream_reply, at, n=1):
    monkeypatch.setattr(engine.providers, "stream_reply", stream_reply)
    chat_id, seats = _seats(app, n)

    async def go():
        gen = engine.run_round(chat_id, seats, "gpt", app.state.settings,
                               memory=None)
        async for chunk in gen:
            if at(chunk):
                break
        await gen.aclose()

    asyncio.run(go())
    return _stored(chat_id)


def _run(app, monkeypatch, stream_reply):
    monkeypatch.setattr(engine.providers, "stream_reply", stream_reply)
    chat_id, seats = _seats(app)

    async def go():
        return [c async for c in engine.run_round(
            chat_id, seats, "gpt", app.state.settings, memory=None)]

    asyncio.run(go())
    return _stored(chat_id)


def test_a_reply_you_talk_over_carries_what_was_known(app, monkeypatch):
    msgs, rows = _barge_in(app, monkeypatch,
                           _metered([("text", "Oak takes oil")]),
                           lambda ch: '"delta"' in ch)
    assert [m["content"] for m in msgs] == ["Oak takes oil\n\n[cut off by User]"]
    u = json.loads(msgs[0]["usage_json"])
    assert u["partial"] is True
    assert (u["input"], u["cache_read"], u["cache_creation"]) == (3000, 12000,
                                                                 200)
    assert u["cost"] > 0 and u["cost_provenance"]["source"]
    assert rows == []


def test_a_call_cut_off_before_it_wrote_anything_is_a_row(app, monkeypatch):
    working = ("work_status", {"phase": "tools", "label": "Searching"})
    msgs, rows = _barge_in(app, monkeypatch, _metered([working]),
                           lambda ch: '"work_status"' in ch)
    assert msgs == []
    assert [r["outcome"] for r in rows] == ["cut_off"]
    u = json.loads(rows[0]["usage_json"])
    assert u["partial"] is True and u["input"] == 3000 and u["cost"] > 0


def test_a_pass_cut_off_is_a_row_and_never_a_message(app, monkeypatch):
    msgs, rows = _barge_in(app, monkeypatch, _metered([("text", "[pa")]),
                           lambda ch: '"delta"' in ch)
    assert msgs == []
    assert [r["outcome"] for r in rows] == ["cut_off"]


def test_a_failed_reply_keeps_its_words_and_what_was_known(app, monkeypatch):
    msgs, rows = _run(app, monkeypatch,
                      _metered([("text", "Oak takes oil")], then="fail"))
    assert [m["content"] for m in msgs] == ["Oak takes oil"]
    assert json.loads(msgs[0]["usage_json"])["partial"] is True
    assert rows == []


def test_a_call_that_failed_with_nothing_is_a_cut_off_row(app, monkeypatch):
    msgs, rows = _run(app, monkeypatch, _metered([], then="fail"))
    assert msgs == []
    assert [r["outcome"] for r in rows] == ["cut_off"]
    assert json.loads(rows[0]["usage_json"])["partial"] is True


def test_nothing_known_records_nothing(app, monkeypatch):
    """An OpenAI call cut off in its first step: the provider had said
    nothing, so there's nothing to count."""
    nothing = {"input": 0, "cache_read": 0, "cache_creation": 0, "output": 0}
    msgs, rows = _barge_in(app, monkeypatch,
                           _metered([("text", "Oak")], known=nothing),
                           lambda ch: '"delta"' in ch)
    assert msgs[0]["usage_json"] is None
    assert rows == []


def test_a_call_that_reported_is_never_counted_again(app, monkeypatch):
    """The first seat finishes and reports; the second is talked over. The
    first seat's meter is done with, so its call is counted once."""
    calls = []

    async def stream_reply(participant, roster, transcript, names, cfg,
                           project, chat_summary, voice_mode, tools=None,
                           memory=None):
        calls.append(participant["slug"])
        cfg["_call_meter"].done = dict(KNOWN)
        if len(calls) == 1:
            yield ("text", "Danish oil suits oak.")
            yield ("usage", {**KNOWN, "output": 12})
            return
        yield ("text", "And")
        await asyncio.sleep(3600)

    seen = []

    def second_delta(chunk):
        if '"delta"' in chunk:
            seen.append(chunk)
        return len(seen) == 2

    msgs, rows = _barge_in(app, monkeypatch, stream_reply, second_delta, n=2)
    first, second = (json.loads(m["usage_json"]) for m in msgs)
    assert "partial" not in first and first["output"] == 12
    assert second["partial"] is True
    assert rows == []


def test_the_spend_page_counts_it(app, monkeypatch):
    msgs, _ = _barge_in(app, monkeypatch,
                        _metered([("text", "Oak takes oil")]),
                        lambda ch: '"delta"' in ch)
    cost = json.loads(msgs[0]["usage_json"])["cost"]
    with TestClient(app, base_url="http://127.0.0.1") as c:
        spend = c.get("/api/usage/summary?window=all").json()["breakdown"]
    assert spend["partial"]["events"] == 1
    assert spend["partial"]["cost"] == pytest.approx(cost)
    assert spend["totals"]["metered"] == pytest.approx(cost)
