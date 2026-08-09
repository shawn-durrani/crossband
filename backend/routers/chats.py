import asyncio
import datetime as datetime_module
import json
import logging

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import Response, StreamingResponse
from pydantic import BaseModel

from .. import accounting, context_weight, db, diarize, engine, introductions, rounds

router = APIRouter(tags=["chats"])

log = logging.getLogger("crossband.chats")


class ChatIn(BaseModel):
    project_id: int | None = None
    title: str | None = None
    voice_mode: bool | None = None
    web_enabled: bool | None = None
    memory_enabled: bool | None = None
    code_enabled: bool | None = None  # summon_claude_code guest (opt-in)
    room_mode: bool | None = None  # multi-human room mode (#28 phase 2): the
                                   # explicit override - off, or on without an
                                   # introduction; introductions flip it on
                                   # server-side themselves
    archived: bool | None = None  # hide from the sidebar; nothing is deleted
    participant_ids: list[int] | None = None


def _chat_payload(con, chat_id):
    chat = dict(con.execute("SELECT * FROM chats WHERE id=?", (chat_id,)).fetchone())
    chat["participant_ids"] = [
        r["participant_id"] for r in con.execute(
            "SELECT participant_id FROM chat_participants WHERE chat_id=?", (chat_id,))
    ]
    chat["voice"] = db.chat_voice_totals(con, chat_id)
    return chat


def _context_estimate(chat, msgs, cfg, memory_summary_len=0):
    """Rough tokens each participant re-reads per reply, by component. It is a
    utility gauge: big contexts dilute attention long before they hit limits.

    Delegates to context_weight.estimate: this used to count message
    text only, which under-reports badly. Every image in a chat is re-sent on
    every turn, so a transcript carrying a few photos can be orders of
    magnitude heavier on the wire than its text estimate suggests. Attachments
    have to be weighed or the gauge reads comfortable on a chat that is
    already unusable.
    """
    return context_weight.estimate(chat, msgs, cfg, memory_summary_len)


@router.post("/api/chats")
def create_chat(body: ChatIn, request: Request):
    con = db.connect()
    t = db.now()
    # web research defaults ON; the code toggle follows code_default_on
    # (config) so a dev-heavy phase doesn't need per-chat flipping
    code_on = int(bool(request.app.state.settings.code_default_on))
    cur = con.execute(
        "INSERT INTO chats(project_id, title, web_enabled, code_enabled, "
        "created_at, updated_at) VALUES(?,?,1,?,?,?)",
        (body.project_id, body.title or "New chat", code_on, t, t),
    )
    chat_id = cur.lastrowid
    ids = body.participant_ids
    if ids is None:
        ids = [p["id"] for p in db.get_participants(con, enabled_only=True)]
    for pid in ids:
        con.execute("INSERT OR IGNORE INTO chat_participants(chat_id, participant_id) VALUES(?,?)",
                    (chat_id, pid))
    con.commit()
    chat = _chat_payload(con, chat_id)
    con.close()
    return chat


@router.get("/api/chats/{chat_id}")
async def get_chat(chat_id: int, request: Request):
    con = db.connect()
    row = con.execute("SELECT * FROM chats WHERE id=?", (chat_id,)).fetchone()
    if not row:
        con.close()
        raise HTTPException(404)
    chat = _chat_payload(con, chat_id)
    msgs = db.get_chat_messages(con, chat_id)
    con.close()
    memory = request.app.state.memory
    mem_len = 0
    if chat["memory_enabled"] and await memory.probe():
        mem_len = len(await memory.get_summary())
    chat["context"] = _context_estimate(chat, msgs, request.app.state.settings.as_cfg(),
                                        memory_summary_len=mem_len)
    return {"chat": chat, "messages": msgs}


@router.get("/api/chats/{chat_id}/messages")
async def get_chat_messages_after(chat_id: int, after: int = 0):
    """Incremental catch-up for ONE chat: returns only rows with
    id > `after`, same attachments/tool_events shape as GET /api/chats/{id}'s
    `messages` - what the frontend fetches when the global events stream
    (GET /api/events/stream) reports a new message for the chat currently
    open, instead of refetching the whole transcript on every live notice."""
    con = db.connect()
    if not con.execute("SELECT 1 FROM chats WHERE id=?", (chat_id,)).fetchone():
        con.close()
        raise HTTPException(404)
    msgs = db.get_messages_after(con, after, chat_id=chat_id)
    con.close()
    return {"messages": msgs}


