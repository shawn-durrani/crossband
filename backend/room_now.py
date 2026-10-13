"""Who's in the room, from the voices actually heard (#669, #670).

Room mode is a switch, on, off or solo, and today it decides how a spoken
turn with no name on it is credited: the owner's with the room off, not
yet known with it on. The redesign drops the switch and decides from who
has been heard instead. This module holds the room list that runs on, and
for now only measures it beside the mode:

- the room list is everyone still present on the room roster, plus every
  voice the open voice session has named, without the owner
- a spoken turn with no name counts as the owner's while the room list is
  empty and no "who's this?" is open, and as not yet known once either
  isn't

For each spoken turn, once its voice label has landed, both credits and
the facts behind them go in one room_shadow row, with counts and no names.
scripts/room_shadow_report.py counts where the two disagree. Nothing here
changes a round.
"""

import asyncio
import logging
import time

from . import db, run_eval

log = logging.getLogger("crossband.room_now")

LABEL_WAIT_S = 2.0
_TASKS: set = set()

# The short word a credit is kept as.
_KINDS = {run_eval.OWNER: "owner", run_eval.NOBODY: "nobody",
          run_eval.PENDING: "pending", run_eval.UNNAMED: "unnamed",
          run_eval.TV: "tv", run_eval.DOUBTED: "doubted",
          run_eval.CROSSTALK: "crosstalk", run_eval.SEVERAL: "several"}


def kind(asker) -> str:
    return _KINDS.get(asker, "guest")


def _not_owner(names, owner) -> list:
    from .introductions import owner_alias
    out = []
    for n in names:
        if n and not owner_alias(n, owner) and n not in out:
            out.append(n)
    return out


def people(con, chat_id, owner) -> dict:
    """The room list's parts: who's present on the roster, who the open
    voice session has named, and the list itself, each without the owner."""
    from . import voice_sessions
    roster = _not_owner([r["name"] for r in
                         db.get_room_roster(con, chat_id, present_only=True)], owner)
    session = _not_owner(voice_sessions.named_people(chat_id), owner)
    return {"roster": roster, "session": session,
            "room": roster + [n for n in session if n not in roster]}


def asking(con, chat_id) -> bool:
    """Is a "who's this?" open in the chat?"""
    from .voice_ask import ASK_KIND
    return any(f.get("kind") == ASK_KIND
               for f in db.get_room_flags(con, chat_id, open_only=True))


def compare(chat_id, message_id, cfg):
    """Both credits for one turn and the facts behind them (worker
    thread), or None when the turn or the chat is gone."""
    owner = cfg.get("user_name") or "User"
    con = db.connect()
    try:
        row = con.execute("SELECT id, chat_id, speaker, voice_turn_id, voice_labels "
                          "FROM messages WHERE id=? AND chat_id=?",
                          (message_id, chat_id)).fetchone()
        chat = con.execute("SELECT room_mode, ambient_off FROM chats WHERE id=?",
                           (chat_id,)).fetchone()
        if row is None or chat is None:
            return None
        msg = dict(row)
        room = people(con, chat_id, owner)
        open_ask = asking(con, chat_id)
        flagged = frozenset(f["message_id"] for f in
                            db.get_room_flags(con, chat_id, open_only=True)
                            if f.get("message_id"))
    finally:
        con.close()
    others = bool(room["room"]) or open_ask
    old = run_eval.classify(msg, owner_name=owner, room_mode=bool(chat["room_mode"]),
                            open_flag_ids=flagged)
    new = run_eval.classify(msg, owner_name=owner, room_mode=others,
                            open_flag_ids=flagged)
    return {"room_mode": int(bool(chat["room_mode"])),
            "solo": int(bool(chat["ambient_off"])),
            "roster": len(room["roster"]), "session_named": len(room["session"]),
            "others": len(room["room"]), "asking": int(open_ask),
            "labelled": int(bool(run_eval._labels(msg).get("labels"))),
            "old": kind(old), "new": kind(new)}


def store(chat_id, message_id, row) -> None:
    con = db.connect()
    try:
        con.execute(
            "INSERT OR REPLACE INTO room_shadow(message_id, chat_id, created_at, "
            "room_mode, solo, roster, session_named, others, asking, labelled, "
            "old, new) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
            (message_id, chat_id, time.time(), row["room_mode"], row["solo"],
             row["roster"], row["session_named"], row["others"], row["asking"],
             row["labelled"], row["old"], row["new"]))
        con.commit()
    finally:
        con.close()


async def shadow_turn(chat_id, message_id, cfg):
    """The comparison for one spoken turn, after its voice label has had
    its moment to land. Never raises."""
    try:
        from .introductions import await_turn_audio
        await await_turn_audio(message_id, timeout=LABEL_WAIT_S)
        row = await asyncio.to_thread(compare, chat_id, message_id, cfg)
        if row is None:
            return None
        await asyncio.to_thread(store, chat_id, message_id, row)
        if row["old"] != row["new"]:
            log.info("room shadow: chat=%s old=%s new=%s room_mode=%s solo=%s "
                     "others=%s", chat_id, row["old"], row["new"], row["room_mode"],
                     row["solo"], row["others"])
        return row
    except Exception as e:
        log.info("room shadow failed: chat=%s %s", chat_id, type(e).__name__)
        return None


def schedule(chat_id, message, cfg):
    """Measure one saved spoken turn in the background, and return at once."""
    if not message.get("voice_turn_id"):
        return None
    task = asyncio.get_running_loop().create_task(
        shadow_turn(chat_id, message["id"], cfg))
    _TASKS.add(task)
    task.add_done_callback(_TASKS.discard)
    return task
