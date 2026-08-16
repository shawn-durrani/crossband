"""Thinking control for OpenAI-compatible endpoints (#159).

A Qwen3-family seat on a local server emits a hidden reasoning trace before
its first visible token, and the app had no way to ask it not to. Neither
reasoning-effort dialect reaches such a server, so the seat's only setting was
a no-op. The seat now names the mechanism its own server documents, and the
chat-completions leg sends exactly that.

Two properties matter more than the wire shape. Nothing is sent unless the
owner picked it, so every existing seat keeps its request byte for byte. And a
server that rejects the field fails the turn by name, because a silent retry
without it is indistinguishable from the bug this fixes.

No network: the fake client records kwargs and streams hand-built chunks. Run
with `.venv/bin/python -m pytest -q`.
"""

import asyncio
import sqlite3
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

from backend import db, providers
from backend.app import create_app
from backend.config import Settings

BASE_URL = "http://127.0.0.1:8080/v1"

P = {"slug": "qwen", "name": "Qwen", "provider": "openai",
     "model": "test-model", "base_url": BASE_URL, "api_key_env": ""}
CFG = {"max_tool_rounds": 3, "max_response_tokens": 64,
       "attribution_audit": False, "user_name": "Owner"}
INPUT_ITEMS = [{"role": "user",
                "content": [{"type": "input_text", "text": "hello there"}]}]


@pytest.fixture(autouse=True)
def _fresh_state(monkeypatch):
    monkeypatch.setattr(providers, "_chat_completions_only", {BASE_URL})
    monkeypatch.setattr(providers, "build_openai_input",
                        lambda *a, **k: [dict(i) for i in INPUT_ITEMS])


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


def _finish_chunk(reason="stop"):
    return SimpleNamespace(usage=None, choices=[SimpleNamespace(
        delta=SimpleNamespace(content=None, tool_calls=None),
        finish_reason=reason)])


class _BadRequest(Exception):
    """Stands in for the SDK's BadRequestError: the adapter matches on
    status_code, which is what every 400 from the SDK carries."""
    status_code = 400


class FakeClient:
    """Records every chat-completions kwargs dict. `fail` is raised by the
    first call instead of streaming, for the rejection path."""

    def __init__(self, fail=None):
        self.chat_calls = []
        outer = self

        async def _chat_create(**kwargs):
            outer.chat_calls.append(kwargs)
            if fail is not None:
                raise fail
            return _ChatStream([_text_chunk("hi"), _finish_chunk()])

        self.responses = SimpleNamespace(create=None)
        self.chat = SimpleNamespace(
            completions=SimpleNamespace(create=_chat_create))


def _run(p, client, monkeypatch):
    monkeypatch.setattr(providers, "_openai_client", lambda _p: client)

    async def collect():
        return [ev async for ev in providers._stream_openai(
            p, "STABLE", "", [], {}, CFG, [], None)]

    return asyncio.run(collect())


# ---------- what lands on the wire ----------

@pytest.mark.parametrize("control,expected", [
    ("chat_template_kwargs", {"chat_template_kwargs": {"enable_thinking": False}}),
    ("enable_thinking", {"enable_thinking": False}),
    ("ollama_think", {"think": False}),
])
def test_each_mechanism_sends_its_own_documented_field(monkeypatch, control,
                                                       expected):
    client = FakeClient()
    events = _run(dict(P, thinking_control=control), client, monkeypatch)
    assert client.chat_calls[0]["extra_body"] == expected
    assert "".join(v for k, v in events if k == "text") == "hi"


def test_default_seat_sends_nothing_at_all(monkeypatch):
    """The backward-compatibility guard: an untouched seat's request must be
    exactly what it was before this setting existed."""
    client = FakeClient()
    _run(dict(P), client, monkeypatch)
    kwargs = client.chat_calls[0]
    assert "extra_body" not in kwargs
    assert kwargs["messages"][0] == {"role": "system", "content": "STABLE"}


def test_empty_string_is_the_same_as_unset(monkeypatch):
    client = FakeClient()
    _run(dict(P, thinking_control=""), client, monkeypatch)
    assert "extra_body" not in client.chat_calls[0]


