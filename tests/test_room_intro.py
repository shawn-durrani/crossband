"""Introduction detection (#28 phase 2): the spoken introduction is the
trigger, and it may NEVER cost the send anything.

What these tests prove, in order of importance:

1. THE LATENCY PIN: POST /send completes - the round is dispatched and the
   SSE stream fully drains - while the utility-model confirmation is
   deliberately wedged open. Introduction detection is fire-and-forget after
   the user message persists; dispatch has no dependence on it.
2. A confirmed introduction flips the chat's durable room_mode, mirrors it
   into diarize's in-process registry, and appends the named people to the
   roster (anchor pending); departures mark them left and free the cap.
3. The roster cap (Settings.room_roster_max, env-mapped) is enforced.
4. The lexical prefilter gates the utility spend: a turn that is not
   introduction-shaped never makes a model call at all.
5. The owner's anchor seeds from the stashed introduction utterance, and a
   remembered (sufficient) person links immediately - re-identification.

The utility model is always mocked (keyless like everything else); with no
key at all the scan quietly does nothing, which is also pinned.
"""

import asyncio
import json
import threading
import time

import pytest
from fastapi.testclient import TestClient

from backend import anchors, db, diarize, introductions
from backend.app import create_app
from backend.config import Settings, load_settings
from roomkit import _chat_room_mode, _wait_for, as_utility_completion, loud_pcm
from tests.conftest import speech_pcm


@pytest.fixture
def app(tmp_path):
    settings = Settings(data_dir=str(tmp_path / "data"),
                        memory_url="http://127.0.0.1:1")
    return create_app(settings)


def _roster(chat_id, present_only=True):
    con = db.connect()
    try:
        return db.get_room_roster(con, chat_id, present_only=present_only)
    finally:
        con.close()


def _send(client, chat_id, text):
    """POST /send and DRAIN the SSE stream - the response completing is the
    proof dispatch never waited on anything."""
    with client.stream("POST", f"/api/chats/{chat_id}/send",
                       json={"text": text}) as r:
        body = b"".join(r.iter_bytes())
    assert b'"user_saved"' in body and b'"done"' in body
    return body


@pytest.fixture
def utility(monkeypatch):
    """Mock the utility model at the llm_util seam. state['verdict'] is what
    it returns (a dict, JSON-encoded); state['gate'] (a threading.Event)
    wedges the call open; state['calls'] records every prompt."""
    state = {"verdict": {"introductions": [], "departures": []},
             "gate": None, "calls": []}

    async def fake_utility(prompt, cfg, max_tokens=2000):
        state["calls"].append(prompt)
        if state["gate"] is not None:
            await asyncio.to_thread(state["gate"].wait, 10)
        return json.dumps(state["verdict"])

    monkeypatch.setattr("backend.llm_util.utility_complete_with_usage",
                        as_utility_completion(fake_utility))
    return state


# ── 1. the latency pin ──────────────────────────────────────────────────────

def test_send_completes_while_the_confirmation_is_wedged_open(app, utility):
    """The core law: the round is dispatched and done while the utility call
    is still blocked. Only after the gate opens does the roster change."""
    utility["verdict"] = {"introductions": ["Alex"], "departures": []}
    utility["gate"] = threading.Event()
    with TestClient(app, base_url="http://127.0.0.1") as c:
        chat = c.post("/api/chats", json={"participant_ids": []}).json()
        _send(c, chat["id"], "say hi to Alex, she's here with me")
        # The send is fully over; the scan is still wedged - nothing changed.
        assert not utility["gate"].is_set()
        assert _chat_room_mode(chat["id"]) is False
        assert _roster(chat["id"]) == []
        utility["gate"].set()  # release; the scan catches up out of band
        assert _wait_for(lambda: _chat_room_mode(chat["id"]))
        roster = _wait_for(lambda: _roster(chat["id"]))
        assert [p["name"] for p in roster] == ["Alex"]


def test_prefilter_gates_the_utility_spend(app, utility):
    """A turn with no introduction shape makes NO model call at all - the
    prefilter is what keeps this feature nearly free on normal turns."""
    with TestClient(app, base_url="http://127.0.0.1") as c:
        chat = c.post("/api/chats", json={"participant_ids": []}).json()
        _send(c, chat["id"], "let's plan the weekend")
        time.sleep(0.3)
        assert utility["calls"] == []


def test_keyless_scan_is_a_quiet_no_op(app, monkeypatch):
    """No utility key: llm_util returns None, the scan finds nothing, room
    mode stays off. (The explicit toggle keeps working regardless.)"""
    with TestClient(app, base_url="http://127.0.0.1") as c:
        chat = c.post("/api/chats", json={"participant_ids": []}).json()
        _send(c, chat["id"], "my wife Alex is here with us")
        time.sleep(0.4)
        assert _chat_room_mode(chat["id"]) is False
        assert _roster(chat["id"]) == []


