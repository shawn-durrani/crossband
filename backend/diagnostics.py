"""Shared, FastAPI-independent diagnostic service functions.

The single source of truth behind FIVE read-only surfaces:

  - GET /api/setup/status         -> health_status()        (backend/routers/setup.py)
  - GET /api/models/status        -> participants_status()  (backend/routers/models.py)
  - GET /api/voice/trace/summary  -> voice_latency_summary() (backend/routers/voice.py)
  - the summoned Claude Code guest's get_diagnostic MCP tool (backend/diag_mcp.py)
  - Claude/GPT's own native get_diagnostic tool in normal chat (backend/tools.py)

Every function here takes only the plain values/objects it actually needs (a
memory-probe object, a pricing dict, model seeds, a time window) - never a
FastAPI `Request` or `app.state`. That is what lets get_diagnostic call the
SAME logic directly, in-process, with no HTTP hop and no request plumbing -
whether the caller is a summoned Claude Code guest (backend/diag_mcp.py) or a
narrating participant's own tool call in a normal chat round
(backend/tools.py). The route handlers in backend/routers/ call these exact
functions too, so there is exactly one implementation of each, not two that
can drift. The name -> function dispatch for get_diagnostic itself
(`dispatch_diagnostic`, below) is likewise defined ONCE here and imported by
both tool surfaces, rather than each keeping its own dispatch map.

Privacy floor: nothing here ever returns a transcript, a message body, a
credential, or an arbitrary log line - only booleans, labels, model ids,
counts and numeric latency percentiles. `voice_latency_summary` in
particular is content-free at the point of INGEST, not just here:
backend/voice_trace.py's `sanitize_stage` accepts only an allowlisted stage
name, a numeric duration, and bounded provider/model/tts/speaker
identifiers - there is no transcript column in the table for this function
(or anything else) to leak.
"""

import json
import os

from . import db
from . import provenance as prov
from . import voice_trace
from .capabilities import CAPABILITIES
from .config import provenance_for

# Derived from the ONE capability table (backend/capabilities.py). Shared by
# GET /api/setup/status, POST /api/setup/key, and the "health" diagnostic.
SERVICES = {
    sid: {"env": list(c["env"]), "unlocked": c["unlocked"], "detail": c["detail"]}
    for sid, c in CAPABILITIES.items()
}

# Live-validated OK this session (never persisted; resets on restart - a
# restart re-reads .env, and `configured` carries the state on its own).
# Mutated by POST /api/setup/key (backend/routers/setup.py); read here.
session_valid: dict[str, bool] = {}


async def health_status(memory) -> dict:
    """Per-service {configured, valid, detail} - booleans and labels only,
    key material NEVER included - plus companion memory-service reachability.

    `memory` is anything exposing `async probe(force: bool) -> bool` (a
    `MemoryClient`). Callers supply their own: the route hands in the app's
    live, cached client (`request.app.state.memory`); the diagnostics MCP
    tool hands in a throwaway one built from the guest's own cfg - either
    way this function itself never touches FastAPI's Request/app.state."""
    services = {}
    for svc, spec in SERVICES.items():
        configured = all(os.environ.get(v) for v in spec["env"])
        services[svc] = {
            "configured": configured,
            # true only when live-validated this session; null = not checked
            "valid": session_valid.get(svc) if configured else None,
            "detail": spec["detail"],
        }
    available = await memory.probe(force=True)  # "Check again" needs a fresh probe
    services["memory_service"] = {
        "configured": available,
        "valid": available,
        "detail": ("Shared memory service is running - every chat starts with your cheat-sheet."
                   if available else
                   "Not detected - chats work fine, but the models start every conversation knowing nothing about you."),
    }
    return {
        "services": services,
        # the wizard's progress line: chatting works once one model key is in
        "any_model_key": services["anthropic"]["configured"] or services["openai"]["configured"],
    }


# config.json only holds a per-provider FIRST-RUN seed for these two default
# seats (backend/db.py maps that seed onto them at seed time).
_SEED_SEAT = {"claude": "anthropic_model", "gpt": "openai_model"}


