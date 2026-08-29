"""Declarative capability registry + unified integration status.

Generalizes the hard-coded setup service definitions (backend/routers/setup.py
`SERVICES`) into ONE capability registry spanning every kind of integration the
app has - LLM providers, audio, web search/research, MCP servers and the memory
companion - and aggregates their status behind a single additive, read-only
view (GET /api/integrations, see routers/integrations.py).

This module is purely observational: nothing here changes how a model is
selected, edited or run, opens no new credential storage, and touches no schema.
Deleting the endpoint + this module is a clean rollback.

Health is normalized to a small, stable vocabulary so any UI can render every
capability uniformly regardless of kind:

  unconfigured - no credentials present yet (the setup wizard's job to fix); for
                 optional capabilities (memory) this just means "not detected".
  unknown      - configured, but not verified this session (or a fact we don't
                 have yet, e.g. cost provenance).
  healthy      - configured and a capability-specific check passed.
  unhealthy    - configured, but a live check FAILED. A bad key, a network
                 blip or a downed server surfaces HERE, honestly - it never
                 crashes the endpoint and never flips `configured` to false
                 (a failed probe is not proof a key is absent).
  disabled     - present and usable, but switched off.

Validation stays capability-specific - we reuse the exact live checks the setup
wizard already uses (LLM: GET /models, audio: ElevenLabs /user, search: a probe
query), the memory client's own /health probe, and the MCP manager's
connect-time handshake/tool-list. There is no faked generic check.

Cost-provenance and onboarding lifecycle are real, derived values rather than
placeholders: each LLM seat carries the provenance of its cost (from the price
table / a self-hosted declaration), its stored lifecycle (trial vs onboarded)
and whether it is eligible for future auto-selection. This module stays purely
observational - it derives and reports, changing nothing.
"""

import os

from . import provenance as prov
from .capabilities import CAPABILITIES
from .config import DEFAULT_PRICING, provenance_for
from .routers import setup as setup_mod

# Health vocabulary (see module docstring).
UNCONFIGURED = "unconfigured"
UNKNOWN = "unknown"
HEALTHY = "healthy"
UNHEALTHY = "unhealthy"
DISABLED = "disabled"

# Capability metadata layered onto setup.SERVICES. setup.SERVICES stays the
# single source of truth for env vars, the plain-English "what this is" copy and
# the live validator - this table only adds the kind + display name that the
# unified view needs. Order here is the order capabilities are reported in.
SETUP_CAPABILITIES = {
    sid: (c["kind"], c["name"]) for sid, c in CAPABILITIES.items()
}

# Provenance/lifecycle for a capability that has no cost model of its own
# (audio/search/mcp/memory): there is no per-seat rate card to read, so these
# stay honestly `unknown`. LLM entries override both with real derived values.
COST_PROVENANCE_NONE = prov.record(prov.UNKNOWN)
LIFECYCLE_NONE = UNKNOWN

# The capability contract: two additive, descriptive fields on every entry -
# nothing existing is renamed and no `available`/`health` value changes.
#
#   chat_toggle  the per-chat flag this capability powers, so the reader can
#                connect "a key I pasted in setup" to "the chip in my chat
#                header" (web_enabled / voice_mode / memory_enabled). None when
#                the capability has no single chat toggle (LLM providers, MCP).
#   requires[]   what the capability needs to work, expressed as dependencies.
#                Credentials live HERE (env var NAMES only, never values), never
#                as peer rows - this is what lets Web research collapse to two
#                rows instead of one row per key.
#
# Requirements can belong to an ANY-OF group: `web_search` is one such group -
# any ONE of Tavily/Brave turns on shared web search. Reddit is a SEPARATE
# optional enhancement, NOT a member of that OR: backend/tools.py reads only
# TAVILY_API_KEY / BRAVE_API_KEY and emits web_search only if one is present,
# while fetch_page / fetch_reddit_thread work unconditionally with zero keys.
SEARCH_GROUP = "web_search"

# sid -> (chat_toggle, any_of group for its credential, credential optional?)
CAPABILITY_WIRING = {
    sid: (c["chat_toggle"], c["any_of"], c["optional"])
    for sid, c in CAPABILITIES.items()
}


def _requirement(*, env, label, satisfied, optional, setup_service,
                 any_of=None, type="credential"):
    """One dependency a capability needs to work. `env` carries the credential's
    var NAMES only - never a value (a secret must never reach this view)."""
    return {
        "type": type,
        "label": label,
        "env": list(env),        # NAMES only, e.g. ["TAVILY_API_KEY"]
        "optional": optional,
        "satisfied": bool(satisfied),
        "setup_service": setup_service,  # still addressable via POST /api/setup/key
        "any_of": any_of,        # group id for any-of sets (satisfied together), else None
    }


