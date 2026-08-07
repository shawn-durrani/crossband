"""Unified, read-only integration status.

GET /api/integrations - one aggregated view of every capability the app can use
(LLM providers, audio, web search/research, MCP servers, memory), each reported
with a stable id, kind, configured/valid/enabled/available state, a normalized
health word + plain-English detail, related model seats for LLMs, and each
seat's real cost provenance + onboarding lifecycle.

Additive and purely observational: it reads existing state (env vars, the setup
wizard's session validity, participants rows, the memory /health probe, the MCP
manager's connect-time status) and changes nothing about how chats run. It never
writes credentials and never touches the participant schema.

  ?probe=true - additionally run each configured capability's OWN live check
                (the same ones the setup wizard uses) and force a fresh memory
                probe. Off by default so a dashboard load is cheap and makes no
                external calls. A failed probe shows unhealthy, never a crash.
"""

from fastapi import APIRouter, Request

from .. import capabilities, db, integrations

router = APIRouter(prefix="/api/integrations", tags=["integrations"])


@router.get("")
async def list_integrations(request: Request, probe: bool = False):
    con = db.connect()
    try:
        participants = db.get_participants(con)
    finally:
        con.close()
    settings = getattr(request.app.state, "settings", None)
    pricing = getattr(settings, "pricing", None) if settings else None
    # The room/ability capabilities read config, not credentials: which
    # repos a guest may open, which GitHub repos the models may touch, whether
    # ingest requires a token.
    cfg = settings.as_cfg() if settings is not None else {}
    entries = await integrations.collect(
        participants=participants,
        memory=request.app.state.memory,
        mcp=getattr(request.app.state, "mcp", None),
        probe=probe,
        pricing=pricing,
        cfg=cfg,
    )
    # Sections ride along so the UI renders whatever the registry defines,
    # rather than keeping its own hand-written list of kinds.
    return {"integrations": entries, "sections": capabilities.sections()}