def participants_status(pricing: dict, seeds: dict | None = None) -> list[dict]:
    """Read-only answer to 'what model is each participant ACTUALLY running?':
    raw provider model ids and onboarding/cost-provenance metadata, never
    message content. See backend/routers/models.py for the full field-by-field
    rationale (unchanged here, just Request-free).

    `seeds` maps the two default seats' config.json first-run model
    ('claude'/'gpt' -> model id or None); omit it (or pass {}) from a
    context with no such config - seed/seed_drift simply come back
    None/False for every participant."""
    seeds = seeds or {}
    con = db.connect()
    try:
        out = []
        for p in db.get_participants(con):
            slug = p["slug"]
            row = con.execute(
                "SELECT usage_json, created_at FROM messages "
                "WHERE speaker=? AND usage_json IS NOT NULL "
                "ORDER BY created_at DESC, id DESC LIMIT 1",
                (slug,),
            ).fetchone()
            last_used = last_used_at = None
            if row:
                try:
                    last_used = (json.loads(row["usage_json"]) or {}).get("model")
                    last_used_at = row["created_at"]
                except (ValueError, TypeError):
                    pass  # malformed usage row: treat as "no stamp", don't 500
            configured = p["model"]
            seed = seeds.get(slug) if slug in _SEED_SEAT else None
            lifecycle = p.get("lifecycle") or prov.TRIAL
            source = provenance_for(configured, pricing,
                                    base_url=p.get("base_url"),
                                    api_key_env=p.get("api_key_env"))["source"]
            out.append({
                "slug": slug,
                "name": p["name"],
                "provider": p["provider"],
                "enabled": bool(p["enabled"]),
                "configured": configured,
                "last_used": last_used,
                "last_used_at": last_used_at,
                "seed": seed,
                "seed_drift": bool(seed and seed != configured),
                "pending": bool(last_used and last_used != configured),
                "lifecycle": lifecycle,
                "cost_provenance": source,
                "cost_provenance_label": prov.PROVENANCE_LABELS.get(source, source),
                "onboardable": source != prov.UNKNOWN,
                "eligible_for_auto_selection":
                    prov.eligible_for_auto_selection(lifecycle, source),
            })
        return out
    finally:
        con.close()


# ---------- shared get_diagnostic dispatch ----------
#
# The ONE name -> function map behind BOTH read-only surfaces that expose
# these read-only diagnostics to a model as a callable tool:
#   - backend/diag_mcp.py    -> the summoned Claude Code guest's MCP tool
#   - backend/tools.py       -> Claude/GPT's own native tool-calling ("get_diagnostic")
# The guest half and the native half were built separately; both import
# DIAGNOSTIC_NAMES/dispatch_diagnostic/diagnostic_input_schema/
# DIAGNOSTIC_DESCRIPTION from HERE rather than each declaring its own copy,
# so there is exactly one place that decides what a name resolves to and one
# place the enum/description live - nothing for the two surfaces to drift on.
#
# The allowlist is structural, not just a validation promise: `name` is
# published as a JSON-Schema `enum` (diagnostic_input_schema, below) in BOTH
# tools' own input schema, so an SDK/provider that enforces its declared
# schema refuses an out-of-enum value before dispatch_diagnostic ever runs.
# dispatch_diagnostic refuses again anyway (belt and braces, and what makes
# the allowlist unit-testable without a live SDK/provider in the loop) -
# there is no url/path/host/query parameter anywhere for a caller to smuggle
# a target through; `name` is the only input either schema accepts.

DIAGNOSTIC_NAMES = ("health", "models", "voice_latency", "conversation_spend",
                    "conversation_performance")

DIAGNOSTIC_DESCRIPTION = (
    "Read one of Crossband's own local diagnostics. Loopback-only, read-only, "
    "GET-equivalent, and content-free by construction: it can NEVER return a "
    "transcript, message text, credentials, or arbitrary logs - there is no "
    "way to pass a URL, path, or query, only a fixed diagnostic name. "
    "name must be exactly one of: \"health\" (which provider keys are "
    "configured/validated, and whether the companion memory service is "
    "reachable - booleans and labels only, never key material), \"models\" "
    "(what model each chat participant is actually configured/last used to "
    "run - model ids and onboarding metadata, never message content), "
    "\"voice_latency\" (recent-turn count plus p50/p95/max latency per voice "
    "pipeline stage - durations and counts only, never what was said), or "
    "\"conversation_spend\" (THIS conversation's running metered API/voice "
    "cash total so far, then a dynamic breakdown by party/producer and by "
    "provider - dollars and token counts only; subscription-equivalent and "
    "unknown usage are shown apart and never summed into the metered total). "
    "Use this instead of guessing, asking the user to check, or summoning "
    "Claude Code just to read the app's own live state."
)