@router.get("/api/chats/{chat_id}/guest_jobs")
async def get_chat_guest_jobs(chat_id: int):
    """Snapshot of this chat's Claude Code guest jobs - the status the
    chip seeds from on open (running / completed / failed / cancelled + which
    completion path). Live changes then arrive over GET /api/events/stream, the
    same channel messages use. Newest first; durable across reconnects."""
    con = db.connect()
    if not con.execute("SELECT 1 FROM chats WHERE id=?", (chat_id,)).fetchone():
        con.close()
        raise HTTPException(404)
    jobs = db.get_chat_guest_jobs(con, chat_id)
    con.close()
    return {"guest_jobs": jobs}


@router.patch("/api/chats/{chat_id}")
def update_chat(chat_id: int, body: ChatIn):
    con = db.connect()
    if not con.execute("SELECT 1 FROM chats WHERE id=?", (chat_id,)).fetchone():
        con.close()
        raise HTTPException(404)
    updates = {}
    if "project_id" in body.model_fields_set:
        updates["project_id"] = body.project_id  # explicit, incl. null → move to standalone
    if body.title is not None:
        updates["title"] = body.title
        updates["title_upto"] = -1  # user named it - stop auto-titling this chat
    if body.voice_mode is not None:
        updates["voice_mode"] = int(body.voice_mode)
    if body.web_enabled is not None:
        updates["web_enabled"] = int(body.web_enabled)
    if body.memory_enabled is not None:
        updates["memory_enabled"] = int(body.memory_enabled)
    if body.code_enabled is not None:
        updates["code_enabled"] = int(body.code_enabled)
    if body.room_mode is not None:
        updates["room_mode"] = int(body.room_mode)
        if body.room_mode:
            # An explicit manual enable clears any sacred ambient-off (#28):
            # the owner turning room mode on is a re-enable.
            updates["ambient_off"] = 0
    if body.archived is not None:
        updates["archived_at"] = db.now() if body.archived else None
    if updates:
        sets = ", ".join(f"{k}=?" for k in updates)
        con.execute(f"UPDATE chats SET {sets} WHERE id=?", (*updates.values(), chat_id))
    if body.participant_ids is not None:
        if not body.participant_ids:
            con.close()
            raise HTTPException(400, "A chat needs at least one participant")
        con.execute("DELETE FROM chat_participants WHERE chat_id=?", (chat_id,))
        for pid in body.participant_ids:
            con.execute("INSERT OR IGNORE INTO chat_participants(chat_id, participant_id) VALUES(?,?)",
                        (chat_id, pid))
    con.commit()
    if body.room_mode is not None:
        # Mirror AFTER the durable write committed: the STT relay reads this
        # dict at commit boundaries (no I/O on the audio path), and the DB
        # row above is what re-seeds it on restart or session open.
        diarize.set_room_enabled(chat_id, bool(body.room_mode))
        if body.room_mode:
            diarize.set_ambient_off(chat_id, False)
    chat = _chat_payload(con, chat_id)
    con.close()
    return chat


@router.delete("/api/chats/{chat_id}")
def delete_chat(chat_id: int):
    con = db.connect()
    con.execute("DELETE FROM chats WHERE id=?", (chat_id,))
    con.commit()
    con.close()
    return {"ok": True}


class NoticeIn(BaseModel):
    text: str


