"""The voice check (#482): one pass names every spoken turn, in every mode.

The session naming (backend/voice_sessions.py) has already followed each
voice through the voice session while the person talked, so the pass mostly
reads its answer:

  1. Ask the feed for this turn's main voice, waiting at most
     voice_sessions.LIVE_WAIT_S. When no feed saw the turn (no diariser
     is configured, it is down, or the backup transcript path carried the
     turn), the turn is named on its own as one voice, with the same
     scorer. A long turn the browser cut into pieces is named from all of
     them (voice_sessions' LONG TURNS): the feed's answer covers every
     piece, and pieces named on their own are joined (join_pieces).
  2. Decide, as a pure rule (`decide`), from that answer and the chat's
     room state: the label, and whether to arm the room, seat someone, or
     ask who a new voice is. The room follows the main voice.
  3. When a second voice spoke for a second or more, the label is
     crosstalk instead: it names every voice and splits the words between
     them on this computer (backend/crosstalk.py). Only such a turn of one
     piece waits for Scribe's word times, at most crosstalk.WORDS_WAIT_S,
     since a long turn's words cover only its last piece. They come
     with the final the browser sends the message on, and the relay hands
     them over first, so the label is normally parked before the message
     is saved and the seats read the split, not a single name that would
     pass one person's words off as another's.
  4. Arm the room and seat the named person first, so by the time the
     label is claimable the room state already agrees with it. Then
     deliver the label through the one label path, then ask about a new
     voice, pointing at the turn. The turn's audio is remembered for a tap
     on it or an introduction in it: for a long turn of one voice, the
     piece with the most clean speech (voice_sessions.turn_audio).
  5. A named single-voice turn gets the mismatch cross-check
     (backend/mismatch.py), which can flag a name the words don't fit and
     never changes a label.
  6. Save the turn's clean speech to the named person's bank when the
     naming is near certain (BANK_PROB) and the voice has BANK_MIN_CLEAN_S
     of clean speech behind it, at most BANK_PER_SESSION clips per voice
     per session, never from a turn with two voices in it. A saved clip
     re-runs the hygiene audit.

The rules that hold whatever the scores say: the owner's name is never
learnt by ear as a second person, an AI participant or an app is never
a person (an app's name leaves the roster before a turn is planned),
solo ("just me") never arms, seats, asks or learns, a turn the pass can't
name is never read as the owner's, a voice someone said is a TV is never
named, seated, asked about or learnt from, and every write goes through
the same guarded helpers (room_state.arm and seat, the one open ask per
chat, the label path, anchors.add_clip and its gate).
"""

import asyncio
import logging
import time

from . import crosstalk, db, voice_sessions as vss

log = logging.getLogger("crossband.voice_pass")

BANK_PROB = 0.99            # calibrated probability a clip is saved at
BANK_MIN_CLEAN_S = 8.0      # clean speech a voice needs before it saves
BANK_PER_SESSION = 3        # clips one session voice may save
BANK_MIN_SPAN_S = 2.0       # the shortest clean span worth saving
MISMATCH_MIN_S = 1.5        # a named turn this long gets the cross-check

LISTENING = "listening"     # the unresolved reasons this pass writes
NEW_VOICE = "new_voice"
MEDIA = vss.MEDIA           # a voice someone said is a TV or a radio


def decide(got, plan):
    """The pass's rule, pure. `got` is the naming's answer for the turn
    (None when there was nothing to name). `plan` carries the chat's state:
    owner, room_on, solo, owner_known, present (names on the roster) and
    unlearnt (present people with no voice saved yet). Returns a dict:
    labels, uncertain, owner, learning, unresolved, arm ("known",
    "unknown" or None), seat (a name or None), ask (bool)."""
    out = {"labels": [], "uncertain": [], "owner": False, "learning": False,
           "unresolved": "", "arm": None, "seat": None, "ask": False}
    if not got:
        out["unresolved"] = LISTENING
        return out
    state = got.get("state")
    name = got.get("name") or ""
    solo = plan.get("solo")
    if state == MEDIA:
        # "That's the TV" (#523): no name, no seat, no ask, and the reason
        # tells the seats it isn't a person in the room.
        out["unresolved"] = MEDIA
        return out
    if state == "named" and name:
        out["labels"] = [name]
        if plan.get("is_owner", lambda n: False)(name):
            out["owner"] = True
            return out
        if solo:
            return out
        if not plan.get("room_on"):
            out["arm"] = "known"
            out["seat"] = name
        elif name.casefold() not in {p.casefold()
                                     for p in plan.get("present") or ()}:
            out["seat"] = name
        return out
    if state == "new":
        unlearnt = plan.get("unlearnt") or []
        if plan.get("room_on") and len(unlearnt) == 1 and not solo:
            # The first meeting: one person seated with no voice saved yet,
            # and one voice nobody knows. Named as them, marked learning;
            # nothing is saved until someone confirms it.
            out["labels"] = [unlearnt[0]]
            out["uncertain"] = [unlearnt[0]]
            out["learning"] = True
            return out
        out["unresolved"] = NEW_VOICE
        if solo or not plan.get("owner_known"):
            return out
        if not plan.get("room_on"):
            out["arm"] = "unknown"
        out["ask"] = True
        return out
    out["unresolved"] = LISTENING
    return out


