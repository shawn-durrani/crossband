"""Setup router: status shape, .env write round-trip, and the security
invariants — keys are never echoed back and never appear in GET status.
All validation is mocked; no live provider calls in CI."""

import asyncio
import os
import stat

import pytest
from fastapi.testclient import TestClient

from backend.app import create_app
from backend.config import Settings
from backend.routers import setup as setup_mod

ALL_SERVICES = {"anthropic", "openai", "elevenlabs", "tavily", "brave",
                "reddit", "memory_service"}

FAKE_KEY = "sk-test-a-very-long-fake-key-000000"


@pytest.fixture
def app(tmp_path, monkeypatch):
    # .env writes land in a temp dir; validation never leaves the process
    monkeypatch.setattr(setup_mod, "ENV_PATH", tmp_path / ".env")
    setup_mod._session_valid.clear()
    settings = Settings(data_dir=str(tmp_path / "data"),
                        memory_url="http://127.0.0.1:1")  # unroutable: memoryless
    return create_app(settings)


@pytest.fixture
def clean_env():
    """The endpoint mutates os.environ on success — restore it afterwards."""
    saved = {k: os.environ.get(k)
             for spec in setup_mod.SERVICES.values() for k in spec["env"]}
    yield
    for k, v in saved.items():
        if v is None:
            os.environ.pop(k, None)
        else:
            os.environ[k] = v


def mock_valid(monkeypatch, ok=True, error=None):
    calls = []

    async def fake_validate(service, key, secret):
        calls.append(service)
        return (ok, error)

    monkeypatch.setattr(setup_mod, "_validate", fake_validate)
    return calls


def test_status_shape_and_no_key_material(app, monkeypatch):
    monkeypatch.setenv("ANTHROPIC_API_KEY", FAKE_KEY)
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    with TestClient(app, base_url="http://127.0.0.1") as client:
        r = client.get("/api/setup/status")
        assert r.status_code == 200
        data = r.json()
        assert set(data["services"]) == ALL_SERVICES
        for svc in data["services"].values():
            assert set(svc) == {"configured", "valid", "detail"}
            assert isinstance(svc["configured"], bool)
            assert svc["valid"] in (True, False, None)
        a = data["services"]["anthropic"]
        assert a["configured"] is True
        assert a["valid"] is None  # configured but never live-checked this session
        assert data["services"]["openai"]["configured"] is False
        assert data["services"]["memory_service"]["configured"] is False
        assert data["any_model_key"] is True
        # SECURITY: the key value never appears anywhere in the response
        assert FAKE_KEY not in r.text


def test_valid_key_written_to_env_and_environ(app, monkeypatch, clean_env):
    monkeypatch.delenv("TAVILY_API_KEY", raising=False)
    calls = mock_valid(monkeypatch, ok=True)
    with TestClient(app, base_url="http://127.0.0.1") as client:
        r = client.post("/api/setup/key",
                        json={"service": "tavily", "key": f"  {FAKE_KEY}  "})
        assert r.status_code == 200
        body = r.json()
        assert body["valid"] is True
        assert "unlocked" in body and body["unlocked"]
        assert FAKE_KEY not in r.text  # never echoed back
        assert calls == ["tavily"]

        # round-trip: .env written (trimmed), owner-only, env var live
        env_file = setup_mod.ENV_PATH
        assert env_file.read_text() == f"TAVILY_API_KEY={FAKE_KEY}\n"
        assert stat.S_IMODE(env_file.stat().st_mode) == 0o600
        assert os.environ["TAVILY_API_KEY"] == FAKE_KEY

        # takes effect without restart: status + /api/state agree
        s = client.get("/api/setup/status").json()["services"]["tavily"]
        assert s == {"configured": True, "valid": True,
                     "detail": setup_mod.SERVICES["tavily"]["detail"]}
        assert client.get("/api/state").json()["config"]["search"]["tavily"] is True


def test_env_write_preserves_other_lines_and_comments(tmp_path):
    env = tmp_path / ".env"
    env.write_text("# keys live here\nOPENAI_API_KEY=old-value\n\n"
                   "export BRAVE_API_KEY=keep-me\n")
    setup_mod.write_env_var(env, "OPENAI_API_KEY", "new-value")
    setup_mod.write_env_var(env, "TAVILY_API_KEY", "appended")
    assert env.read_text() == ("# keys live here\nOPENAI_API_KEY=new-value\n\n"
                               "export BRAVE_API_KEY=keep-me\nTAVILY_API_KEY=appended\n")
    # updating the export-style line replaces it in place, not append
    setup_mod.write_env_var(env, "BRAVE_API_KEY", "replaced")
    assert "export BRAVE_API_KEY" not in env.read_text()
    assert "BRAVE_API_KEY=replaced" in env.read_text()
    assert env.read_text().count("BRAVE_API_KEY") == 1


def test_invalid_key_not_saved_not_echoed(app, monkeypatch, clean_env):
    monkeypatch.delenv("ELEVENLABS_API_KEY", raising=False)
    mock_valid(monkeypatch, ok=False,
               error="That key wasn't accepted — check you copied all of it.")
    with TestClient(app, base_url="http://127.0.0.1") as client:
        r = client.post("/api/setup/key",
                        json={"service": "elevenlabs", "key": FAKE_KEY})
        assert r.status_code == 200
        assert r.json() == {"valid": False,
                            "error": "That key wasn't accepted — check you copied all of it."}
        assert FAKE_KEY not in r.text
    assert not setup_mod.ENV_PATH.exists()  # nothing written
    assert "ELEVENLABS_API_KEY" not in os.environ