@router.post("/api/chats/{chat_id}/notice")
async def post_notice(chat_id: int, body: NoticeIn):
    """Tooling notices - the mirror image of slash commands: machine-side
    tooling on this computer (deploy watchers, schedulers, anything local)
    reports a status line INTO a chat. Persists as speaker='system', never
    runs a round, and the models see it next round (it's ground truth).
    Core assigns no meaning to the content. Loopback-only like all /api/*.

    This is the exact out-of-band path the global events bus was built for: it
    went straight to the database with no way to wake an already-connected
    client. db.insert_message() now wakes one via that bus, same as any normal
    round's messages.

    async def, not def (this matters): db.insert_message()'s notify() call
    must run ON the event loop - a plain `def` route runs in FastAPI's
    threadpool instead, where touching the events bus's asyncio primitives
    from a different thread would be unsafe. See backend/events.py."""
    text = body.text.strip()
    if not text:
        raise HTTPException(400, "Empty notice")
    con = db.connect()
    if not con.execute("SELECT 1 FROM chats WHERE id=?", (chat_id,)).fetchone():
        con.close()
        raise HTTPException(404)
    msg = db.insert_message(con, chat_id, "system", text[:2000])
    con.close()
    return msg


# ---------- conversation (SSE) ----------

class SendIn(BaseModel):
    text: str = ""
    attachment_ids: list[int] = []
    # The client's own voice-trace correlation id (frontend/src/
    # voiceTrace.js), present only for a live voice turn - lets the server
    # attribute its own context-assembly/provider-TTFT split to the SAME turn
    # the browser is timing. None for a normal text send: no server-side
    # split is recorded then. Bounded/opaque - never rendered, never
    # interpreted as anything but a correlation key.
    turn_id: str | None = None


@router.post("/api/chats/{chat_id}/send")
async def send_message(chat_id: int, body: SendIn, request: Request):
    # Bound/clean the client-supplied correlation id the same way the
    # voice-trace ingest path does (backend/voice_trace._clean_str) - it's
    # only ever used as an opaque DB key, never rendered or interpreted.
    # Cleaned BEFORE the persist: it is stored on the message row so the
    # diarization pass can label the EXACT turn it heard (#28 phase 3).
    turn_id = (body.turn_id or "").strip()[:64] or None

    # The user-message persist (INSERT + fsync + placeholder-title
    # update) runs on a worker thread - this route shares the event loop
    # with live voice websocket relays, and a blocked fsync here is heard
    # as a stutter there. Same logic, same errors, off the loop.
    def _persist_user_message():
        con = db.connect()
        try:
            chat = con.execute("SELECT * FROM chats WHERE id=?", (chat_id,)).fetchone()
            if not chat:
                raise HTTPException(404)
            if not body.text.strip() and not body.attachment_ids:
                raise HTTPException(400, "Empty message")
            if chat["title"] == "New chat" and body.text.strip():
                title = body.text.strip().splitlines()[0][:60]
                con.execute("UPDATE chats SET title=? WHERE id=?", (title, chat_id))
            user_msg = db.insert_message(con, chat_id, "user", body.text,
                                         attachment_ids=body.attachment_ids,
                                         voice_turn_id=turn_id or "")
            roster = db.get_chat_participants(con, chat_id)
            return chat, user_msg, roster
        finally:
            con.close()

    chat, user_msg, roster = await asyncio.to_thread(_persist_user_message)

    # Slash command: a message starting with "/" is addressed to tooling, not
    # to the participants - persisted (the record stays complete), but no
    # round runs and no model replies. Deliberately allowed while a round is
    # streaming (it's a side-channel note, not a barge-in), so no Round is
    # registered and the active round's re-attach buffer is untouched.
    if body.text.lstrip().startswith("/"):
        async def slash_gen():
            yield engine.sse({"type": "user_saved", "message": user_msg})
            yield engine.sse({"type": "done"})
        return StreamingResponse(slash_gen(), media_type="text/event-stream")

    # Introduction detection (#28 phase 2): a cheap lexical prefilter decides
    # whether this turn is worth one utility-model confirmation; a hit runs as
    # a fire-and-forget task AFTER the user message is durable. schedule_scan
    # returns immediately and NOTHING below awaits it - the round dispatches
    # exactly as if this line did not exist, and a failure to even schedule
    # must not break the send.
    try:
        introductions.schedule_scan(chat_id, user_msg["id"], body.text,
                                    request.app.state.settings.as_cfg())
    except Exception:
        log.warning("introduction scan scheduling failed; send continues",
                    exc_info=True)

    responders, next_first = engine.pick_responders(body.text, dict(chat), roster)
    settings = request.app.state.settings
    memory = request.app.state.memory
    if rounds.active(chat_id):
        raise HTTPException(409, "a round is already running - abort it first")

    async def gen():
        yield engine.sse({"type": "user_saved", "message": user_msg})
        # run_turn, not run_round: if this round ends on a Claude Code guest
        # turn, the roster automatically gets a follow-up round to react to
        # it, so the chat resumes on its own instead of parking after the guest.
        async for chunk in engine.run_turn(chat_id, responders, next_first,
                                           settings, memory,
                                           mcp=request.app.state.mcp,
                                           turn_id=turn_id):
            yield chunk

    # Detached: the round runs to completion whether or not this response
    # survives (a dead zone must not kill the reply) - we merely tail it.
    return StreamingResponse(_tail_new_round(chat_id, gen()),
                             media_type="text/event-stream")


