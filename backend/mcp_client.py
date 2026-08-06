"""MCP client layer: Crossband consumes external MCP servers.

Config (config.local.json, private) lists stdio servers; Crossband connects at
startup, discovers their tools, and offers them to every participant
namespaced as `mcp__<server>__<tool>`. Calls dispatch through the existing
run_tool pipeline and render as ordinary tool chips — no new UI (a
plugins-style UX remains a separate, open question). The public repo never
knows what any server means: names, tools, and descriptions all come from the
servers at runtime.

Design notes:
- MCP at the edge, native at the core: the built-in tool families
  (web/memory/github/code) stay native — this layer is only for EXTERNAL
  capabilities, mirroring Membro's own choice (MCP adapter for outsiders,
  native interface for itself).
- Lifecycle lives in ONE owning task per app (connect → serve → close in the
  same task): anyio cancel scopes must exit in the task that entered them,
  so FastAPI's startup/shutdown (different tasks) signal via events instead
  of touching the sessions directly.
- Graceful degrade everywhere: a server that fails to connect is reported in
  /api/state and retried every RETRY_S; a call that fails drops that
  server's tools until the retry loop restores them; the models just see an
  honest error string.
"""

import asyncio
import logging
from contextlib import AsyncExitStack

log = logging.getLogger("mmc.mcp")

RETRY_S = 60
CALL_TIMEOUT_S = 45


class McpManager:
    def __init__(self, servers: dict):
        self.servers = servers or {}
        self.sessions: dict = {}   # server name -> ClientSession
        self.tools: dict = {}      # qualified name -> (server, tool_name, definition)
        self.errors: dict = {}     # server name -> last error string
        self._stop = asyncio.Event()
        self._stopped = asyncio.Event()

    # ---------- the owning task ----------

    async def run(self):
        """Own every session for the app's lifetime. Spawn via engine.spawn at
        startup; signal stop() at shutdown. All enters/exits happen HERE."""
        if not self.servers:
            self._stopped.set()
            return
        async with AsyncExitStack() as stack:
            for name, spec in self.servers.items():
                await self._connect(stack, name, spec)
            while not self._stop.is_set():
                try:
                    await asyncio.wait_for(self._stop.wait(), timeout=RETRY_S)
                except asyncio.TimeoutError:
                    for name, spec in self.servers.items():
                        if name not in self.sessions:
                            await self._connect(stack, name, spec)
        self.sessions.clear()
        self.tools.clear()
        self._stopped.set()

    async def stop(self):
        self._stop.set()
        try:
            await asyncio.wait_for(self._stopped.wait(), timeout=10)
        except asyncio.TimeoutError:
            log.warning("mcp manager did not stop cleanly")

    async def _connect(self, stack, name, spec):
        try:
            from mcp import ClientSession, StdioServerParameters
            from mcp.client.stdio import stdio_client
            params = StdioServerParameters(
                command=spec["command"], args=spec.get("args", []),
                env=spec.get("env"))
            read, write = await stack.enter_async_context(stdio_client(params))
            session = await stack.enter_async_context(ClientSession(read, write))
            await asyncio.wait_for(session.initialize(), timeout=20)
            listed = await asyncio.wait_for(session.list_tools(), timeout=20)
            self.sessions[name] = session
            for t in listed.tools:
                q = f"mcp__{name}__{t.name}"[:64]
                self.tools[q] = (name, t.name, {
                    "name": q,
                    "description": ((t.description or t.name).strip()[:900]
                                    + f" (external tool from \"{name}\")"),
                    "input_schema": t.inputSchema
                    or {"type": "object", "properties": {}},
                })
            self.errors.pop(name, None)
            log.info("mcp server %s connected (%d tools)", name, len(listed.tools))
        except Exception as e:
            self.errors[name] = str(e)[:200]
            log.warning("mcp server %s failed to connect: %s", name, e)

    # ---------- the surface the rest of the app uses ----------

    def tool_definitions(self) -> list:
        return [d for (_, _, d) in self.tools.values()]

    def status(self) -> dict:
        return {name: {
            "connected": name in self.sessions,
            "tools": sorted(q for q, (s, _, _) in self.tools.items() if s == name),
            "error": self.errors.get(name),
        } for name in self.servers}

    def activity_label(self, server_name: str) -> str | None:
        """Trusted, operator-configured display label for this server's work
        (config.local.json's mcp_servers[name]["label"]) — the work-status
        event shows this INSTEAD OF a generic fallback whenever a server has
        one, because Crossband deliberately never learns what a third-party
        MCP server actually does (module docstring above); only the person
        who configured it can honestly describe it. None when unset —
        backend/work_status.py falls back to its own generic label."""
        spec = self.servers.get(server_name) or {}
        label = (spec.get("label") or "").strip()
        return label or None

    async def call(self, qualified: str, args: dict, cap: int = 8000) -> str:
        entry = self.tools.get(qualified)
        if not entry:
            return f"Error: external tool {qualified} is not available right now"
        server, tool, _ = entry
        session = self.sessions.get(server)
        if session is None:
            return f"Error: external server {server} is disconnected"
        try:
            res = await asyncio.wait_for(session.call_tool(tool, args or {}),
                                         timeout=CALL_TIMEOUT_S)
        except Exception as e:
            # drop the server's tools until the retry loop restores them
            self.sessions.pop(server, None)
            self.errors[server] = str(e)[:200]
            for q in [q for q, (s, _, _) in self.tools.items() if s == server]:
                self.tools.pop(q)
            return f"Error: external server {server} failed mid-call: {e}"
        parts = [c.text for c in (res.content or [])
                 if getattr(c, "text", None)]
        out = "\n".join(parts).strip() or "(empty result)"
        if getattr(res, "isError", False) and not out.lower().startswith("error"):
            out = "Error: " + out
        return out[:cap]