def should_bank(got, cfg=None):
    """Is this turn's naming sure enough to save its clean speech? Only a
    single-voice turn of a voice named by the calibrated scorer at
    BANK_PROB, or by the fallback scorer over the banking bar
    (voiceid.score_banks), with BANK_MIN_CLEAN_S behind it. A voice a
    person named by hand is saved by that correction itself, not here."""
    if not got or got.get("state") != "named" or got.get("human"):
        return False
    if (got.get("voices_in_turn") or 1) > 1 or got.get("overlap_s"):
        return False
    if (got.get("voice_clean_s") or 0.0) < BANK_MIN_CLEAN_S:
        return False
    if got.get("method") == "calibrated":
        return (got.get("prob") or 0.0) >= BANK_PROB
    from . import voiceid
    return voiceid.score_banks(got.get("score"), cfg or {})


def cross_checks(decision, plan, seconds):
    """Does a single-voice turn get the mismatch cross-check (pure)? Only
    when it's named, not marked learning, and MISMATCH_MIN_S or longer: a
    guest's name in every mode, and the owner's own name only while the
    room is on, where the owner is one of several people."""
    if not decision["labels"] or decision["learning"] \
            or decision["uncertain"]:
        return False
    if seconds < MISMATCH_MIN_S:
        return False
    return not (decision["owner"] and not plan.get("room_on"))


def _plan(chat_id, cfg):
    """The chat's room state for `decide` (worker thread)."""
    from . import anchors, introductions
    owner = (cfg.get("user_name") or "").strip()
    con = db.connect()
    try:
        row = con.execute("SELECT room_mode, ambient_off FROM chats "
                          "WHERE id=?", (chat_id,)).fetchone()
        roster = db.get_room_roster(con, chat_id, present_only=True)
    finally:
        con.close()
    people = {p["person_id"]: p for p in anchors.store().people()}
    unlearnt = [r["name"] for r in roster
                if not introductions.owner_alias(r["name"], owner)
                and not (people.get(r["person_id"] or "") or {})
                .get("sufficient")]
    from .diarize import owner_sufficient
    return {"room_on": bool(row and row["room_mode"]),
            "solo": bool(row and row["ambient_off"]),
            "present": [r["name"] for r in roster],
            "unlearnt": unlearnt,
            "owner_known": owner_sufficient(people.values(), owner),
            "is_owner": (lambda n: bool(owner)
                         and introductions.owner_alias(n, owner))}


def _arm_and_seat(chat_id, decision, cfg, got):
    """Arm and seat (worker thread), through room_state's guarded writers,
    before the label is delivered (#28 remembered-first). The turn's
    message may not exist yet, so a seat's trigger is the path,
    message-less. A remembered voice seated in an armed room answers an
    open "who's this?" ask, as a naming introduction does. Past the
    roster cap the turn is still named; the roster just doesn't grow."""
    from . import diarize, room_state
    if decision["arm"] == "known" and decision["seat"]:
        diarize._arm_known(chat_id, {"session": decision["seat"]}, cfg)
    elif decision["arm"] == "unknown":
        diarize._arm_ambient_unknown(chat_id, cfg)
    elif decision["seat"]:
        room_state.seat(chat_id, decision["seat"], cfg, via="voice-match",
                        person_id=(got or {}).get("pid") or "",
                        enforce_cap=True, resolve_ask=True)


def _bank(chat_id, got, pcm, sample_rate, cfg):
    """Save the voice's longest clean stretch in this turn (worker thread),
    once per BANK_PER_SESSION. The store's own gate, 10 s cap and rotation
    apply as for any clip, and a saved clip re-runs the hygiene audit."""
    from . import anchors, voiceid
    spans = [s for s in got.get("clean_spans") or ()
             if s[1] - s[0] >= BANK_MIN_SPAN_S]
    if not spans or not got.get("pid"):
        return False
    if not vss.take_bank_allowance(chat_id, got.get("voice"),
                                   BANK_PER_SESSION):
        return False
    start, end = max(spans, key=lambda s: s[1] - s[0])
    clip = vss.span_pcm(pcm, sample_rate, [(start, end)])
    # The score the clip is later trusted by, in its own units (#523):
    # the calibrated scorer's probability, or the fallback's cosine.
    calibrated = got.get("method") == "calibrated"
    added = anchors.store().add_clip(
        got["pid"], clip, sample_rate, source="accumulated",
        score=got.get("prob") if calibrated else got.get("score"),
        score_unit=anchors.CALIBRATED_UNIT if calibrated else None)
    if added:
        voiceid.audit_banks_if_changed(cfg)
    return added