# ── 2. confirmed introductions and departures ───────────────────────────────

def test_confirmed_introduction_flips_room_mode_and_grows_the_roster(
        app, utility):
    utility["verdict"] = {"introductions": ["Alex"], "departures": []}
    with TestClient(app, base_url="http://127.0.0.1") as c:
        chat = c.post("/api/chats", json={"participant_ids": []}).json()
        _send(c, chat["id"], "my wife Alex is here with us")
        # Wait on the roster - the LAST thing the scan writes - not the
        # room-mode flag, which commits first and can win the race (#71):
        # anchor-store I/O (people list, voice-match, variant matching) runs
        # between the two commits, so asserting on room_mode alone can catch
        # the scan mid-flight with room mode on and nobody in the roster yet.
        roster = _wait_for(lambda: _roster(chat["id"]))
        assert [p["name"] for p in roster] == ["Alex"]
        assert roster[0]["person_id"] == ""  # anchor pending - nothing heard yet
        assert _chat_room_mode(chat["id"]) is True
        # the live mirror the STT relay reads at commit boundaries
        assert diarize.room_enabled(chat["id"]) is True


def test_room_mode_commit_can_precede_the_roster_row(app, utility, monkeypatch):
    """Regression guard for #71: room_mode and the roster row are two
    separate commits inside the scan, with anchor-store work (people list,
    voice-match, variant matching) running in between - so a reader CAN
    observe room_mode on with an empty roster mid-scan. Widen that gap
    deterministically and prove two things: (a) the gap is real, so a test
    (or any consumer) that reads the roster immediately after room_mode can
    race it, and (b) waiting on the roster - the scan's last write, per the
    fixed test above - always converges regardless of the gap's width."""
    gate = threading.Event()
    real_add_room_person = db.add_room_person

    def widened_add_room_person(*a, **kw):
        gate.wait(2)
        return real_add_room_person(*a, **kw)

    monkeypatch.setattr(introductions.db, "add_room_person",
                        widened_add_room_person)
    utility["verdict"] = {"introductions": ["Alex"], "departures": []}
    with TestClient(app, base_url="http://127.0.0.1") as c:
        chat = c.post("/api/chats", json={"participant_ids": []}).json()
        _send(c, chat["id"], "my wife Alex is here with us")
        # room_mode is the FIRST commit - it lands while the roster write is
        # still gated, catching the exact window the old test could race.
        assert _wait_for(lambda: _chat_room_mode(chat["id"]))
        assert _roster(chat["id"]) == []
        gate.set()  # release the gated roster write
        roster = _wait_for(lambda: _roster(chat["id"]))
        assert [p["name"] for p in roster] == ["Alex"]


def test_group_introduction_names_several_people(app, utility):
    utility["verdict"] = {"introductions": ["Ana", "Ben", "Cass"],
                          "departures": []}
    with TestClient(app, base_url="http://127.0.0.1") as c:
        chat = c.post("/api/chats", json={"participant_ids": []}).json()
        _send(c, chat["id"], "the whole family is here: Ana, Ben and Cass")
        roster = _wait_for(lambda: len(_roster(chat["id"])) == 3
                           and _roster(chat["id"]))
        assert [p["name"] for p in roster] == ["Ana", "Ben", "Cass"]


def test_departure_frees_the_roster_slot(app, utility):
    utility["verdict"] = {"introductions": ["Alex"], "departures": []}
    with TestClient(app, base_url="http://127.0.0.1") as c:
        chat = c.post("/api/chats", json={"participant_ids": []}).json()
        _send(c, chat["id"], "say hi to Alex everyone")
        assert _wait_for(lambda: _roster(chat["id"]))
        utility["verdict"] = {"introductions": [], "departures": ["Alex"]}
        _send(c, chat["id"], "Alex has left the room")
        assert _wait_for(lambda: not _roster(chat["id"]))
        gone = _roster(chat["id"], present_only=False)
        assert gone[0]["status"] == "left" and gone[0]["left_at"]
        # room mode STAYS on - the explicit toggle is the way off
        assert _chat_room_mode(chat["id"]) is True


def test_mid_session_introduction_appends_and_reintroduction_is_idempotent(
        app, utility):
    utility["verdict"] = {"introductions": ["Alex"], "departures": []}
    with TestClient(app, base_url="http://127.0.0.1") as c:
        chat = c.post("/api/chats", json={"participant_ids": []}).json()
        _send(c, chat["id"], "this is Alex")
        assert _wait_for(lambda: _roster(chat["id"]))
        utility["verdict"] = {"introductions": ["Dave"], "departures": []}
        _send(c, chat["id"], "and this is Dave")
        assert _wait_for(lambda: len(_roster(chat["id"])) == 2)
        utility["verdict"] = {"introductions": ["Alex"], "departures": []}
        _send(c, chat["id"], "this is Alex again")
        time.sleep(0.4)
        assert len(_roster(chat["id"])) == 2  # no duplicate row


