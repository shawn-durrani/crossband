"""What every seat is told about the room each round (#460).

The 25 September field test, traced content free: a misheard "solo mode"
switched the room off, taking everyone off the room list. That round's
room note said room mode was off, named nobody, and said spoken turns
weren't being attributed by voice. The transcript held the guest's named
turns among several unnamed ones, and a seat asked about her said she
hadn't been identified. The note was read fresh for every seat and was
up to date. What it lacked was content, so the fix is content:

1. The note, every seat, every round: room mode on or off, whether it can
   switch itself back on, who is in the room, and whose voices the recent
   spoken turns were matched to. The off note says names on earlier turns
   still stand, and never that turns go unattributed.
2. The recent-voices summary reads the same rules as the turn heads, so
   the note and the heads can't disagree.
3. The engine builds the note per seat from fresh state: a room switch or
   a late label that lands while seat one replies reaches seat two.
4. The seat rules: talk about the room only when asked, answer only from
   the note and the turn heads, never from an earlier reply, add no status
   line while holding back, and don't confirm or deny a switch the newest
   turn asks for.

Names are the synthetic roster (Alex the owner, Sam, Dave, Mateo).
"""
import asyncio
import json
import time

import pytest
from fastapi.testclient import TestClient

from backend import db, engine, providers, room_state
from backend.app import create_app
from backend.config import Settings
from backend.providers import (ROOM_VOICE_TURNS, _hhmm, _turn_attribution,
                               _user_turn_head, room_state_note,
                               room_voices_note, split_system_prompt)

OWNER = "Alex"
CFG = {"user_name": OWNER}
PARTICIPANT = {"name": "Claude", "slug": "claude", "model": "claude-opus-4-8",
               "provider": "anthropic", "system_prompt": ""}
ROSTER = [{"name": "Claude", "slug": "claude"}, {"name": "GPT", "slug": "gpt"}]


def _turn(i, labels=None, *, age=60.0, voice=True, now=None):
    now = time.time() if now is None else now
    return {"id": i, "speaker": "user", "content": f"turn {i}",
            "created_at": now - age,
            "voice_turn_id": f"vt-{i}" if voice else "",
            "voice_labels": json.dumps(labels) if labels is not None else ""}


def _field_transcript(now):
    """The field shape, made up: the owner's confirmed turns, the guest's
    named turns, unnamed turns between, and a crosstalk turn naming both."""
    return [
        _turn(1, {"labels": [OWNER]}, age=300, now=now),
        _turn(2, {"labels": [], "unresolved": "below_threshold"}, age=280,
              now=now),
        _turn(3, {"labels": ["Sam"]}, age=240, now=now),
        _turn(4, {"labels": ["Sam"]}, age=230, now=now),
        _turn(5, {"labels": [OWNER]}, age=200, now=now),
        _turn(6, {"labels": [], "unresolved": "below_threshold"}, age=180,
              now=now),
        _turn(7, {"labels": [], "unresolved": "too_short"}, age=120, now=now),
        _turn(8, {"labels": [], "unresolved": "not_speech"}, age=90, now=now),
        _turn(9, {"labels": ["Sam", OWNER], "crosstalk": True}, age=30,
              now=now),
        {"id": 10, "speaker": "gpt", "content": "a seat's reply",
         "created_at": now - 20},
    ]


# ---------- 1. the note ----------

def test_the_off_note_says_earlier_names_stand():
    note = room_state_note(dict(CFG, room_mode=False, room_ambient_off=True))
    assert "room mode is OFF" in note
    assert "Names already on earlier turns still stand" in note
    # the sentence the seat read as "nobody is named" is gone
    assert "not being attributed" not in note
    # an unnamed new turn shows as the owner, and the note says so
    assert f"didn't name is headed {OWNER}, whoever spoke it" in note


def test_the_off_note_says_whether_the_room_can_switch_itself_back_on():
    solo = room_state_note(dict(CFG, room_mode=False, room_ambient_off=True))
    assert "won't switch itself back on" in solo
    assert '"room mode on"' in solo
    listening = room_state_note(dict(CFG, room_mode=False,
                                     room_ambient_off=False))
    assert "can switch itself back on" in listening
    assert "won't" not in listening


def test_the_on_note_names_the_room_and_the_recent_voices():
    note = room_state_note(dict(
        CFG, room_mode=True, room_roster_names=[OWNER, "Sam"],
        room_voices_note="Names the voice check put on the last 3 spoken "
                         "turns: Sam on 3."))
    assert "room mode is ON; in the room: Alex, Sam." in note
    assert "Sam on 3" in note
    assert "come from the on-device voice check alone" in note


