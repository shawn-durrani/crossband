"""Reasoning-effort gating table - ported semantics from the predecessor:
low/medium/high/max normalized per participant, translated per provider,
gated by model so unsupported combinations are omitted rather than 400ing.

This file also pins the reasoning-POLICY fix: `thinking` is no longer
forced on every Anthropic call. It must be omitted for every level except an
explicit "adaptive" choice, and `thinking`/`output_config.effort` are never
sent together (see backend/providers.py's _anthropic_thinking docstring for
why, verified against anthropic==0.116.0's message_create_params)."""

import pytest

from backend.providers import (
    _anthropic_effort,
    _anthropic_thinking,
    _openai_effort,
    reasoning_choices,
    valid_reasoning_effort,
)


def p(effort, model):
    return {"reasoning_effort": effort, "model": model}


@pytest.mark.parametrize("effort,model,expected", [
    # '' / unknown -> provider default (omit)
    ("", "claude-opus-4-8", None),
    (None, "claude-opus-4-8", None),
    ("turbo", "claude-opus-4-8", None),
    # plain levels pass through on supporting models
    ("low", "claude-opus-4-8", "low"),
    ("medium", "claude-opus-4-6", "medium"),
    ("high", "claude-sonnet-4-6", "high"),
    # effort unsupported on these families -> omit entirely (would 400)
    ("high", "claude-haiku-4-5", None),
    ("low", "claude-3-5-sonnet-20241022", None),
    ("medium", "claude-sonnet-4-5", None),
    ("medium", "claude-sonnet-4-0", None),
    # "max" needs Opus 4.6+ / Sonnet 4.6 / Fable; others cap at high
    ("max", "claude-opus-4-8", "max"),
    ("max", "claude-opus-4-7", "max"),
    ("max", "claude-opus-4-6", "max"),
    ("max", "claude-sonnet-4-6", "max"),
    ("max", "claude-fable-5", "max"),
    ("max", "claude-opus-4-5", "high"),
])
def test_anthropic_effort_gating(effort, model, expected):
    assert _anthropic_effort(p(effort, model)) == expected


@pytest.mark.parametrize("effort,model,expected", [
    ("", "gpt-5.1", None),
    ("low", "gpt-5.1", "low"),
    ("medium", "o3-mini", "medium"),
    ("high", "gpt-5.5", "high"),
    # OpenAI scale caps at high - "max" translates down
    ("max", "gpt-5.5", "high"),
    # non-reasoning models: omit entirely
    ("high", "gpt-4o", None),
    ("low", "llama-3-70b", None),
    # "adaptive" isn't an OpenAI concept at all - never applies here
    ("adaptive", "gpt-5.1", None),
])
def test_openai_effort_gating(effort, model, expected):
    assert _openai_effort(p(effort, model)) == expected


# ---------- `thinking` is never forced ----------

@pytest.mark.parametrize("effort,model", [
    ("", "claude-opus-4-8"),
    (None, "claude-opus-4-8"),
    ("low", "claude-opus-4-8"),
    ("medium", "claude-opus-4-6"),
    ("high", "claude-sonnet-4-6"),
    ("max", "claude-opus-4-8"),
    ("max", "claude-opus-4-5"),  # downgrades to high effort, still no thinking
    ("medium", "claude-haiku-4-5"),  # unsupported family, still None not a crash
])
def test_thinking_omitted_for_default_and_fixed_levels(effort, model):
    """The fix: NEITHER Default NOR any fixed low/medium/high/max level
    ever sends `thinking` - reasoning depth on those is controlled solely by
    output_config.effort. Previously `thinking={"type": "adaptive"}` was sent
    unconditionally here, which is exactly what made a "medium" participant's
    voice replies pay for open-ended, variable-duration deliberation."""
    assert _anthropic_thinking(p(effort, model)) is None


@pytest.mark.parametrize("model,expected", [
    ("claude-opus-4-8", {"type": "adaptive"}),
    ("claude-sonnet-4-6", {"type": "adaptive"}),
    # unsupported families degrade to no override rather than a 400 - same
    # gating list as _anthropic_effort's
    ("claude-haiku-4-5", None),
    ("claude-3-5-sonnet-20241022", None),
    ("claude-sonnet-4-5", None),
])
def test_thinking_only_for_explicit_adaptive_choice(model, expected):
    assert _anthropic_thinking(p("adaptive", model)) == expected


def test_adaptive_never_also_sets_output_config_effort():
    """"adaptive" is a thinking-mode choice, not an output_config.effort
    value - _anthropic_effort must return None for it so the two are never
    stacked in the same request (see backend/providers.py's _stream_anthropic:
    thinking and output_config.effort are sent as ALTERNATIVES)."""
    part = p("adaptive", "claude-opus-4-8")
    assert _anthropic_thinking(part) == {"type": "adaptive"}
    assert _anthropic_effort(part) is None


# ---------- provider-aware reasoning_effort validation ----------

def test_reasoning_choices_anthropic_includes_adaptive():
    assert reasoning_choices("anthropic") == ("", "low", "medium", "high", "max", "adaptive")


def test_reasoning_choices_openai_excludes_adaptive():
    assert "adaptive" not in reasoning_choices("openai")


@pytest.mark.parametrize("provider,value,ok", [
    ("anthropic", "", True),
    ("anthropic", "medium", True),
    ("anthropic", "adaptive", True),
    ("anthropic", "turbo", False),
    ("openai", "", True),
    ("openai", "high", True),
    ("openai", "adaptive", False),  # Anthropic-only concept
    (None, "adaptive", False),  # unknown provider falls back to the stricter set
])
def test_valid_reasoning_effort(provider, value, ok):
    assert valid_reasoning_effort(provider, value) is ok
