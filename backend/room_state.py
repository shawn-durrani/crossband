"""The single write path for room and roster state (#239).

Room mode, the sacred ambient-off flag, and the roster had six writers,
each with a hand-rolled ceremony, in the subsystem with the worst
incident history (#65: an undocumented seat path minted phantom people
three times in one evening). The ceremony now lives here, once.
tests/test_room_state_guard.py holds the walls: no new direct writer can
land outside this module.

THE ORDERING LAW (db.py's store discipline, kept): the database is the
durable state; diarize's in-process dicts are the live mirror the STT
relay reads at commit boundaries with no I/O on the audio path; the
room-update bell (events.notify_room_update) is only the wake-up. Every
function here commits the durable write FIRST - one statement, one
commit, even when a call touches both flags, so a crash can no longer
split a flip - then updates the mirror, then runs the roster steps
(whose db writers ring their own bells), then rings the bell once. The
bell rings on every arm/disarm call, including one that changed
nothing: it is a wake-up, not a delta, and the early-return paths that
used to skip it left durable ambient flips nobody was told about.

The keyword arguments are the six sites' GENUINE differences, stated at
each call site instead of averaged away:

- arm(clear_ambient=...): only an EXPLICIT owner act (an introduction,
  the spoken arm command, the manual toggle) may clear the sacred
  ambient-off. The automatic ambient arms pass False: they only ever
  fire while the flag is unset, and they must never gain the power to
  clear it.
- arm(seat_owner=...): "on_arm" seats the owner only when THIS call
  flipped the room on (the spoken command, the ambient-unknown arm - a
  room some other path armed first has already seated them); "always"
  re-seats and re-links every time (the manual toggle is a plain
  idempotent switch); "never" leaves the owner to the caller (the
  introduction scan seats from its stash gate, and the known-voice arm
  seats the matched guests instead - a remembered guest proves a
  household, and the anchored-pass rationale only needs the owner on
  the UNKNOWN path).
- disarm(set_ambient_off / clear_roster / resolve_asks): the spoken
  "solo mode" is the sacred disarm and passes all three; the manual
  toggle-off is the degraded/accessibility path and passes none, which
  preserves its shipped behaviour exactly. Whether the toggle-off
  SHOULD keep leaving ambient armable, the roster present and the asks
  open is Shawn's call; either answer is now a one-argument change.
- seat(enforce_cap=...): required, no default. Every seat site states
  its cap stance, so the two exemptions (the owner's own seat, the
  owner's tap-correction) are written down instead of drifted into.

THREADING: arm(), disarm() and seat() are synchronous and block on
sqlite commits - never call them on the event loop. Every production
caller is already off it: diarize's arms run under _in_voice_thread,
apply_scan and apply_command run on worker threads, and the chats/room
routers are sync endpoints on the request threadpool. seed_mirrors() is
the exception: dict writes only (single-key assignments, atomic under
the GIL), no I/O, safe from the event loop - the relay's session-open
seed calls it there.

What deliberately stays at the call sites: the introduction scan's
naming ceremony (owner aliases, the #65 participant boundary,
relationship nouns, variant merging, alias capture, merge questions)
and its once-per-scan ask resolution (re-introducing a present person
still answers the ask, so it cannot hang off any single seat); the
stash-gated owner anchor seed (it needs the scan's armed_before
context); the ambient ask raise (_raise_unknown_voice needs the
attached message id, which does not exist at arm time); and
reassign_speaker's message-scoped flag resolution (it belongs to the
label rewrite, not the seat).
"""
import logging

from . import db, diarize
from .config import Settings

log = logging.getLogger("crossband.room_state")

_SEAT_OWNER = ("never", "on_arm", "always")


def roster_cap(cfg) -> int:
    """The room roster cap: one key (room_roster_max), one default
    (Settings' own). Missing or None mean the default; an explicit 0
    means no guest can be seated (#295) - the owner's own hand still
    outranks the cap, per #239's second ruling. Every writer-side
    comparison and the snapshot cap the UI shows derive the number here;
    the guard test holds the key literal to config.py, db.py's schema
    comment and this module."""
    v = (cfg or {}).get("room_roster_max")
    if v is None:
        v = Settings.model_fields["room_roster_max"].default
    return int(v)


def seed_mirrors(chat_id, *, enabled, ambient_disarmed):
    """Seed diarize's live mirrors from durable state already read
    elsewhere (the relay's session-open path). Mirror-only by design: no
    database write and no bell, because the chat row is the truth this
    copies - ringing would announce a change that did not happen. Safe
    on the event loop: two dict assignments, no I/O."""
    diarize.set_room_enabled(chat_id, bool(enabled))
    diarize.set_ambient_off(chat_id, bool(ambient_disarmed))


