"""MCP client layer: Crossband consumes external stdio MCP
servers — discovery, namespacing, dispatch through run_tool, graceful
degrade. Exercised against a real FastMCP server (tests/fake_mcp.py), no
network, keyless."""

import asyncio
import sys
import time
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from backend import engine
from backend import tools as tools_mod
from backend.app import create_app
from backend.config import Settings
from backend.mcp_client import McpManager

FAKE = str(Path(__file__).parent / "fake_mcp.py")
SPEC = {"fake": {"command": sys.executable, "args": [FAKE]}}


def run_with_manager(coro_fn, servers=SPEC):
    """Start a manager (owning task), run coro_fn(mgr), stop cleanly."""
    async def go():
        mgr = McpManager(servers)
        task = asyncio.get_event_loop().create_task(mgr.run())
        try:
            for _ in range(100):  # wait for discovery
                if mgr.tool_definitions() or mgr.errors:
                    break
                await asyncio.sleep(0.1)
            return await coro_fn(mgr)
        finally:
            await mgr.stop()
            await task
    return asyncio.run(go())


def test_discovery_namespacing_and_call():
    async def body(mgr):
        names = [d["name"] for d in mgr.tool_definitions()]
        assert "mcp__fake__echo" in names and "mcp__fake__boom" in names
        d = next(x for x in mgr.tool_definitions() if x["name"] == "mcp__fake__echo")
        assert "external tool" in d["description"]
        assert d["input_schema"]["properties"]["text"]["type"] == "string"
        # dispatch through the real run_tool path, manager riding in cfg
        out = await tools_mod.run_tool("mcp__fake__echo", {"text": "hi"},
                                       {"_mcp": mgr, "max_tool_output": 8000})
        assert out == "echo:hi"
        assert mgr.status()["fake"]["connected"] is True
        return True
    assert run_with_manager(body)


def test_tool_error_is_contained():
    async def body(mgr):
        out = await tools_mod.run_tool("mcp__fake__boom", {},
                                       {"_mcp": mgr, "max_tool_output": 8000})
        assert out.lower().startswith("error")
        return True
    assert run_with_manager(body)


def test_activity_label_reads_the_operators_own_trusted_config():
    """mcp_servers[name]["label"] is a trusted, operator-written display
    string -- shown verbatim in the work-status event; a server with none
    configured returns None so the caller (work_status.activity_label) can
    fall back to its own generic label instead of guessing."""
    mgr = McpManager({
        "build-watcher": {"command": "x", "label": "Checking build status"},
        "unlabeled": {"command": "x"},
    })
    assert mgr.activity_label("build-watcher") == "Checking build status"
    assert mgr.activity_label("unlabeled") is None
    assert mgr.activity_label("never-configured") is None


def test_unconfigured_and_dead_server_degrade():
    async def body(mgr):
        # bad command never connects: reported, no tools, nothing raises
        assert mgr.tool_definitions() == []
        assert mgr.status()["dead"]["connected"] is False
        assert mgr.status()["dead"]["error"]
        return True
    assert run_with_manager(body, servers={"dead": {"command": "/nonexistent"}})
    # and dispatch without a manager errors politely
    out = asyncio.run(tools_mod.run_tool("mcp__x__y", {}, {"max_tool_output": 100}))
    assert "unavailable" in out


def test_app_exposes_health_and_offers_tools(tmp_path, monkeypatch):
    seen = []

    async def stream_reply(participant, roster, transcript, names, cfg, project,
                           chat_summary, voice_mode, tools=None, memory=None):
        seen.append([t["name"] for t in (tools or [])])
        yield ("text", "ok")
    monkeypatch.setattr(engine.providers, "stream_reply", stream_reply)

    settings = Settings(data_dir=str(tmp_path / "data"),
                        memory_url="http://127.0.0.1:1", mcp_servers=SPEC)
    with TestClient(create_app(settings), base_url="http://127.0.0.1") as c:
        # startup spawned the owning task; wait for discovery
        for _ in range(100):
            mcp = c.get("/api/state").json()["config"]["mcp"]
            if mcp.get("fake", {}).get("connected"):
                break
            time.sleep(0.1)
        assert mcp["fake"]["connected"] and "mcp__fake__echo" in mcp["fake"]["tools"]
        chat = c.post("/api/chats", json={}).json()
        with c.stream("POST", f"/api/chats/{chat['id']}/send", json={"text": "hi"}) as r:
            "".join(r.iter_text())
    assert any("mcp__fake__echo" in tools for tools in seen)


def test_background_tasks_actually_run_under_lifespan(tmp_path):
    """Regression: on_startup hooks are ignored when lifespan= is set, and
    the reflection sweep silently never ran. Both background tasks must exist
    and be live inside the app context."""
    settings = Settings(data_dir=str(tmp_path / "data"),
                        memory_url="http://127.0.0.1:1")
    app = create_app(settings)
    with TestClient(app, base_url="http://127.0.0.1") as c:
        c.get("/api/state")
        assert not app.state.reflection_sweep.done()
        assert app.state.mcp_task is not None
