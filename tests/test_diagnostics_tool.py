"""get_diagnostic on the NATIVE tool-calling surface: the normal-chat half of
the diagnostic tool. Earlier work mounted get_diagnostic only on a summoned
Claude Code guest's MCP surface (backend/diag_mcp.py); this is the missing
half: the exact same scoped, read-only, content-free diagnostic offered
directly to Claude/GPT's own tool-calling (backend/tools.py), so a
plain-language "what's voice latency right now?" no longer needs an
escalation to a guest just to get answered.

Guardrails pinned here mirror tests/test_diag_mcp.py's, one layer up (the
surface normal participants actually use):
  - backend/tools.py's tool definition publishes the SAME closed enum/schema/
    description as the guest tool - sourced from backend/diagnostics.py, not
    a second copy;
  - run_tool's native dispatch resolves through the exact SAME dispatch table
    object as the guest's `diag_mcp._DISPATCH` - one map, not two that can
    drift;
  - unknown/sensitive/path-like names are refused, never partially handled;
  - extra caller-supplied args (path/url/host) never influence what's reached;
  - the round loop offers the tool unconditionally - no per-chat toggle,
    matching the guest mount's "no safety reason to gate it" precedent;
  - an end-to-end chat-turn test drives a fake provider that actually issues
    the native tool call through the real engine + real run_tool dispatch.
"""

import asyncio
import json

import pytest
from fastapi.testclient import TestClient

from backend import diag_mcp, diagnostics, engine
from backend import tools as tools_mod
from backend.app import create_app
from backend.config import Settings


# ---------- one dispatch map behind both surfaces (no drift possible) ----------

def test_native_and_guest_tools_share_the_exact_same_dispatch_table():
    assert diag_mcp._DISPATCH is diagnostics._DIAGNOSTIC_DISPATCH
    assert diag_mcp.DIAGNOSTIC_NAMES is diagnostics.DIAGNOSTIC_NAMES
    assert diag_mcp._dispatch is diagnostics.dispatch_diagnostic


def test_native_tool_definition_matches_the_guest_tools_schema_and_description():
    defs = tools_mod.diagnostics_tool_definitions()
    assert [d["name"] for d in defs] == ["get_diagnostic"]
    d = defs[0]
    assert d["input_schema"] == diagnostics.diagnostic_input_schema()
    assert set(d["input_schema"]["properties"]) == {"name"}  # no url/path/host/query, ever
    assert d["input_schema"]["properties"]["name"]["enum"] == list(diag_mcp.DIAGNOSTIC_NAMES)
    assert d["input_schema"]["required"] == ["name"]
    assert d["description"] == diag_mcp._DESCRIPTION


# ---------- native dispatch retrieves a real, permitted payload ----------

def test_run_tool_models_returns_real_participant_data(cfg):
    out = asyncio.run(tools_mod.run_tool("get_diagnostic", {"name": "models"}, cfg))
    payload = json.loads(out)
    assert {p["slug"] for p in payload["participants"]} >= {"claude", "gpt"}


def test_run_tool_voice_latency_returns_bounded_shape(cfg):
    out = asyncio.run(tools_mod.run_tool("get_diagnostic", {"name": "voice_latency"}, cfg))
    payload = json.loads(out)
    assert set(payload) == {"turns", "stages", "window_hours", "samples",
                            "measurement_epoch", "samples_excluded_prior_epoch",
                            "epoch_note", "stage_notes", "excluded_stages_note"}


def test_voice_latency_excludes_prior_epoch_samples_and_says_so(cfg, app):
    """Rows measured under older stage semantics are excluded, counted,
    and explained - never silently blended into current percentiles (the
    live failure: seats read a mixed window and reported latency 'got
    worse' the day the fixes landed)."""
    from backend import db, diagnostics, voice_trace
    con = db.connect()
    try:
        epoch = voice_trace.ensure_epoch(con)
        assert voice_trace.ensure_epoch(con) == epoch  # stamped once, stable
        # one row from the OLD era, one from the current one
        db.insert_voice_trace(con, "t-old", None, "end_to_end_first_audio",
                              4000, provider="", model="", tts_provider="", speaker="")
        con.execute("UPDATE voice_turn_traces SET created_at=? WHERE turn_id='t-old'",
                    (epoch - 3600,))
        db.insert_voice_trace(con, "t-new", None, "playback_queue_wait",
                              1500, provider="", model="", tts_provider="", speaker="")
        con.commit()
    finally:
        con.close()
    out = diagnostics.voice_latency_summary(window_hours=24.0)
    assert out["samples"] == 1                       # only the current-epoch row
    assert out["samples_excluded_prior_epoch"] == 1  # the old row, counted
    assert "not comparable" in out["epoch_note"]
    assert "end_to_end_first_audio" not in out["stages"]
    assert "playback_queue_wait" in out["stages"]
    # the numbers travel with their framing
    assert "by design" in out["stage_notes"]["playback_queue_wait"]


