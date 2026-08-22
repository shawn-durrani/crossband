"""Ollama keep-alive: hold a seat's model loaded between turns.

Ollama unloads a model five minutes after its last request, and a seat talks
to it through the OpenAI-compatible layer - whose source carries no
keep_alive field at all, so the parameter structurally cannot ride the seat's
own request. The setting therefore travels Ollama's documented native route:
a near-empty /api/generate nudge (empty prompt, the documented shape for
unloading with keep_alive=0) sent just before the seat's real request.

Pinned properties, in order of importance:

1. Nothing fires unless the owner set it - the default seat's request is
   byte for byte what it was before this setting existed, and no nudge goes
   out at all.
2. A set seat nudges exactly once per reply, with the Ollama-documented
   shape, before its first real request - and the real request itself is
   unchanged.
3. A nudge the endpoint rejects (HTTP >= 400) or cannot reach never breaks
   the turn: retention is a side channel, the seat's speech is the point -
   the warning names the setting instead.
4. Where the setting cannot apply (no base_url = OpenAI proper, Anthropic
   seats, unparseable stored values) it stays inert, and the router rejects
   a value a seat can never apply with a 400 naming it.

No network: a fake httpx.AsyncClient records the nudge, a fake openai client
serves the real request. Run with `.venv/bin/python -m pytest -q`.
"""

import asyncio
from types import SimpleNamespace

import httpx
import pytest
from fastapi.testclient import TestClient

from backend import db, providers
from backend.app import create_app
from backend.config import Settings

BASE_URL = "http://127.0.0.1:8999/v1"

P = {"slug": "llama", "name": "Llama", "provider": "openai",
     "model": "local-model", "base_url": BASE_URL, "api_key_env": ""}
CFG = {"max_tool_rounds": 3, "max_response_tokens": 64,
       "attribution_audit": False, "user_name": "Owner"}
INPUT_ITEMS = [{"role": "user",
                "content": [{"type": "input_text", "text": "hello there"}]}]


@pytest.fixture(autouse=True)
def _local_seat(monkeypatch):
    monkeypatch.setattr(providers, "_chat_completions_only", {BASE_URL})
    monkeypatch.setattr(providers, "build_openai_input",
                        lambda *a, **k: [dict(i) for i in INPUT_ITEMS])


@pytest.fixture
def nudges():
    """Records every nudge the code attempts; also serves as the fake
    transport, so 'no nudge' is observable as an empty list."""
    calls = []

    class FakeAsyncClient:
        def __init__(self, **kwargs):
            calls.append(kwargs)

        async def __aenter__(self):
            return self

        async def __aexit__(self, *exc):
            return False

        async def post(self, url, json=None):
            calls.append({"url": url, "json": dict(json or {})})
            return self._resp

    FakeAsyncClient._resp = SimpleNamespace(status_code=200, text="ok")
    monkeypatch = pytest.MonkeyPatch()
    monkeypatch.setattr(httpx, "AsyncClient", FakeAsyncClient)
    yield calls
    monkeypatch.undo()


def text_seat(monkeypatch):
    """The real request leg: a fake openai client that streams one chunk,
    so the nudge test can prove the seat still speaks after it."""
    from tests.test_thinking_control import FakeClient

    class C(FakeClient):
        pass

    client = C()
    monkeypatch.setattr(providers, "_openai_client", lambda _p: client)
    return client


def _run(p, monkeypatch):
    async def collect():
        return [ev async for ev in providers._stream_openai(
            p, "STABLE", "", [], {}, CFG, [], None)]

    return asyncio.run(collect())


# ---------- what fires ----------

def test_default_seat_send_nothing_and_make_no_nudge(monkeypatch, nudges):
    """The backward-compatibility guard: an untouched seat must go out with
    exactly the request it had before, and no keep-alive traffic at all."""
    text_seat(monkeypatch)
    events = _run(dict(P), monkeypatch)
    assert nudges == []
    assert "".join(v for k, v in events if k == "text") == "hi"


def test_empty_string_is_the_same_as_unset(monkeypatch, nudges):
    text_seat(monkeypatch)
    _run(dict(P, keep_alive=""), monkeypatch)
    assert nudges == []


def test_set_seat_nudges_once_with_the_documented_shape(monkeypatch, nudges):
    text_seat(monkeypatch)
    _run(dict(P, keep_alive="30m"), monkeypatch)
    post = [c for c in nudges if "url" in c]
    assert len(post) == 1, "one nudge per reply - not per tool round"
    assert post[0]["url"] == "http://127.0.0.1:8999/api/generate"
    # Ollama's documented retention shape: model named, empty prompt, the
    # window on keep_alive. Nothing else - no transcript, no settings.
    assert post[0]["json"] == {"model": "local-model", "prompt": "",
                               "keep_alive": "30m"}


def test_nudge_targets_the_native_route_regardless_of_v1_suffix(nudges):
    assert providers.keep_alive_url(dict(P)) == "http://127.0.0.1:8999/api/generate"
    assert providers.keep_alive_url(dict(P, base_url="http://127.0.0.1:8999")) \
        == "http://127.0.0.1:8999/api/generate"
    assert providers.keep_alive_url(dict(P, base_url="http://127.0.0.1:8999/v1/")) \
        == "http://127.0.0.1:8999/api/generate"
    assert providers.keep_alive_url(dict(P, base_url="")) == ""


def test_indefinite_window_passes_through(monkeypatch, nudges):
    text_seat(monkeypatch)
    _run(dict(P, keep_alive="-1"), monkeypatch)
    post = [c for c in nudges if "url" in c]
    assert post and post[0]["json"]["keep_alive"] == "-1"


