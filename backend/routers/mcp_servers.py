"""Manage MCP servers from the Connections page.

Until now the two MCP slots — `mcp_servers` (resident models) and `code_mcp`
(summoned guests) — were hand-edited in config.local.json and applied only at
startup; the difference between "configured" and "working" was invisible until
a chat quietly lacked a tool. This router gives the Connections page the full
loop: list both slots with live status, add/edit/remove entries (written
atomically to config.local.json via config.write_local_key),
and TEST a spec by actually spawning it over stdio and listing its tools.

Trust model: the operator AT THE LOOPBACK PAGE already owns
config.local.json; these endpoints let them edit it without a text editor,
and the test endpoint runs the command they typed exactly as startup would.

That equivalence only holds for someone who already has filesystem access,
so the write and test routes are loopback-only. The app has no
authentication, and the documented remote path (tailscale serve) proxies
through loopback, so the non-loopback bind guard in __main__ does not cover
it: without this gate, anything that could reach the tailnet could POST a
command here and have it spawned. Reaching the app from a phone is for
chatting, not for reconfiguring what runs on the host. For the same reason a
non-loopback caller sees env var NAMES only, never their values.

Restarts: connected sessions are owned by the startup McpManager, so changes
apply on the next service restart. GET reports `restart_required` honestly by
comparing the file against what's actually running — the UI shows it loudly
rather than letting a stale process masquerade as the new config.
"""

from __future__ import annotations

import asyncio
import re

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel, Field

from ..config import LOCAL_CONFIG_PATH, _read_json, write_local_key

router = APIRouter(prefix="/api/mcp-servers", tags=["mcp-servers"])

# slot (URL name) -> config.local.json key
SLOTS = {"models": "mcp_servers", "guests": "code_mcp"}
_NAME = re.compile(r"^[a-z0-9][a-z0-9_-]{0,31}$")
PROBE_TIMEOUT_S = 20


class ServerSpec(BaseModel):
    command: str = Field(min_length=1)
    args: list[str] = Field(default_factory=list)
    env: dict[str, str] | None = None
    label: str | None = Field(default=None, max_length=120)


LOOPBACK = {"127.0.0.1", "localhost", "::1"}


def _is_loopback(request: Request) -> bool:
    return (request.url.hostname or "").lower() in LOOPBACK


def _require_loopback(request: Request) -> None:
    """Spawning a command, or persisting one to be spawned at every startup,
    is host-level access. A tailnet caller reaching this app is not that."""
    if not _is_loopback(request):
        raise HTTPException(status_code=403, detail=(
            "MCP servers can only be changed or tested from the machine "
            "running the app (http://127.0.0.1), not over a remote host."))


def _slot_key(slot: str) -> str:
    if slot not in SLOTS:
        raise HTTPException(status_code=404, detail=f"unknown slot '{slot}'")
    return SLOTS[slot]


def _spec_dict(spec: ServerSpec) -> dict:
    out: dict = {"command": spec.command, "args": spec.args}
    if spec.env:
        out["env"] = spec.env
    if spec.label and spec.label.strip():
        out["label"] = spec.label.strip()
    return out


def _running_config(request: Request, key: str) -> dict:
    settings = getattr(request.app.state, "settings", None)
    return dict(getattr(settings, key, {}) or {}) if settings else {}


@router.get("")
def list_servers(request: Request):
    show_env_values = _is_loopback(request)
    local = _read_json(LOCAL_CONFIG_PATH)
    mcp = getattr(request.app.state, "mcp", None)
    live = mcp.status() if mcp else {}
    out, restart_required = [], False
    for slot, key in SLOTS.items():
        file_block = local.get(key) or {}
        running = _running_config(request, key)
        if file_block != running:
            restart_required = True
        for name, spec in sorted(file_block.items()):
            status = live.get(name, {}) if slot == "models" else {}
            out.append({
                "slot": slot, "name": name,
                "command": spec.get("command", ""),
                "args": spec.get("args", []),
                # Values only for a caller on the machine itself: an entry is
                # meant to hold ${VAR} references, but nothing enforces that,
                # so a literal secret must not travel to a remote page.
                "env": (spec.get("env") or {}) if show_env_values
                       else {k: "" for k in (spec.get("env") or {})},
                "label": spec.get("label") or "",
                "connected": bool(status.get("connected")),
                "tools": status.get("tools", []),
                "error": status.get("error"),
                "pending_restart": name not in running or running.get(name) != spec,
            })
    return {"servers": out, "restart_required": restart_required}


@router.put("/{slot}/{name}")
def upsert_server(slot: str, name: str, spec: ServerSpec, request: Request):
    _require_loopback(request)
    key = _slot_key(slot)
    if not _NAME.match(name):
        raise HTTPException(status_code=400, detail=(
            "name must be 1-32 chars of lowercase letters, digits, '-' or '_' "
            "(it becomes the mcp__<name>__<tool> namespace)"))
    local = _read_json(LOCAL_CONFIG_PATH)
    block = dict(local.get(key) or {})
    block[name] = _spec_dict(spec)
    write_local_key(key, block)
    return {"ok": True, "slot": slot, "name": name, "restart_required": True}


@router.delete("/{slot}/{name}")
def delete_server(slot: str, name: str, request: Request):
    _require_loopback(request)
    key = _slot_key(slot)
    local = _read_json(LOCAL_CONFIG_PATH)
    block = dict(local.get(key) or {})
    if name not in block:
        raise HTTPException(status_code=404, detail=f"no '{name}' in {slot} slot")
    block.pop(name)
    write_local_key(key, block)
    return {"ok": True, "restart_required": True}


async def _probe(spec: ServerSpec) -> dict:
    """One-shot: spawn over stdio, initialize, list tools, tear down.
    The difference between 'configured' and 'working', made visible."""
    from mcp import ClientSession, StdioServerParameters
    from mcp.client.stdio import stdio_client
    params = StdioServerParameters(command=spec.command, args=spec.args,
                                   env=spec.env)
    async with stdio_client(params) as (read, write):
        async with ClientSession(read, write) as session:
            await asyncio.wait_for(session.initialize(), timeout=PROBE_TIMEOUT_S)
            listed = await asyncio.wait_for(session.list_tools(),
                                            timeout=PROBE_TIMEOUT_S)
    return {"ok": True, "tools": sorted(t.name for t in listed.tools)}


@router.post("/test")
async def test_server(spec: ServerSpec, request: Request):
    _require_loopback(request)
    try:
        return await asyncio.wait_for(_probe(spec), timeout=PROBE_TIMEOUT_S * 2)
    except Exception as e:  # noqa: BLE001 — every failure is an honest answer here
        detail = str(e).strip() or e.__class__.__name__
        return {"ok": False, "error": detail[:400]}