def _tail_new_round(chat_id, gen):
    r = rounds.start(chat_id, gen)

    async def tail():
        # round_start carries the id the client needs to re-attach after a drop
        yield engine.sse({"type": "round_start", "round_id": r.round_id})
        async for chunk in r.tail(0):
            yield chunk

    return tail()


@router.get("/api/chats/{chat_id}/round/stream")
async def round_stream(chat_id: int, round_id: int, after: int = 0):
    """Re-attach to a round after a connection drop: replay buffered events
    from index `after`, then keep streaming live. 404 when that round is gone
    (the client falls back to refetching the chat - replies persisted anyway)."""
    r = rounds.get(chat_id, round_id)
    if not r:
        raise HTTPException(404, "no such round")

    async def tail():
        async for chunk in r.tail(max(0, after)):
            yield chunk

    return StreamingResponse(tail(), media_type="text/event-stream")


@router.get("/api/chats/{chat_id}/active_round")
async def active_round(chat_id: int):
    """The chat's currently-generating detached round, if any - its id plus how
    many SSE events have buffered so far. A VOICE-active client that didn't start
    the round (a Claude Code hand-back narration, or a round that began while the
    tab was backgrounded) reads this to attach and TAIL that round, so its
    narrator is actually spoken. Without it those rounds reach the client
    only as persisted messages over the global events stream - which never
    touches the voice pipeline, so text arrives and audio never does. Returns
    {"round_id": null} when the chat is idle."""
    r = rounds.active(chat_id)
    if not r:
        return {"round_id": None}
    return {"round_id": r.round_id, "count": len(r.events)}


@router.post("/api/chats/{chat_id}/round/abort")
async def round_abort(chat_id: int):
    """The deliberate stop (barge-in / Stop button). Network drops no longer
    cancel generation, so cutting the models off is an explicit act; the
    in-flight reply persists with its cut-off marker."""
    return {"aborted": await rounds.abort(chat_id)}


class ContinueIn(BaseModel):
    rounds: int = 1


@router.post("/api/chats/{chat_id}/continue")
async def continue_round(chat_id: int, request: Request, body: ContinueIn = ContinueIn()):
    """Model-to-model rounds without the user. rounds > 1 lets them collaborate
    autonomously; abort any time (Stop button / barge-in) and the in-flight
    reply persists with a cut-off marker."""
    n_rounds = max(1, min(body.rounds, 10))
    con = db.connect()
    if not con.execute("SELECT 1 FROM chats WHERE id=?", (chat_id,)).fetchone():
        con.close()
        raise HTTPException(404)
    con.close()
    settings = request.app.state.settings
    memory = request.app.state.memory
    if rounds.active(chat_id):
        raise HTTPException(409, "a round is already running - abort it first")

    async def gen():
        for n in range(1, n_rounds + 1):
            con = db.connect()
            chat = con.execute("SELECT * FROM chats WHERE id=?", (chat_id,)).fetchone()
            roster = db.get_chat_participants(con, chat_id) if chat else []
            con.close()
            if not chat or not roster:
                break
            if n_rounds > 1:
                yield engine.sse({"type": "round", "n": n, "total": n_rounds})
            responders, next_first = engine.pick_responders("", dict(chat), roster)
            async for chunk in engine.run_turn(chat_id, responders, next_first,
                                               settings, memory,
                                               mcp=request.app.state.mcp):
                yield chunk

    return StreamingResponse(_tail_new_round(chat_id, gen()),
                             media_type="text/event-stream")