def test_run_tool_conversation_spend_is_metered_only(app):
    """The in-call cost query returns THIS conversation's running metered
    total (never blending the subscription-equivalent estimate in), then a
    dynamic party/producer/provider split - reachable through the real native
    run_tool dispatch, scoped by cfg['chat_id']."""
    from backend import db
    con = db.connect()
    con.execute("INSERT INTO chats(title, created_at, updated_at) VALUES('t',0,0)")
    cid = con.execute("SELECT id FROM chats").fetchone()["id"]
    con.execute("INSERT INTO messages(chat_id, speaker, content, usage_json, "
                "created_at) VALUES(?,?,?,?,0)",
                (cid, "claude", "x", json.dumps({"input": 1, "output": 1,
                 "cost": 0.10, "model": "claude-opus-4-8"})))
    con.execute("INSERT INTO messages(chat_id, speaker, content, usage_json, "
                "created_at) VALUES(?,?,?,?,0)",
                (cid, "claude-code", "x", json.dumps({"input": 1, "output": 1,
                 "cost": 9.0, "auth": "subscription"})))
    con.commit()
    con.close()

    cfg = app.state.settings.as_cfg()
    cfg["chat_id"] = cid
    out = asyncio.run(tools_mod.run_tool("get_diagnostic",
                                         {"name": "conversation_spend"}, cfg))
    payload = json.loads(out)
    assert payload["active_conversation"] is True
    assert payload["metered_total"] == pytest.approx(0.10)      # not the $9 sub
    assert payload["informational"]["subscription_equiv"] == pytest.approx(9.0)
    assert {g["key"] for g in payload["by_party"]} == {"claude", "claude-code"}


def test_run_tool_health_never_leaks_key_material(cfg, monkeypatch):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-super-secret-value")
    out = asyncio.run(tools_mod.run_tool("get_diagnostic", {"name": "health"}, cfg))
    assert "sk-super-secret-value" not in out
    payload = json.loads(out)
    assert payload["services"]["anthropic"]["configured"] is True


# ---------- unknown / sensitive / path-like names are refused ----------

@pytest.mark.parametrize("bad", [
    "shell", "curl", "logs", "transcript", "messages", "credentials", ".env",
    "config.local.json", "Health", "health ", "",
    "health/../../export", "../../.env", "http://127.0.0.1:8902/api/export",
])
def test_unknown_or_sensitive_or_path_like_names_are_refused(cfg, bad):
    out = asyncio.run(tools_mod.run_tool("get_diagnostic", {"name": bad}, cfg))
    payload = json.loads(out)
    assert payload.get("refused") is True
    assert "not a recognized diagnostic" in payload["error"]


def test_missing_name_is_refused_not_defaulted(cfg):
    out = asyncio.run(tools_mod.run_tool("get_diagnostic", {}, cfg))
    assert json.loads(out).get("refused") is True


# ---------- caller-controlled extras cannot influence what is reached ----------

def test_extra_args_are_ignored_not_forwarded(cfg):
    out = asyncio.run(tools_mod.run_tool("get_diagnostic", {
        "name": "models", "path": "/api/export", "url": "http://evil.example",
        "host": "evil.example",
    }, cfg))
    payload = json.loads(out)
    assert "participants" in payload
    assert "evil.example" not in out


# ---------- the round loop offers it unconditionally ----------

@pytest.fixture
def app(tmp_path):
    settings = Settings(data_dir=str(tmp_path / "data"),
                        memory_url="http://127.0.0.1:1")
    return create_app(settings)


def sse_events(body):
    return [json.loads(line[6:]) for line in body.splitlines()
            if line.startswith("data: ")]


