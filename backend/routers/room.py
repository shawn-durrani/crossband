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

from .. import anchors, db, introductions

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
        # Learning progress (#28 phase 4): accepted anchor seconds, so a
        # still-learning voice can show how far along it is instead of a
        # bare "uncertain". Content-free numbers only, same as /voice/people.
        row["anchor_seconds"] = float((person or {}).get("seconds") or 0.0)
        # The short-clip half of the two-part bar (#28 PR-B), so learning
        # progress can say which part is still missing.
        row["anchor_short_clips"] = int((person or {}).get("short_clips") or 0)
        # The correctable display name (#28 phase 3): a remembered person is
        # shown under their preferred name; an anchor-pending person under
        # the name they were introduced as. `name` stays the identity key
        # the voice labels are written with.
        row["display_name"] = (person or {}).get("preferred_name") or row["name"]
    return {"room_mode": bool(chat["room_mode"]),
            "cap": int(request.app.state.settings.room_roster_max),
            "sufficient_seconds": anchors.SUFFICIENT_SECONDS,
            "min_short_clips": anchors.MIN_SHORT_CLIPS,
            "roster": roster, "flags": flags}


@router.get("/api/voice/people")
def get_people():
    """Remembered voices: names, clip counts, accepted seconds, sufficiency
    (both halves of the two-part bar), set-aside counts and close-pair
    hints (#28 PR-B). No audio and no transcript text ever leaves this
    endpoint."""
    return {"people": anchors.store().people(),
            "sufficient_seconds": anchors.SUFFICIENT_SECONDS,
            "min_short_clips": anchors.MIN_SHORT_CLIPS,
            "short_clip_max_seconds": anchors.SHORT_CLIP_MAX_SECONDS}


@router.get("/api/voice/health")
def voice_health(request: Request, chat_id: int | None = None):
    """The voice health strip's snapshot (#28): matcher state, remembered-
    voice counts, the chat's mode flags, and the most recent identification
    decision's path and latency. CONTENT-FREE by design - states, counts and
    milliseconds only; the names the strip shows come from the roster and
    people snapshots the caller already has, and transcript text never
    appears anywhere near this endpoint. Session-gated like every /api
    route."""
    from .. import diarize, voiceid
    cfg = request.app.state.settings.as_cfg()
    people = anchors.store().people()
    out = {
        "matcher": voiceid.matcher_status(cfg),
        "people_total": len(people),
        "people_sufficient": sum(1 for p in people if p["sufficient"]),
        "chat": None,
        "last_decision": None,
    }
    if chat_id is not None:
        con = db.connect()
        try:
            row = con.execute(
                "SELECT room_mode, ambient_off FROM chats WHERE id=?",
                (chat_id,)).fetchone()
            if not row:
                raise HTTPException(404)
            roster_count = len(db.get_room_roster(con, chat_id,
                                                  present_only=True))
        finally:
            con.close()
        out["chat"] = {"room_mode": bool(row["room_mode"]),
                       "ambient_off": bool(row["ambient_off"]),
                       "roster_count": roster_count}
        out["last_decision"] = diarize.last_decision(chat_id)
    return out


@router.post("/api/voice/people/{person_id}/name")
def rename_person(person_id: str, body: dict = Body(...)):
    """Set a remembered person's preferred display name (#28 phase 3) - the
    correctable spelling the roster chip, memory ingest, the model-facing
    projection and STT keyterms use. The introduced name stays as the
    identity key, so existing voice labels and roster rows keep matching.
    Owner action, so the name is OWNER-SET (#28: naming is law): no
    automated path may change it afterwards.

    Merge affordance (#28): renaming this person to a name that already
    belongs to ANOTHER remembered person does not rename - it returns the
    conflict ({"ok": False, "conflict": {...}}) so the UI can offer merging
    the two, which is almost always what a shared name means."""
    name = (body.get("name") or "").strip()[:anchors.MAX_PREFERRED_CHARS]
    store = anchors.store()
    if not any(p["person_id"] == person_id for p in store.people()):
        raise HTTPException(404, "no such remembered voice")
    folded = introductions.fold_name(name)
    if folded:
        for p in store.people():
            if p["person_id"] == person_id:
                continue
            known_as = [p["name"], p["preferred_name"]] + p["merged_names"]
            if any(introductions.fold_name(k) == folded for k in known_as if k):
                return {"ok": False,
                        "conflict": {"person_id": p["person_id"],
                                     "name": p["name"],
                                     "display_name": p["preferred_name"]
                                     or p["name"]}}
    if not store.set_preferred_name(person_id, name):
        raise HTTPException(400, "a preferred name needs at least one letter")
    from .. import events
    events.notify_room_update()
    log.info("voice renamed: person=%s", person_id)
    return {"ok": True}