def requirements_met(requires) -> bool:
    """Apply the any-of rule to a flat requirement list: True when every
    non-optional, ungrouped requirement is satisfied AND every any-of GROUP has
    at least one satisfied member. Optional ungrouped requirements are pure
    enhancements and never block. An empty list is vacuously met.

    This is the testable core of the capability contract above: a Reddit-only
    config does NOT satisfy the {Tavily, Brave} search group, because Reddit is
    not in it."""
    groups: dict[str, list[bool]] = {}
    for r in requires:
        g = r.get("any_of")
        if g is not None:
            groups.setdefault(g, []).append(bool(r.get("satisfied")))
        elif not r.get("optional") and not r.get("satisfied"):
            return False
    return all(any(members) for members in groups.values())


def requirements_for_toggle(entries, toggle):
    """Every requirement of every entry that powers a given chat toggle, flattened
    - so the any-of rule can be applied across capabilities that share a toggle
    (the {Tavily, Brave} search group spans two entries)."""
    return [r for e in entries if e.get("chat_toggle") == toggle
            for r in e.get("requires", [])]


def _health(configured: bool, valid, enabled: bool) -> str:
    """Normalize (configured, credential-validity, enabled) into one health word.

    A failed probe (valid is False) is `unhealthy`, never a claim that the
    capability is unconfigured - the credential is still present."""
    if not configured:
        return UNCONFIGURED
    if not enabled:
        return DISABLED
    if valid is False:
        return UNHEALTHY
    if valid is True:
        return HEALTHY
    return UNKNOWN  # configured, not verified this session


def _entry(*, id, display_name, kind, description, configured, valid, enabled,
           available, detail, seats=None, cost_provenance=None, lifecycle=None,
           chat_toggle=None, requires=None):
    """Assemble one integration entry with the full, stable shape."""
    return {
        "id": id,
        "display_name": display_name,
        "kind": kind,
        "description": description,
        "configured": configured,
        "valid": valid,          # True/False where meaningful, else None
        "enabled": enabled,      # user intent: switched on
        "available": available,  # usable right now (creds present / connected)
        "health": _health(configured, valid, enabled),
        "detail": detail,        # human-readable explanation of the health state
        # The per-chat flag this powers, and what it depends on. Additive and
        # descriptive: neither changes `available` or `health`.
        "chat_toggle": chat_toggle,
        "requires": requires or [],
        "seats": seats or [],    # related model seats (LLMs only)
        # Real for LLMs, honest `unknown`/None for capabilities with no
        # per-seat cost model.
        "cost_provenance": dict(cost_provenance or COST_PROVENANCE_NONE),
        "lifecycle": lifecycle if lifecycle is not None else LIFECYCLE_NONE,
    }


def _creds(spec, environ):
    """(key, secret) from the env vars a setup service declares. secret is the
    2nd var (reddit) or None. Values may be None when unconfigured."""
    vals = [environ.get(v) for v in spec["env"]]
    key = vals[0]
    secret = vals[1] if len(vals) > 1 else None
    return key, secret


def _seats_for(provider, participants, pricing):
    """Model seats wired to a provider, so an LLM capability shows which
    roster seats depend on its credential. Read-only projection of the
    participants rows, enriched with each seat's cost provenance and onboarding
    lifecycle - no schema touched."""
    out = []
    for p in participants:
        if p.get("provider") != provider:
            continue
        model = p.get("model") or ""
        cost_provenance = provenance_for(model, pricing,
                                         base_url=p.get("base_url"),
                                         api_key_env=p.get("api_key_env"))
        # Lifecycle defaults to 'trial' for any row without an explicit value
        # (older rows, or seats never onboarded) - never a silent upgrade.
        lifecycle = p.get("lifecycle") or prov.TRIAL
        out.append({
            "slug": p.get("slug"),
            "name": p.get("name"),
            "model": model,
            "enabled": bool(p.get("enabled")),
            # A custom key/endpoint means this seat does NOT ride the global
            # provider key (e.g. a local Ollama/LM-Studio server). Surfaced so
            # the reader isn't misled about which credential it uses.
            "api_key_env": p.get("api_key_env") or None,
            "base_url": p.get("base_url") or None,
            "lifecycle": lifecycle,
            "cost_provenance": cost_provenance,
            # The label and the onboarding gate ship WITH the record, exactly
            # as /api/models/status does (diagnostics.py) - the console
            # renders them verbatim instead of keeping its own copy of
            # provenance.PROVENANCE_LABELS (#234).
            "cost_provenance_label": prov.PROVENANCE_LABELS.get(
                cost_provenance["source"], cost_provenance["source"]),
            "onboardable": cost_provenance["source"] != prov.UNKNOWN,
            # Only onboarded + known-provenance seats may be auto-picked by
            # future price-aware selection; a trial/unknown seat can still be
            # invoked MANUALLY (this flag never blocks execution).
            "eligible_for_auto_selection": prov.eligible_for_auto_selection(
                lifecycle, cost_provenance["source"]),
        })
    return out


