"""Endpoint fallback for OpenAI-compatible servers without the Responses API.

mlx_lm.server, LM Studio, vLLM and llama.cpp implement /v1/chat/completions
but not /v1/responses, so every turn on such a seat died with a 404 before a
token arrived (#144). The adapter now discovers the missing route per
endpoint: the first 404 on a custom base_url flips that endpoint to a
classic chat-completions leg, replayed within the same turn. The default
OpenAI endpoint never falls back.

No network: fake clients raise the SDK's real NotFoundError and stream
hand-built chunks. Run with `.venv/bin/python -m pytest -q`.
"""

import asyncio
from types import SimpleNamespace

import httpx
import openai
import pytest

from backend import providers

BASE_URL = "http://127.0.0.1:8080/v1"

P = {"slug": "qwen", "name": "Qwen", "provider": "openai",
     "model": "test-model", "base_url": BASE_URL, "api_key_env": ""}
CFG = {"max_tool_rounds": 3, "max_response_tokens": 64,
       "attribution_audit": False, "user_name": "Owner"}
INPUT_ITEMS = [{"role": "user",
                "content": [{"type": "input_text", "text": "hello there"}]}]


@pytest.fixture(autouse=True)
def _fresh_state(monkeypatch):
    monkeypatch.setattr(providers, "_chat_completions_only", set())
    monkeypatch.setattr(providers, "build_openai_input",
                        lambda *a, **k: [dict(i) for i in INPUT_ITEMS])


def _not_found():
    req = httpx.Request("POST", BASE_URL + "/responses")
    return openai.NotFoundError("404 page not found",
                                response=httpx.Response(404, request=req),
                                body=None)


class _ChatStream:
    def __init__(self, chunks):
        self._chunks = list(chunks)

    def __aiter__(self):
        return self

    async def __anext__(self):
        if not self._chunks:
            raise StopAsyncIteration
        return self._chunks.pop(0)


def _text_chunk(text):
    return SimpleNamespace(usage=None, choices=[SimpleNamespace(
        delta=SimpleNamespace(content=text, tool_calls=None),
        finish_reason=None)])


def _finish_chunk(reason="stop", usage=None):
    return SimpleNamespace(usage=usage, choices=[SimpleNamespace(
        delta=SimpleNamespace(content=None, tool_calls=None),
        finish_reason=reason)])


def _tool_call_chunk(index, call_id, name, arguments):
    tc = SimpleNamespace(index=index, id=call_id,
                         function=SimpleNamespace(name=name,
                                                  arguments=arguments))
    return SimpleNamespace(usage=None, choices=[SimpleNamespace(
        delta=SimpleNamespace(content=None, tool_calls=[tc]),
        finish_reason=None)])


class FakeClient:
    """responses.create fails as configured; chat.completions.create pops
    one prepared stream per round and records its kwargs. A stream=False
    call is the #151 liveness ping, counted separately and answered (or
    refused) without consuming a prepared round."""

    def __init__(self, chat_rounds, responses_exc=None, ping_ok=True):
        self.responses_calls = 0
        self.chat_calls = []
        self.ping_calls = 0
        rounds = [list(r) for r in chat_rounds]
        outer = self

        async def _responses_create(**kwargs):
            outer.responses_calls += 1
            raise responses_exc if responses_exc else _not_found()

        async def _chat_create(**kwargs):
            if kwargs.get("stream") is False:
                outer.ping_calls += 1
                if not ping_ok:
                    raise httpx.ReadError("Connection reset by peer")
                return SimpleNamespace(choices=[SimpleNamespace(
                    message=SimpleNamespace(content="ok"))])
            outer.chat_calls.append(kwargs)
            return _ChatStream(rounds.pop(0))

        self.responses = SimpleNamespace(create=_responses_create)
        self.chat = SimpleNamespace(
            completions=SimpleNamespace(create=_chat_create))


def _run(p, client, monkeypatch, tools=None):
    monkeypatch.setattr(providers, "_openai_client", lambda _p: client)

    async def collect():
        out = []
        async for ev in providers._stream_openai(
                p, "STABLE", "", [], {}, CFG, tools or [], None):
            out.append(ev)
        return out

    return asyncio.run(collect())