def test_both_notes_are_ground_truth_used_only_when_asked():
    for cfg in (dict(CFG, room_mode=True), dict(CFG, room_mode=False)):
        note = room_state_note(cfg)
        assert "ground truth about the room" in note
        assert "only when someone asks about it" in note
        assert "answer from them alone" in note


def test_the_note_rides_the_volatile_tail_with_its_new_inputs():
    live = dict(CFG, room_mode=False, room_ambient_off=True,
                room_voices_note="Names the voice check put on the last "
                                 "spoken turn: Sam on 1.")
    stable, volatile = split_system_prompt(PARTICIPANT, ROSTER, live, None,
                                           "", False)
    assert "Sam on 1" in volatile and "won't switch itself" in volatile
    assert "Sam on 1" not in stable and "won't switch itself" not in stable


# ---------- 2. the recent-voices summary ----------

def test_the_summary_names_who_the_voice_check_recognised():
    """The seat that said the guest was unidentified would have read this
    beside her named turns."""
    now = time.time()
    transcript = _field_transcript(now)
    summary = room_voices_note(transcript, dict(CFG, room_mode=False),
                               now=now)
    sam_latest = _hhmm(transcript[8]["created_at"])
    assert summary.startswith(
        "Names the voice check put on the last 9 spoken turns: ")
    assert f"Sam on 3 (latest {sam_latest})" in summary
    assert "Alex on 3" in summary
    assert "no name on 4" in summary


def test_the_most_recently_heard_person_leads_the_summary():
    now = time.time()
    summary = room_voices_note(
        [_turn(1, {"labels": ["Dave"]}, age=90, now=now),
         _turn(2, {"labels": ["Mateo"]}, age=30, now=now)],
        dict(CFG, room_mode=True), now=now)
    assert summary.index("Mateo on 1") < summary.index("Dave on 1")


def test_the_summary_reads_the_same_rules_as_the_heads():
    """One reading of the labels: a turn the summary counts as named is a
    turn whose head names that person, and one it counts as unnamed has
    an unnamed head."""
    now = time.time()
    cfg = dict(CFG, room_mode=True)
    shapes = [
        {"labels": [OWNER]},                                # confirmed owner
        {"labels": [OWNER], "corrected": True},             # owner, corrected
        {"labels": ["Sam"]},                                # named guest
        {"labels": ["Mateo"], "uncertain": ["Mateo"],
         "learning": True},                                 # cold start
        {"labels": ["Dave"], "uncertain": ["Dave"]},        # uncertain
        {"labels": ["Voice 2"]},                            # ordinal
        {"labels": [], "unresolved": "ambiguous"},          # looked, no name
        {"labels": ["Sam", "Voice 3"]},                     # one of two
    ]
    for i, labels in enumerate(shapes):
        msg = _turn(i, labels, now=now)
        head, state, names = _turn_attribution(msg, cfg, now=now)
        assert head == _user_turn_head(msg, cfg, now=now)
        for name in names:
            assert name in head
        if state == "unnamed":
            assert head.startswith("Unidentified speaker")
    summary = room_voices_note([_turn(i, s, now=now)
                                for i, s in enumerate(shapes)], cfg, now=now)
    assert "Alex on 2" in summary
    assert "Sam on 2" in summary
    assert "Mateo on 1" in summary
    assert "Dave" not in summary          # an uncertain name is never told
    assert "no name on 3" in summary


def test_a_turn_still_being_checked_and_an_unchecked_one_are_told_apart():
    now = time.time()
    fresh = _turn(1, None, age=1.0, now=now)
    old = _turn(2, None, age=120, now=now)
    named = _turn(3, {"labels": ["Sam"]}, age=60, now=now)
    on = room_voices_note([old, named, fresh], dict(CFG, room_mode=True),
                          now=now)
    assert "still being checked on 1" in on
    assert "not checked on 1" in on
    # with the room off, a young unlabelled turn is not in the race
    off = room_voices_note([old, named, fresh], dict(CFG, room_mode=False),
                           now=now)
    assert "not checked on 2" in off and "still being checked" not in off


def test_no_summary_where_the_voice_check_never_looked():
    now = time.time()
    typed = [_turn(i, None, voice=False, now=now) for i in range(3)]
    unchecked = [_turn(i, None, now=now) for i in range(3)]
    assert room_voices_note(typed, dict(CFG, room_mode=False), now=now) == ""
    assert room_voices_note(unchecked, dict(CFG, room_mode=False),
                            now=now) == ""
    assert room_voices_note([], CFG) == ""


def test_the_summary_speaks_preferred_names():
    now = time.time()
    cfg = dict(CFG, room_mode=True, preferred_names={"samantha": "Sam"})
    summary = room_voices_note([_turn(1, {"labels": ["Samantha"]}, now=now)],
                               cfg, now=now)
    assert summary == ("Names the voice check put on the last spoken "
                       f"turn: Sam on 1 (latest {_hhmm(now - 60)}).")