def test_prompt_hint_rides_the_system_message_and_sends_no_field(monkeypatch):
    """The /no_think hack is opt-in, so it applies only on this explicit
    choice and never as a side effect of a Qwen-looking model id."""
    client = FakeClient()
    _run(dict(P, thinking_control="no_think_hint"), client, monkeypatch)
    kwargs = client.chat_calls[0]
    assert "extra_body" not in kwargs
    assert kwargs["messages"][0]["content"] == "STABLE\n\n/no_think"


def test_prompt_hint_never_leaks_into_the_other_mechanisms(monkeypatch):
    client = FakeClient()
    _run(dict(P, thinking_control="ollama_think"), client, monkeypatch)
    assert "/no_think" not in client.chat_calls[0]["messages"][0]["content"]


def test_the_control_persists_across_tool_rounds(monkeypatch):
    """Round two must not quietly revert to thinking-on."""
    client = FakeClient()
    _run(dict(P, thinking_control="enable_thinking"), client, monkeypatch)
    for call in client.chat_calls:
        assert call["extra_body"] == {"enable_thinking": False}


# ---------- where it must stay inert ----------

def test_openai_proper_is_untouched(monkeypatch):
    """No base_url means OpenAI itself, which has no such field. Preserving
    that endpoint's behaviour is the point of gating on base_url."""
    p = dict(P, base_url="", thinking_control="chat_template_kwargs")
    assert providers.thinking_extra_body(p) is None
    assert providers.thinking_prompt_hint(p) == ""


def test_anthropic_seat_is_untouched():
    p = dict(P, provider="anthropic", thinking_control="ollama_think")
    assert providers.thinking_extra_body(p) is None
    assert providers.thinking_prompt_hint(p) == ""


def test_unknown_stored_value_sends_nothing(monkeypatch):
    """A row written by an older or hand-edited client must not become a
    guess about what the server wanted."""
    client = FakeClient()
    _run(dict(P, thinking_control="enable_thinking_pls"), client, monkeypatch)
    assert "extra_body" not in client.chat_calls[0]


def test_extra_body_is_a_fresh_dict_each_call():
    """The table of shapes is module-level; handing it out by reference would
    let one turn's mutation follow every later seat."""
    p = dict(P, thinking_control="enable_thinking")
    first, second = providers.thinking_extra_body(p), providers.thinking_extra_body(p)
    assert first == second and first is not second
    first["enable_thinking"] = True
    assert providers.thinking_extra_body(p) == {"enable_thinking": False}


# ---------- rejection is loud ----------

def test_rejected_control_names_the_setting_and_is_not_retried(monkeypatch):
    """"Surface it rather than pretend it applied": a 400 while the control
    is in flight must not be papered over by a silent retry without it."""
    client = FakeClient(fail=_BadRequest("unrecognized parameter"))
    with pytest.raises(RuntimeError) as exc:
        _run(dict(P, thinking_control="chat_template_kwargs"), client, monkeypatch)
    assert "chat_template_kwargs" in str(exc.value)
    assert "Qwen" in str(exc.value)
    assert len(client.chat_calls) == 1  # no second, control-free attempt


def test_a_400_on_a_default_seat_still_raises_as_itself(monkeypatch):
    """Only a turn that actually sent a control gets the reworded error;
    everything else keeps its original exception."""
    client = FakeClient(fail=_BadRequest("context length exceeded"))
    with pytest.raises(_BadRequest):
        _run(dict(P), client, monkeypatch)


def test_non_400_failures_are_never_blamed_on_the_control(monkeypatch):
    client = FakeClient(fail=RuntimeError("connection reset"))
    with pytest.raises(RuntimeError, match="connection reset"):
        _run(dict(P, thinking_control="ollama_think"), client, monkeypatch)


# ---------- the vocabulary and its validation ----------

def test_choices_are_provider_aware():
    assert providers.thinking_control_choices("anthropic") == ("",)
    assert providers.thinking_control_choices("openai") == \
        providers.THINKING_CONTROL_CHOICES


@pytest.mark.parametrize("provider,value,ok", [
    ("openai", "", True),
    ("openai", None, True),
    ("openai", "chat_template_kwargs", True),
    ("openai", "no_think_hint", True),
    ("openai", "off", False),
    ("anthropic", "", True),
    ("anthropic", "chat_template_kwargs", False),
])
def test_valid_thinking_control(provider, value, ok):
    assert providers.valid_thinking_control(provider, value) is ok