# ── 3. the cap ──────────────────────────────────────────────────────────────

def test_cap_allows_math():
    assert introductions.cap_allows(0, 3, 6) == 3
    assert introductions.cap_allows(5, 3, 6) == 1
    assert introductions.cap_allows(6, 1, 6) == 0
    assert introductions.cap_allows(2, 0, 6) == 0


def test_roster_cap_is_enforced_from_settings(tmp_path, utility):
    diarize._ROOM_ENABLED.clear()
    app = create_app(Settings(data_dir=str(tmp_path / "data"),
                              memory_url="http://127.0.0.1:1",
                              room_roster_max=2))
    utility["verdict"] = {"introductions": ["Ana", "Ben", "Cass"],
                          "departures": []}
    with TestClient(app, base_url="http://127.0.0.1") as c:
        chat = c.post("/api/chats", json={"participant_ids": []}).json()
        _send(c, chat["id"], "the whole family is here: Ana, Ben and Cass")
        roster = _wait_for(lambda: len(_roster(chat["id"])) == 2
                           and _roster(chat["id"]))
        assert [p["name"] for p in roster] == ["Ana", "Ben"]  # Cass dropped


def test_room_roster_max_rides_the_auto_env_mapping():
    s = load_settings(environ={"CROSSBAND_ROOM_ROSTER_MAX": "3"})
    assert s.room_roster_max == 3


# ── 4. owner anchor + re-identification ─────────────────────────────────────

def test_owner_anchor_seeds_from_the_stashed_introduction_utterance(app):
    """The voice that spoke the introduction is the owner's anchor: the relay
    stashes each finished utterance while room mode is off, and apply_scan
    claims it. The owner also joins the roster, linked."""
    with TestClient(app, base_url="http://127.0.0.1"):
        con = db.connect()
        cur = con.execute("INSERT INTO chats(title, created_at, updated_at) "
                          "VALUES('t', 0, 0)")
        con.commit()
        chat_id = cur.lastrowid
        con.close()
        diarize.stash_utterance(chat_id, loud_pcm(2.0), 16000)
        introductions.apply_scan(chat_id,
                                 {"introductions": ["Alex"], "departures": []},
                                 {"user_name": "Shawn", "room_roster_max": 6})
        people = anchors.store().people()
        assert [p["name"] for p in people] == ["Shawn"]
        assert people[0]["clip_count"] == 1  # the introduction utterance
        roster = _roster(chat_id)
        assert sorted(p["name"] for p in roster) == ["Alex", "Shawn"]
        owner_row = next(p for p in roster if p["name"] == "Shawn")
        assert owner_row["person_id"] == people[0]["person_id"]
        # the stash is CLAIMED - a second scan cannot double-feed it
        assert diarize.take_stashed_utterance(chat_id) is None


def test_typed_introduction_has_no_audio_and_that_is_fine(app):
    with TestClient(app, base_url="http://127.0.0.1"):
        con = db.connect()
        cur = con.execute("INSERT INTO chats(title, created_at, updated_at) "
                          "VALUES('t', 0, 0)")
        con.commit()
        chat_id = cur.lastrowid
        con.close()
        introductions.apply_scan(chat_id,
                                 {"introductions": ["Alex"], "departures": []},
                                 {"user_name": "Shawn", "room_roster_max": 6})
        assert anchors.store().people() == []  # nothing stashed, nothing stored
        assert [p["name"] for p in _roster(chat_id)] == ["Alex"]


def test_remembered_sufficient_person_links_immediately(app):
    """Re-identification: introducing a name whose anchors are already
    sufficient links the roster row on the spot - no anchor-pending phase."""
    with TestClient(app, base_url="http://127.0.0.1"):
        store = anchors.store()
        pid = store.ensure_person("Alex")
        for _ in range(3):
            store.add_clip(pid, loud_pcm(2.0), 16000, source="accumulated")
        con = db.connect()
        cur = con.execute("INSERT INTO chats(title, created_at, updated_at) "
                          "VALUES('t', 0, 0)")
        con.commit()
        chat_id = cur.lastrowid
        con.close()
        introductions.apply_scan(chat_id,
                                 {"introductions": ["Alex"], "departures": []},
                                 {"user_name": "Shawn", "room_roster_max": 6})
        roster = _roster(chat_id)
        assert roster[0]["person_id"] == pid


# ── pure rules ──────────────────────────────────────────────────────────────

def test_prefilter_truth_table():
    yes = [
        "my wife Alex is here with us",
        "say hi to Dave",
        "This is Ana, she'll be joining us",
        "the whole family is here: Ana, Ben and Cass",
        "meet Sam, my colleague",
        "I'm here with my dad",
        "Dave has left",
        "she had to go, it's just the two of us",
    ]
    no = [
        "let's plan the weekend",
        "what do you both think about the plan?",
        "left-align the header please",  # 'left' without the departure shape
        "",
        None,
    ]
    for t in yes:
        assert introductions.prefilter(t), t
    for t in no:
        assert not introductions.prefilter(t), t


