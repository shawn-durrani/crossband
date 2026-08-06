"""backend.llm_util: routing, degrade-without-keys, and the usage/timeout
telemetry the critic eval harness needs (utility_complete_with_usage).
utility_complete itself must keep behaving exactly as before -- it's used by
chat_memory.py's rolling summary/auto-title/distillation paths."""

import asyncio

import pytest

from backend import llm_util


class _TextBlock:
    def __init__(self, text):
        self.type = "text"
        self.text = text


class _Usage:
    def __init__(self, input_tokens, output_tokens):
        self.input_tokens = input_tokens
        self.output_tokens = output_tokens


class FakeAnthropicResp:
    def __init__(self, text, input_tokens=11, output_tokens=22):
        self.content = [_TextBlock(text)]
        self.usage = _Usage(input_tokens, output_tokens)


class FakeAsyncAnthropic:
    last_kwargs = None

    def __init__(self, *a, **kw):
        self.messages = self

    async def create(self, **kwargs):
        FakeAsyncAnthropic.last_kwargs = kwargs
        return FakeAnthropicResp(" haiku says hi ")


class FakeOpenAIResp:
    def __init__(self, text, input_tokens=33, output_tokens=44):
        self.output_text = text
        self.usage = _Usage(input_tokens, output_tokens)


class FakeAsyncOpenAI:
    last_kwargs = None

    def __init__(self, *a, **kw):
        self.responses = self

    async def create(self, **kwargs):
        FakeAsyncOpenAI.last_kwargs = kwargs
        return FakeOpenAIResp(" gpt says hi ")


@pytest.fixture(autouse=True)
def _clear_client_cache():
    """llm_util caches one SDK client per provider per process; tests patch a
    different fake per test, so the cache must reset between them."""
    llm_util._clients.clear()
    yield
    llm_util._clients.clear()


@pytest.fixture(autouse=True)
def _clear_keys(monkeypatch):
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)


def test_model_family_routes_by_prefix():
    assert llm_util.model_family("gpt-5.1") == "openai"
    assert llm_util.model_family("o3-mini") == "openai"
    assert llm_util.model_family("claude-haiku-4-5") == "anthropic"
    assert llm_util.model_family("") == "anthropic"


def test_utility_complete_missing_anthropic_key_returns_none():
    out = asyncio.run(
        llm_util.utility_complete("hi", {"utility_model": "claude-haiku-4-5"}))
    assert out is None


def test_utility_complete_missing_openai_key_returns_none():
    out = asyncio.run(llm_util.utility_complete("hi", {"utility_model": "gpt-5.1"}))
    assert out is None


def test_utility_complete_anthropic_strips_text(monkeypatch):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-test")
    import anthropic
    monkeypatch.setattr(anthropic, "AsyncAnthropic", FakeAsyncAnthropic)
    out = asyncio.run(
        llm_util.utility_complete("hi", {"utility_model": "claude-haiku-4-5"}))
    assert out == "haiku says hi"


def test_utility_complete_openai_strips_text(monkeypatch):
    monkeypatch.setenv("OPENAI_API_KEY", "sk-test")
    import openai
    monkeypatch.setattr(openai, "AsyncOpenAI", FakeAsyncOpenAI)
    out = asyncio.run(llm_util.utility_complete("hi", {"utility_model": "gpt-5.1"}))
    assert out == "gpt says hi"


def test_utility_complete_with_usage_reports_tokens_and_latency(monkeypatch):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-test")
    import anthropic
    monkeypatch.setattr(anthropic, "AsyncAnthropic", FakeAsyncAnthropic)
    result = asyncio.run(llm_util.utility_complete_with_usage(
        "hi", {"utility_model": "claude-haiku-4-5"}))
    assert result.text == "haiku says hi"
    assert result.input_tokens == 11
    assert result.output_tokens == 22
    assert result.latency_s >= 0
    assert result.timed_out is False


def test_utility_complete_with_usage_model_override(monkeypatch):
    """A `model=` kwarg overrides cfg["utility_model"] -- the eval harness
    sweeps critic models while holding cfg fixed."""
    monkeypatch.setenv("OPENAI_API_KEY", "sk-test")
    import openai
    monkeypatch.setattr(openai, "AsyncOpenAI", FakeAsyncOpenAI)
    result = asyncio.run(llm_util.utility_complete_with_usage(
        "hi", {"utility_model": "claude-haiku-4-5"}, model="gpt-5.1"))
    assert result.text == "gpt says hi"


def test_utility_complete_with_usage_timeout(monkeypatch):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-test")

    class SlowAsyncAnthropic(FakeAsyncAnthropic):
        async def create(self, **kwargs):
            await asyncio.sleep(10)
            return FakeAnthropicResp("too slow")

    import anthropic
    monkeypatch.setattr(anthropic, "AsyncAnthropic", SlowAsyncAnthropic)
    result = asyncio.run(llm_util.utility_complete_with_usage(
        "hi", {"utility_model": "claude-haiku-4-5"}, timeout=0.01))
    assert result.text is None
    assert result.timed_out is True


def test_client_is_built_once_and_reused_across_calls(monkeypatch):
    """One SDK client per provider per process: a fresh client per
    utility call paid a TLS handshake for every summary/title/distill."""
    import anthropic
    built = {"n": 0}

    class CountingFake(FakeAsyncAnthropic):
        def __init__(self, *a, **kw):
            built["n"] += 1
            super().__init__(*a, **kw)

    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-test")
    monkeypatch.setattr(anthropic, "AsyncAnthropic", CountingFake)

    async def go():
        for _ in range(3):
            await llm_util.utility_complete("p", {"utility_model": "claude-haiku-4-5"})
    asyncio.run(go())
    assert built["n"] == 1