# ---------- the API surface ----------

@pytest.fixture
def app(tmp_path):
    return create_app(Settings(data_dir=str(tmp_path / "data"),
                               memory_url="http://127.0.0.1:1"))


def test_create_and_patch_roundtrip(app):
    with TestClient(app, base_url="http://127.0.0.1") as client:
        r = client.post("/api/participants", json={
            "name": "Local Seat", "provider": "openai", "model": "test-model",
            "base_url": BASE_URL, "thinking_control": "chat_template_kwargs"})
        assert r.status_code == 200
        assert r.json()["thinking_control"] == "chat_template_kwargs"
        pid = r.json()["id"]
        r = client.patch(f"/api/participants/{pid}",
                         json={"thinking_control": "ollama_think"})
        assert r.json()["thinking_control"] == "ollama_think"
        r = client.patch(f"/api/participants/{pid}", json={"thinking_control": ""})
        assert r.json()["thinking_control"] == ""


def test_existing_seats_default_to_no_control(app):
    with TestClient(app, base_url="http://127.0.0.1") as client:
        seats = client.get("/api/state").json()["participants"]
        assert [s["thinking_control"] for s in seats] == ["", ""]


def test_unknown_value_is_rejected_not_stored(app):
    with TestClient(app, base_url="http://127.0.0.1") as client:
        r = client.post("/api/participants", json={
            "name": "Bad Seat", "provider": "openai", "model": "test-model",
            "thinking_control": "disable_thinking_maybe"})
        assert r.status_code == 400
        assert "thinking_control" in r.json()["detail"]


def test_anthropic_seat_cannot_take_a_control(app):
    with TestClient(app, base_url="http://127.0.0.1") as client:
        r = client.post("/api/participants", json={
            "name": "Claude Local", "provider": "anthropic",
            "model": "claude-opus-4-8", "thinking_control": "ollama_think"})
        assert r.status_code == 400


def test_patch_leaves_other_columns_alone(app):
    with TestClient(app, base_url="http://127.0.0.1") as client:
        seat = next(p for p in client.get("/api/state").json()["participants"]
                    if p["provider"] == "openai")
        r = client.patch(f"/api/participants/{seat['id']}",
                         json={"thinking_control": "enable_thinking"}).json()
        assert r["model"] == seat["model"] and r["name"] == seat["name"]
        assert r["reasoning_effort"] == seat["reasoning_effort"]


def test_patch_validates_against_the_effective_provider(app):
    """Moving a seat to Anthropic and setting a control in one body is
    rejected, matching the reasoning_effort rule."""
    with TestClient(app, base_url="http://127.0.0.1") as client:
        pid = next(p["id"] for p in client.get("/api/state").json()["participants"]
                   if p["provider"] == "openai")
        r = client.patch(f"/api/participants/{pid}", json={
            "provider": "anthropic", "thinking_control": "ollama_think"})
        assert r.status_code == 400


# ---------- schema migration (v18 -> v19) ----------

def test_v18_to_v19_migration_lands_every_seat_on_default(tmp_path):
    """An upgraded database keeps every seat exactly as it was: the column
    arrives empty, which sends no field at all."""
    data = tmp_path / "data"
    data.mkdir()
    c = sqlite3.connect(data / "chat.db")
    c.executescript(
        "CREATE TABLE participants(id INTEGER PRIMARY KEY, slug TEXT, name TEXT,"
        " provider TEXT, model TEXT, reasoning_effort TEXT NOT NULL DEFAULT '',"
        " enabled INTEGER DEFAULT 1, position INTEGER DEFAULT 0,"
        " created_at REAL DEFAULT 0);")
    c.execute("INSERT INTO participants(slug,name,provider,model,reasoning_effort)"
              " VALUES('sam','Sam','openai','test-model','high')")
    c.execute("PRAGMA user_version = 18")
    c.commit()
    c.close()
    db.configure(data)
    db.init()
    con = db.connect()
    try:
        assert con.execute("PRAGMA user_version").fetchone()[0] == db.SCHEMA_VERSION
        row = con.execute("SELECT * FROM participants WHERE slug='sam'").fetchone()
        assert row["thinking_control"] == ""
        assert row["reasoning_effort"] == "high"  # untouched
    finally:
        con.close()
