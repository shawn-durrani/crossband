"""get_diagnostic - the ONE MCP tool every summoned Claude Code guest is
mounted with automatically, independent of the operator's
`code_mcp` config (backend/guest.py). It exposes a small, fixed set of
read-only, content-free diagnostics behind a CLOSED enum - never a URL, path,
or free-form query - so the allowlist is structural, not a validation promise:
`name` isn't just checked in Python, it's declared as a JSON-Schema `enum` in
the tool's own input schema, so the SDK itself rejects any other value before
this module's code ever runs. `_dispatch` double-checks anyway (belt and
braces, and it's what makes the allowlist unit-testable without the SDK).

Built on claude_agent_sdk's IN-PROCESS SDK MCP server support
(`create_sdk_mcp_server` / `@tool`, confirmed present in the installed SDK
version) rather than a stdio subprocess like the operator-configured
`code_mcp` servers (e.g. Membro's `mcp_server.py`): no extra process to spawn
or manage, direct access to this app's own functions, and - the reason it's
always-mounted rather than opt-in - nothing here carries a secret or a
network egress path, so there is no credential-exposure tradeoff the way
wiring an authenticated `code_mcp` server has (see
docs/GUEST_PERMISSIONS.md's "Giving a guest scoped read access to memory").

The allowlist, the description, the input schema, and the name -> function
dispatch itself all live in backend/diagnostics.py - this
module is a thin MCP-protocol adapter around them, re-exported here under
their original names (`DIAGNOSTIC_NAMES`, `_DISPATCH`, `_dispatch`,
`_INPUT_SCHEMA`, `_DESCRIPTION`) so this module's own behavior and tests are
unchanged. Claude/GPT's native tool-calling surface (backend/tools.py)
imports the exact same things from backend/diagnostics.py directly, so there
is exactly ONE dispatch map behind both surfaces, not two that could drift.
No mutation capability exists anywhere in that shared dispatch: it maps to
read-only functions and nothing else, and `build_server`/`build_tool` never
accept or forward a caller-supplied URL, path, host, or query - `cfg` (this
guest visit's own settings) is the only thing ever closed over, and it flows
one-way, backend -> guest, never guest -> backend.
"""

import json

from . import diagnostics

SERVER_NAME = "crossband-diag"
TOOL_NAME = "get_diagnostic"

# Re-exported from backend/diagnostics.py (the single source of truth for
# both this guest MCP tool and backend/tools.py's native equivalent).
DIAGNOSTIC_NAMES = diagnostics.DIAGNOSTIC_NAMES
_DISPATCH = diagnostics._DIAGNOSTIC_DISPATCH
_dispatch = diagnostics.dispatch_diagnostic
_INPUT_SCHEMA = diagnostics.diagnostic_input_schema()
_DESCRIPTION = diagnostics.DIAGNOSTIC_DESCRIPTION


def build_tool(cfg: dict):
    """Build the get_diagnostic SdkMcpTool, closing over `cfg` - this guest
    visit's own settings dict (pricing, model seeds, memory_url; the exact
    same dict backend/guest.py already threads through the rest of the guest
    run) - so the tool needs no FastAPI Request/app.state and no network hop
    to reach this same process's own data. Returned separately from
    build_server (below) so tests can call `.handler(...)` directly - the
    real code path the SDK itself calls, without standing up the MCP
    protocol layer."""
    from claude_agent_sdk import tool

    @tool(TOOL_NAME, _DESCRIPTION, _INPUT_SCHEMA)
    async def get_diagnostic(args):
        name = args.get("name")
        payload = await _dispatch(name, cfg)
        is_error = bool(payload.get("refused"))
        return {"content": [{"type": "text", "text": json.dumps(payload)}],
                "is_error": is_error}

    return get_diagnostic


def build_server(cfg: dict):
    """This visit's get_diagnostic MCP server - an in-process
    `McpSdkServerConfig`, mounted unconditionally for every guest run
    (backend/guest.py), in both investigate and implement mode. Cheap to
    build; called once per visit."""
    from claude_agent_sdk import create_sdk_mcp_server

    return create_sdk_mcp_server(name=SERVER_NAME, tools=[build_tool(cfg)])