# ── the fallback itself ─────────────────────────────────────────────────────

def test_404_on_custom_endpoint_falls_back_within_the_same_turn(monkeypatch):
    usage = SimpleNamespace(prompt_tokens=10, completion_tokens=2,
                            prompt_tokens_details=None)
    client = FakeClient([[_text_chunk("Hel"), _text_chunk("lo"),
                          _finish_chunk("stop", usage)]])
    events = _run(dict(P), client, monkeypatch)
    text = "".join(v for k, v in events if k == "text")
    assert text == "Hello"                      # the user saw a normal reply
    assert client.responses_calls == 1
    assert BASE_URL in providers._chat_completions_only
    assert events[-1] == ("usage", {"input": 10, "cache_read": 0,
                                    "cache_creation": 0, "output": 2})


def test_chat_leg_request_shape(monkeypatch):
    """No Responses-only params may leak: reasoning and store are 400s on
    the servers this path exists for; the system prompt rides as the first
    message because chat completions has no `instructions` channel."""
    client = FakeClient([[_text_chunk("ok"), _finish_chunk()]])
    _run(dict(P), client, monkeypatch)
    kwargs = client.chat_calls[0]
    assert kwargs["messages"][0] == {"role": "system", "content": "STABLE"}
    assert kwargs["max_tokens"] == CFG["max_response_tokens"]
    assert "reasoning" not in kwargs and "store" not in kwargs


def test_remembered_endpoint_skips_responses_entirely(monkeypatch):
    providers._chat_completions_only.add(BASE_URL)
    client = FakeClient([[_text_chunk("hi"), _finish_chunk()]])
    events = _run(dict(P), client, monkeypatch)
    assert client.responses_calls == 0
    assert "".join(v for k, v in events if k == "text") == "hi"


def test_default_openai_endpoint_never_falls_back(monkeypatch):
    """No base_url means OpenAI proper: /v1/responses exists there, so a
    404 is a real error and must stay loud, not be papered over."""
    p = dict(P, base_url="")
    client = FakeClient([])
    with pytest.raises(openai.NotFoundError):
        _run(p, client, monkeypatch)
    assert providers._chat_completions_only == set()
    assert client.chat_calls == []


def test_non_404_errors_are_not_treated_as_missing_route(monkeypatch):
    client = FakeClient([], responses_exc=RuntimeError("connection reset"))
    with pytest.raises(RuntimeError, match="connection reset"):
        _run(dict(P), client, monkeypatch)
    assert providers._chat_completions_only == set()


def test_reset_while_reading_the_404_still_falls_back(monkeypatch):
    """#151: mlx closes the Responses 404 without draining the request
    body, so a transcript-sized request sees a raw ReadError instead of
    NotFoundError - every time. A live one-token chat ping disambiguates
    a missing route from a dead server and classifies the endpoint."""
    client = FakeClient([[_text_chunk("hi"), _finish_chunk()]],
                        responses_exc=httpx.ReadError("reset by peer"))
    events = _run(dict(P), client, monkeypatch)
    assert client.ping_calls == 1
    assert "".join(v for k, v in events if k == "text") == "hi"
    assert BASE_URL in providers._chat_completions_only


def test_apiconnectionerror_variant_also_falls_back(monkeypatch):
    """The same race surfaces as the SDK's own wrapper when it dies inside
    the request send rather than the error-body read."""
    client = FakeClient(
        [[_text_chunk("hi"), _finish_chunk()]],
        responses_exc=openai.APIConnectionError(
            request=httpx.Request("POST", BASE_URL + "/responses")))
    events = _run(dict(P), client, monkeypatch)
    assert "".join(v for k, v in events if k == "text") == "hi"
    assert BASE_URL in providers._chat_completions_only


