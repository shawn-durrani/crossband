"""list_models keeps what the provider actually said (#263).

The Anthropic Models API returns max_input_tokens (context window),
max_tokens (output cap) and capabilities on every model object; for a long
time list_models threw all three away, so nothing in the app could answer
"does this chat fit that model". These tests pin the pass-through, and pin
that absence stays honest: a field the provider did not supply is None -
explicitly unknown - never a guessed default. OpenAI's /v1/models has no
equivalent fields at all, so its records carry None for all three while
keeping the same five-key shape.
"""

import sys
from types import SimpleNamespace

import pytest

from backend.providers import _capability_data, list_models


class _FakeAnthropic:
    """Stands in for anthropic.Anthropic: .models.list() yields model objects."""

    last_kwargs = None

    def __init__(self, **kwargs):
        _FakeAnthropic.last_kwargs = kwargs
        self.models = SimpleNamespace(list=lambda: _FakeAnthropic.payload)


class _FakeOpenAI:
    def __init__(self, **kwargs):
        self.models = SimpleNamespace(list=lambda: _FakeOpenAI.payload)


@pytest.fixture
def anthropic_stub(monkeypatch):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "test-key")
    monkeypatch.setitem(sys.modules, "anthropic",
                        SimpleNamespace(Anthropic=_FakeAnthropic))
    return _FakeAnthropic


@pytest.fixture
def openai_stub(monkeypatch):
    monkeypatch.setenv("OPENAI_API_KEY", "test-key")
    monkeypatch.setitem(sys.modules, "openai",
                        SimpleNamespace(OpenAI=_FakeOpenAI))
    return _FakeOpenAI


def test_anthropic_fields_ride_along(anthropic_stub):
    anthropic_stub.payload = [SimpleNamespace(
        id="claude-opus-5", display_name="Claude Opus 5",
        max_input_tokens=500_000, max_tokens=64_000,
        capabilities=["effort", "vision", "pdf"],
    )]
    (rec,) = list_models("anthropic")
    assert rec == {
        "id": "claude-opus-5",
        "label": "Claude Opus 5",
        "max_input_tokens": 500_000,
        "max_tokens": 64_000,
        "capabilities": ["effort", "vision", "pdf"],
    }


def test_missing_fields_are_none_not_guessed(anthropic_stub):
    # An older SDK's model object without the March-2026 fields: every key is
    # still present, valued None, so "unknown" can never read as a real limit.
    anthropic_stub.payload = [SimpleNamespace(id="claude-haiku-4-5",
                                              display_name=None)]
    (rec,) = list_models("anthropic")
    assert rec["label"] == "claude-haiku-4-5"  # display_name None -> id
    assert rec["max_input_tokens"] is None
    assert rec["max_tokens"] is None
    assert rec["capabilities"] is None


def test_openai_records_keep_the_shape_with_unknowns(openai_stub):
    _FakeOpenAI.payload = [
        SimpleNamespace(id="gpt-5.6-terra"),
        SimpleNamespace(id="text-embedding-3-large"),  # non-chat: filtered
    ]
    (rec,) = list_models("openai")
    assert rec == {"id": "gpt-5.6-terra", "label": "gpt-5.6-terra",
                   "max_input_tokens": None, "max_tokens": None,
                   "capabilities": None}


def test_capability_data_unwraps_sdk_objects():
    class Caps:
        def model_dump(self):
            return {"effort": True, "vision": False}

    assert _capability_data(Caps()) == {"effort": True, "vision": False}
    assert _capability_data(None) is None
    assert _capability_data(["a", "b"]) == ["a", "b"]
    # An unrecognised object degrades to its string, never to an exception.
    assert isinstance(_capability_data(object()), str)