def test_parse_verdict_is_defensive():
    ok = introductions.parse_verdict(
        'Sure! {"introductions": ["Alex", "alex", "Dave"], "departures": []}')
    assert ok == {"introductions": ["Alex", "Dave"], "departures": [],
                  "aliases": {}}
    for bad in (None, "", "not json", "{}", '{"introductions": "Alex"}',
                '{"introductions": [42, null]}', "[1,2]"):
        v = introductions.parse_verdict(bad)
        assert v == {"introductions": [], "departures": [], "aliases": {}}, bad


def test_parse_verdict_cleans_and_bounds_names():
    v = introductions.parse_verdict(json.dumps({
        "introductions": ["  alex  ", "<b>Dave</b>", "x" * 100,
                          "A", "B", "C", "D", "E", "F"],
        "departures": ["!!!"],
    }))
    assert v["introductions"][0] == "Alex"          # trimmed, title-cased
    assert v["introductions"][1] == "BDaveb"        # markup stripped
    assert all(len(n) <= introductions.MAX_NAME_CHARS
               for n in v["introductions"])
    assert len(v["introductions"]) <= introductions.MAX_NAMES_PER_TURN
    assert v["departures"] == []                    # no letters, no name


# ── the owner's roster identity is the user_name SETTING (#28 phase 3) ──────
#
# Field-test defect 3: the transcriber wrote the owner's name phonetically
# wrong, and a self-introduction ("I'm Shaun, my wife Alex is here") minted
# a phantom second person beside the real owner row. owner_alias is the
# filter: an introduction name that is plausibly the owner's own name - as
# the transcriber spelt it - never reaches the roster.

def test_owner_alias_truth_table():
    yes = [
        ("Shawn", "Shawn"),    # exact
        ("shawn", "Shawn"),    # case
        ("Shaun", "Shawn"),    # the field-test misspelling (one edit)
        ("Sean", "Seán"),      # punctuation/diacritic-adjacent forms
        ("Shawn.", "Shawn"),   # trailing punctuation
    ]
    no = [
        ("Alex", "Shawn"),
        ("Dawn", "Shawn"),     # two edits away - a real different name
        ("Jo", "Mo"),          # too short to fuzzy-match: exact only
        ("", "Shawn"),
        ("Alex", ""),
    ]
    for name, owner in yes:
        assert introductions.owner_alias(name, owner), (name, owner)
    for name, owner in no:
        assert not introductions.owner_alias(name, owner), (name, owner)


def test_self_introduction_never_mints_a_roster_person(app):
    """The model returning the owner's own (misspelt) name as an introduction
    changes NOTHING: no roster row, and room mode stays off."""
    with TestClient(app, base_url="http://127.0.0.1") as c:
        chat = c.post("/api/chats", json={"participant_ids": []}).json()
        introductions.apply_scan(chat["id"],
                                 {"introductions": ["Shaun"], "departures": []},
                                 {"user_name": "Shawn", "room_roster_max": 6})
        assert _chat_room_mode(chat["id"]) is False
        assert _roster(chat["id"]) == []


def test_owner_alias_is_dropped_but_the_real_guest_still_joins(app):
    """'I'm Shaun, and my wife Alex is here': Alex joins under her own name,
    the owner-alias is dropped, and no roster row ever carries a name
    transcribed by ear for the owner."""
    with TestClient(app, base_url="http://127.0.0.1") as c:
        chat = c.post("/api/chats", json={"participant_ids": []}).json()
        introductions.apply_scan(
            chat["id"],
            {"introductions": ["Shaun", "Alex"], "departures": []},
            {"user_name": "Shawn", "room_roster_max": 6})
        assert _chat_room_mode(chat["id"]) is True
        assert [p["name"] for p in _roster(chat["id"])] == ["Alex"]


# ── the participant boundary: an AI is never a person in the room (#65) ─────
#
# Field failure: a user turn the transcriber rendered as "This is Claude..."
# seated the AI participant on the roster; by-elimination then banked HUMAN
# audio under that pending seat until "Claude" was a remembered voice that
# re-seated itself in every session. Agents are addressed by name in nearly
# every sentence of a voice chat, so participant names get the same
# spelt-by-ear treatment the owner's name gets, and the seat writer itself
# refuses the exact names as a last-ditch guard.

def test_participant_alias_truth_table():
    participants = ["claude", "Claude", "gpt", "GPT"]
    yes = ["Claude", "claude", "Claud", "Clyde", "Cloud", "GPT", "gpt"]
    no = ["Clark", "Alex", "Sam", "Dave", "Kat", ""]
    for name in yes:
        assert introductions.participant_alias(name, participants), name
    for name in no:
        assert not introductions.participant_alias(name, participants), name
    assert not introductions.participant_alias("Claude", [])