def _capability_lifecycle(seats):
    """Roll a provider credential's seats up to one lifecycle word. Conservative:
    'onboarded' only when there is at least one seat and EVERY enabled seat is
    onboarded; otherwise 'trial'. No seats → 'trial' (nothing proven yet)."""
    enabled = [s for s in seats if s["enabled"]] or seats
    if enabled and all(s["lifecycle"] == prov.ONBOARDED for s in enabled):
        return prov.ONBOARDED
    return prov.TRIAL


def _capability_provenance(seats):
    """One provenance record for the credential: the seats' shared source when
    they agree, else `unknown` (mixed provenance can't be summarized honestly)."""
    sources = {s["cost_provenance"]["source"] for s in seats}
    if len(sources) == 1:
        return dict(seats[0]["cost_provenance"])
    return prov.record(prov.UNKNOWN)


async def _setup_entry(sid, participants, *, probe, environ, session_valid,
                       validate, pricing):
    """One entry for a setup-managed capability (anthropic/openai/elevenlabs/
    tavily/brave/reddit). Configured == credentials present, independent of any
    probe result. When probe=True, run the capability's OWN live validator."""
    spec = setup_mod.SERVICES[sid]
    kind, display = SETUP_CAPABILITIES[sid]
    key, secret = _creds(spec, environ)
    configured = all(environ.get(v) for v in spec["env"])

    valid = session_valid.get(sid) if configured else None
    detail = spec["detail"]
    if configured and probe:
        try:
            ok, err = await validate(sid, key, secret)
            valid = bool(ok)
            if not ok and err:
                detail = err
        except Exception as e:  # a probe must never crash the aggregate
            valid = False
            detail = f"Check failed unexpectedly ({type(e).__name__}); the key may still be fine."

    seats = _seats_for(sid, participants, pricing) if kind == "llm" else []
    # For LLM capabilities, "enabled" reflects whether any seat that depends on
    # this credential is switched on. Others have no per-capability toggle, so
    # enabled mirrors configured (there's nothing to switch off separately).
    if kind == "llm":
        enabled = configured and any(s["enabled"] for s in seats)
        # A configured provider with zero enabled seats is present but idle:
        # report it disabled rather than falsely healthy.
        if configured and not seats:
            enabled = True  # provider ready; seats can be added
        cost_provenance = _capability_provenance(seats) if seats else None
        lifecycle = _capability_lifecycle(seats) if seats else prov.TRIAL
    else:
        enabled = configured
        cost_provenance = None
        lifecycle = None

    chat_toggle, any_of, optional = CAPABILITY_WIRING[sid]
    requires = [_requirement(
        env=spec["env"], label=display, satisfied=configured,
        optional=optional, setup_service=sid, any_of=any_of,
    )]

    return _entry(
        id=sid, display_name=display, kind=kind, description=spec["detail"],
        configured=configured, valid=valid, enabled=enabled,
        available=configured, detail=detail, seats=seats,
        cost_provenance=cost_provenance, lifecycle=lifecycle,
        chat_toggle=chat_toggle, requires=requires,
    )


async def _memory_entry(memory, *, probe):
    """The memory companion is optional - absence is by-design, not an error.
    It has no credential; `available` is whether the service answers /health."""
    try:
        available = await memory.probe(force=probe)
    except Exception:
        available = False
    st = memory.status() if hasattr(memory, "status") else {}
    if available:
        detail = ("Shared memory service is running - every chat starts with "
                  "your cheat-sheet, and models can recall/save facts.")
        health_valid = True
    else:
        detail = ("Not detected - chats work fine, but the models start every "
                  "conversation knowing nothing about you.")
        health_valid = None
    entry = _entry(
        id="memory", display_name="Memory (Membro)", kind="memory",
        description="Durable cross-chat memory: summary injection + recall/save/search.",
        configured=available, valid=health_valid, enabled=available,
        available=available, detail=detail,
        # A companion service, not a credential - no `requires` to satisfy; its
        # availability is the /health probe above, not the any-of rule.
        chat_toggle="memory_enabled", requires=[],
    )
    # Memory absence is not a fault: keep it out of the alarming states.
    if not available:
        entry["health"] = UNCONFIGURED
    entry["url"] = st.get("url")
    entry["contract_version"] = st.get("contract_version")
    return entry


