"""get_diagnostic: the always-mounted, read-only MCP tool that
gives a summoned Claude Code guest a SCOPED window onto Crossband's own local
diagnostics - never a general shell/curl, never a URL/path/query parameter,
and never transcripts, message content, credentials, or arbitrary logs.

Guardrails pinned here:
  - allowlist enforcement is STRUCTURAL (the enum in the tool's own input
    schema), not just a validation promise, and refuses anything outside it;
  - the dispatch table can never reach a write-capable function - it's
    exactly the three read diagnostics, nothing else;
  - the schema exposes no url/path/host/query - `name` is the only input,
    and anything else a caller adds is silently ignored, never forwarded;
  - voice_latency's output stays within its known, content-free key set -
    a regression guard on top of voice_trace.py's own ingest-time allowlist;
  - an END-TO-END test drives the REAL MCP protocol (the `mcp` package's
    in-memory client/server session) against the exact server object
    backend/guest.py mounts into a live (fake-SDK) guest run, proving the
    tool is reachable through the mounted MCP surface with a real payload -
    not just a unit test around the dispatch map in isolation.
"""

import asyncio
import json
import subprocess

import pytest

from backend import db, diag_mcp, guest, voice_trace
from backend.app import create_app
from backend.config import Settings


@pytest.fixture
def app(tmp_path):
    return create_app(Settings(data_dir=str(tmp_path / "data"),
                               memory_url="http://127.0.0.1:1",  # unroutable: memoryless
                               anthropic_model="claude-opus-4-8",
                               openai_model="gpt-5.1"))


def _cfg(settings, **extra):
    cfg = settings.as_cfg()
    cfg["chat_id"] = 0
    cfg.update(extra)
    return cfg


# ---------- allowlist enforcement (structural, not a validation promise) ----------

def test_enum_is_the_only_input_and_is_the_structural_allowlist():
    schema = diag_mcp._INPUT_SCHEMA
    assert set(schema["properties"]) == {"name"}          # no url/path/host/query, ever
    assert schema["properties"]["name"]["enum"] == list(diag_mcp.DIAGNOSTIC_NAMES)
    assert schema["required"] == ["name"]


@pytest.mark.parametrize("bad", [
    "shell", "curl", "logs", "transcript", "messages", "credentials", ".env",
    "config.local.json", "Health", "health ", "", None,
    "health/../../export", "../../.env", "http://127.0.0.1:8902/api/export",
])
def test_every_out_of_enum_name_is_refused_not_passed_through(bad):
    out = asyncio.run(diag_mcp._dispatch(bad, {}))
    assert out.get("refused") is True
    assert "not a recognized diagnostic" in out["error"]


def test_tool_handler_marks_a_refusal_as_an_error_result(app):
    t = diag_mcp.build_tool(_cfg(app.state.settings))
    result = asyncio.run(t.handler({"name": "not-real"}))
    assert result["is_error"] is True
    payload = json.loads(result["content"][0]["text"])
    assert payload["refused"] is True


def test_valid_names_are_never_refused(app):
    cfg = _cfg(app.state.settings)
    for name in diag_mcp.DIAGNOSTIC_NAMES:
        out = asyncio.run(diag_mcp._dispatch(name, cfg))
        assert "refused" not in out


# ---------- no write-capable function is ever reachable via this path ----------

def test_dispatch_table_is_exactly_the_read_diagnostics():
    assert set(diag_mcp._DISPATCH) == set(diag_mcp.DIAGNOSTIC_NAMES) == {
        "health", "models", "voice_latency", "conversation_spend",
        "conversation_performance"}
    for name, fn in diag_mcp._DISPATCH.items():
        assert asyncio.iscoroutinefunction(fn)
        # structural, not just by convention: nothing dispatched here can be
        # named like a mutator - adding a write means adding it to this dict,
        # which this assertion would catch by name alone.
        assert not any(w in fn.__name__ for w in
                       ("save", "set", "write", "delete", "update", "insert", "mutate"))


def test_extra_args_beyond_name_are_ignored_not_forwarded(app):
    """`name` is the ONLY thing that ever drives dispatch - a caller can't
    smuggle a path/url/host through an extra key; nothing else is read."""
    t = diag_mcp.build_tool(_cfg(app.state.settings))
    result = asyncio.run(t.handler({
        "name": "models", "path": "/api/export", "url": "http://evil.example",
        "host": "evil.example",
    }))
    payload = json.loads(result["content"][0]["text"])
    assert "participants" in payload
    assert "evil.example" not in json.dumps(payload)


