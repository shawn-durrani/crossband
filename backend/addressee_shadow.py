"""Who each spoken turn is for, asked of Jev and kept, never acted on (#650).

The trial in #648 measured, on made-up turns, whether one quick question
could say who a turn is for, whether the AIs owe it a reply and which seat
it names. This asks the same questions of real spoken turns, in the
background, and keeps Jev's answers beside the turn so a report can set
them against what the seats did (scripts/addressee_shadow_report.py).
Nothing here changes a round: the send never waits on it, and no answer
reaches a seat.

Off unless `addressee_shadow` is on and TYPESAFE_API_KEY is set. Spoken
turns only. What goes to TypeSafe: the turn, the last three messages before
it, the seats' names and the names of the people in the room. What's kept:
one addressee_shadow row with the answers, the kind of speaker, how many
others were present and the time Jev took, never any words. Logging is
content-free.
"""

import asyncio
import logging
import time

from . import addressee, config, db, jev

log = logging.getLogger("crossband.addressee_shadow")

TIMEOUT_S = 3.0
RECENT = 3
MAX_TEXT_CHARS = 600
MAX_RECENT_CHARS = 300
# A turn's voice label can land a moment after the save; wait this long for
# it before asking, so the speaker is the one the room would credit.
LABEL_WAIT_S = 2.0

_TASKS: set = set()


def enabled(cfg) -> bool:
    return bool(cfg.get("addressee_shadow")) and bool(jev.api_key())


def schedule(chat_id, message, cfg):
    """Ask about one saved user turn in the background and return at once.
    Typed turns, empty ones and `/` messages are skipped."""
    text = (message.get("content") or "").strip()
    if not enabled(cfg) or not message.get("voice_turn_id") \
            or not text or text.startswith("/"):
        return None
    task = asyncio.get_running_loop().create_task(
        shadow_turn(chat_id, message["id"], cfg))
    _TASKS.add(task)
    task.add_done_callback(_TASKS.discard)
    return task


def _speaker(con, chat_id, message_id, cfg):
    """(name for the description, kind for the row). The kind is owner,
    guest, unknown or tv, so the row names nobody."""
    from . import depth, voice_ask
    owner = cfg.get("user_name") or "User"
    if voice_ask.said_by_media(message_id):
        return "tv", "tv"
    name = depth.resolve_speaker(con, chat_id, message_id, cfg)
    if not name:
        return "unknown", "unknown"
    if name == owner:
        return owner, "owner"
    return name, "guest"


def _context(chat_id, message_id, cfg):
    """Everything the description needs, read in one go (worker thread).
    None when the message is gone."""
    from . import depth
    owner = cfg.get("user_name") or "User"
    con = db.connect()
    try:
        row = con.execute("SELECT * FROM messages WHERE id=? AND chat_id=?",
                          (message_id, chat_id)).fetchone()
        if not row:
            return None
        seats = {p["slug"]: p["name"] for p in db.get_chat_participants(con, chat_id)}
        room = [r["name"] for r in db.get_room_roster(con, chat_id, present_only=True)]
        who, kind = _speaker(con, chat_id, message_id, cfg)
        recent = []
        for r in reversed(con.execute(
                "SELECT * FROM messages WHERE chat_id=? AND id<? "
                "AND speaker!='system' ORDER BY id DESC LIMIT ?",
                (chat_id, message_id, RECENT)).fetchall()):
            msg = db.heard_view(dict(r))
            if msg["speaker"] == "user":
                said_by = depth.resolve_speaker(con, chat_id, msg["id"], cfg) or "unknown"
            else:
                said_by = seats.get(msg["speaker"], msg["speaker"])
            recent.append({"who": said_by,
                           "text": (msg.get("content") or "")[:MAX_RECENT_CHARS]})
    finally:
        con.close()
    others = [p for p in room if p.casefold() != owner.casefold()]
    return {"owner": owner, "seats": list(seats.values()), "room": room,
            "recent": recent, "who": who, "kind": kind, "others": len(others),
            "text": (row["content"] or "")[:MAX_TEXT_CHARS]}


def _store(chat_id, message_id, ctx, verdict, got, cfg):
    """The row, and one utility_usage row for the call (worker thread).
    Unpriced until Jev is on the rate card: recorded as not tracked."""
    usage = got.get("usage") or {}
    tokens = {"input": int(usage.get("input_tokens") or 0),
              "output": int(usage.get("output_tokens") or 0)}
    pricing = cfg.get("pricing") or config.DEFAULT_PRICING
    con = db.connect()
    try:
        con.execute(
            "INSERT OR REPLACE INTO addressee_shadow(message_id, chat_id, created_at, "
            "said_for, asks, seat, confidence, speaker, others, ms, model, error) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
            (message_id, chat_id, time.time(), verdict["for"], verdict["asks"],
             verdict["seat"], _confidence(got), ctx["kind"], ctx["others"],
             int(got.get("ms") or 0), got.get("model") or "", got.get("error") or ""))
        if not got.get("error"):
            db.log_utility_usage(
                con, chat_id, "addressee_shadow", got["model"], tokens["input"],
                tokens["output"], config.compute_cost(got["model"], tokens, pricing),
                provenance=config.provenance_for(got["model"], pricing)["source"])
        con.commit()
    finally:
        con.close()


def _confidence(got):
    try:
        return float(got["answers"]["for"]["confidence"])
    except (KeyError, TypeError, ValueError):
        return None


async def shadow_turn(chat_id, message_id, cfg, client=None):
    """The whole shadow for one turn. Never raises."""
    try:
        from .introductions import await_turn_audio
        await await_turn_audio(message_id, timeout=LABEL_WAIT_S)
        ctx = await asyncio.to_thread(_context, chat_id, message_id, cfg)
        if ctx is None:
            return None
        state = addressee.describe(ctx["owner"], ctx["seats"], ctx["room"],
                                   ctx["recent"], ctx["who"], ctx["text"])
        got = await jev.ask(state, addressee.jev_questions(ctx["seats"]),
                            timeout_s=TIMEOUT_S, client=client)
        verdict = addressee.parse_jev(got["answers"], ctx["seats"])
        await asyncio.to_thread(_store, chat_id, message_id, ctx, verdict, got, cfg)
        log.info("addressee shadow: chat=%s msg=%s for=%s asks=%s seat_named=%s "
                 "speaker=%s others=%s ms=%s%s", chat_id, message_id,
                 verdict["for"] or "none", verdict["asks"] or "none",
                 verdict["seat"] not in ("", "all", "none"), ctx["kind"],
                 ctx["others"], got["ms"],
                 f" error={got['error']}" if got.get("error") else "")
        return verdict
    except Exception as e:
        log.info("addressee shadow failed: chat=%s msg=%s %s", chat_id, message_id,
                 type(e).__name__)
        log.debug("addressee shadow failure", exc_info=True)
        return None
