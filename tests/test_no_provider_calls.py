"""No test reaches a model provider (#573). Two fixtures in conftest.py
run around every test. One makes it keyless, as CI is, even where the real
keys are in the environment or the repo's .env. The other refuses the name
lookup for a provider's domain and fails the test that tried, for a test
that sets a key of its own and forgets to stub the call. These pin both,
and that every other name is left alone."""

import asyncio
import os
import socket

import pytest

from conftest import (MODEL_PROVIDER_DOMAINS, PROVIDER_ATTEMPTS, PROXY_ENV,
                      _model_provider_host)


@pytest.mark.parametrize("host", [
    "api.anthropic.com", "api.openai.com", "api.elevenlabs.io",
    "API.Anthropic.com.", b"api.openai.com", "anthropic.com",
])
def test_a_provider_host_matches(host):
    assert _model_provider_host(host)


@pytest.mark.parametrize("host", [
    "127.0.0.1", "localhost", "example.com", "notanthropic.com",
    "api.anthropic.com.example.net", "openai.community", None, "",
])
def test_any_other_host_does_not(host):
    assert not _model_provider_host(host)


def test_the_paid_providers_are_all_listed():
    assert set(MODEL_PROVIDER_DOMAINS) == {"anthropic.com", "openai.com",
                                           "elevenlabs.io"}


def test_a_provider_lookup_is_refused_and_recorded():
    with pytest.raises(socket.gaierror):
        socket.getaddrinfo("api.anthropic.com", 443)
    with pytest.raises(socket.gaierror):
        socket.create_connection(("api.openai.com", 443), timeout=1)
    assert PROVIDER_ATTEMPTS == ["api.anthropic.com", "api.openai.com"]
    PROVIDER_ATTEMPTS.clear()  # this test meant to try; the teardown passes


def test_the_sdk_path_is_refused_too(monkeypatch):
    """The SDKs run on httpx2, not httpx, and retry a refused connection.
    The lookup still refuses every try, and nothing leaves the machine."""
    anthropic = pytest.importorskip("anthropic")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-test-not-a-key")
    client = anthropic.AsyncAnthropic(max_retries=0)

    async def call():
        await client.messages.create(model="claude-haiku-4-5", max_tokens=1,
                                     messages=[{"role": "user", "content": "x"}])

    with pytest.raises(anthropic.APIConnectionError):
        asyncio.run(call())
    assert PROVIDER_ATTEMPTS and set(PROVIDER_ATTEMPTS) == {"api.anthropic.com"}
    PROVIDER_ATTEMPTS.clear()


def test_no_proxy_can_take_the_lookup_away():
    assert not any(os.environ.get(name) for name in PROXY_ENV)


def test_every_test_starts_keyless():
    from backend.config import KEY_ROLES
    assert not any(os.environ.get(k) for k in KEY_ROLES)
    assert not os.environ.get("CLAUDE_CODE_OAUTH_TOKEN")


def test_the_app_never_loads_the_repos_env_file(tmp_path, monkeypatch):
    """The deploy box keeps its real keys in the repo's .env, and create_app
    used to load them into the test process."""
    from backend import app as app_mod
    from backend.config import Settings
    env = tmp_path / "repo"
    env.mkdir()
    (env / ".env").write_text("ANTHROPIC_API_KEY=sk-ant-from-the-file\n")
    monkeypatch.setattr(app_mod, "ROOT", env)
    app_mod.create_app(Settings(data_dir=str(tmp_path / "data"),
                                memory_url="http://127.0.0.1:1"))
    assert not os.environ.get("ANTHROPIC_API_KEY")
