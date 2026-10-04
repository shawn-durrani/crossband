"""MCP client layer: Crossband consumes external MCP servers.

Config (config.local.json, private) lists stdio servers; Crossband connects at
startup, discovers their tools, and offers them to every participant
namespaced as `mcp__<server>__<tool>`. Calls dispatch through the existing
run_tool pipeline and render as ordinary tool chips - no new UI (a
plugins-style UX remains a separate, open question). The public repo never
knows what any server means: names, tools, and descriptions all come from the
servers at runtime.

Design notes:
- MCP at the edge, native at the core: the built-in tool families
  (web/memory/github/code) stay native - this layer is only for EXTERNAL
  capabilities, mirroring Membro's own choice (MCP adapter for outsiders,
  native interface for itself).
- Lifecycle lives in ONE owning task per app (connect → serve → close in the
  same task): anyio cancel scopes must exit in the task that entered them,
  so FastAPI's startup/shutdown (different tasks) signal via events instead
  of touching the sessions directly.
- Graceful degrade everywhere: a server that fails to connect is reported in
  /api/state and retried every RETRY_S; a call that fails marks that server
  disconnected until the retry loop reconnects it; the models just see an
  honest error string.
- A server's tools stay on offer while it's down (#564). The tool list
  leads every seat's cached prompt, so dropping a server's tools on a
  failed call and adding them back on reconnect made every seat write its
  whole cached prompt twice. A call while it's down is refused instead.
  A server that has never connected has no known tools, so its tools join
  the list when it first connects.
- A result can say work carries on in the background (#604). The seat still
  gets text; `call_result` hands the dispatcher the result's structured
  content too, and backend/mcpjobs.py reads its `background` block. `poll`
  is the watcher's own call: short, and a slow answer never marks the
  server down, because a progress check that took too long says nothing
  about the work.
"""

import asyncio
import logging
from contextlib import AsyncExitStack
from dataclasses import dataclass

log = logging.getLogger("crossband.mcp")

RETRY_S = 60
CALL_TIMEOUT_S = 45


@dataclass(frozen=True)
class CallOutcome:
    """One tool call's result: the text the seat reads, and the result's
    structured content when the server sent a dict (None otherwise)."""
    text: str
    structured: dict | None = None


def structured_of(res) -> dict | None:
    """A CallToolResult's structured content as a dict, or None. The SDK
    this repo pins (mcp 2.x) names the field `structured_content`, and 1.x
    named it `structuredContent`, so both are read."""
    for attr in ("structured_content", "structuredContent"):
        val = getattr(res, attr, None)
        if isinstance(val, dict):
            return val
    return None


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
            fresh = {}
            for t in listed.tools:
                q = f"mcp__{name}__{t.name}"[:64]
                fresh[q] = (name, t.name, {
                    "name": q,
                    "description": ((t.description or t.name).strip()[:900]
                                    + f" (external tool from \"{name}\")"),
                    "input_schema": t.input_schema
                    or {"type": "object", "properties": {}},
                })
            # A reconnect that lists what it listed before leaves the list
            # byte for byte as it was: existing keys keep their place. Only
            # a tool the server no longer offers goes, and a new one joins.
            for q in [q for q, (s, _, _) in self.tools.items()
                      if s == name and q not in fresh]:
                self.tools.pop(q)
            self.tools.update(fresh)
            self.errors.pop(name, None)
            log.info("mcp server %s connected (%d tools)", name, len(listed.tools))
        except Exception as e:
            self.errors[name] = str(e)[:200]
            log.warning("mcp server %s failed to connect: %s", name, e)

    # ---------- the surface the rest of the app uses ----------

    def tool_definitions(self) -> list:
        return [d for (_, _, d) in self.tools.values()]

    def status(self) -> dict:
        # `tools` is what can be called right now, so a server that's down
        # lists none, though its tools stay on offer to the seats (#564).
        return {name: {
            "connected": name in self.sessions,
            "tools": (sorted(q for q, (s, _, _) in self.tools.items()
                             if s == name)
                      if name in self.sessions else []),
            "error": self.errors.get(name),
        } for name in self.servers}

    def activity_label(self, server_name: str) -> str | None:
        """Trusted, operator-configured display label for this server's work
        (config.local.json's mcp_servers[name]["label"]) - the work-status
        event shows this INSTEAD OF a generic fallback whenever a server has
        one, because Crossband deliberately never learns what a third-party
        MCP server actually does (module docstring above); only the person
        who configured it can honestly describe it. None when unset -
        backend/work_status.py falls back to its own generic label."""
        spec = self.servers.get(server_name) or {}
        label = (spec.get("label") or "").strip()
        return label or None

    def server_of(self, qualified: str) -> tuple[str, str] | None:
        """(server, tool) behind a qualified name, or None."""
        entry = self.tools.get(qualified)
        return (entry[0], entry[1]) if entry else None

    def qualified(self, server: str, tool: str) -> str | None:
        """The name the seats know a server's tool by, or None when the
        server never listed it."""
        for q, (s, t, _) in self.tools.items():
            if s == server and t == tool:
                return q
        return None

    async def call(self, qualified: str, args: dict, cap: int = 8000) -> str:
        return (await self.call_result(qualified, args, cap=cap)).text

    async def call_result(self, qualified: str, args: dict,
                          cap: int = 8000) -> CallOutcome:
        """`call`, plus the result's structured content (#604)."""
        entry = self.tools.get(qualified)
        if not entry:
            return CallOutcome(
                f"Error: external tool {qualified} is not available right now")
        server, tool, _ = entry
        session = self.sessions.get(server)
        if session is None:
            return CallOutcome(
                f"Error: external server {server} is disconnected right "
                f"now, so {qualified} did nothing. The app retries it "
                f"every {RETRY_S} seconds; say it's unavailable rather "
                "than guessing what it would have returned.")
        try:
            res = await asyncio.wait_for(session.call_tool(tool, args or {}),
                                         timeout=CALL_TIMEOUT_S)
        except Exception as e:
            # Mark the server down until the retry loop reconnects it. Its
            # tools stay listed (#564): the next call is refused above.
            self.sessions.pop(server, None)
            self.errors[server] = str(e)[:200]
            return CallOutcome(
                f"Error: external server {server} failed mid-call: {e}")
        parts = [c.text for c in (res.content or [])
                 if getattr(c, "text", None)]
        out = "\n".join(parts).strip() or "(empty result)"
        if res.is_error and not out.lower().startswith("error"):
            out = "Error: " + out
        return CallOutcome(out[:cap],
                           None if res.is_error else structured_of(res))

    async def poll(self, server: str, tool: str,
                   timeout: float = 10.0) -> dict | None:
        """Call a server's own progress tool with no arguments for the
        background watcher (#604). Returns the result's structured content,
        or None for any failure, which the watcher retries. A timeout leaves
        the server up: a slow progress answer isn't a broken connection.
        Any other failure marks it down, exactly as a seat's call does."""
        session = self.sessions.get(server)
        if session is None or self.qualified(server, tool) is None:
            return None
        try:
            res = await asyncio.wait_for(session.call_tool(tool, {}),
                                         timeout=timeout)
        except asyncio.TimeoutError:
            return None
        except Exception as e:
            self.sessions.pop(server, None)
            self.errors[server] = str(e)[:200]
            return None
        if res.is_error:
            return None
        return structured_of(res)