def test_dead_server_is_never_classified(monkeypatch):
    """Ping fails too: the server is down, not routeless. The original
    error propagates and nothing is remembered, so recovery is retried
    fresh next turn."""
    client = FakeClient([], responses_exc=httpx.ReadError("reset"),
                        ping_ok=False)
    with pytest.raises(httpx.ReadError):
        _run(dict(P), client, monkeypatch)
    assert client.ping_calls == 1
    assert providers._chat_completions_only == set()
    assert client.chat_calls == []


# ── the projection ──────────────────────────────────────────────────────────

def test_projection_roles_text_and_attachment_marker():
    msgs = providers.build_chat_completion_messages("SYS", [
        {"role": "user", "content": [
            {"type": "input_text", "text": "look at this"},
            {"type": "input_image", "image_url": "data:image/png;base64,x"}]},
        {"role": "assistant", "content": "plain string reply"},
        {"role": "developer", "content": [
            {"type": "input_text", "text": "per-turn note"}]},
    ])
    assert msgs[0] == {"role": "system", "content": "SYS"}
    assert msgs[1]["role"] == "user"
    assert "look at this" in msgs[1]["content"]
    assert "[attachment omitted" in msgs[1]["content"]
    assert msgs[2] == {"role": "assistant", "content": "plain string reply"}
    assert msgs[3] == {"role": "user", "content": "per-turn note"}


def test_projection_never_places_system_after_index_zero():
    """#157: the trailing volatile block rides as a developer item, and
    mlx_lm refuses any system message that is not the first. The framed
    block was designed to ride in a user turn (the Anthropic adapter has
    always delivered it that way), so the projection must too."""
    msgs = providers.build_chat_completion_messages("SYS", [
        {"role": "user", "content": [{"type": "input_text", "text": "hi"}]},
        {"role": "assistant", "content": "reply"},
        {"role": "developer", "content": [
            {"type": "input_text", "text": "[Context refresh] note"}]},
    ])
    assert [m["role"] for m in msgs].index("system") == 0
    assert all(m["role"] != "system" for m in msgs[1:])
    assert msgs[-1] == {"role": "user", "content": "[Context refresh] note"}


def test_projection_tool_items_become_tool_call_pairs():
    msgs = providers.build_chat_completion_messages("SYS", [
        {"type": "function_call", "call_id": "c1", "name": "echo",
         "arguments": '{"x": 1}'},
        {"type": "function_call_output", "call_id": "c1", "output": "OUT"},
    ])
    assert msgs[1]["role"] == "assistant"
    assert msgs[1]["tool_calls"][0]["id"] == "c1"
    assert msgs[1]["tool_calls"][0]["function"]["name"] == "echo"
    assert msgs[2] == {"role": "tool", "tool_call_id": "c1", "content": "OUT"}


# ── tool loop on the chat leg ───────────────────────────────────────────────

def test_tool_round_runs_and_feeds_back_into_the_next_round(monkeypatch):
    async def fake_run_tool(name, tool_input, cfg, origin_agent=None,
                            memory=None):
        return f"ran {name} with {tool_input}"

    monkeypatch.setattr(providers, "run_tool", fake_run_tool)
    tools = [{"name": "echo", "description": "echo back",
              "input_schema": {"type": "object"}}]
    client = FakeClient([
        [_tool_call_chunk(0, "call_1", "echo", '{"x"'),
         _tool_call_chunk(0, None, None, ": 1}"),
         _finish_chunk("tool_calls")],
        [_text_chunk("Done"), _finish_chunk("stop")],
    ])
    events = _run(dict(P), client, monkeypatch, tools=tools)
    tool_events = [v for k, v in events if k == "tool"]
    assert tool_events == [{"tool": "echo", "input": {"x": 1},
                            "output": "ran echo with {'x': 1}"}]
    assert "".join(v for k, v in events if k == "text") == "Done"
    round2 = client.chat_calls[1]["messages"]
    assert round2[-1] == {"role": "tool", "tool_call_id": "call_1",
                          "content": "ran echo with {'x': 1}"}
    assert round2[-2]["tool_calls"][0]["function"]["arguments"] == '{"x": 1}'
    # The chat leg advertises tools in the nested chat-completions shape.
    assert client.chat_calls[0]["tools"][0]["function"]["name"] == "echo"
