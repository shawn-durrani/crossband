"""One assistant turn's tool calls run concurrently, in-order.

The serial loop paid tool latencies as a SUM (three 20s searches = 60s).
These tests pin the replacement contract: sibling calls overlap in time,
while every ordered artifact — yielded tool events, the tool_result replay,
the OpenAI item layout — stays exactly what the serial loop produced.
"""

import asyncio
import time

import pytest

from backend import providers

PARTICIPANT = {"name": "Claude", "slug": "claude", "model": "claude-opus-4-8",
               "provider": "anthropic", "system_prompt": ""}
ROSTER = [{"name": "Claude", "slug": "claude"}, {"name": "GPT", "slug": "gpt"}]
TOOLS = [{"name": "alpha", "description": "d", "input_schema": {}},
         {"name": "beta", "description": "d", "input_schema": {}}]


class _Block:
    def __init__(self, name, block_id):
        self.type = "tool_use"
        self.name = name
        self.input = {}
        self.id = block_id


class _Usage:
    input_tokens = 1
    output_tokens = 1
    cache_read_input_tokens = 0
    cache_creation_input_tokens = 0
    cache_creation = None


class _Final:
    def __init__(self, stop_reason, content):
        self.usage = _Usage()
        self.stop_reason = stop_reason
        self.content = content


class _StreamCtx:
    def __init__(self, final):
        self._final = final

    async def __aenter__(self):
        return self

    async def __aexit__(self, *a):
        return False

    @property
    def text_stream(self):
        async def gen():
            yield "hi"
        return gen()

    async def get_final_message(self):
        return self._final


class _TwoRoundClient:
    """First request returns two tool calls; second ends the turn."""

    def __init__(self):
        self.calls = 0
        self.captured = []

        class _Msgs:
            def stream(inner, **kwargs):
                self.captured.append(kwargs)
                final = (_Final("tool_use", [_Block("alpha", "t1"), _Block("beta", "t2")])
                         if self.calls == 0 else _Final("end_turn", []))
                self.calls += 1
                return _StreamCtx(final)
        self.messages = _Msgs()


def test_sibling_tool_calls_overlap_and_keep_order(cfg, monkeypatch):
    fake = _TwoRoundClient()
    monkeypatch.setattr(providers, "_anthropic_client", lambda p: fake)
    timeline = []

    async def fake_run_tool(name, tool_input, cfg_, origin_agent=None, memory=None):
        timeline.append(("start", name))
        await asyncio.sleep(0.05)
        timeline.append(("end", name))
        return f"out-{name}"

    monkeypatch.setattr(providers, "run_tool", fake_run_tool)

    events = []

    async def go():
        async for ev in providers.stream_reply(
                PARTICIPANT, ROSTER, [], {}, dict(cfg), None, "", False, tools=TOOLS):
            events.append(ev)

    t0 = time.monotonic()
    asyncio.run(go())
    elapsed = time.monotonic() - t0

    # CONCURRENT: beta started before alpha finished (serial code interleaves
    # start/end strictly); and the pair took ~one sleep, not two
    assert timeline[:2] == [("start", "alpha"), ("start", "beta")]
    assert elapsed < 0.5  # generous CI headroom; serial would add sleeps

    # ORDERED: tool events in block order, with the right outputs
    tool_events = [e for e in events if e[0] == "tool"]
    assert [t[1]["tool"] for t in tool_events] == ["alpha", "beta"]
    assert [t[1]["output"] for t in tool_events] == ["out-alpha", "out-beta"]

    # ORDERED: the tool_result replay matches tool_use ids in order
    second_call_messages = fake.captured[1]["messages"]
    results = second_call_messages[-1]["content"]
    assert [r["tool_use_id"] for r in results] == ["t1", "t2"]
    assert [r["content"] for r in results] == ["out-alpha", "out-beta"]


def test_abort_mid_tool_round_cancels_the_sibling(cfg, monkeypatch):
    """Barge-in closes the generator while call one streams out; its sibling
    must be cancelled, not left running detached."""
    fake = _TwoRoundClient()
    monkeypatch.setattr(providers, "_anthropic_client", lambda p: fake)
    states = {}

    async def fake_run_tool(name, tool_input, cfg_, origin_agent=None, memory=None):
        states[name] = "running"
        try:
            await asyncio.sleep(0.05 if name == "alpha" else 30)
            states[name] = "done"
            return "ok"
        except asyncio.CancelledError:
            states[name] = "cancelled"
            raise

    monkeypatch.setattr(providers, "run_tool", fake_run_tool)

    async def go():
        agen = providers.stream_reply(
            PARTICIPANT, ROSTER, [], {}, dict(cfg), None, "", False, tools=TOOLS)
        async for ev in agen:
            if ev[0] == "tool":       # first tool event: alpha finished
                await agen.aclose()   # simulate the round being torn down
                break
        # give cancellation a tick to propagate
        await asyncio.sleep(0.01)

    asyncio.run(go())
    assert states["alpha"] == "done"
    assert states["beta"] == "cancelled"