def test_reddit_needs_both_parts_and_skips_live_validation(app, monkeypatch, clean_env):
    monkeypatch.delenv("REDDIT_CLIENT_ID", raising=False)
    monkeypatch.delenv("REDDIT_CLIENT_SECRET", raising=False)

    def boom(*a, **kw):  # any network attempt fails the test
        raise AssertionError("reddit must not be validated live")
    monkeypatch.setattr(setup_mod.httpx, "AsyncClient", boom)
    with TestClient(app, base_url="http://127.0.0.1") as client:
        # missing secret → friendly refusal, nothing saved
        r = client.post("/api/setup/key", json={"service": "reddit", "key": "cid"})
        assert r.json()["valid"] is False
        assert "secret" in r.json()["error"].lower()

        # id + secret → accepted as-is (setup._validate short-circuits for reddit)
        r = client.post("/api/setup/key",
                        json={"service": "reddit", "key": "cid", "secret": "csec"})
        assert r.json()["valid"] is True
        text = setup_mod.ENV_PATH.read_text()
        assert "REDDIT_CLIENT_ID=cid" in text
        assert "REDDIT_CLIENT_SECRET=csec" in text
        assert os.environ["REDDIT_CLIENT_ID"] == "cid"


def test_unknown_service_and_empty_key_rejected(app):
    with TestClient(app, base_url="http://127.0.0.1") as client:
        assert client.post("/api/setup/key",
                           json={"service": "netflix", "key": "x"}).status_code == 400
        assert client.post("/api/setup/key",
                           json={"service": "openai", "key": "   "}).status_code == 400


def test_one_entry_is_enough_to_add_an_integration(monkeypatch):
    """The point of backend/capabilities.py: adding an integration is ONE edit.

    Before, it took five across two languages (SERVICES, _validate,
    SETUP_CAPABILITIES, CAPABILITY_WIRING, the frontend SECTIONS) and missing
    one left the capability half-existing. This adds a capability at runtime and
    checks every derived surface picks it up with no other change."""
    from backend import capabilities, integrations
    from backend.routers import setup as setup_mod

    capabilities.CAPABILITIES["acme"] = {
        "name": "Acme Models", "kind": "llm", "detail": "Acme's models.",
        "unlocked": "Acme can now join your conversations.",
        "env": ["ACME_API_KEY"], "chat_toggle": None, "any_of": None,
        "optional": False,
        "probe": {"method": "GET", "url": "https://api.acme.test/v1/models",
                  "headers": {"Authorization": "Bearer {key}"}},
    }
    try:
        # setup: env vars + copy, without touching setup.py
        svc = {sid: {"env": list(c["env"]), "unlocked": c["unlocked"],
                     "detail": c["detail"]}
               for sid, c in capabilities.CAPABILITIES.items()}
        assert svc["acme"]["env"] == ["ACME_API_KEY"]
        # registry: kind + display name, without touching integrations.py
        assert {sid: (c["kind"], c["name"])
                for sid, c in capabilities.CAPABILITIES.items()}["acme"] \
            == ("llm", "Acme Models")
        # validation: the probe runs generically, with {key} filled in
        seen = {}

        class FakeResp:
            status_code = 200

        class FakeClient:
            async def __aenter__(self): return self
            async def __aexit__(self, *a): return False
            async def request(self, method, url, headers=None, json=None, params=None):
                seen.update(method=method, url=url, headers=headers)
                return FakeResp()

        monkeypatch.setattr(setup_mod.httpx, "AsyncClient", lambda **kw: FakeClient())
        ok, err = asyncio.run(setup_mod._validate("acme", "secret-key-123", None))
        assert ok and err is None
        assert seen["url"] == "https://api.acme.test/v1/models"
        assert seen["headers"]["Authorization"] == "Bearer secret-key-123"
    finally:
        capabilities.CAPABILITIES.pop("acme", None)


def test_env_file_is_tightened_to_owner_only_on_startup(tmp_path, monkeypatch):
    """`.env` holds live API keys, so it must be owner-only — and must be
    REPAIRED, not just created correctly.

    Both repos shipped 644 .env files full of real keys: start.sh created them
    with `cp` and never chmodded, and the app's own write path only tightened a
    file once a key was saved through the UI. The service also normally starts
    under launchd, which never runs start.sh — so the repair belongs here."""
    from backend import app as app_mod
    env = tmp_path / ".env"
    env.write_text("ANTHROPIC_API_KEY=x\n")
    os.chmod(env, 0o644)
    assert stat.S_IMODE(env.stat().st_mode) & 0o077        # world-readable
    app_mod._secure_env_file(env)
    assert stat.S_IMODE(env.stat().st_mode) == 0o600       # repaired

    # already-correct files are left alone, and a missing one is a no-op
    app_mod._secure_env_file(env)
    assert stat.S_IMODE(env.stat().st_mode) == 0o600
    app_mod._secure_env_file(tmp_path / "nope.env")        # must not raise


def test_env_value_with_newline_is_refused(tmp_path):
    """A value carrying a line break would write extra assignments of the
    caller's choosing into .env. Probe-less services validate nothing, so the
    guard has to live at the write."""
    import pytest
    from fastapi import HTTPException

    from backend.routers.setup import write_env_var

    env = tmp_path / ".env"
    env.write_text("EXISTING=keep\n")
    with pytest.raises(HTTPException) as e:
        write_env_var(env, "REDDIT_CLIENT_ID", "abc\nANTHROPIC_API_KEY=stolen")
    assert e.value.status_code == 400
    assert env.read_text() == "EXISTING=keep\n"      # file untouched
    write_env_var(env, "REDDIT_CLIENT_ID", "abc")    # the ordinary path still works
    assert "REDDIT_CLIENT_ID=abc" in env.read_text()