def test_get_diagnostic_is_offered_even_with_every_other_toggle_off(app, monkeypatch):
    """web_enabled/code_enabled/memory_enabled all off - get_diagnostic is
    still offered: the same "no toggle, no safety reason to gate it"
    reasoning DECISIONS.md records for the guest's own always-on MCP mount
    applies here too."""
    seen = []

    async def stream_reply(participant, roster, transcript, names, cfg, project,
                           chat_summary, voice_mode, tools=None, memory=None):
        seen.append([t["name"] for t in (tools or [])])
        yield ("text", "ok")

    monkeypatch.setattr(engine.providers, "stream_reply", stream_reply)
    with TestClient(app, base_url="http://127.0.0.1") as c:
        chat = c.post("/api/chats", json={}).json()
        patched = c.patch(f"/api/chats/{chat['id']}", json={
            "web_enabled": False, "code_enabled": False, "memory_enabled": False})
        assert patched.status_code == 200
        with c.stream("POST", f"/api/chats/{chat['id']}/send",
                      json={"text": "hi"}) as r:
            "".join(r.iter_text())
    assert seen and all(tools == ["get_diagnostic"] for tools in seen)


# ---------- end-to-end chat turn: a fake provider issuing the native call ----------

def test_e2e_chat_turn_fake_provider_calls_get_diagnostic(app, monkeypatch):
    """Drives a full round through the real engine + the real run_tool
    dispatch (not a mock of run_tool itself) - a fake `stream_reply` stands
    in for the provider round-trip (the existing harness's mocking seam, same
    as tests/test_delegation.py and tests/test_mcp_client.py) and issues the
    native get_diagnostic tool call exactly the way backend/providers.py's
    real Anthropic/OpenAI agentic loop would, from inside its own tool-call
    handling. Proves the tool call is reachable end to end and its activity
    is persisted, without any Claude Code guest involved."""
    async def stream_reply(participant, roster, transcript, names, cfg, project,
                           chat_summary, voice_mode, tools=None, memory=None):
        assert any(t["name"] == "get_diagnostic" for t in (tools or []))
        out = await tools_mod.run_tool("get_diagnostic", {"name": "models"}, cfg,
                                       origin_agent=participant["slug"], memory=memory)
        yield ("tool", {"tool": "get_diagnostic", "input": {"name": "models"}, "output": out})
        yield ("text", "Both of us are configured and running.")
        yield ("usage", {"input": 5, "cache_read": 0, "cache_creation": 0, "output": 5})

    monkeypatch.setattr(engine.providers, "stream_reply", stream_reply)
    with TestClient(app, base_url="http://127.0.0.1") as c:
        chat = c.post("/api/chats", json={}).json()
        with c.stream("POST", f"/api/chats/{chat['id']}/send",
                      json={"text": "what model are you two actually running?"}) as r:
            body = "".join(r.iter_text())
        events = sse_events(body)
        tool_activity = [e for e in events if e["type"] == "tool_activity"]
        assert tool_activity and tool_activity[0]["tool"] == "get_diagnostic"
        payload = json.loads(tool_activity[0]["output_text"])
        assert "participants" in payload

        got = c.get(f"/api/chats/{chat['id']}").json()
        replies = [m for m in got["messages"] if m["speaker"] in ("claude", "gpt")]
        assert replies and replies[0]["tool_events"][0]["tool"] == "get_diagnostic"


def test_model_facing_latency_excludes_queue_wait_but_dev_endpoint_keeps_it(cfg, app):
    """After two live over-index incidents (the second WITH the framing
    note in the payload), playback_queue_wait is not handed to models at all
    - while the development summary still reports everything, and the model
    payload names its exclusion instead of hiding it."""
    from backend import db, diagnostics
    con = db.connect()
    try:
        diagnostics.voice_trace.ensure_epoch(con)
        db.insert_voice_trace(con, "t-q", None, "playback_queue_wait", 5000,
                              provider="", model="", tts_provider="", speaker="")
        con.commit()
    finally:
        con.close()
    dev = diagnostics.voice_latency_summary(window_hours=24.0)
    assert "playback_queue_wait" in dev["stages"]          # dev sees it
    out = asyncio.run(tools_mod.run_tool("get_diagnostic", {"name": "voice_latency"}, cfg))
    payload = json.loads(out)
    assert "playback_queue_wait" not in payload["stages"]      # models don't
    assert "playback_queue_wait" not in payload["stage_notes"]
    assert "by design" in payload["excluded_stages_note"]      # named, not hidden