def test_health_never_leaks_key_material(app, monkeypatch):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-super-secret-value")
    out = asyncio.run(diag_mcp._dispatch("health", _cfg(app.state.settings)))
    assert "sk-super-secret-value" not in json.dumps(out)
    assert out["services"]["anthropic"]["configured"] is True


# ---------- voice_latency stays within its known, content-free key set ----------

def test_voice_latency_output_keys_are_the_known_bounded_set(app):
    con = db.connect()
    con.execute("INSERT INTO chats(title, created_at, updated_at) VALUES('t',0,0)")
    cid = con.execute("SELECT id FROM chats").fetchone()["id"]
    db.insert_voice_trace(con, "turn1", cid, "end_to_end_first_audio", 1800.0,
                          model="gpt-5.1", tts_provider="elevenlabs")
    db.insert_voice_trace(con, "turn1", cid, "final_to_first_token", 600.0,
                          model="gpt-5.1")
    con.commit()
    con.close()

    out = asyncio.run(diag_mcp._dispatch("voice_latency", _cfg(app.state.settings)))
    assert set(out) == {"turns", "stages", "window_hours", "samples",
                        "measurement_epoch", "samples_excluded_prior_epoch",
                        "epoch_note", "stage_notes",
                        "excluded_stages_note"}  # epoch/framing; exclusion named
    assert isinstance(out["turns"], int) and isinstance(out["samples"], int)
    for stage_name, entry in out["stages"].items():
        assert stage_name in voice_trace.ALLOWED_STAGES
        assert set(entry) == {"count", "p50", "p95", "max", "by_model", "by_tts_provider"}
        for segment in (entry["by_model"], entry["by_tts_provider"]):
            for seg_key, seg_val in segment.items():
                assert isinstance(seg_key, str) and len(seg_key) <= 64  # bounded identifier
                assert set(seg_val) == {"count", "p50", "p95", "max"}


# ---------- conversation_spend: metered-only total + dynamic breakdown ----------

def test_conversation_spend_is_metered_only_with_dynamic_party_breakdown(app):
    """The in-call cost query: metered cash first, then a party/producer/
    provider split keyed on the events themselves (no fixed roster), with
    subscription-equivalent and unknown kept apart and never summed in."""
    import json as _json

    con = db.connect()
    con.execute("INSERT INTO chats(title, created_at, updated_at) VALUES('t',0,0)")
    cid = con.execute("SELECT id FROM chats").fetchone()["id"]

    def _msg(speaker, usage):
        con.execute("INSERT INTO messages(chat_id, speaker, content, usage_json, "
                    "created_at) VALUES(?,?,?,?,0)",
                    (cid, speaker, "x", _json.dumps(usage)))

    _msg("claude", {"input": 10, "output": 20, "cost": 0.10, "model": "claude-opus-4-8"})
    _msg("gpt", {"input": 10, "output": 20, "cost": 0.20, "model": "gpt-5.1"})
    _msg("claude-code", {"input": 10, "output": 20, "cost": 9.0, "auth": "subscription"})
    _msg("claude-code", {"input": 10, "output": 20, "cost": 3.0, "auth": "api_key"})
    db.log_voice_usage(con, cid, "tts", 5000, 0.55)
    con.commit()
    con.close()

    out = asyncio.run(diag_mcp._dispatch("conversation_spend",
                                         _cfg(app.state.settings, chat_id=cid)))
    assert "refused" not in out
    assert out["active_conversation"] is True and out["chat_id"] == cid
    # metered = 0.10 + 0.20 + 3.0 (api_key guest) + 0.55 voice - NOT the $9 sub
    assert out["metered_total"] == pytest.approx(3.85)
    assert out["informational"]["subscription_equiv"] == pytest.approx(9.0)
    assert out["informational"]["unknown"] == 0.0

    # dynamic party split: every distinct speaker appears, no hardcoded roster
    parties = {g["key"] for g in out["by_party"]}
    assert parties == {"claude", "gpt", "claude-code", "voice"}
    guest = next(g for g in out["by_party"] if g["key"] == "claude-code")
    assert guest["metered"] == pytest.approx(3.0)              # only the api_key turn
    assert guest["subscription_equiv"] == pytest.approx(9.0)  # shown, not summed in

    producers = {g["key"] for g in out["by_producer"]}
    assert {"normal", "claude_code", "tts"} <= producers
    providers = {g["key"] for g in out["by_provider"]}
    assert {"anthropic", "openai", "elevenlabs"} <= providers


def test_conversation_spend_without_active_chat_says_so(app):
    """No chat id (e.g. a context with no active conversation) → an honest
    'nothing to price' rather than another chat's numbers. _cfg pins chat_id=0."""
    out = asyncio.run(diag_mcp._dispatch("conversation_spend", _cfg(app.state.settings)))
    assert out["active_conversation"] is False
    assert "metered_total" not in out