def diagnostic_input_schema() -> dict:
    """The `name` enum IS the allowlist (see module note above) - returns a
    fresh dict each call so neither caller can mutate the other's copy."""
    return {
        "type": "object",
        "properties": {
            "name": {
                "type": "string",
                "enum": list(DIAGNOSTIC_NAMES),
                "description": (
                    "Which diagnostic to read: \"health\" (service keys "
                    "configured/validated + memory-service reachability), "
                    "\"models\" (each chat participant's actual configured/"
                    "last-used model), \"voice_latency\" (recent-turn count "
                    "+ content-free stage latency percentiles), or "
                    "\"conversation_spend\" (this conversation's running "
                    "metered cash total + dynamic party/producer/provider "
                    "breakdown), or \"conversation_performance\" (why THIS "
                    "conversation is fast or slow, regardless of modality: "
                    "context weight by component, megabytes re-uploaded every "
                    "turn, recent turn latencies, and voice stage latencies "
                    "when the chat has used voice)."),
            },
        },
        "required": ["name"],
    }


async def _diag_health(cfg: dict) -> dict:
    """Service key configured/validated + memory-service reachability,
    reusing `health_status` above with a throwaway MemoryClient built from
    the caller's own cfg (guest visit or chat round) - this module still
    never touches FastAPI's Request/app.state."""
    from .memory_client import MemoryClient
    memory = MemoryClient(cfg.get("memory_url") or "http://127.0.0.1:8901")
    try:
        return await health_status(memory)
    finally:
        await memory.aclose()


async def _diag_models(cfg: dict) -> dict:
    """What model each chat participant is actually configured/last used to
    run, reusing `participants_status` above."""
    seeds = {"claude": cfg.get("anthropic_model"), "gpt": cfg.get("openai_model")}
    return {"participants": participants_status(cfg.get("pricing") or {}, seeds)}


# Stages the MODEL-facing surfaces exclude: the seats repeatedly read
# by-design multi-voice serialization as a defect, and a framing note sitting
# right there in the payload does not change what they conclude, so the number
# is simply not handed to them anymore. The development endpoint
# (/api/voice/trace/summary) still reports everything; the exclusion is named
# in the payload, not hidden.
MODEL_EXCLUDED_STAGES = ("playback_queue_wait",)


async def _diag_voice_latency(cfg: dict) -> dict:
    """Recent voice-turn count plus p50/p95/max latency per pipeline stage,
    reusing `voice_latency_summary` above - minus the stages models have
    repeatedly misread as defects."""
    summary = voice_latency_summary()
    for s in MODEL_EXCLUDED_STAGES:
        summary["stages"].pop(s, None)
        summary["stage_notes"].pop(s, None)
    summary["excluded_stages_note"] = (
        "playback_queue_wait - a later speaker's finished audio waiting for "
        "the current speaker to stop, i.e. multi-voice serialization by "
        "design - is excluded from this view and reported only on the "
        "development endpoint; do not raise it as a latency concern")
    return summary


def _spend_group(g: dict) -> dict:
    """One breakdown row, trimmed to the metered-cash view this diagnostic
    reports. Keeps the metered figure (the only incremental cash) up front and
    carries subscription-equivalent / unknown alongside - separately labelled,
    never folded in - so a party that ran entirely on a subscription is still
    visible without inflating the metered total."""
    return {
        "key": g["key"],
        "label": g["label"],
        # Keys are accounting.CATEGORIES - the literal cash-axis names the
        # summarize() groups carry; kept as literals here so this row-shaper
        # needs no import of the accounting module.
        "metered": round(g.get("metered", 0.0), 6),
        "subscription_equiv": round(g.get("subscription_equiv", 0.0), 6),
        "unknown": round(g.get("unknown", 0.0), 6),
        "tokens": g.get("tokens", 0),
        "not_tracked": g.get("not_tracked", False),
    }


