"""Provider seam: `voice_provider` resolves voice
to exactly one engine, or to the clean "voice unavailable" state.

What these tests pin, in order:

1. THE DEFAULT IS TODAY, BYTE-FOR-BYTE. `auto` with no key is voice
   cleanly unavailable, and the rejection sentence is exactly
   "ELEVENLABS_API_KEY not set". A keyless machine and an existing client
   see no difference at all from before the seam existed.
2. THE KEY GATES ELEVENLABS IN BOTH WORDINGS. `auto` and explicit
   `elevenlabs` both need ELEVENLABS_API_KEY, and both select ElevenLabs
   when it is present.
3. THE RESERVED LOCAL PROVIDER NEVER EGRESS. `local`
   resolves to unavailable even WHEN a key is set: no local engine ships
   yet, and until one does the value must mean "nothing", not "cloud".
   Its rejection sentence then names the reservation, not the key.
4. BAD VALUES NEVER CRASH AND NEVER INVENT. Typos and non-string
   providers degrade to `auto` semantics: ElevenLabs with the key,
   nothing without it - the repo convention that a bad value is ignored,
   not crashed on.
5. LAYERING FOLLOWS THE REPO CONVENTION. config.json < config.local.json
   < CROSSBAND_VOICE_PROVIDER, so an operator's machine file and env win
   over the committed default exactly like every other setting.
6. THE CHOKE POINT IS ON THE WIRE. /api/voice/status reflects the
   resolved provider, not just key presence: reserving the local provider
   yields enabled:false, never a ghost ElevenLabs.
"""
import json

import pytest
from fastapi.testclient import TestClient

from backend import voice
from backend.app import create_app
from backend.config import Settings, load_settings


@pytest.fixture(autouse=True)
def _deterministic_key(monkeypatch):
    """Both suites run keyless by design: the key never leaks in from the
    developer's shell, so every selection below is explicit."""
    monkeypatch.delenv("ELEVENLABS_API_KEY", raising=False)


def _cfg(tmp_path, local=None, env=None):
    if local is not None:
        (tmp_path / "config.local.json").write_text(json.dumps(local))
    return load_settings(root=tmp_path, environ=env or {}).as_cfg()


def test_auto_without_key_is_cleanly_unavailable(tmp_path):
    cfg = _cfg(tmp_path)
    assert cfg["voice_provider"] == "auto"  # built-in default
    assert voice.provider_for(cfg) == voice.NO_VOICE
    assert voice.disabled_reason(cfg) == "ELEVENLABS_API_KEY not set"


def test_auto_with_key_selects_elevenlabs(tmp_path, monkeypatch):
    monkeypatch.setenv("ELEVENLABS_API_KEY", "xi-test")
    cfg = _cfg(tmp_path)
    assert voice.provider_for(cfg) == voice.PROVIDER_ELEVENLABS
    assert voice.enabled()  # the old key predicate agrees while the key is set


def test_explicit_elevenlabs_still_needs_the_key(tmp_path):
    cfg = _cfg(tmp_path, local={"voice_provider": "elevenlabs"})
    assert voice.provider_for(cfg) == voice.NO_VOICE
    assert voice.disabled_reason(cfg) == "ELEVENLABS_API_KEY not set"


def test_reserved_local_never_egress(tmp_path, monkeypatch):
    # The trap this file exists to close: the operator asks for the local
    # engines AND holds a key. The reserved provider must mean "nothing"
    # until a local engine ships, never "cloud".
    monkeypatch.setenv("ELEVENLABS_API_KEY", "xi-test")
    cfg = _cfg(tmp_path, local={"voice_provider": voice.PROVIDER_LOCAL})
    assert voice.provider_for(cfg) == voice.NO_VOICE
    reason = voice.disabled_reason(cfg)
    assert reason != "ELEVENLABS_API_KEY not set"
    assert "local" in reason.lower()


def test_unknown_values_degrade_to_auto(tmp_path, monkeypatch):
    bad = {"voice_provider": "whisper-cake"}
    assert voice.provider_for(bad) == voice.NO_VOICE  # no key: nothing
    assert voice.provider_for({"voice_provider": 42}) == voice.NO_VOICE  # no crash
    monkeypatch.setenv("ELEVENLABS_API_KEY", "xi-test")
    assert voice.provider_for(bad) == voice.PROVIDER_ELEVENLABS
    assert voice.provider_for({"voice_provider": 42}) == voice.PROVIDER_ELEVENLABS


def test_layering_and_env(tmp_path, monkeypatch):
    monkeypatch.setenv("ELEVENLABS_API_KEY", "xi-test")
    (tmp_path / "config.json").write_text(
        json.dumps({"voice_provider": "elevenlabs"}))
    local = _cfg(tmp_path, local={"voice_provider": voice.PROVIDER_LOCAL})
    assert local["voice_provider"] == voice.PROVIDER_LOCAL  # local file wins
    assert voice.provider_for(local) == voice.NO_VOICE  # reserved: not shipped
    s = load_settings(root=tmp_path,
                      environ={"CROSSBAND_VOICE_PROVIDER": "elevenlabs"})
    assert s.voice_provider == "elevenlabs"  # env wins over both files
    assert voice.provider_for(s.as_cfg()) == voice.PROVIDER_ELEVENLABS


def test_status_endpoint_reflects_the_provider(tmp_path, monkeypatch):
    """Enabled on the wire means the RESOLVED provider serves voice, not
    merely that a key exists in the environment."""
    monkeypatch.setenv("ELEVENLABS_API_KEY", "xi-test")
    local = Settings(data_dir=str(tmp_path / "data"),
                     memory_url="http://127.0.0.1:1",
                     voice_provider=voice.PROVIDER_LOCAL)
    with TestClient(create_app(local), base_url="http://127.0.0.1") as c:
        assert c.get("/api/voice/status").json() == {"enabled": False}

    # And with the default provider the key still lights the surface on,
    # with the quota read mocked out (the suite never calls a provider).
    monkeypatch.setattr(voice, "subscription", lambda: {})
    plain = Settings(data_dir=str(tmp_path / "data"),
                     memory_url="http://127.0.0.1:1")
    with TestClient(create_app(plain), base_url="http://127.0.0.1") as c:
        assert c.get("/api/voice/status").json()["enabled"] is True


def test_benchmark_catalogue_follows_the_provider(tmp_path, monkeypatch):
    """The benchmark panel's voice-legs flag rides the seam too: reserving
    the local provider reads as no voice even with a key in the
    environment, so the panel can never offer ElevenLabs legs the resolved
    provider would refuse."""
    monkeypatch.setenv("ELEVENLABS_API_KEY", "xi-test")
    local = Settings(data_dir=str(tmp_path / "data"),
                     memory_url="http://127.0.0.1:1",
                     voice_provider=voice.PROVIDER_LOCAL)
    with TestClient(create_app(local), base_url="http://127.0.0.1") as c:
        assert c.get("/api/benchmark").json()["eleven"] is False

    plain = Settings(data_dir=str(tmp_path / "data"),
                     memory_url="http://127.0.0.1:1")
    with TestClient(create_app(plain), base_url="http://127.0.0.1") as c:
        assert c.get("/api/benchmark").json()["eleven"] is True