def test_participant_intro_is_dropped_but_the_real_guest_still_joins(app):
    """'This is Claude...' beside a real guest: the guest joins, the AI never
    does, and a spelt-by-ear variant of the AI's name is dropped too. Uses
    the DEFAULT seeded participants (claude/gpt) - the out-of-the-box
    install is the one that must be protected."""
    with TestClient(app, base_url="http://127.0.0.1") as c:
        chat = c.post("/api/chats", json={"participant_ids": []}).json()
        introductions.apply_scan(
            chat["id"],
            {"introductions": ["Claude", "Alex"], "departures": []},
            {"user_name": "Shawn", "room_roster_max": 6})
        assert [p["name"] for p in _roster(chat["id"])] == ["Alex"]
        introductions.apply_scan(
            chat["id"],
            {"introductions": ["Clyde"], "departures": []},
            {"user_name": "Shawn", "room_roster_max": 6})
        assert [p["name"] for p in _roster(chat["id"])] == ["Alex"]


def test_participant_only_intro_changes_nothing(app):
    """An introduction verdict naming ONLY the AI participant leaves the
    room exactly as it was: no seat, and room mode stays off."""
    with TestClient(app, base_url="http://127.0.0.1") as c:
        chat = c.post("/api/chats", json={"participant_ids": []}).json()
        introductions.apply_scan(
            chat["id"],
            {"introductions": ["Claude"], "departures": []},
            {"user_name": "Shawn", "room_roster_max": 6})
        assert _roster(chat["id"]) == []


def test_seat_writer_refuses_exact_participant_names(app):
    """The last-ditch guard: db.add_room_person refuses a participant's
    exact slug or display name no matter which path asked, so no voice-path
    caller can seat an AI even if the scan layer is bypassed. Runs against
    the DEFAULT seeded participants."""
    with TestClient(app, base_url="http://127.0.0.1") as c:
        chat = c.post("/api/chats", json={"participant_ids": []}).json()
        con = db.connect()
        try:
            assert db.add_room_person(con, chat["id"], "claude") is None
            assert db.add_room_person(con, chat["id"], "Claude") is None
            assert db.add_room_person(con, chat["id"], "CLAUDE") is None
            assert db.add_room_person(con, chat["id"], "gpt") is None
            row = db.add_room_person(con, chat["id"], "Alex")
            assert row and row["name"] == "Alex"
        finally:
            con.close()
        assert [p["name"] for p in _roster(chat["id"])] == ["Alex"]


# ── naming hygiene: a relationship noun is never a name (#28 phase 4) ───────
#
# Second-field-test defect 1: "this is me, Sam, [the owner]'s wife" minted a
# roster person named "Wife", and the retest greeted "Wife?". The parser must
# prefer the proper name in the verdict, resolve a relationship-only
# introduction against REMEMBERED people, and otherwise ask - never mint a
# placeholder noun as somebody's identity.

def _flags(chat_id):
    con = db.connect()
    try:
        return db.get_room_flags(con, chat_id, open_only=True)
    finally:
        con.close()


def test_relationship_noun_truth_table():
    yes = ["Wife", "wife", "My Wife", "Husband", "Partner", "Mum", "Mom",
           "Dad", "Brother", "Sister", "Friend", "Mate", "Colleague", "Boss",
           "Neighbour", "Neighbor", "The Kids", "his wife", "Grandma",
           "Kids", "Flatmate"]
    no = ["Sam", "Alex", "Mary Rose", "Dave", "", None, "Sister Act",
          "Mateo",       # 'Mate' with a suffix is a real name
          "Bossman"]
    for n in yes:
        assert introductions.relationship_noun(n), n
    for n in no:
        assert not introductions.relationship_noun(n), n


def test_field_test_pin_kat_never_wife(app, utility):
    """THE pinned case, end to end through /send: 'this is me, Sam, Shawn's
    wife' must yield a person named Sam - never Wife - whatever mix the
    utility model returns."""
    utility["verdict"] = {"introductions": ["Sam", "Wife"], "departures": []}
    with TestClient(app, base_url="http://127.0.0.1") as c:
        chat = c.post("/api/chats", json={"participant_ids": []}).json()
        _send(c, chat["id"], "this is me, Sam, Shawn's wife")
        assert _wait_for(lambda: _chat_room_mode(chat["id"]))
        roster = _wait_for(lambda: _roster(chat["id"]))
        assert [p["name"] for p in roster] == ["Sam"]
        # and no surface anywhere holds a person called Wife
        assert anchors.store().find_by_name("Wife") is None
        assert _flags(chat["id"]) == []  # a named introduction asks nothing