async def _diag_conversation_spend(cfg: dict) -> dict:
    """THIS conversation's running metered API/voice cash total, then a
    dynamic breakdown - by conversation party / usage producer, and by
    provider/service.

    Incremental METERED cash is the headline and the only figure summed:
    subscription-equivalent and unknown-provenance usage are returned as
    separate labelled values, never added into the metered total (a fixed
    subscription is a sunk cost, not spend Crossband is incurring). Every
    breakdown is keyed on the cost event's own speaker/source/provider, so any
    number of current-or-future participants, guests and integrations flow in
    with no change here - nothing is hardcoded to a two-model roster.

    Scoped to the active chat via ``cfg['chat_id']`` (the same chat id the
    engine already threads through every round, backend/engine.py). Content-free:
    dollars, token counts, slugs and labels only - never message text. When
    there is no active conversation (e.g. a context with no chat id) it says so
    rather than reporting another chat's numbers."""
    chat_id = cfg.get("chat_id")
    if not chat_id:  # None, 0, or missing → no conversation to attribute to
        return {
            "active_conversation": False,
            "note": ("No active conversation to price - this surface reports "
                     "the running metered total of the chat it is called from."),
        }
    from . import accounting
    con = db.connect()
    try:
        events = list(accounting.iter_cost_events(
            con, pricing=cfg.get("pricing") or None, chat_id=chat_id))
        s = accounting.summarize(events)
    finally:
        con.close()
    totals = s["totals"]
    return {
        "active_conversation": True,
        "chat_id": chat_id,
        "currency": "USD",
        # The answer first: incremental metered cash for this conversation.
        "metered_total": round(totals[accounting.CAT_METERED], 6),
        # …then the modular split, biggest metered first (dynamic keys).
        "by_party": [_spend_group(g) for g in s["by_party"]],
        "by_producer": [_spend_group(g) for g in s["by_source"]],
        "by_provider": [_spend_group(g) for g in s["by_provider"]],
        "tokens": s["tokens"],
        # Shown apart, never summed into metered_total.
        "informational": {
            "subscription_equiv": round(totals[accounting.CAT_SUBSCRIPTION], 6),
            "unknown": round(totals[accounting.CAT_UNKNOWN], 6),
            "note": ("Subscription-equivalent is an API-list-price estimate a "
                     "subscription already covers; unknown had no recorded "
                     "billing mode. Neither is incremental cash - reported "
                     "separately, never added to metered_total."),
        },
        # Producers that emitted an event but recorded no cost (a gap, not $0).
        "not_tracked": s["not_tracked"],
    }


# The ENTIRE dispatch surface - no write-capable function is ever reachable
# through it. Adding a mutation here would mean adding it to this literal
# dict, which every reviewer of this file can see at a glance.

async def _diag_conversation_performance(cfg: dict) -> dict:
    """Why THIS conversation is fast or slow - both modalities, one answer.

    Exists because a chat can become unusable with no surface able to say why:
    the header gauge counted text only and read "green", the auto-fold never
    fired, and nothing anywhere reported the megabytes being re-uploaded to
    every provider on every turn. Asking in-chat now returns the whole picture
    rather than requiring someone to read the SQLite file by hand.

    Content-free by construction: component sizes, byte counts, message counts
    and durations only - never message text, filenames, or attachment content.
    """
    from . import context_weight, db
    chat_id = cfg.get("chat_id")
    if not chat_id:
        return {"error": "no chat in scope for this diagnostic"}

    con = db.connect()
    try:
        row = con.execute("SELECT * FROM chats WHERE id=?", (chat_id,)).fetchone()
        if not row:
            return {"error": f"chat {chat_id} not found"}
        chat = dict(row)
        msgs = [dict(m) for m in con.execute(
            "SELECT id, speaker, content, created_at, usage_json FROM messages "
            "WHERE chat_id=? ORDER BY id", (chat_id,))]
        by_id = {m["id"]: m for m in msgs}
        for m in msgs:
            m["attachments"], m["tool_events"] = [], []
        for a in con.execute(
                "SELECT message_id, mime, size FROM attachments WHERE message_id IN "
                "(SELECT id FROM messages WHERE chat_id=?)", (chat_id,)):
            if a["message_id"] in by_id:
                by_id[a["message_id"]]["attachments"].append(dict(a))
        for e in con.execute(
                "SELECT message_id, input_json, output_text FROM tool_events WHERE "
                "message_id IN (SELECT id FROM messages WHERE chat_id=?)", (chat_id,)):
            if e["message_id"] in by_id:
                by_id[e["message_id"]]["tool_events"].append(dict(e))

        ctx = context_weight.estimate(chat, msgs, cfg, len(cfg.get("memory_summary") or ""))
        participants = len(db.get_chat_participants(con, chat_id))
    finally:
        con.close()

    upload_mb = round(ctx["upload_bytes"] / 1e6, 1)
    findings = []
    if ctx["attachments"] > ctx["history"] * 2 and ctx["attachments"] > 5000:
        findings.append(
            f"attachments are {ctx['attachments']:,} of {ctx['total']:,} context "
            "tokens - far more than the conversation's own words")
    if upload_mb >= 5:
        findings.append(
            f"~{upload_mb} MB of attachments is re-sent to EACH of the "
            f"{participants} participant(s) on every turn; this is upload time "
            "that grows with the conversation and is usually the real cause of "
            "a slow-feeling chat")
    if ctx["total"] > 60000:
        findings.append("context is large enough to dilute attention - a fresh "
                        "chat stays sharper, and memory carries over")

    out = {
        "chat_id": chat_id,
        "participants": participants,
        "context_tokens": {k: ctx[k] for k in
                           ("history", "attachments", "research", "chat_summary",
                            "memory", "overhead", "total")},
        "per_turn_upload_mb": upload_mb,
        "images_in_context": ctx["image_count"],
        "findings": findings or ["nothing notable - this conversation is light"],
        "note": ("Context is what every participant re-reads per reply. Upload is "
                 "what physically goes over the wire each turn, per participant - "
                 "the figure no other surface reports."),
    }

    voice = voice_latency_summary()
    turns = voice.get("turns") or 0
    if turns:
        for s in MODEL_EXCLUDED_STAGES:
            voice.get("stages", {}).pop(s, None)
            voice.get("stage_notes", {}).pop(s, None)
        out["voice"] = voice
    else:
        out["voice"] = {"turns": 0, "note": "no voice turns recorded recently"}
    return out