# ---------- leave hook ----------

@router.post("/api/chats/{chat_id}/distill")
async def distill(chat_id: int, request: Request):
    """Reflection pass when leaving a chat, as fire-and-forget background work:
    chat-side rolling summary / auto-title / project distill, plus the memory
    service handoff (ingest new messages, then service-side distill). Status is
    surfaced in /api/state; a failure is reported to the models next round."""
    con = db.connect()
    exists = con.execute("SELECT 1 FROM chats WHERE id=?", (chat_id,)).fetchone()
    con.close()
    if not exists:
        return {"ok": False, "reason": "no such chat"}
    engine.spawn(engine.leave_chat_job(
        chat_id, request.app.state.settings.as_cfg(), request.app.state.memory))
    return {"ok": True, "queued": True}


# ---------- export & usage ----------

def _blank_chat_usage(messages=0):
    return {"messages": messages, "tokens": 0, "not_tracked": False,
            **{c: 0.0 for c in accounting.CATEGORIES}}


@router.get("/api/usage/chats")
def chats_usage():
    """Per-chat totals for the export picker: messages, tokens, and cost split
    across the three provenance buckets.

    There is deliberately no single `cost` field. This endpoint used to add
    every `usage_json.cost` plus voice into one number, which presented a Claude
    Code guest turn covered by a subscription as money the user was charged.
    That is the same defect the chat header's running total fixed one layer up,
    and one the browser cannot undo once the buckets are collapsed here. Built on backend/accounting so the
    picker, the chat header and the Spend page all classify a dollar the same
    way; that also means voice and utility spend are now counted consistently
    with the Spend page's own per-chat rows, where the hand-rolled query it
    replaces counted voice but silently dropped utility work."""
    con = db.connect()
    counts = {r["chat_id"]: r["c"] for r in con.execute(
        "SELECT chat_id, COUNT(*) c FROM messages GROUP BY chat_id")}
    events = list(accounting.iter_cost_events(con))
    con.close()

    out = {cid: _blank_chat_usage(n) for cid, n in counts.items()}
    for g in accounting.summarize(events)["by_chat"]:
        row = out.setdefault(int(g["key"]), _blank_chat_usage())
        row["tokens"] = g["tokens"]
        row["not_tracked"] = g["not_tracked"]
        for cat in accounting.CATEGORIES:
            row[cat] = g[cat]
    return out