def test_relationship_only_reidentifies_a_remembered_person(app):
    """'Sam's here with us - my wife' where the verdict carried only 'Wife':
    the utterance names a REMEMBERED person, so she is re-identified - no
    placeholder, no interruption."""
    store = anchors.store()
    pid = store.ensure_person("Sam")
    for _ in range(3):
        assert store.add_clip(pid, loud_pcm(2.0), 16000, source="accumulated")
    with TestClient(app, base_url="http://127.0.0.1") as c:
        chat = c.post("/api/chats", json={"participant_ids": []}).json()
        introductions.apply_scan(
            chat["id"], {"introductions": ["Wife"], "departures": []},
            {"user_name": "Shawn", "room_roster_max": 6},
            text="Sam's here with us - my wife")
        roster = _roster(chat["id"])
        assert [p["name"] for p in roster] == ["Sam"]
        assert roster[0]["person_id"] == pid  # remembered voice linked
        assert _flags(chat["id"]) == []


def test_relationship_only_with_no_match_asks_instead_of_minting(app):
    """'My wife is here' with nobody rememberable: room mode still flips on
    (someone IS present), but the roster gains NO placeholder - the
    ask-fallback opens instead, once, and a later real name resolves it."""
    with TestClient(app, base_url="http://127.0.0.1") as c:
        chat = c.post("/api/chats", json={"participant_ids": []}).json()
        cfg = {"user_name": "Shawn", "room_roster_max": 6}
        introductions.apply_scan(
            chat["id"], {"introductions": ["Wife"], "departures": []},
            cfg, text="my wife is here with us")
        assert _chat_room_mode(chat["id"]) is True
        assert _roster(chat["id"]) == []           # no Wife, no placeholder
        flags = _flags(chat["id"])
        assert [f["kind"] for f in flags] == ["unknown_voice"]
        # repeating the unnamed introduction does not stack a second ask
        introductions.apply_scan(
            chat["id"], {"introductions": ["Wife"], "departures": []},
            cfg, text="I said my wife is here")
        assert len(_flags(chat["id"])) == 1
        # the eventual name answers the ask and joins the roster
        introductions.apply_scan(
            chat["id"], {"introductions": ["Sam"], "departures": []},
            cfg, text="her name is Sam")
        assert [p["name"] for p in _roster(chat["id"])] == ["Sam"]
        assert _flags(chat["id"]) == []


def test_match_remembered_name_rules():
    known = ["Sam", "Dave", "Mateo"]
    # word-bounded, case-insensitive
    assert introductions.match_remembered_name(
        "sam is here, my wife", known) == "Sam"
    # a substring inside another word is not a mention
    assert introductions.match_remembered_name(
        "the catalogue arrived", known) == ""
    # excluded names (owner, already present) never match
    assert introductions.match_remembered_name(
        "Sam is here", known, exclude={"sam"}) == ""
    # two remembered names in one breath is ambiguous - the ask decides
    assert introductions.match_remembered_name(
        "Sam and Dave are here", known) == ""
    assert introductions.match_remembered_name("", known) == ""
    assert introductions.match_remembered_name("Sam is here", []) == ""


# ── the third field test (#28): two phrasings that silently never armed ─────
#
# 2026-08-08 evening, a fresh chat: room mode never armed for the whole
# session and the log showed NO introduction-scan activity at all. Both
# spoken triggers died at the lexical prefilter - the scan was never even
# scheduled, so there was no utility call, no warning, and nothing to
# distinguish "rejected" from "never ran". These are the exact phrasings
# (synthetic names), pinned end to end.

FIELD_HANDOVER = ("I'm gonna stop talking, and then I'm gonna hand over "
                  "to a guest")
FIELD_SELF_INTRO = "I'm here too. I'm Samantha, Alex's wife, also known as Sam"


def test_field_phrasings_pass_the_prefilter():
    """The root-cause pin: both field phrasings must be worth one utility
    call. Neither matched any pattern before this fix."""
    assert introductions.prefilter(FIELD_HANDOVER)
    assert introductions.prefilter(FIELD_SELF_INTRO)


def test_more_prefilter_shapes_for_handover_and_self_introduction():
    yes = [
        "I'll hand over to Sam now",
        "handing it over to my wife",
        "someone else wants to say hi",
        "Sam wants to say hello",
        "I'm Samantha, nice to meet you all",   # capitalised self-intro
        "hi, I am Dave",
        "my name is Samantha",
        "call me Sam",
        "Alex's wife is here",                 # possessive relationship
    ]
    no = [
        "I'm gonna grab a coffee",             # I'm + lowercase = not a name
        "I'm here to help with the plan",
        "let's plan the weekend",
        "what do you both think about the plan?",
    ]
    for t in yes:
        assert introductions.prefilter(t), t
    for t in no:
        assert not introductions.prefilter(t), t