def arm(chat_id, cfg, *, source, clear_ambient, seat_owner, con=None):
    """Turn room mode ON. Returns True when this call flipped it (off to
    on), False when it was already on or the chat does not exist.

    `source` feeds the content-free log line ("introduction", "command",
    "manual toggle", "ambient (known voice)", "ambient (unknown
    voice)"). `con`: pass a caller-held connection to ride its
    transaction scope; omit it and one is opened and closed here.
    Idempotent: two racing arms re-run the same writes harmlessly."""
    if seat_owner not in _SEAT_OWNER:
        raise ValueError(f"seat_owner must be one of {_SEAT_OWNER}")
    own = con is None
    if own:
        con = db.connect()
    try:
        row = con.execute("SELECT room_mode FROM chats WHERE id=?",
                          (chat_id,)).fetchone()
        if row is None:
            return False
        flipped = not row["room_mode"]
        # Durable first, in one statement: room_mode, plus ambient_off
        # when this arm is an explicit owner re-enable.
        db.set_chat_room_state(con, chat_id, room_mode=True,
                               ambient_off=False if clear_ambient else None)
        diarize.set_room_enabled(chat_id, True)
        if clear_ambient:
            diarize.set_ambient_off(chat_id, False)
        if flipped:
            log.info("room mode ON via %s: chat=%s", source, chat_id)
        if seat_owner == "always" or (seat_owner == "on_arm" and flipped):
            _seat_owner(con, chat_id, cfg)
    finally:
        if own:
            con.close()
    from . import events  # lazy, same circularity reason as db.py's bell
    events.notify_room_update()
    return flipped


def disarm(chat_id, *, source, set_ambient_off, clear_roster,
           resolve_asks, con=None):
    """Turn room mode OFF. Returns True when this call flipped it (on to
    off).

    `set_ambient_off=True` is the sacred half of "solo mode": it is
    written even when room mode was already off, because the owner is
    stating a privacy preference, not toggling a switch. `clear_roster`
    marks everyone present as left (the cap frees) and `resolve_asks`
    closes the chat's open unknown-voice asks (moot once solo); both run
    only when this call actually flipped the room off, exactly as the
    command path always behaved. Mismatch flags are never touched: they
    doubt past turns, and going solo answers nothing about those."""
    own = con is None
    if own:
        con = db.connect()
    try:
        row = con.execute("SELECT room_mode FROM chats WHERE id=?",
                          (chat_id,)).fetchone()
        if row is None:
            return False
        flipped = bool(row["room_mode"])
        db.set_chat_room_state(con, chat_id, room_mode=False,
                               ambient_off=True if set_ambient_off else None)
        diarize.set_room_enabled(chat_id, False)
        if set_ambient_off:
            diarize.set_ambient_off(chat_id, True)
        departed = 0
        if flipped and clear_roster:
            for r in db.get_room_roster(con, chat_id, present_only=True):
                if db.mark_room_person_left(con, chat_id, r["name"]):
                    departed += 1
        if flipped and resolve_asks:
            db.resolve_room_flags(con, chat_id, kind="unknown_voice")
        if flipped:
            log.info("room mode OFF via %s: chat=%s departed=%d",
                     source, chat_id, departed)
        elif set_ambient_off:
            log.info("ambient off via %s (room already off): chat=%s",
                     source, chat_id)
    finally:
        if own:
            con.close()
    from . import events  # lazy, same circularity reason as db.py's bell
    events.notify_room_update()
    return flipped


def seat(chat_id, name, cfg, *, via, enforce_cap, person_id="",
         message_id=None, link_existing=False, resolve_ask=False,
         con=None):
    """Seat one person on a chat's roster through the one guarded path.
    Returns the roster row when this call wrote a seat (new, or a left
    row re-marked present), None when nothing was written: the name is
    already present (linked instead when `link_existing` and a
    `person_id` are given), the cap is full (`enforce_cap=True`), or
    db.add_room_person refused the name at the #65 participant boundary.

    `via` and `message_id` are the seat's provenance (#84), recorded at
    write time. `enforce_cap` has no default on purpose: every call site
    states its cap stance, so the uncapped seats (the owner's own, the
    owner's tap-correction) read as decisions, not drift. `resolve_ask`
    closes the chat's open unknown-voice asks, and only after a genuine
    seat - a refused or duplicate seat answers nothing. The roster
    writers below ring the bell themselves, so seat() adds none."""
    own = con is None
    if own:
        con = db.connect()
    try:
        present = db.get_room_roster(con, chat_id, present_only=True)
        if any((p["name"] or "").casefold() == (name or "").casefold()
               for p in present):
            if link_existing and person_id:
                db.link_room_person(con, chat_id, name, person_id)
            return None
        if enforce_cap:
            cap = roster_cap(cfg)
            if len(present) >= cap:
                log.info("roster cap reached; not seated: chat=%s via=%s "
                         "cap=%d", chat_id, via, cap)
                return None
        row = db.add_room_person(con, chat_id, name, person_id=person_id,
                                 seated_by_message_id=message_id,
                                 seated_via=via)
        if row is None:
            return None  # the #65 participant boundary held
        if resolve_ask:
            db.resolve_room_flags(con, chat_id, kind="unknown_voice")
        return row
    finally:
        if own:
            con.close()


def _seat_owner(con, chat_id, cfg):
    """The owner's seat for arm(): under the user_name SETTING (#28
    phase 3 - a spelt-by-ear transcription never mints a roster person),
    linked to their remembered anchors when those exist, anchor-pending
    otherwise. Uncapped: the owner rides the roster, and their seat is
    never what the cap guards."""
    from . import anchors  # lazy: keep room_state importable without the store
    owner = cfg.get("user_name", "User")
    person = anchors.store().find_by_name(owner)
    pid = person["person_id"] if person else ""
    seat(chat_id, owner, cfg, via="owner", person_id=pid,
         enforce_cap=False, link_existing=True, con=con)
