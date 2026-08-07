"""External event ingestion - the generic inbound half of the tooling
side-channel (slash commands go out, notices and events come in).

Any local producer (a parcel-tracking watcher, a calendar watcher, the deploy
tooling) can POST an event into a chat. Crossband stays payload-agnostic: it
renders the payload as text, badges the source, and assigns no meaning. That
contract is the firewall that keeps producer logic out of this repo.

Trust model: loopback is the primary boundary, like every /api/* route.
MMC_INGEST_TOKEN (optional, default off) adds a bearer check for producers
arriving via the tailnet proxy - mirroring Membro's MEMORY_AUTH_TOKEN. This
is the app's first optional auth surface; it guards exactly one route.

The speaker is stored namespaced (`ext:<source>`) - speaker flows into the
transcript the models read, so an un-namespaced producer calling itself
"claude" or "user" could impersonate a participant. The prefix makes that
structurally impossible without Crossband ever holding a producer whitelist.
"""

import re

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel, Field

from .. import db

router = APIRouter(tags=["ingest"])

SOURCE_RE = re.compile(r"^[a-z0-9][a-z0-9_-]{0,31}$")


class IngestPayload(BaseModel):
    title: str = Field(min_length=1, max_length=200)
    body: str = Field(default="", max_length=8000)
    url: str | None = Field(default=None, max_length=500)


class IngestIn(BaseModel):
    source: str
    target_chat: int
    dedupe_key: str = Field(min_length=1, max_length=200)
    priority: str = "normal"  # "normal" | "high" - rendering hint only
    payload: IngestPayload


@router.post("/api/ingest")
async def ingest(body: IngestIn, request: Request):
    token = getattr(request.app.state.settings, "ingest_token", "") or ""
    if token:
        got = (request.headers.get("authorization") or "").removeprefix("Bearer ").strip()
        import hmac
        if not hmac.compare_digest(got, token):
            raise HTTPException(401, "ingest token required")
    if not SOURCE_RE.match(body.source):
        raise HTTPException(400, "source must be short lowercase [a-z0-9_-]")
    if body.priority not in ("normal", "high"):
        raise HTTPException(400, "priority must be normal|high")

    con = db.connect()
    if not con.execute("SELECT 1 FROM chats WHERE id=?", (body.target_chat,)).fetchone():
        con.close()
        raise HTTPException(404, "no such chat")

    # idempotency: same (source, dedupe_key) never lands twice
    cur = con.execute(
        "INSERT OR IGNORE INTO inbound_events(source, dedupe_key, chat_id, created_at) "
        "VALUES(?,?,?,?)",
        (body.source, body.dedupe_key, body.target_chat, db.now()))
    if cur.rowcount == 0:
        row = con.execute(
            "SELECT message_id FROM inbound_events WHERE source=? AND dedupe_key=?",
            (body.source, body.dedupe_key)).fetchone()
        con.close()
        return {"deduped": True, "message_id": row["message_id"]}

    mark = "❗ " if body.priority == "high" else ""
    text = f"{mark}**{body.payload.title.strip()}**"
    if body.payload.body.strip():
        text += "\n" + body.payload.body.strip()
    if body.payload.url:
        text += f"\n{body.payload.url.strip()}"
    # The centralized insert path - wakes any connected client via
    # the global events bus (this route is async def for exactly that reason;
    # see backend/events.py + db.insert_message's docstrings).
    msg = db.insert_message(con, body.target_chat, f"ext:{body.source}", text[:8500])
    con.execute("UPDATE inbound_events SET message_id=? WHERE source=? AND dedupe_key=?",
                (msg["id"], body.source, body.dedupe_key))
    con.commit()
    con.close()
    return {"deduped": False, "message_id": msg["id"]}