def test_conversation_spend_is_content_free(app):
    """Only dollars, tokens, slugs and labels - never message text."""
    import json as _json
    con = db.connect()
    con.execute("INSERT INTO chats(title, created_at, updated_at) VALUES('t',0,0)")
    cid = con.execute("SELECT id FROM chats").fetchone()["id"]
    con.execute("INSERT INTO messages(chat_id, speaker, content, usage_json, "
                "created_at) VALUES(?,?,?,?,0)",
                (cid, "claude", "SECRET-TRANSCRIPT-BODY",
                 _json.dumps({"input": 1, "output": 1, "cost": 0.01,
                              "model": "claude-opus-4-8"})))
    con.commit()
    con.close()
    out = asyncio.run(diag_mcp._dispatch("conversation_spend",
                                         _cfg(app.state.settings, chat_id=cid)))
    assert "SECRET-TRANSCRIPT-BODY" not in json.dumps(out)


# ---------- end-to-end: through the real MCP protocol, from a mounted guest ----------

def _git_repo(path):
    """Make tmp_path a real (tiny) git repo so worktree ops are honest."""
    def g(*a):
        subprocess.run(["git", "-C", str(path), *a], check=True, capture_output=True,
                       env={**__import__("os").environ,
                            "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@t",
                            "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@t"})
    subprocess.run(["git", "init", "-q", "-b", "main", str(path)], check=True)
    (path / "README.md").write_text("hi")
    g("add", "-A")
    g("commit", "-qm", "init")


def test_e2e_guest_can_call_get_diagnostic_through_the_mounted_mcp_surface(
        app, tmp_path, monkeypatch):
    """Mounts get_diagnostic exactly the way a real summoned guest gets it
    (via backend.guest.run_guest, fake-SDK options capture - same pattern as
    tests/test_guest.py's read-only wall test), then drives the CAPTURED
    server object through the `mcp` package's real client/server session -
    the same protocol layer Claude Code itself speaks - to prove a guest can
    actually call the tool and get a real, content-free payload back."""
    import claude_agent_sdk as sdk

    captured = {}

    def fake_query(*, prompt, options=None, transport=None):
        captured["options"] = options

        async def gen():
            yield sdk.ResultMessage(
                subtype="success", duration_ms=1, duration_api_ms=1,
                is_error=False, num_turns=1, session_id="s-1",
                total_cost_usd=0.0, usage={"input_tokens": 1, "output_tokens": 1},
                result="ok")
        return gen()

    class FakeSdk:
        ClaudeAgentOptions = sdk.ClaudeAgentOptions
        AssistantMessage = sdk.AssistantMessage
        UserMessage = sdk.UserMessage
        ResultMessage = sdk.ResultMessage
        StreamEvent = sdk.StreamEvent
        TextBlock = sdk.TextBlock
        ToolUseBlock = sdk.ToolUseBlock
        ToolResultBlock = sdk.ToolResultBlock
        query = staticmethod(fake_query)

    monkeypatch.setattr(guest, "_sdk", lambda: FakeSdk)
    _git_repo(tmp_path)
    cfg = _cfg(app.state.settings, code_repos={"demo": str(tmp_path)})

    async def drain():
        return [ev async for ev in guest.run_guest("check diagnostics", "demo", "", cfg)]

    asyncio.run(drain())

    opts = captured["options"]
    assert "mcp__crossband-diag" in opts.allowed_tools   # offered to the guest
    server_config = opts.mcp_servers["crossband-diag"]   # the exact mounted server

    from mcp.shared.memory import create_connected_server_and_client_session

    async def talk_to_it():
        async with create_connected_server_and_client_session(
                server_config["instance"]) as session:
            listed = await session.list_tools()
            assert [t.name for t in listed.tools] == ["get_diagnostic"]
            schema = listed.tools[0].inputSchema
            assert schema["properties"]["name"]["enum"] == list(diag_mcp.DIAGNOSTIC_NAMES)

            ok = await session.call_tool("get_diagnostic", {"name": "models"})
            assert ok.isError is not True
            payload = json.loads(ok.content[0].text)
            assert "participants" in payload
            assert {p["slug"] for p in payload["participants"]} >= {"claude", "gpt"}

            # An out-of-enum value is refused by the MCP protocol layer itself
            # - the `mcp` package validates the call against the tool's own
            # declared inputSchema BEFORE our handler ever runs, so the
            # structural allowlist holds even one layer earlier than our code
            # (backend/diag_mcp.py's own _dispatch refusal, exercised directly
            # in the unit tests above, is the defense-in-depth layer under
            # THAT - for a caller that bypasses schema validation entirely).
            refused = await session.call_tool("get_diagnostic", {"name": "shell"})
            assert refused.isError is True
            refusal_text = refused.content[0].text
            assert "shell" in refusal_text  # names what was rejected
            # never a diagnostic payload leaking through a rejected call
            assert "participants" not in refusal_text and "services" not in refusal_text

    asyncio.run(talk_to_it())