def _mcp_entries(mcp, cfg=None):
    """One entry per configured MCP server. Read-only: reflects the manager's
    connect-time handshake/tool-list state (config.local.json is the only way to
    add a server - no management surface here)."""
    entries = []
    try:
        status = mcp.status() if mcp is not None else {}
    except Exception:
        status = {}
    # Who a server's tools are FOR - a real distinction that was completely
    # invisible. `mcp_servers` are offered to every model in the round;
    # `code_mcp` are mounted into a Claude Code guest's own session and no model
    # in the chat can call them. Same wire protocol, different blast radius.
    code_mcp = set((cfg or {}).get("code_mcp") or {})
    model_mcp = set((cfg or {}).get("mcp_servers") or {})
    for name, s in status.items():
        connected = bool(s.get("connected"))
        error = s.get("error")
        tools = s.get("tools") or []
        if connected:
            valid, detail = True, f"Connected - {len(tools)} tool(s) available."
        elif error:
            valid, detail = False, f"Not connected: {error}"
        else:
            valid, detail = None, "Configured; awaiting connection."
        entry = _entry(
            id=f"mcp:{name}", display_name=f"MCP · {name}", kind="mcp",
            description="External MCP server (config.local.json only; read-only here).",
            configured=True, valid=valid, enabled=True,
            available=connected, detail=detail,
        )
        entry["tools"] = tools
        # "models" | "code" | "both"; None when cfg wasn't supplied (unknown,
        # never guessed).
        if name in code_mcp and name in model_mcp:
            entry["used_by"] = "both"
        elif name in code_mcp:
            entry["used_by"] = "code"
        elif model_mcp or code_mcp:
            entry["used_by"] = "models"
        else:
            entry["used_by"] = None
        entries.append(entry)
    return entries