# ================= the pass =================================================

def schedule(chat_id, pcm, sample_rate, commit_ts, session, cfg, turn_id):
    """Fire the pass for one committed turn and return at once (the relay
    never awaits it). The task joins diarize's own set, so the shutdown
    drain and the busy route see it."""
    from . import diarize
    if not pcm:
        return None
    task = asyncio.get_running_loop().create_task(
        run(chat_id, pcm, sample_rate, commit_ts, session, cfg, turn_id))
    diarize._TASKS.add(task)
    task.add_done_callback(diarize._TASKS.discard)
    return task


async def run(chat_id, pcm, sample_rate, commit_ts, session, cfg, turn_id):
    """One turn's pass. Never raises: a failure leaves the turn unlabelled
    and is logged once per occurrence, content-free."""
    from . import diarize
    t0 = time.perf_counter()
    try:
        got = await vss.await_turn(turn_id) if turn_id else None
        if got is None:
            got = await diarize._in_voice_thread(
                vss.name_single_turn, chat_id, pcm, sample_rate, cfg)
        # A long turn cut into pieces is named from all of them (#469),
        # and remembered by the piece with the most clean speech (#540).
        vss.hold_piece(turn_id, got, pcm, sample_rate)
        got = vss.join_pieces(turn_id, got, len(pcm) / 2 / (sample_rate
                                                              or 16000))
        pieces = (got or {}).get("pieces") or 1
        # #602: a seat named after an app leaves before the plan reads the
        # roster, so elimination can never name a voice after it.
        from . import room_state
        await diarize._in_voice_thread(room_state.unseat_apps, chat_id, cfg)
        plan = await diarize._in_voice_thread(_plan, chat_id, cfg)
        decision = decide(got, plan)
        listed = crosstalk.listed_voices(got)
        if len(listed) >= 2:
            # Two voices: the label names each and splits the words by the
            # tracker's spans. The words ride the same Scribe answer as the
            # final the browser is waiting for, and the relay hands them
            # over before it sends that final, so they are here (or known
            # to be missing) before /send can save the message. A turn of
            # several pieces has only its last piece's words, so it keeps
            # the two-voice note without the split.
            words = await crosstalk.await_words(turn_id) \
                if turn_id and pieces == 1 else None
            payload = crosstalk.label(got, listed, words,
                                      source=vss.SESSION_SOURCE)
        else:
            payload = diarize.label_payload(
                decision["labels"], clusters=(vss.SESSION_SOURCE,),
                uncertain=decision["uncertain"], source=vss.SESSION_SOURCE,
                score=(got or {}).get("score") if decision["labels"]
                else None,
                owner=decision["owner"], learning=decision["learning"],
                unresolved=decision["unresolved"])
        ms = (time.perf_counter() - t0) * 1000
        diarize.record_decision(
            chat_id, diarize.DECISION_LOCAL if decision["labels"]
            else diarize.DECISION_UNRESOLVED, ms, decision["unresolved"],
            turn_id=turn_id)
        if not plan.get("solo"):
            await diarize._in_voice_thread(_arm_and_seat, chat_id, decision,
                                           cfg, got)
        heard_pcm, heard_rate = vss.turn_audio(turn_id, pcm, sample_rate,
                                               got)
        target_id = await diarize._deliver_label(
            chat_id, heard_pcm, heard_rate, commit_ts, session, payload,
            turn_id=turn_id,
            clusters_remembered=(got or {}).get("voices_in_turn") or 1)
        if decision["ask"] and not plan.get("solo"):
            # The room has armed on a voice nobody knows, so the question
            # stands whether or not a row came back; the id, when there is
            # one, lets the ask point at the turn (#461).
            await diarize._in_voice_thread(diarize._raise_unknown_voice,
                                           chat_id, target_id)
        checked = bool(target_id) and len(listed) < 2 and cross_checks(
            decision, plan, (got or {}).get("turn_s")
            or len(pcm) / 2 / (sample_rate or 16000))
        if checked:
            from . import mismatch
            mismatch.schedule_check(chat_id, target_id, decision["labels"][0],
                                    cfg)
        banked = False
        if not plan.get("solo") and should_bank(got, cfg):
            banked = await diarize._in_voice_thread(_bank, chat_id, got, pcm,
                                                    sample_rate, cfg)
        log.info("voice pass: chat=%s ms=%.0f state=%s method=%s single=%s "
                 "arm=%s ask=%s banked=%s voices=%d split=%s checked=%s "
                 "pieces=%d",
                 chat_id, ms, (got or {}).get("state", "none"),
                 (got or {}).get("method", "-"), bool((got or {}).get(
                     "single")), decision["arm"], decision["ask"], banked,
                 len(listed), bool(payload.get("segments")), checked, pieces)
    except Exception:
        log.info("voice pass failed: chat=%s", chat_id)
        log.debug("voice pass failure detail", exc_info=True)