def test_e2e_guest_conversation_spend_is_scoped_to_the_summoning_chat(
        app, tmp_path, monkeypatch):
    """Regression: a summoned guest's OWN operator cfg never carries a
    chat_id (only engine.py's per-round round_cfg does - backend/engine.py's
    `_launch_guest_job` is handed the original `cfg`, not `round_cfg`), so
    before the fix `get_diagnostic("conversation_spend")` always reported
    active_conversation: false for a guest, no matter which chat summoned it.
    Proves the fix by driving the tool through the REAL mounted MCP server -
    the exact object backend/guest.py builds - with two chats seeded with
    different spend, summoning against ONE of them via run_guest's own
    `chat_id=` argument (never present in cfg itself), and asserting the
    payload reflects that chat and NOT the other one's numbers."""
    import claude_agent_sdk as sdk

    con = db.connect()
    con.execute("INSERT INTO chats(title, created_at, updated_at) VALUES('a',0,0)")
    con.execute("INSERT INTO chats(title, created_at, updated_at) VALUES('b',0,0)")
    rows = con.execute("SELECT id FROM chats ORDER BY id").fetchall()
    summoning_chat, other_chat = rows[0]["id"], rows[1]["id"]

    def _msg(cid, speaker, usage):
        con.execute("INSERT INTO messages(chat_id, speaker, content, usage_json, "
                    "created_at) VALUES(?,?,?,?,0)",
                    (cid, speaker, "x", json.dumps(usage)))
    _msg(summoning_chat, "claude",
         {"input": 10, "output": 20, "cost": 0.42, "model": "claude-opus-4-8"})
    _msg(other_chat, "gpt",
         {"input": 10, "output": 20, "cost": 99.0, "model": "gpt-5.1"})
    con.commit()
    con.close()

    captured = {}

    def fake_query(*, prompt, options=None, transport=None):
        captured["options"] = options

        async def gen():
            yield sdk.ResultMessage(
                subtype="success", duration_ms=1, duration_api_ms=1,
                is_error=False, num_turns=1, session_id="s-1",
                total_cost_usd=0.0, usage={"input_tokens": 1, "output_tokens": 1},
                result="ok")
        return gen()

    class FakeSdk:
        ClaudeAgentOptions = sdk.ClaudeAgentOptions
        AssistantMessage = sdk.AssistantMessage
        UserMessage = sdk.UserMessage
        ResultMessage = sdk.ResultMessage
        StreamEvent = sdk.StreamEvent
        TextBlock = sdk.TextBlock
        ToolUseBlock = sdk.ToolUseBlock
        ToolResultBlock = sdk.ToolResultBlock
        query = staticmethod(fake_query)

    monkeypatch.setattr(guest, "_sdk", lambda: FakeSdk)
    _git_repo(tmp_path)
    # The operator cfg a real summon hands to run_guest - deliberately WITHOUT
    # a chat_id key, matching backend/engine.py's `_launch_guest_job(chat_id,
    # summons, cfg, ...)` call, which passes the plain cfg, not round_cfg.
    cfg = app.state.settings.as_cfg()
    cfg["code_repos"] = {"demo": str(tmp_path)}
    assert "chat_id" not in cfg

    async def drain():
        return [ev async for ev in guest.run_guest(
            "check spend", "demo", "", cfg, chat_id=summoning_chat)]

    asyncio.run(drain())

    opts = captured["options"]
    server_config = opts.mcp_servers["crossband-diag"]

    from mcp.shared.memory import create_connected_server_and_client_session

    async def talk_to_it():
        async with create_connected_server_and_client_session(
                server_config["instance"]) as session:
            result = await session.call_tool(
                "get_diagnostic", {"name": "conversation_spend"})
            assert result.isError is not True
            payload = json.loads(result.content[0].text)
            assert payload["active_conversation"] is True
            assert payload["chat_id"] == summoning_chat
            assert payload["metered_total"] == pytest.approx(0.42)  # this chat only
            assert "99.0" not in json.dumps(payload)  # not the other chat's $99

    asyncio.run(talk_to_it())
