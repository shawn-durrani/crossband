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
    # Learning visibility (#28, thirteenth field test): "is it still
    # learning?" was unanswerable from the app - the numbers existed only in
    # the store file, so the question needed a maintainer with a shell. These
    # are read straight off the snapshot already loaded above: no new work,
    # nothing on the voice path, and still content-free (counts and seconds,
    # never audio, never text). `last_learned_age_s` is None when a person
    # has never banked a clip.
    now_ts = db.now()

    def _learning(p):
        last = p.get("last_clip_at") or 0
        return {
            "clips": p.get("clip_count", 0),
            "seconds": p.get("seconds", 0),
            "short_clips": p.get("short_clips", 0),
            "sufficient": bool(p.get("sufficient")),
            "at_capacity": bool(p.get("at_capacity")),
            "last_learned_age_s": (round(now_ts - last, 1)
                                   if last else None),
        }

    out = {
        "matcher": voiceid.matcher_status(cfg),
        # #154: which speaker model is live (file, hash prefix, pin state) -
        # content-free, and answerable without a shell on the box.
        "model": voiceid.model_identity(cfg),
        "people_total": len(people),
        "people_sufficient": sum(1 for p in people if p["sufficient"]),
        "learning": {p["person_id"]: _learning(p) for p in people},
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


@router.post("/api/voice/people")
def create_person(request: Request, body: dict = Body(...)):
    """Create a remembered person by name, no voice required (#90) - they
    start anchor-pending, exactly as a spoken introduction would leave
    them. Two refusals: an AI participant's name (the #77 boundary holds at
    every door, spelt-by-ear variants included), and a name that already
    belongs to someone (409 with the existing person, so the UI can offer
    them instead of minting a twin)."""
    name = introductions._clean_name(body.get("name", ""))
    if not name:
        raise HTTPException(400, "a name is required")
    con = db.connect()
    try:
        pnames = introductions._participant_names(con)
    finally:
        con.close()
    if introductions.participant_alias(name, pnames):
        raise HTTPException(400, "an AI participant's name can never be a "
                                 "person in the room")
    store = anchors.store()
    existing = store.find_by_name(name)
    if existing:
        raise HTTPException(status_code=409, detail={
            "conflict": {"person_id": existing["person_id"],
                         "display_name": existing.get("preferred_name")
                         or existing["name"]}})
    pid = store.ensure_person(name)
    from .. import events
    events.notify_room_update()
    log.info("person created by owner: person=%s", pid)
    return {"ok": True, "person_id": pid}


@router.post("/api/voice/people/{person_id}/alias")
def add_person_alias(person_id: str, body: dict = Body(...)):
    """Record another spelling for a person (#90): a transcriber's
    misspelling worth keeping, or a phonetic form beside the written one
    ("Catriona", said "Cat"). It joins their identity names - find_by_name,
    re-introductions and the STT keyterms all resolve it - without touching
    the owner-set display name. A spelling that belongs to someone else is
    refused with the conflict, so folding two people stays the merge
    endpoint's explicit, consented job."""
    name = introductions._clean_name(body.get("name", ""))
    if not name:
        raise HTTPException(400, "a name is required")
    con = db.connect()
    try:
        pnames = introductions._participant_names(con)
    finally:
        con.close()
    if introductions.participant_alias(name, pnames):
        raise HTTPException(400, "an AI participant's name can never be a "
                                 "person in the room")
    store = anchors.store()
    existing = store.find_by_name(name)
    if existing and existing["person_id"] != person_id:
        raise HTTPException(status_code=409, detail={
            "conflict": {"person_id": existing["person_id"],
                         "display_name": existing.get("preferred_name")
                         or existing["name"]}})
    if not store.add_merged_name(person_id, name):
        # already one of their names, or no such person - the former is a
        # harmless no-op, the latter a 404.
        if store.clips_of(person_id) is None:
            raise HTTPException(404, "no such remembered voice")
    from .. import events
    events.notify_room_update()
    return {"ok": True}


@router.post("/api/voice/people/{person_id}/clips/{fname}/move")
def move_person_clip(person_id: str, fname: str, body: dict = Body(...)):
    """Refile one clip under the person it actually belongs to (#90). The
    audio is untouched on disk; the index changes hands, a stale quarantine
    clears, and both banks re-derive from what remains."""
    to = str(body.get("to", "") or "")
    if not to:
        raise HTTPException(400, "'to' person_id is required")
    if to == person_id:
        raise HTTPException(400, "that clip is already theirs")
    if not anchors.store().move_clip(person_id, fname, to):
        raise HTTPException(404, "no such clip or person")
    from .. import events
    events.notify_room_update()
    log.info("clip moved by owner: from=%s to=%s", person_id, to)
    return {"ok": True}


@router.post("/api/voice/people/{person_id}/audition")
def confirm_audition(person_id: str):
    """The owner listened and confirmed this bank is who it claims (#83):
    restores the identification rights an unvouched sufficiency crossing
    withholds. No negative route - a wrong bank gets the existing tools
    (reassign clips, merge, forget)."""
    if not anchors.store().confirm_audition(person_id):
        raise HTTPException(404, "no such person")
    return {"ok": True}


@router.get("/api/voice/people/{person_id}/clips")
def get_person_clips(person_id: str):
    """The audition list (#68): every stored clip's metadata - source, when
    it was banked, seconds, quality score, whether the hygiene audit set it
    aside. Read-only; no audio in this response. Session-gated like every
    /api route: clips are the most personal thing the app stores."""
    clips = anchors.store().clips_of(person_id)
    if clips is None:
        raise HTTPException(404, "no such remembered voice")
    return {"clips": clips}


@router.get("/api/voice/people/{person_id}/clips/{fname}/audio")
def get_person_clip_audio(person_id: str, fname: str):
    """One clip's audio, for the owner's ear (#68). The file token must be
    listed in THIS person's index - a token is never trusted as a path - and
    playback is read-only with respect to anchor state. Clips are stored as
    WAV, so this is a straight file response."""
    from fastapi.responses import FileResponse
    path = anchors.store().clip_path(person_id, fname)
    if path is None:
        raise HTTPException(404, "no such clip")
    return FileResponse(path, media_type="audio/wav")


@router.delete("/api/voice/people/{person_id}/clips/{fname}")
def delete_person_clip(person_id: str, fname: str):
    """Delete ONE clip the owner heard and judged wrong (#68). The anchor's
    sufficiency and capacity recompute from what remains; deleting the last
    clip leaves the person known but unlearnt, the same state a fresh
    introduction produces."""
    if not anchors.store().delete_clip(person_id, fname):
        raise HTTPException(404, "no such clip")
    from .. import events
    events.notify_room_update()
    log.info("clip deleted by owner: person=%s", person_id)
    return {"ok": True}


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
        # Resolve an owner alias BEFORE the label is written, so the turn
        # carries the canonical owner name rather than the spelling that was
        # typed or heard (#28, tenth field test - see the anchor note below).
        # Two guards, spelling and VOICE: the live store proved spelling alone
        # is not enough (Shawn -> "Sean" is two edits, so it slipped through
        # and minted a phantom owner-double). The voice check is the reliable
        # one: if this turn's audio matches the owner's own bank confidently,
        # the speaker IS the owner whatever name was typed.
        from .. import introductions, voiceid
        cfg = request.app.state.settings.as_cfg()
        owner = cfg.get("user_name") or "User"
        if introductions.owner_alias(name, owner):
            name = owner
        elif name.strip().casefold() != owner.strip().casefold():
            owner_person = anchors.store().find_by_name(owner)
            peek = anchors.peek_audio(message_id)
            if owner_person and peek and peek[2] == 1:
                verdict = voiceid.identify_utterance(
                    peek[0], peek[1],
                    [{"person_id": owner_person["person_id"], "name": owner}],
                    cfg)
                if voiceid.matched(verdict):
                    log.info("correction resolved to the owner by voice: "
                             "chat=%s score=%.3f", chat_id, verdict["score"])
                    name = owner
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
        # `name` is already owner-resolved above. That guard is what stops a
        # correction minting a SECOND person holding byte-identical copies of
        # the owner's own clips - two perfect matches for one voice, which is
        # exactly why the owner sat on "identity pending" forever in the
        # tenth field test. owner_alias covers spelling variants of the
        # configured name, so the phantom cannot be minted by ear either.
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