@router.get("/api/usage/summary")
def usage_summary(request: Request, window: str = "all"):
    """Cumulative spend across ALL chats over time. Headline totals for
    today / last 7 days / all time, a 30-day daily trend, and - for the
    selected `window` - breakdowns by source, provider, model and chat.

    Provenance is kept honest: metered spend, subscription-equivalent usage and
    unknown/unverified are reported separately and never collapsed. Shares
    backend/accounting with every other money-showing surface (the chat header
    and the export picker), so none of them can diverge.
    Read-only; touches no provider."""
    cfg = request.app.state.settings.as_cfg()
    now_ts = datetime_module.datetime.now().timestamp()
    midnight = datetime_module.datetime.now().replace(
        hour=0, minute=0, second=0, microsecond=0).timestamp()
    # Selectable spans, in DAYS. The trend, the breakdown and the
    # previous-period comparison all follow the chosen one - asking "how am I
    # doing over 90 days" and being shown a 30-day chart is the confusion this
    # replaces.
    SPANS = {"today": 1, "7d": 7, "30d": 30, "90d": 90, "all": None}
    if window not in SPANS:
        window = "all"
    span_days = SPANS[window]
    start = None if span_days is None else midnight - (span_days - 1) * 86400
    # The period of the SAME LENGTH immediately before it, so "up or down" has
    # something honest to be measured against. "all" has no before, so it gets
    # no direction rather than a fabricated one.
    prev_start = None if start is None else start - span_days * 86400
    prev_end = start

    week_start = midnight - 6 * 86400          # last 7 days, today inclusive
    series_start = start if start is not None else None
    windows = {
        "today": (midnight, None),
        "7d": (week_start, None),
        "all": (None, None),
    }

    con = db.connect()
    events = list(accounting.iter_cost_events(con))
    titles = {r["id"]: r["title"] for r in con.execute("SELECT id, title FROM chats")}
    con.close()

    headline = {
        name: accounting.summarize(events, since=lo, until=hi)
        for name, (lo, hi) in windows.items()
    }
    breakdown = accounting.summarize(events, since=start, until=None,
                                     chat_titles=titles)
    previous = (accounting.summarize(events, since=prev_start, until=prev_end)
                if prev_start is not None else None)

    return {
        "generated_at": now_ts,
        "window": window,
        "auth": accounting.auth_provenance(cfg),
        "windows": {name: {"totals": h["totals"], "tokens": h["tokens"],
                           "events": h["events"], "not_tracked": h["not_tracked"]}
                    for name, h in headline.items()},
        "series": accounting.daily_series(events, since=series_start),
        # The previous period's daily series, so the trend can show THIS period
        # against the last one at a glance rather than as two numbers to compare
        # in your head.
        "previous_series": (accounting.daily_series(events, since=prev_start,
                                                    until=prev_end)
                            if prev_start is not None else []),
        # The same-length period immediately before the selected one (null for
        # "all", which has no before). Direction is computed by the client from
        # this, so the comparison is always stated rather than implied.
        "previous": ({"totals": previous["totals"], "tokens": previous["tokens"],
                      "events": previous["events"]} if previous else None),
        "span_days": span_days,
        "breakdown": breakdown,
    }


@router.get("/api/export")
def export_chats(ids: str, request: Request, format: str = "md"):
    """Export selected chats as one markdown file (paste anywhere) or JSON."""
    try:
        id_list = [int(x) for x in ids.split(",") if x.strip()]
    except ValueError:
        raise HTTPException(400, "ids must be comma-separated chat ids")
    if not id_list:
        raise HTTPException(400, "No chats selected")
    cfg = request.app.state.settings.as_cfg()
    con = db.connect()
    names = db.participant_names(con)
    label = lambda s: cfg["user_name"] if s == "user" else names.get(s, s)
    day = lambda ts: datetime_module.date.fromtimestamp(ts).isoformat()
    chats_out = []
    for cid in id_list:
        chat = con.execute("SELECT * FROM chats WHERE id=?", (cid,)).fetchone()
        if not chat:
            continue
        msgs = db.get_chat_messages(con, cid)
        chats_out.append((dict(chat), msgs))
    con.close()
    stamp = datetime_module.date.today().isoformat()

    if format == "json":
        payload = {
            "exported_at": stamp,
            "source": "crossband",
            "chats": [{
                "title": c["title"],
                "created_at": c["created_at"],
                "messages": [{
                    "speaker": label(m["speaker"]),
                    "content": m["content"],
                    "created_at": m["created_at"],
                } for m in msgs],
            } for c, msgs in chats_out],
        }
        return Response(
            json.dumps(payload, indent=1),
            media_type="application/json",
            headers={"Content-Disposition":
                     f'attachment; filename="crossband-export-{stamp}.json"'},
        )

    lines = [f"# Crossband export: {stamp}",
             f"_{len(chats_out)} conversation(s); speakers labelled inline._", ""]
    for c, msgs in chats_out:
        lines.append(f"\n---\n\n## {c['title']}")
        lines.append(f"_{day(c['created_at'])} · {len(msgs)} messages_\n")
        for m in msgs:
            lines.append(f"**{label(m['speaker'])}:**\n{m['content']}\n")
            for t in m.get("tool_events", []):
                lines.append(f"> 🔧 {t['tool']}({t['input_json']})\n")
    return Response(
        "\n".join(lines),
        media_type="text/markdown",
        headers={"Content-Disposition":
                 f'attachment; filename="crossband-export-{stamp}.md"'},
    )