def test_field_handover_arms_room_mode_and_asks_never_minting_guest(
        app, utility):
    """Phrasing 1 end to end: a relationship-only handover confirms as
    'Guest', which arms room mode, adds NO placeholder person, and raises
    the ask-fallback - the phase-4 rule, now reachable from the prefilter."""
    utility["verdict"] = {"introductions": ["Guest"], "departures": []}
    with TestClient(app, base_url="http://127.0.0.1") as c:
        chat = c.post("/api/chats", json={"participant_ids": []}).json()
        _send(c, chat["id"], FIELD_HANDOVER)
        assert _wait_for(lambda: _chat_room_mode(chat["id"]))
        assert utility["calls"]                      # the scan actually ran
        assert _roster(chat["id"]) == []             # no person named Guest
        assert anchors.store().find_by_name("Guest") is None
        flags = _wait_for(lambda: _flags(chat["id"]))
        assert [f["kind"] for f in flags] == ["unknown_voice"]


def test_field_handover_naming_a_remembered_person_reidentifies(app):
    """A handover that names a remembered person in the same breath
    re-identifies them - matched before any ask, per the phase-4 rule."""
    store = anchors.store()
    pid = store.ensure_person("Sam")
    for _ in range(3):
        assert store.add_clip(pid, loud_pcm(2.0), 16000, source="accumulated")
    with TestClient(app, base_url="http://127.0.0.1") as c:
        chat = c.post("/api/chats", json={"participant_ids": []}).json()
        introductions.apply_scan(
            chat["id"], {"introductions": ["Guest"], "departures": []},
            {"user_name": "Alex", "room_roster_max": 6},
            text="I'll hand over to our guest now - Sam, you there?")
        roster = _roster(chat["id"])
        assert [p["name"] for p in roster] == ["Sam"]
        assert roster[0]["person_id"] == pid
        assert _flags(chat["id"]) == []


def test_field_self_introduction_adds_the_person_with_their_alias(
        app, utility):
    """Phrasing 2 end to end: a guest introducing THEMSELVES with a proper
    name arms room mode, joins the roster under that name, and 'also known
    as Sam' becomes their preferred display name at creation."""
    utility["verdict"] = {"introductions": ["Samantha"], "departures": [],
                          "aliases": {"Samantha": "Sam"}}
    with TestClient(app, base_url="http://127.0.0.1") as c:
        chat = c.post("/api/chats", json={"participant_ids": []}).json()
        _send(c, chat["id"], FIELD_SELF_INTRO)
        assert _wait_for(lambda: _chat_room_mode(chat["id"]))
        roster = _wait_for(lambda: _roster(chat["id"]))
        assert [p["name"] for p in roster] == ["Samantha"]
        person = anchors.store().find_by_name("Samantha")
        assert person is not None
        assert person["preferred_name"] == "Sam"
        assert roster[0]["person_id"] == person["person_id"]
        assert _flags(chat["id"]) == []      # a named introduction asks nothing


# ── alias capture rules ─────────────────────────────────────────────────────

def test_parse_verdict_aliases_are_cleaned_and_bounded():
    v = introductions.parse_verdict(json.dumps({
        "introductions": ["Samantha", "Dave"],
        "departures": [],
        "aliases": {"Samantha": "Sam",
                    "Dave": "Wife",       # a relationship noun is no alias
                    "Nobody": "Nob",      # alias for a non-introduced name
                    "dave": "dave"},      # alias equal to the name says nothing
    }))
    assert v["aliases"] == {"Samantha": "Sam"}
    # a junk aliases value degrades to empty, never raises
    for bad in ('{"introductions": ["A"], "aliases": "Sam"}',
                '{"introductions": ["A"], "aliases": [1, 2]}',
                '{"introductions": ["A"], "aliases": {"A": 42}}'):
        assert introductions.parse_verdict(bad)["aliases"] == {}, bad


def test_call_me_alias_applies_at_creation_only(app):
    """An alias sets the preferred name when the person is CREATED; a later
    re-introduction with a different short form does not overwrite what may
    since have been corrected by hand."""
    with TestClient(app, base_url="http://127.0.0.1") as c:
        chat = c.post("/api/chats", json={"participant_ids": []}).json()
        cfg = {"user_name": "Alex", "room_roster_max": 6}
        introductions.apply_scan(
            chat["id"], {"introductions": ["Samantha"], "departures": [],
                         "aliases": {"Samantha": "Sam"}},
            cfg, text="I'm Samantha, call me Sam")
        person = anchors.store().find_by_name("Samantha")
        assert person["preferred_name"] == "Sam"
        introductions.apply_scan(
            chat["id"], {"introductions": ["Samantha"], "departures": [],
                         "aliases": {"Samantha": "Katie"}},
            cfg, text="I'm Samantha, call me Katie")
        person = anchors.store().find_by_name("Samantha")
        assert person["preferred_name"] == "Sam"   # first capture stands


