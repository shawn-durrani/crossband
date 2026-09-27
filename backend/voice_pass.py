"""The single naming pass (#482 stage 3, `voice_session_only`).

With the switch on, every spoken turn in every mode goes through this one
pass, and the old matcher's three routes (armed room, room off, solo) don't
run. The session naming (backend/voice_session_shadow.py) has already
followed each voice through the voice session while the person talked, so
the pass mostly reads its answer:

  1. Ask the live feed for this turn's main voice, waiting at most
     voice_session_shadow.LIVE_WAIT_S. When no feed saw the turn (the
     diariser is down, or the backup transcript path carried it), the turn
     is named on its own as one voice, with the same scorer.
  2. Decide, as a pure rule (`decide`), from that answer and the chat's
     room state: the label, and whether to arm the room, seat someone, or
     ask who a new voice is. The room follows the main voice.
  3. When a second voice spoke for a second or more (#482 item D), the
     label is crosstalk instead: it names every voice and splits the words
     between them on this computer (backend/crosstalk.py). Only such a
     turn waits for Scribe's word times, at most crosstalk.WORDS_WAIT_S.
     They come with the final the browser sends the message on, and the
     relay hands them over first, so the label is normally parked before
     the message is saved and the seats read the split, not a single name
     that would pass one person's words off as another's.
  4. Deliver the label through the one label path, then act.
  5. Save the turn's clean speech to the named person's bank when the
     naming is near certain (BANK_PROB) and the voice has BANK_MIN_CLEAN_S
     of clean speech behind it, at most BANK_PER_SESSION clips per voice
     per session, never from a turn with two voices in it.

What stays exactly as it was: the owner's name is never learnt by ear as a
second person, an AI participant is never a person, solo ("just me") never
arms, seats, asks or learns, and every write goes through the same guarded
helpers the old passes used (room_state.arm and seat, the one open ask per
chat, the label path, anchors.add_clip and its gate).
"""

import asyncio
import logging
import time

from . import crosstalk, db, voice_session_shadow as vss

log = logging.getLogger("crossband.voice_pass")

BANK_PROB = 0.99            # calibrated probability a clip is saved at
BANK_MIN_CLEAN_S = 8.0      # clean speech a voice needs before it saves
BANK_PER_SESSION = 3        # clips one session voice may save
BANK_MIN_SPAN_S = 2.0       # the shortest clean span worth saving

LISTENING = "listening"     # the unresolved reasons this pass writes
NEW_VOICE = "new_voice"


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


def should_bank(got):
    """Is this turn's naming sure enough to save its clean speech? Only a
    single-voice turn of a voice named by the calibrated scorer at
    BANK_PROB, or by the fallback scorer over the banking bar, with
    BANK_MIN_CLEAN_S behind it. A voice a person named by hand is saved by
    that correction itself, not here."""
    if not got or got.get("state") != "named" or got.get("human"):
        return False
    if (got.get("voices_in_turn") or 1) > 1 or got.get("overlap_s"):
        return False
    if (got.get("voice_clean_s") or 0.0) < BANK_MIN_CLEAN_S:
        return False
    if got.get("method") == "calibrated":
        return (got.get("prob") or 0.0) >= BANK_PROB
    from . import voiceid
    return (got.get("score") or 0.0) >= voiceid._threshold({}) \
        + voiceid._banking_extra({})


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


def _act(chat_id, decision, cfg, target_id, got):
    """Arm, seat and ask (worker thread), through the old passes' guarded
    helpers, so the rules on who may arm and seat are the ones that were
    already tested."""
    from . import diarize, room_state
    if decision["arm"] == "known" and decision["seat"]:
        diarize._arm_known(chat_id, {"session": decision["seat"]}, cfg)
    elif decision["arm"] == "unknown":
        diarize._arm_ambient_unknown(chat_id, cfg)
    elif decision["seat"]:
        room_state.seat(chat_id, decision["seat"], cfg, via="voice-match",
                        person_id=(got or {}).get("pid") or "",
                        message_id=target_id, enforce_cap=True)
    if decision["ask"]:
        diarize._raise_unknown_voice(chat_id, target_id)


def _bank(chat_id, got, pcm, sample_rate):
    """Save the voice's longest clean stretch in this turn (worker thread),
    once per BANK_PER_SESSION. The store's own gate, 10 s cap and rotation
    apply as for any clip."""
    from . import anchors, voice_shadow
    spans = [s for s in got.get("clean_spans") or ()
             if s[1] - s[0] >= BANK_MIN_SPAN_S]
    if not spans or not got.get("pid"):
        return False
    if not vss.take_bank_allowance(chat_id, got.get("voice"),
                                   BANK_PER_SESSION):
        return False
    start, end = max(spans, key=lambda s: s[1] - s[0])
    clip = voice_shadow.span_pcm(pcm, sample_rate, [(start, end)])
    return anchors.store().add_clip(got["pid"], clip, sample_rate,
                                    source="accumulated",
                                    score=got.get("score"))


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
        plan = await diarize._in_voice_thread(_plan, chat_id, cfg)
        decision = decide(got, plan)
        listed = crosstalk.listed_voices(got)
        if len(listed) >= 2:
            # Two voices: the label names each and splits the words by the
            # tracker's spans. The words ride the same Scribe answer as the
            # final the browser is waiting for, and the relay hands them
            # over before it sends that final, so they are here (or known
            # to be missing) before /send can save the message.
            words = await crosstalk.await_words(turn_id) if turn_id \
                else None
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
        target_id = await diarize._deliver_label(
            chat_id, pcm, sample_rate, commit_ts, session, payload,
            turn_id=turn_id,
            clusters_remembered=(got or {}).get("voices_in_turn") or 1)
        if not plan.get("solo"):
            await diarize._in_voice_thread(_act, chat_id, decision, cfg,
                                           target_id, got)
        banked = False
        if not plan.get("solo") and should_bank(got):
            banked = await diarize._in_voice_thread(_bank, chat_id, got, pcm,
                                                    sample_rate)
        log.info("voice pass: chat=%s ms=%.0f state=%s method=%s single=%s "
                 "arm=%s ask=%s banked=%s voices=%d split=%s", chat_id, ms,
                 (got or {}).get("state", "none"),
                 (got or {}).get("method", "-"), bool((got or {}).get(
                     "single")), decision["arm"], decision["ask"], banked,
                 len(listed), bool(payload.get("segments")))
    except Exception:
        log.info("voice pass failed: chat=%s", chat_id)
        log.debug("voice pass failure detail", exc_info=True)
