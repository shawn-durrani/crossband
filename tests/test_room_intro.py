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


@pytest.fixture
def app(tmp_path):
    diarize._ROOM_ENABLED.clear()
    diarize._STASHED.clear()
    settings = Settings(data_dir=str(tmp_path / "data"),
                        memory_url="http://127.0.0.1:1")
    return create_app(settings)


def _wait_for(pred, timeout=6.0, interval=0.02):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        v = pred()
        if v:
            return v
        time.sleep(interval)
    return pred()


def _chat_room_mode(chat_id):
    con = db.connect()
    try:
        row = con.execute("SELECT room_mode FROM chats WHERE id=?",
                          (chat_id,)).fetchone()
        return bool(row and row["room_mode"])
    finally:
        con.close()


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

    monkeypatch.setattr("backend.llm_util.utility_complete", fake_utility)
    return state


def loud_pcm(seconds, sample_rate=16000):
    return b"\x00\x40" * int(seconds * sample_rate)


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
        assert _wait_for(lambda: _chat_room_mode(chat["id"]))
        roster = _roster(chat["id"])
        assert [p["name"] for p in roster] == ["Alex"]
        assert roster[0]["person_id"] == ""  # anchor pending - nothing heard yet
        # the live mirror the STT relay reads at commit boundaries
        assert diarize.room_enabled(chat["id"]) is True


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
    assert ok == {"introductions": ["Alex", "Dave"], "departures": []}
    for bad in (None, "", "not json", "{}", '{"introductions": "Alex"}',
                '{"introductions": [42, null]}', "[1,2]"):
        v = introductions.parse_verdict(bad)
        assert v == {"introductions": [], "departures": []}, bad


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


# ── naming hygiene: a relationship noun is never a name (#28 phase 4) ───────
#
# Second-field-test defect 1: "this is me, Kat, [the owner]'s wife" minted a
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
    no = ["Kat", "Alex", "Mary Rose", "Dave", "", None, "Sister Act",
          "Mateo",       # 'Mate' with a suffix is a real name
          "Bossman"]
    for n in yes:
        assert introductions.relationship_noun(n), n
    for n in no:
        assert not introductions.relationship_noun(n), n


def test_field_test_pin_kat_never_wife(app, utility):
    """THE pinned case, end to end through /send: 'this is me, Kat, Shawn's
    wife' must yield a person named Kat - never Wife - whatever mix the
    utility model returns."""
    utility["verdict"] = {"introductions": ["Kat", "Wife"], "departures": []}
    with TestClient(app, base_url="http://127.0.0.1") as c:
        chat = c.post("/api/chats", json={"participant_ids": []}).json()
        _send(c, chat["id"], "this is me, Kat, Shawn's wife")
        assert _wait_for(lambda: _chat_room_mode(chat["id"]))
        roster = _wait_for(lambda: _roster(chat["id"]))
        assert [p["name"] for p in roster] == ["Kat"]
        # and no surface anywhere holds a person called Wife
        assert anchors.store().find_by_name("Wife") is None
        assert _flags(chat["id"]) == []  # a named introduction asks nothing


def test_relationship_only_reidentifies_a_remembered_person(app):
    """'Kat's here with us - my wife' where the verdict carried only 'Wife':
    the utterance names a REMEMBERED person, so she is re-identified - no
    placeholder, no interruption."""
    store = anchors.store()
    pid = store.ensure_person("Kat")
    for _ in range(3):
        assert store.add_clip(pid, loud_pcm(2.0), 16000, source="accumulated")
    with TestClient(app, base_url="http://127.0.0.1") as c:
        chat = c.post("/api/chats", json={"participant_ids": []}).json()
        introductions.apply_scan(
            chat["id"], {"introductions": ["Wife"], "departures": []},
            {"user_name": "Shawn", "room_roster_max": 6},
            text="Kat's here with us - my wife")
        roster = _roster(chat["id"])
        assert [p["name"] for p in roster] == ["Kat"]
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
            chat["id"], {"introductions": ["Kat"], "departures": []},
            cfg, text="her name is Kat")
        assert [p["name"] for p in _roster(chat["id"])] == ["Kat"]
        assert _flags(chat["id"]) == []


def test_match_remembered_name_rules():
    known = ["Kat", "Dave", "Mateo"]
    # word-bounded, case-insensitive
    assert introductions.match_remembered_name(
        "kat is here, my wife", known) == "Kat"
    # a substring inside another word is not a mention
    assert introductions.match_remembered_name(
        "the catalogue arrived", known) == ""
    # excluded names (owner, already present) never match
    assert introductions.match_remembered_name(
        "Kat is here", known, exclude={"kat"}) == ""
    # two remembered names in one breath is ambiguous - the ask decides
    assert introductions.match_remembered_name(
        "Kat and Dave are here", known) == ""
    assert introductions.match_remembered_name("", known) == ""
    assert introductions.match_remembered_name("Kat is here", []) == ""
