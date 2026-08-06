"""Keyless-local edge for the OpenAI-compatible adapter.

Local OpenAI-compatible servers (Ollama, LM Studio) need no auth, but the SDK
refuses to construct without a non-empty api_key. `_openai_client` supplies a
harmless placeholder when a base_url is set and no key is configured — while the
default OpenAI endpoint (no base_url) must still demand a real key. No network:
AsyncOpenAI construction is captured, never called out.
"""

import pytest

from backend import providers


class FakeAsyncOpenAI:
    """Records the api_key/base_url it was constructed with; never connects."""
    def __init__(self, api_key=None, base_url=None):
        self.api_key = api_key
        self.base_url = base_url


@pytest.fixture(autouse=True)
def _isolate(monkeypatch):
    # Fresh client cache per test, and a stub SDK so no real client is built.
    monkeypatch.setattr(providers, "_openai_clients", {})
    import openai
    monkeypatch.setattr(openai, "AsyncOpenAI", FakeAsyncOpenAI)
    # Ensure no ambient key leaks in from the environment.
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    monkeypatch.delenv("OLLAMA_KEY", raising=False)


def test_local_base_url_no_key_uses_placeholder():
    """base_url set + no key configured -> placeholder, construction succeeds."""
    p = {"provider": "openai", "name": "Llama (local)", "model": "llama3.1",
         "base_url": "http://localhost:11434/v1", "api_key_env": ""}
    client = providers._openai_client(p)
    assert client.api_key == "no-key"          # SDK gets a non-empty key
    assert client.base_url == "http://localhost:11434/v1"


def test_base_url_with_key_present_uses_real_key(monkeypatch):
    """A real key, when set, still wins over the placeholder."""
    monkeypatch.setenv("GROQ_API_KEY", "gsk-live-123")
    p = {"provider": "openai", "name": "Groq", "model": "llama-3.1-8b-instant",
         "base_url": "https://api.groq.com/openai/v1", "api_key_env": "GROQ_API_KEY"}
    client = providers._openai_client(p)
    assert client.api_key == "gsk-live-123"
    assert client.base_url == "https://api.groq.com/openai/v1"


def test_default_endpoint_no_base_url_still_requires_key():
    """No base_url + no key -> the real 'set OPENAI_API_KEY' error, not masked."""
    p = {"provider": "openai", "name": "GPT", "model": "gpt-5.1",
         "base_url": "", "api_key_env": ""}
    with pytest.raises(RuntimeError, match="OPENAI_API_KEY is not set"):
        providers._openai_client(p)


def test_default_endpoint_with_key_unchanged(monkeypatch):
    """No base_url + key present -> unchanged: real key, base_url None."""
    monkeypatch.setenv("OPENAI_API_KEY", "sk-real-key")
    p = {"provider": "openai", "name": "GPT", "model": "gpt-5.1",
         "base_url": "", "api_key_env": ""}
    client = providers._openai_client(p)
    assert client.api_key == "sk-real-key"
    assert client.base_url is None