_DIAGNOSTIC_DISPATCH = {
    "health": _diag_health,
    "models": _diag_models,
    "voice_latency": _diag_voice_latency,
    "conversation_spend": _diag_conversation_spend,
    "conversation_performance": _diag_conversation_performance,
}
assert set(_DIAGNOSTIC_DISPATCH) == set(DIAGNOSTIC_NAMES)  # the two allowlists can't drift


async def dispatch_diagnostic(name, cfg: dict) -> dict:
    """The refusal gate shared by both surfaces: anything not in
    `_DIAGNOSTIC_DISPATCH` - including a name that LOOKS like a path, a
    query, or a sensitive surface (transcripts, message content,
    credentials, arbitrary logs, any mutation endpoint) - is refused here,
    never looked up, never partially handled."""
    fn = _DIAGNOSTIC_DISPATCH.get(name)
    if fn is None:
        return {
            "refused": True,
            "error": (f"{name!r} is not a recognized diagnostic - choose one "
                      f"of: {', '.join(DIAGNOSTIC_NAMES)}"),
        }
    return await fn(cfg)


def voice_latency_summary(window_hours: float = 24.0) -> dict:
    """Stage-level p50/p95/max voice latency over the last `window_hours`,
    segmented by model and TTS provider - recent-turn count and numeric
    percentiles only. Content-free by construction, not just by convention:
    `voice_trace.aggregate` only ever sees rows written through
    `voice_trace.sanitize_stage`'s closed allowlist (an allowlisted stage
    name, a numeric duration, and bounded provider/model/tts/speaker
    identifiers) - there is no transcript column in the table for this
    function to leak.

    Epoch-aware: rows measured under OLDER stage semantics (the earlier clocks
    read ~2s fast) are excluded rather than blended in, because a mixed window
    can make a fix look like a regression: the seats read the blend and report
    that latency "got worse" on the very release that improved it.
    `samples_excluded_prior_epoch` says what was left out, and `stage_notes`
    ships each stage's one-line interpretation with the numbers."""
    window_start = db.now() - max(window_hours, 0) * 3600 if window_hours else None
    con = db.connect()
    try:
        epoch = voice_trace.ensure_epoch(con)
        since = max(window_start, epoch) if window_start is not None else epoch
        rows = db.get_voice_traces(con, since=since)
        excluded = 0
        if window_start is not None and window_start < epoch:
            excluded = con.execute(
                "SELECT COUNT(*) FROM voice_turn_traces "
                "WHERE created_at >= ? AND created_at < ?",
                (window_start, epoch)).fetchone()[0]
    finally:
        con.close()
    summary = voice_trace.aggregate(rows)
    summary["window_hours"] = window_hours
    summary["samples"] = len(rows)
    summary["measurement_epoch"] = epoch
    summary["samples_excluded_prior_epoch"] = excluded
    # unconditional so the payload's key set never varies with the data
    summary["epoch_note"] = (
        "rows older than measurement_epoch were measured with different "
        "stage clocks (e.g. speech_end is now backdated by the end-of-turn "
        "pause) and are excluded - their numbers are not comparable; "
        "samples_excluded_prior_epoch counts them")
    summary["stage_notes"] = {s: voice_trace.STAGE_NOTES[s]
                              for s in summary.get("stages", {})
                              if s in voice_trace.STAGE_NOTES}
    return summary
