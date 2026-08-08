"""Room-mode surfaces (#28 phase 2): the roster snapshot, remembered voices,
attribution flags, and tap-to-correct.

Everything here is read/write on durable state (room_roster, room_flags, the
voice-anchor store) - nothing touches a live voice session or a round. Live
change notification rides the global events bus (room_roster / room_flag
events); these endpoints are the snapshots those events tell clients to
refetch, exactly the guest-jobs pattern.
"""

import json
import logging

from fastapi import APIRouter, Body, HTTPException, Request

from .. import anchors, db

router = APIRouter(tags=["room"])

log = logging.getLogger("crossband.room")


@router.get("/api/chats/{chat_id}/roster")
def get_roster(chat_id: int, request: Request):
    """The chip's snapshot: room mode, who is in the room (with per-person
    anchor sufficiency, so 'still learning their voice' is honest), the cap,
    and the open attribution flags."""
    con = db.connect()
    try:
        chat = con.execute("SELECT room_mode FROM chats WHERE id=?",
                           (chat_id,)).fetchone()
        if not chat:
            raise HTTPException(404)
        roster = db.get_room_roster(con, chat_id, present_only=True)
        flags = db.get_room_flags(con, chat_id, open_only=True)
    finally:
        con.close()
    by_id = {p["person_id"]: p for p in anchors.store().people()}
    for row in roster:
        person = by_id.get(row["person_id"])
        row["sufficient"] = bool(person and person["sufficient"])
    return {"room_mode": bool(chat["room_mode"]),
            "cap": int(request.app.state.settings.room_roster_max),
            "roster": roster, "flags": flags}


@router.get("/api/voice/people")
def get_people():
    """Remembered voices: names, clip counts, accepted seconds, sufficiency.
    No audio and no transcript text ever leaves this endpoint."""
    return {"people": anchors.store().people(),
            "sufficient_seconds": anchors.SUFFICIENT_SECONDS}


@router.delete("/api/voice/people/{person_id}")
def forget_person(person_id: str):
    """Forget a remembered voice: deletes the person's anchor AUDIO from disk
    and the index entry, and unlinks every roster row that pointed at them
    (those names drop back to 'anchor pending' - they can be re-learned, but
    only by being heard again)."""
    if not anchors.store().forget(person_id):
        raise HTTPException(404, "no such remembered voice")
    con = db.connect()
    try:
        cur = con.execute(
            "UPDATE room_roster SET person_id='', updated_at=? WHERE person_id=?",
            (db.now(), person_id))
        con.commit()
    finally:
        con.close()
    if cur.rowcount:
        from .. import events
        events.notify_room_update()
    log.info("voice forgotten: person=%s roster_rows_unlinked=%d",
             person_id, cur.rowcount)
    return {"ok": True}


@router.post("/api/chats/{chat_id}/flags/{flag_id}/resolve")
def resolve_flag(chat_id: int, flag_id: int):
    """Dismiss one open flag (the 'never mind' path). Corrections resolve
    their message's flags themselves."""
    con = db.connect()
    try:
        n = db.resolve_room_flags(con, chat_id, flag_id=flag_id)
    finally:
        con.close()
    if not n:
        raise HTTPException(404, "no such open flag")
    return {"ok": True}


@router.post("/api/chats/{chat_id}/messages/{message_id}/speaker")
def reassign_speaker(chat_id: int, message_id: int, body: dict = Body(...)):
    """Tap-to-correct: reassign a labelled turn to `name`. Three effects, in
    order:

    1. the turn's voice label is rewritten to the given name (through the
       single label-update path, so connected clients re-render live);
    2. every open flag on that turn resolves - the doubt has been answered;
    3. if the utterance's audio is still in the recent cache AND it was a
       single-speaker utterance, it feeds the person's anchor set as ground
       truth (source='correction'). A two-voice utterance is never fed - it
       is not clean evidence of anyone's voice - and after a restart there is
       simply no audio left to learn from; the label still corrects."""
    name = (body.get("name") or "").strip()[:40]
    if not name:
        raise HTTPException(400, "name required")
    con = db.connect()
    try:
        row = con.execute(
            "SELECT * FROM messages WHERE id=? AND chat_id=?",
            (message_id, chat_id)).fetchone()
        if not row:
            raise HTTPException(404)
        if row["speaker"] != "user":
            raise HTTPException(400, "only spoken user turns carry voice labels")
        try:
            old = json.loads(row["voice_labels"] or "{}")
        except json.JSONDecodeError:
            old = {}
        payload = {"clusters": old.get("clusters") or [],
                   "labels": [name], "uncertain": [], "corrected": True}
        db.set_message_voice_labels(con, message_id, payload)
        db.resolve_room_flags(con, chat_id, message_id=message_id)

        store = anchors.store()
        pid = store.ensure_person(name)
        learned = False
        cached = anchors.take_audio(message_id)
        if cached:
            pcm, sample_rate, n_clusters = cached
            if n_clusters == 1:
                learned = store.add_clip(pid, pcm, sample_rate,
                                         source="correction")
        # The corrected person is evidently in the room: put them on the
        # roster (or re-mark them present) and link their anchors.
        db.add_room_person(con, chat_id, name, person_id=pid)
        db.link_room_person(con, chat_id, name, pid)
    finally:
        con.close()
    log.info("speaker corrected: chat=%s msg=%s learned=%s",
             chat_id, message_id, learned)
    return {"ok": True, "learned": learned}