def _room_and_ability_entries(cfg, environ):
    """The three shipped capabilities that appeared in NO registry section.

    The coding guest, the GitHub tools and event ingestion are all real, live,
    user-visible things this room can do - and all three existed only inside the
    `/api/state` config blob, so the console could not show their health and
    there was no single place listing what a room can actually do.

    Kinds are chosen to say what each thing IS, not where it came from:
      code    - the guest TAKES A TURN, like a participant. It belongs with the
                room, not with the credential-shaped rows.
      toolset - abilities the models can call (GitHub).
      channel - ways things get INTO a chat without a model asking (ingest).

    Observational, like the rest of this module: `/api/state` keeps its existing
    `code`/`github` keys untouched, so nothing downstream changes.
    """
    from . import guest as guest_mod
    from . import tools as tools_mod

    entries = []

    # ── the coding guest ────────────────────────────────────────────────────
    # CONFIG facts come from cfg; AVAILABILITY facts come from status(). Keeping
    # them apart matters: `guest.status()` zeroes `repos` and `writes` on any
    # machine where the SDK or CLI is missing, so reading configuration out of it
    # would report a fully-configured room as "not set up" when the real problem
    # is a missing CLI - the exact unconfigured-vs-unhealthy confusion this entry
    # exists to prevent. (Caught by CI, which has no Claude Code installed.)
    repos = sorted(cfg.get("code_repos") or {})
    st = guest_mod.status(cfg)
    available = bool(st.get("available"))
    reason = (st.get("reason") or "").strip()
    if available:
        detail = (f"Ready - can be summoned into a chat with the code toggle on "
                  f"({len(repos)} repo(s): {', '.join(repos)}).")
    elif repos:
        detail = f"Configured ({len(repos)} repo(s)), but not usable here: {reason}."
    else:
        detail = "Not set up - add `code_repos` to config.local.json."
    entry = _entry(
        id="code:claude_code", display_name="Claude Code guest", kind="code",
        description=("A coding agent that joins a chat for one turn to read your "
                     "code and answer, or to branch, test and open a pull request."),
        # `configured` is "the operator asked for this": repos are the opt-in.
        # Without them the tool is never offered, so it is unconfigured, not broken.
        configured=bool(repos), valid=(True if available else (False if repos else None)),
        enabled=True, available=available, detail=detail,
        chat_toggle="code_enabled",
    )
    # A typed sub-object, NOT flattened strings: the billing-drift warning stays
    # presentation logic in the UI rather than a sentence the backend guesses at.
    entry["guest"] = {
        # Config facts, so they still describe the room on a machine where the
        # guest can't run (see above). `use_api_key` is the exception by design:
        # status() computes it from cfg AND the environment and returns it on
        # every path, including the unavailable ones.
        "repos": repos,
        "writes": bool(cfg.get("code_allow_writes")),
        "use_api_key": bool(st.get("use_api_key")),
    }
    entries.append(entry)

    # ── GitHub tools ────────────────────────────────────────────────────────
    gh_map = dict(cfg.get("github_repos") or {})
    gh_repos = sorted(gh_map)
    try:
        gh_ok = bool(tools_mod.github_available(cfg))
    except Exception:
        gh_ok = False
    entry = _entry(
        id="toolset:github", display_name="GitHub tools", kind="toolset",
        description=("Lets the models read your issues and pull requests, and file, "
                     "comment on and edit them - every write signed with which AI did it."),
        configured=bool(gh_repos),
        valid=(True if gh_ok else (False if gh_repos else None)),
        enabled=True, available=gh_ok,
        detail=(f"Ready - {len(gh_repos)} repo(s): {', '.join(gh_repos)}."
                if gh_ok else
                ("Configured, but no GitHub token could be resolved - log in with "
                 "`gh auth login` or set GH_TOKEN." if gh_repos else
                 "Not set up - add `github_repos` to config.local.json.")),
        chat_toggle="code_enabled",
        requires=[_requirement(
            env=["GH_TOKEN", "GITHUB_TOKEN"], label="A GitHub token (or a logged-in `gh` CLI)",
            satisfied=gh_ok, optional=False, setup_service=None)],
    )
    # nickname -> owner/repo, typed for the repo-access panel (#86) - which
    # GitHub repository each short name actually reaches, so a rename is
    # visible instead of asserted from memory. Config facts, no secrets.
    entry["repos"] = gh_map
    entries.append(entry)

    # ── event ingestion ─────────────────────────────────────────────────────
    # Always available: the endpoint is part of the app. The token is optional
    # and only meaningful off-loopback, so an unset one is not a fault.
    tokened = bool((cfg.get("ingest_token") or "").strip())
    entries.append(_entry(
        id="channel:ingest", display_name="Incoming events", kind="channel",
        description=("An endpoint your own tools can post into a chat - a job watcher, "
                     "a build system, a calendar. Crossband renders what arrives and "
                     "assigns it no meaning."),
        configured=True, valid=True, enabled=True, available=True,
        detail=("Listening on POST /api/ingest - "
                + ("requests must carry the configured bearer token."
                   if tokened else
                   "no token set, so it accepts any local caller (loopback-only by default).")),
        requires=[_requirement(
            env=["CROSSBAND_INGEST_TOKEN"], label="A bearer token (only needed off-loopback)",
            satisfied=tokened, optional=True, setup_service=None)],
    ))
    return entries


async def collect(*, participants, memory, mcp, probe=False,
                  environ=None, session_valid=None, validate=None, pricing=None,
                  cfg=None):
    """Aggregate every capability into the unified, read-only status list.

    probe=False (default): report KNOWN state cheaply - session validity for
      setup services, the memory client's cached /health, and the MCP manager's
      already-established connection state. No new external LLM/search calls.
    probe=True: additionally run each configured setup capability's OWN live
      validator and force a fresh memory probe. Every probe is isolated in a
      try/except so one failing external service can never crash the aggregate.
    """
    environ = os.environ if environ is None else environ
    session_valid = setup_mod._session_valid if session_valid is None else session_valid
    validate = setup_mod._validate if validate is None else validate
    pricing = DEFAULT_PRICING if pricing is None else pricing

    entries = []
    for sid in SETUP_CAPABILITIES:
        entries.append(await _setup_entry(
            sid, participants, probe=probe, environ=environ,
            session_valid=session_valid, validate=validate, pricing=pricing))
    entries.extend(_mcp_entries(mcp, cfg))
    entries.append(await _memory_entry(memory, probe=probe))
    # cfg absent (an older caller / a test stub) → skip rather than invent state.
    if cfg is not None:
        try:
            entries.extend(_room_and_ability_entries(cfg, environ))
        except Exception:
            pass  # observational: a capability we can't read never breaks the view
    return entries