def test_prompt_documents_self_introductions_handover_and_aliases():
    """The confirmation prompt was a suspected regression surface: it must
    tell the model that guests introduce themselves on the shared mic, that
    an unnamed handover confirms as the relationship word, and that short
    forms belong in the aliases key."""
    p = introductions.build_prompt("hello", "Alex", [])
    assert "aliases" in p
    assert "THEMSELVES" in p
    assert "hand over" in p


# ── the per-scan verdict line (#28 third field test, observability) ─────────
#
# Tonight's failure was INVISIBLE: no scan activity in the log, and nothing
# to say whether the scan rejected the turn or never ran. Every scan now
# ends in exactly one content-free INFO line with an allowlisted outcome.

def _verdict_lines(caplog):
    return [r.getMessage() for r in caplog.records
            if "introduction scan verdict" in r.getMessage()]


def test_every_scan_logs_one_allowlisted_verdict_line(app, utility, caplog):
    import logging as _logging
    with caplog.at_level(_logging.INFO, logger="crossband.introductions"):
        with TestClient(app, base_url="http://127.0.0.1") as c:
            chat = c.post("/api/chats", json={"participant_ids": []}).json()
            # 1. not introduction-shaped: the scan is not scheduled, and the
            #    verdict line SAYS so instead of leaving silence.
            _send(c, chat["id"], "let's plan the weekend")
            assert _wait_for(lambda: any(
                "outcome=no_prefilter_match" in m for m in _verdict_lines(caplog)))
            # 2. prefilter hit, model says no: model_rejected.
            utility["verdict"] = {"introductions": [], "departures": []}
            _send(c, chat["id"], "say hi to nobody in this story I'm telling")
            assert _wait_for(lambda: any(
                "outcome=model_rejected" in m for m in _verdict_lines(caplog)))
            # 3. a named introduction arms: armed.
            utility["verdict"] = {"introductions": ["Samantha"], "departures": []}
            _send(c, chat["id"], "say hi to Samantha, she's here with me")
            assert _wait_for(lambda: any(
                "outcome=armed" in m for m in _verdict_lines(caplog)))
            # 4. a second named introduction while already armed: roster_grew.
            utility["verdict"] = {"introductions": ["Dave"], "departures": []}
            _send(c, chat["id"], "and say hi to Dave too")
            assert _wait_for(lambda: any(
                "outcome=roster_grew" in m for m in _verdict_lines(caplog)))
            # 5. a departure: roster_shrank.
            utility["verdict"] = {"introductions": [], "departures": ["Dave"]}
            _send(c, chat["id"], "Dave has left the room")
            assert _wait_for(lambda: any(
                "outcome=roster_shrank" in m for m in _verdict_lines(caplog)))
    # every emitted outcome is allowlisted, and no line carries transcript
    # text or a person's name - content-free by construction
    for m in _verdict_lines(caplog):
        outcome = m.split("outcome=")[1].split()[0]
        assert outcome in introductions.SCAN_OUTCOMES, m
        for leak in ("Samantha", "Dave", "weekend", "story"):
            assert leak not in m, m


def test_relationship_only_scan_logs_ask_raised(app, utility, caplog):
    import logging as _logging
    with caplog.at_level(_logging.INFO, logger="crossband.introductions"):
        with TestClient(app, base_url="http://127.0.0.1") as c:
            chat = c.post("/api/chats", json={"participant_ids": []}).json()
            utility["verdict"] = {"introductions": ["Guest"], "departures": []}
            _send(c, chat["id"], FIELD_HANDOVER)
            assert _wait_for(lambda: any(
                "outcome=ask_raised" in m for m in _verdict_lines(caplog)))
    assert "Guest" not in " ".join(_verdict_lines(caplog))


def test_apply_scan_returns_its_outcome():
    """The outcome the verdict line logs is apply_scan's return value - pin
    the mapping on the pure-ish seam (no_change for an unknown chat)."""
    assert introductions.apply_scan(
        999999, {"introductions": ["Alex"], "departures": []},
        {"user_name": "Shawn"}) == "no_change"


def test_voice_match_name_compatible_truth_table():
    """#81: when may the introduction voice-arm rebind silently? Only when
    the introduced name is a plausible spelling of one of the matched
    person's names - identity, preferred, or merged - with both variant
    verdicts counting. Anything else is two humans until the owner says
    otherwise."""
    person = {"name": "Sonja", "preferred_name": "Sonny",
              "merged_names": ["Sonje"]}
    yes = ["Sonja", "sonja", "Sanya", "Sonny", "Sonje"]
    no = ["Faye", "Dave", "Alex", ""]
    for n in yes:
        assert introductions.voice_match_name_compatible(n, person), n
    for n in no:
        assert not introductions.voice_match_name_compatible(n, person), n
    assert not introductions.voice_match_name_compatible(
        "Anything", {"name": None, "merged_names": []})
