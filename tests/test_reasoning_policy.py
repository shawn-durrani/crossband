"""The reasoning-effort policy must be AUTHORITATIVE at the actual
request-kwargs level, not just in the translation helpers (tests/test_effort.py
covers those in isolation). This file drives providers.stream_reply end to end
against a fake Anthropic client and inspects the literal kwargs that would hit
the API - proving `thinking={"type": "adaptive"}` is no longer sent
unconditionally, and that a fixed effort level and "adaptive" are never sent
together in the same request."""

import asyncio

import pytest

from backend import providers

PARTICIPANT = {"name": "Claude", "slug": "claude", "model": "claude-opus-4-8",
               "provider": "anthropic", "system_prompt": ""}
ROSTER = [{"name": "Claude", "slug": "claude"}, {"name": "GPT", "slug": "gpt"}]


class _FakeUsage:
    input_tokens = 10
    output_tokens = 5
    cache_read_input_tokens = 0
    cache_creation_input_tokens = 0
    cache_creation = None


class _FakeFinalMessage:
    usage = _FakeUsage()
    stop_reason = "end_turn"
    content = []


class _FakeStreamCtx:
    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc, tb):
        return False

    @property
    def text_stream(self):
        async def gen():
            yield "hi"
        return gen()

    async def get_final_message(self):
        return _FakeFinalMessage()


class _FakeMessagesAPI:
    def __init__(self):
        self.captured_kwargs = None

    def stream(self, **kwargs):
        self.captured_kwargs = kwargs
        return _FakeStreamCtx()


class _FakeAnthropicClient:
    def __init__(self):
        self.messages = _FakeMessagesAPI()


def _drive(monkeypatch, cfg, participant):
    fake_client = _FakeAnthropicClient()
    monkeypatch.setattr(providers, "_anthropic_client", lambda p: fake_client)

    async def go():
        async for _ in providers.stream_reply(
            participant, ROSTER, [], {}, cfg, None, "", False):
            pass

    asyncio.run(go())
    return fake_client.messages.captured_kwargs


@pytest.mark.parametrize("reasoning_effort", ["", "low", "medium", "high", "max"])
def test_default_and_fixed_levels_never_send_thinking(cfg, monkeypatch, reasoning_effort):
    """The core regression guard: a participant saved as Default OR any
    fixed level must never trigger the model's own unbounded deliberation -
    `thinking` must be entirely absent from the request kwargs."""
    part = dict(PARTICIPANT, reasoning_effort=reasoning_effort)
    kwargs = _drive(monkeypatch, dict(cfg), part)
    assert "thinking" not in kwargs


def test_medium_still_sets_output_config_effort(cfg, monkeypatch):
    """The companion half of the same guard: a configured level must actually
    reach the API. That was already true before the policy fix, but is
    re-pinned here alongside the `thinking` assertion so the two cannot drift
    again (e.g. a future edit disabling both instead of just `thinking`)."""
    part = dict(PARTICIPANT, reasoning_effort="medium")
    kwargs = _drive(monkeypatch, dict(cfg), part)
    assert kwargs["output_config"] == {"effort": "medium"}
    assert "thinking" not in kwargs


def test_default_sends_no_effort_or_thinking_override_at_all(cfg, monkeypatch):
    part = dict(PARTICIPANT, reasoning_effort="")
    kwargs = _drive(monkeypatch, dict(cfg), part)
    assert "thinking" not in kwargs
    assert "output_config" not in kwargs


def test_adaptive_sends_thinking_and_never_output_config_effort(cfg, monkeypatch):
    """"adaptive" is the ONLY route to {"type": "adaptive"} - and it's an
    alternative to output_config.effort, never stacked with it (verified
    against anthropic==0.116.0: `thinking` is fully optional/independent)."""
    part = dict(PARTICIPANT, reasoning_effort="adaptive")
    kwargs = _drive(monkeypatch, dict(cfg), part)
    assert kwargs["thinking"] == {"type": "adaptive"}
    assert "output_config" not in kwargs


def test_voice_mode_does_not_change_the_reasoning_policy(cfg, monkeypatch):
    """Voice must obey the saved participant policy, never silently downgrade
    (or upgrade) it just because the chat is a live voice session - same
    kwargs whether voice_mode is True or False."""
    part = dict(PARTICIPANT, reasoning_effort="medium")

    async def go(voice_mode):
        fake_client = _FakeAnthropicClient()
        monkeypatch.setattr(providers, "_anthropic_client", lambda p: fake_client)
        async for _ in providers.stream_reply(
            part, ROSTER, [], {}, dict(cfg), None, "", voice_mode):
            pass
        return fake_client.messages.captured_kwargs

    text_kwargs = asyncio.run(go(False))
    voice_kwargs = asyncio.run(go(True))
    assert text_kwargs["output_config"] == voice_kwargs["output_config"] == {"effort": "medium"}
    assert "thinking" not in text_kwargs and "thinking" not in voice_kwargs


def test_adaptive_unsupported_model_degrades_to_no_override(cfg, monkeypatch):
    """A model that rejects extended thinking (same family list as effort's
    gating) degrades to sending nothing, not a 400."""
    part = dict(PARTICIPANT, model="claude-haiku-4-5", reasoning_effort="adaptive")
    kwargs = _drive(monkeypatch, dict(cfg), part)
    assert "thinking" not in kwargs
    assert "output_config" not in kwargs