# ---------- where it must stay inert ----------

def test_openai_proper_is_inert():
    """No base_url means OpenAI itself, which has no native route to nudge.
    Preserving that endpoint's behaviour is the point of the gate."""
    p = dict(P, base_url="", keep_alive="30m")
    assert providers.keep_alive_nudge(p) == ""


def test_anthropic_seat_is_inert():
    p = dict(P, provider="anthropic", keep_alive="30m")
    assert providers.keep_alive_nudge(p) == ""


@pytest.mark.parametrize("value", ["45", "forever", "30", "1.5",
                                   "30M", "1h30", ""])
def test_unparseable_stored_value_is_inert(value):
    """A row written by an older or hand-edited client must not become a
    guess about what the server wanted."""
    assert providers.keep_alive_nudge(dict(P, keep_alive=value)) == ""


@pytest.mark.parametrize("value", ["30m", "1h", "24h", "1h30m", "-1"])
def test_valid_windows_are_accepted(value):
    assert providers.keep_alive_nudge(dict(P, keep_alive=value)) == value


# ---------- rejection is loud, the turn is not broken ----------

def test_nudge_404_warns_and_the_seat_still_speaks(monkeypatch, caplog):
    """A server that does not know the native keep-alive call is not Ollama:
    the setting then cannot apply, and the honest outcome is a loud warning
    plus the turn going through - not a dead seat."""
    class FakeAsyncClient:
        def __init__(self, **kwargs):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *exc):
            return False

        async def post(self, url, json=None):
            return SimpleNamespace(status_code=404, text="no such route")

    monkeypatch.setattr(httpx, "AsyncClient", FakeAsyncClient)
    text_seat(monkeypatch)
    with caplog.at_level("WARNING", logger="crossband.providers"):
        events = _run(dict(P, keep_alive="30m"), monkeypatch)
    assert "".join(v for k, v in events if k == "text") == "hi"
    assert any("keep_alive" in m for m in caplog.messages)


def test_nudge_unreachable_turns_proceed_anyway(monkeypatch):
    """Same contract when the nudge cannot connect: the seat speaks, the
    warning says the setting did not apply."""
    class FakeAsyncClient:
        def __init__(self, **kwargs):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *exc):
            return False

        async def post(self, url, json=None):
            raise httpx.ConnectError("connection refused")

    monkeypatch.setattr(httpx, "AsyncClient", FakeAsyncClient)
    text_seat(monkeypatch)
    events = _run(dict(P, keep_alive="30m"), monkeypatch)
    assert "".join(v for k, v in events if k == "text") == "hi"


# ---------- the router accepts and rejects ----------

@pytest.fixture
def client(tmp_path):
    db.configure(str(tmp_path / "data"))
    settings = Settings(data_dir=str(tmp_path / "data"),
                        memory_url="http://127.0.0.1:1")
    with TestClient(create_app(settings), base_url="http://127.0.0.1") as c:
        yield c


def test_create_saves_a_valid_window(client):
    r = client.post("/api/participants", json={
        "name": "Llama", "provider": "openai", "model": "local-model",
        "base_url": BASE_URL, "keep_alive": "30m"})
    assert r.status_code == 200, r.text
    assert r.json()["keep_alive"] == "30m"


def test_create_rejects_a_unitless_duration(client):
    r = client.post("/api/participants", json={
        "name": "Llama", "provider": "openai", "model": "local-model",
        "base_url": BASE_URL, "keep_alive": "45"})
    assert r.status_code == 400
    assert "keep_alive" in r.json()["detail"]


def test_create_rejects_a_window_on_an_anthropic_seat(client):
    r = client.post("/api/participants", json={
        "name": "Claude-extra", "provider": "anthropic", "model": "a-model",
        "keep_alive": "30m"})
    assert r.status_code == 400
    assert "keep_alive" in r.json()["detail"]


def test_patch_clears_the_window_and_rejects_an_impossible_move(client):
    pid = client.post("/api/participants", json={
        "name": "Llama", "provider": "openai", "model": "local-model",
        "base_url": BASE_URL, "keep_alive": "1h"}).json()["id"]
    r = client.patch(f"/api/participants/{pid}", json={"keep_alive": ""})
    assert r.status_code == 200 and r.json()["keep_alive"] == ""
    # A PATCH may move the provider and set the window in one body; the value
    # must validate against where the seat ends up.
    r = client.patch(f"/api/participants/{pid}",
                     json={"provider": "anthropic", "keep_alive": "30m"})
    assert r.status_code == 400
    # ...but clearing it is always allowed, on any provider, for any seat.
    r = client.patch(f"/api/participants/{pid}",
                     json={"keep_alive": "", "provider": "openai"})
    assert r.status_code == 200


# ---------- migration ----------

def test_v23_database_gains_the_column_defaulting_empty(tmp_path):
    """An existing install migrates up with every seat exactly as it was:
    the new column reads empty, which is the 5-minute-unload behaviour."""
    data = str(tmp_path / "data")
    db.configure(data)
    db.init(Settings(data_dir=data))
    con = db.connect()
    try:
        cur = con.execute("SELECT keep_alive FROM participants LIMIT 1").fetchone()
        assert cur["keep_alive"] == ""
        con.execute("ALTER TABLE participants DROP COLUMN keep_alive")
        con.execute("PRAGMA user_version = 23")
    finally:
        con.close()
    db.init(Settings(data_dir=data))
    row = db.connect().execute(
        "SELECT keep_alive FROM participants LIMIT 1").fetchone()
    assert row["keep_alive"] == ""
