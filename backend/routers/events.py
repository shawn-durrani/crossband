"""GET /api/events/stream - the global live-message push channel.

One connection per browser tab, not per chat (see backend/events.py's module
docstring for the full design/lost-wakeup rationale). Loopback-only like every
/api/* route (enforced by app.py's host-allowlist middleware, applied before
this router ever sees the request)."""

from fastapi import APIRouter
from fastapi.responses import StreamingResponse

from .. import events

router = APIRouter(tags=["events"])


@router.get("/api/events/stream")
async def events_stream(since: int = 0):
    """`since`: the highest message id the client already has (0 on first
    load). Replays everything missed - including everything inserted while
    this exact connection didn't exist, e.g. across a deploy restart - before
    subscribing live. See events.stream()'s docstring for the full contract."""
    return StreamingResponse(events.stream(since), media_type="text/event-stream")