def test_the_summary_covers_the_last_spoken_turns_only():
    now = time.time()
    turns = [_turn(i, {"labels": ["Dave"]}, age=1000 - i, now=now)
             for i in range(5)]
    turns += [_turn(100 + i, {"labels": ["Sam"]}, age=500 - i, now=now)
              for i in range(ROOM_VOICE_TURNS)]
    summary = room_voices_note(turns, dict(CFG, room_mode=True), now=now)
    assert f"Sam on {ROOM_VOICE_TURNS}" in summary
    assert "Dave" not in summary


# ---------- 3. the engine builds the note per seat, from fresh state ----------

@pytest.fixture
def app(tmp_path):
    return create_app(Settings(data_dir=str(tmp_path / "data"),
                               memory_url="http://127.0.0.1:1",
                               user_name=OWNER))


def test_every_seat_gets_the_note_from_fresh_state(app, monkeypatch):
    """The field sequence inside one round: the room is on with a guest
    seated, and the newest turn's name is still being checked. While seat
    one replies, the scan switches the room off (solo mode) and the late
    label lands naming the guest. Seat two's note must say off, say it
    won't switch itself back on, and count the guest's late-labelled turn."""
    with TestClient(app, base_url="http://127.0.0.1") as c:
        chat_id = c.post("/api/chats", json={}).json()["id"]
    con = db.connect()
    seats = db.get_chat_participants(con, chat_id)[:2]
    db.set_chat_room_state(con, chat_id, room_mode=True, ambient_off=False)
    db.add_room_person(con, chat_id, OWNER)
    db.add_room_person(con, chat_id, "Sam")
    for labels in ({"labels": [OWNER]}, {"labels": ["Sam"]},
                   {"labels": ["Sam"]},
                   {"labels": [], "unresolved": "below_threshold"}):
        db.insert_message(con, chat_id, "user", "an earlier spoken turn",
                          voice_turn_id=f"vt-{id(labels)}",
                          voice_labels=labels)
    newest = db.insert_message(con, chat_id, "user",
                               "go quiet now, we're talking",
                               voice_turn_id="vt-newest")
    con.close()

    notes = []

    async def seat_reply(participant, roster, transcript, names, cfg,
                         project, chat_summary, voice_mode, tools=None,
                         memory=None):
        notes.append("".join(providers._volatile_system_parts(cfg)))
        if len(notes) == 1:
            # the scan and the late label land while seat one replies
            room_state.disarm(chat_id, source="command",
                              set_ambient_off=True, clear_roster=True,
                              resolve_asks=True)
            c2 = db.connect()
            db.set_message_voice_labels(
                c2, newest["id"], {"labels": ["Sam", OWNER],
                                   "crosstalk": True})
            c2.close()
        yield ("text", f"A reply from {participant['name']}.")

    monkeypatch.setattr(engine.providers, "stream_reply", seat_reply)

    async def go():
        async for _ in engine.run_round(chat_id, seats, "gpt",
                                        app.state.settings, memory=None):
            pass

    asyncio.run(go())
    assert len(notes) == 2
    first, second = notes
    assert "room mode is ON; in the room: Alex, Sam." in first
    assert "Sam on 2" in first
    assert "still being checked on 1" in first
    assert "room mode is OFF" in second
    assert "won't switch itself back on" in second
    assert "Names already on earlier turns still stand" in second
    assert "Sam on 3" in second
    assert "still being checked" not in second


# ---------- 4. the seat rules ----------

def _stable():
    stable, _ = split_system_prompt(PARTICIPANT, ROSTER, dict(CFG), None, "",
                                    False)
    return stable


def test_seats_talk_about_the_room_only_when_asked():
    stable = _stable()
    assert "talk about ONLY when someone asks" in stable
    assert "from this round's room state note and the turn heads alone" \
        in stable
    assert "never from an earlier reply" in stable
    assert "Never add a room status line nobody asked for" in stable
    assert "least of all while you're holding back" in stable


def test_seats_neither_confirm_nor_deny_a_switch_just_asked_for():
    stable = _stable()
    assert "don't confirm or deny the switch" in stable
    assert "the app posts its own line saying what changed" in stable


def test_a_held_back_seat_puts_nothing_around_its_pass():
    stable = _stable()
    assert "reply with exactly [pass] - nothing before it or after it" \
        in stable
    assert 'no "nothing to add"' in stable
    assert "no status update" in stable


def test_the_seat_rules_are_constant_and_stay_out_of_the_volatile_tail():
    live = dict(CFG, room_mode=True, room_roster_names=[OWNER])
    stable, volatile = split_system_prompt(PARTICIPANT, ROSTER, live, None,
                                           "", False)
    assert stable == _stable()
    assert "talk about ONLY when someone asks" not in volatile