@router.post("/api/voice/people/{person_id}/merge")
def merge_person(person_id: str, request: Request, body: dict = Body(...)):
    """Merge this remembered person into another (#28: variants merge
    instead of duplicating): banks fold together under the keep-best-N rule,
    the OLDEST person_id survives, the other is forgotten, and the survivor
    answers to both names. `body`: {"into": <person_id>, "name": <optional
    display name the owner chose>}. The chosen (or the target's) display
    name is applied OWNER-SET - the owner just said who this is. Roster rows
    pointing at the forgotten id re-point at the survivor, and any open
    merge-question flags resolve - the question has been answered."""
    other_id = (body.get("into") or "").strip()
    store = anchors.store()
    people = {p["person_id"]: p for p in store.people()}
    if person_id not in people or other_id not in people:
        raise HTTPException(404, "no such remembered voice")
    if person_id == other_id:
        raise HTTPException(400, "cannot merge a person into themselves")
    chosen = (body.get("name") or "").strip()[:anchors.MAX_PREFERRED_CHARS] \
        or people[other_id]["preferred_name"] or people[other_id]["name"]
    survivor = store.merge_people(person_id, other_id)
    if survivor is None:
        raise HTTPException(400, "merge failed")
    store.set_preferred_name(survivor, chosen)  # owner-set: they chose it
    # A merged bank is a changed bank: re-run the pairwise hygiene audit
    # (#28 PR-B). No-op when the matcher is unavailable; never raises.
    from .. import voiceid
    voiceid.audit_banks_if_changed(request.app.state.settings.as_cfg())
    gone = other_id if survivor == person_id else person_id
    merged_names = set()
    for pid in (person_id, other_id):
        p = people[pid]
        for n in [p["name"], p["preferred_name"], chosen] + p["merged_names"]:
            folded_n = introductions.fold_name(n)
            if folded_n:
                merged_names.add(folded_n)
    con = db.connect()
    try:
        con.execute(
            "UPDATE room_roster SET person_id=?, updated_at=? WHERE person_id=?",
            (survivor, db.now(), gone))
        con.commit()
        # An open merge question ABOUT either of these names is answered by
        # this merge; questions about other people stand.
        for f in con.execute(
                "SELECT * FROM room_flags WHERE kind='merge_question' "
                "AND resolved_at IS NULL").fetchall():
            if introductions.fold_name(f["label"]) in merged_names \
                    or introductions.fold_name(f["suspected"]) in merged_names:
                db.resolve_room_flags(con, f["chat_id"], flag_id=f["id"])
    finally:
        con.close()
    from .. import events
    events.notify_room_update()
    log.info("voices merged via endpoint: survivor=%s forgotten=%s",
             survivor, gone)
    return {"ok": True, "person_id": survivor}


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
def reassign_speaker(chat_id: int, message_id: int, request: Request,
                     body: dict = Body(...)):
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
        if old.get("crosstalk") is True:
            # A correction answers WHO spoke, not WHAT was lost: the turn's
            # audio still held two voices, so the crosstalk marker survives
            # (and keeps the turn quarantine-safe on memory ingest). The
            # best-effort split is dropped - its per-voice labels no longer
            # match the corrected attribution.
            payload["crosstalk"] = True
            if "overlap" in old:
                payload["overlap"] = bool(old.get("overlap"))
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
    if learned:
        # A correction clip changed the bank: re-run the pairwise hygiene
        # audit (#28 PR-B). No-op when the matcher is unavailable.
        from .. import voiceid
        voiceid.audit_banks_if_changed(request.app.state.settings.as_cfg())
    log.info("speaker corrected: chat=%s msg=%s learned=%s",
             chat_id, message_id, learned)
    return {"ok": True, "learned": learned}
