"""Room-mode commands (#28, chat 198): "Group mode, please" must DO something.

The field evidence: the owner said "Group mode, please" in a live voice
chat, the seats verbally agreed - and the app did nothing, because the turn
is not an introduction (the scan correctly logged no_prefilter_match). A
seat confirming a mode switch it cannot perform is a small dishonesty; these
tests pin the two halves of the fix.

1. THE LATENCY PIN, same law as every #28 scan: POST /send completes - the
   round is dispatched and the SSE stream fully drains - while the command
   confirmation is deliberately wedged open. Nothing about a command is ever
   awaited by dispatch.
2. The command paths: one merged utility call per turn (#412) hears the
   command among everything else; a confirmed ARM flips the chat's durable
   room mode on through the existing
   control plumbing (durable flag + diarize's live mirror), rosters the
   owner (linked to remembered anchors when they exist, seeded from the
   stashed utterance when the command was spoken); a confirmed DISARM flips
   it off, marks everyone still present left (the cap frees, the chip
   disappears) and resolves the open unknown-voice ask. Talk ABOUT the mode
   ("is group mode on?") confirms as none and changes nothing.
3. The verdict line: command scans end in the same single content-free
   verdict line as introduction scans, with the allowlist extended by
   armed_by_command / disarmed_by_command.
4. Grounded seat awareness: the engine hands each seat the chat's current
   room mode and present roster names, the volatile room-state line's
   inputs (the projection half is pinned in tests/test_projection.py and
   the cache safety in tests/test_cache_split.py).
"""

import asyncio
import json
import logging
import threading
import time

import pytest
from fastapi.testclient import TestClient

from backend import anchors, db, diarize, engine, introductions
from backend.app import create_app
from backend.config import Settings
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


def _flags(chat_id):
    con = db.connect()
    try:
        return db.get_room_flags(con, chat_id, open_only=True)
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
    """Mock the utility model at the llm_util seam. Since #412 every turn
    makes exactly ONE merged call, so state['reply'] is the one JSON verdict
    returned for it (introductions/departures default empty so a plain
    command reply still parses); state['gate'] (a threading.Event) wedges
    every call open; state['calls'] records every prompt."""
    state = {"reply": {"mode_command": "none", "introductions": [],
                       "departures": []},
             "gate": None, "calls": []}

    async def fake_utility(prompt, cfg, max_tokens=2000):
        state["calls"].append(prompt)
        if state["gate"] is not None:
            await asyncio.to_thread(state["gate"].wait, 10)
        return json.dumps(state["reply"])

    monkeypatch.setattr("backend.llm_util.utility_complete_with_usage",
                        as_utility_completion(fake_utility))
    return state


CFG = {"user_name": "Shawn", "room_roster_max": 6}


def _make_chat(client):
    return client.post("/api/chats", json={"participant_ids": []}).json()


def _verdict_lines(caplog):
    return [r.getMessage() for r in caplog.records
            if "introduction scan verdict" in r.getMessage()]


# ── 1. the latency pin ──────────────────────────────────────────────────────

def test_send_completes_while_the_command_confirm_is_wedged_open(app, utility):
    """The core law: the round is dispatched and done while the command
    confirmation is still blocked. Only after the gate opens does the chat's
    room mode change."""
    utility["reply"] = {"mode_command": "on"}
    utility["gate"] = threading.Event()
    with TestClient(app, base_url="http://127.0.0.1") as c:
        chat = _make_chat(c)
        _send(c, chat["id"], "Group mode, please")
        assert not utility["gate"].is_set()
        assert _chat_room_mode(chat["id"]) is False
        utility["gate"].set()  # release; the scan catches up out of band
        assert _wait_for(lambda: _chat_room_mode(chat["id"]))


# ── 2. the chat-198 pin, end to end ─────────────────────────────────────────

def test_group_mode_please_arms_end_to_end(app, utility, caplog):
    """THE field phrasing: "Group mode, please" flips the durable flag,
    mirrors it into diarize's registry so a live session's pass machinery
    starts at its next commit, rosters the owner, and logs
    armed_by_command."""
    utility["reply"] = {"mode_command": "on"}
    with caplog.at_level(logging.INFO, logger="crossband.introductions"):
        with TestClient(app, base_url="http://127.0.0.1") as c:
            chat = _make_chat(c)
            _send(c, chat["id"], "Group mode, please")
            assert _wait_for(lambda: _chat_room_mode(chat["id"]))
            assert diarize.room_enabled(chat["id"]) is True
            roster = _wait_for(lambda: _roster(chat["id"]))
            # the owner is the only person a command honestly knows present
            assert [p["name"] for p in roster] == ["User"]
            assert _wait_for(lambda: any(
                "outcome=armed_by_command" in m for m in _verdict_lines(caplog)))


def test_room_mode_off_disarms_end_to_end(app, utility, caplog):
    """"Room mode off": durable flag off, live mirror off, everyone still
    present marked left (the chip disappears), disarmed_by_command logged."""
    with caplog.at_level(logging.INFO, logger="crossband.introductions"):
        with TestClient(app, base_url="http://127.0.0.1") as c:
            chat = _make_chat(c)
            introductions.apply_scan(
                chat["id"], {"introductions": ["Alex"], "departures": []},
                CFG)
            assert _chat_room_mode(chat["id"]) is True
            utility["reply"] = {"mode_command": "off"}
            _send(c, chat["id"], "room mode off, thanks")
            assert _wait_for(lambda: not _chat_room_mode(chat["id"]))
            assert diarize.room_enabled(chat["id"]) is False
            assert _wait_for(lambda: not _roster(chat["id"]))
            gone = _roster(chat["id"], present_only=False)
            assert all(p["status"] == "left" for p in gone)
            assert _wait_for(lambda: any(
                "outcome=disarmed_by_command" in m
                for m in _verdict_lines(caplog)))
    # content-free: no transcript text, no names in any verdict line
    for m in _verdict_lines(caplog):
        outcome = m.split("outcome=")[1].split()[0]
        assert outcome in introductions.SCAN_OUTCOMES, m
        for leak in ("Alex", "thanks", "please"):
            assert leak not in m, m


def test_typed_and_spoken_commands_take_the_same_path(app, utility):
    """A TYPED "room mode on" arms exactly like a spoken one - both arrive
    through /send and the same scan. (No audio means no owner anchor seed,
    same as a typed introduction.)"""
    utility["reply"] = {"mode_command": "on"}
    with TestClient(app, base_url="http://127.0.0.1") as c:
        chat = _make_chat(c)
        _send(c, chat["id"], "turn on room mode")
        assert _wait_for(lambda: _chat_room_mode(chat["id"]))
        assert anchors.store().people() == []  # nothing stashed, nothing stored


def test_talk_about_the_mode_is_rejected_not_armed(app, utility, caplog):
    """"Is group mode on?" reaches the merged call same as any turn; the
    model confirms none, and nothing changes - the seat answers the question
    from the volatile room-state line, not by flipping anything."""
    utility["reply"] = {"mode_command": "none"}
    with caplog.at_level(logging.INFO, logger="crossband.introductions"):
        with TestClient(app, base_url="http://127.0.0.1") as c:
            chat = _make_chat(c)
            _send(c, chat["id"], "is group mode on right now?")
            assert _wait_for(lambda: any(
                "outcome=model_rejected" in m for m in _verdict_lines(caplog)))
            assert _chat_room_mode(chat["id"]) is False
            assert _roster(chat["id"]) == []


def test_keyless_command_scan_is_a_quiet_no_op(app):
    """No utility key: llm_util returns None, the command confirms as
    nothing, room mode stays off. (The explicit toggle keeps working.)"""
    with TestClient(app, base_url="http://127.0.0.1") as c:
        chat = _make_chat(c)
        _send(c, chat["id"], "group mode please")
        time.sleep(0.4)
        assert _chat_room_mode(chat["id"]) is False


def test_command_and_introduction_in_one_breath(app, utility, caplog):
    """"Group mode on - this is Dave": one merged call hears both a command
    and an introduction, both apply - the mode arms AND Dave joins the
    roster - and the single verdict line reports the command outcome."""
    utility["reply"] = {"mode_command": "on", "introductions": ["Dave"],
                        "departures": []}
    with caplog.at_level(logging.INFO, logger="crossband.introductions"):
        with TestClient(app, base_url="http://127.0.0.1") as c:
            chat = _make_chat(c)
            _send(c, chat["id"], "group mode on - this is Dave")
            assert _wait_for(lambda: _chat_room_mode(chat["id"]))
            roster = _wait_for(lambda: len(_roster(chat["id"])) == 2
                               and _roster(chat["id"]))
            assert sorted(p["name"] for p in roster) == ["Dave", "User"]
            lines = _wait_for(lambda: [
                m for m in _verdict_lines(caplog)
                if f"chat={chat['id']} " in m])
            assert len(lines) == 1  # ONE verdict line, even for both scans
            assert "outcome=armed_by_command" in lines[0]


def test_just_me_now_disarms_even_though_it_is_also_a_departure_shape(
        app, utility, caplog):
    """"It's just me now" is shaped like both a departure and a command. The
    command wins the scan: room mode off, everyone left, one
    disarmed_by_command line."""
    utility["reply"] = {"mode_command": "off"}
    with caplog.at_level(logging.INFO, logger="crossband.introductions"):
        with TestClient(app, base_url="http://127.0.0.1") as c:
            chat = _make_chat(c)
            introductions.apply_scan(
                chat["id"], {"introductions": ["Alex"], "departures": []},
                CFG)
            _send(c, chat["id"], "it's just me now")
            assert _wait_for(lambda: not _chat_room_mode(chat["id"]))
            assert _wait_for(lambda: not _roster(chat["id"]))
            assert _wait_for(lambda: any(
                "outcome=disarmed_by_command" in m
                for m in _verdict_lines(caplog)))


# ── 2b. a spoken room change is never silent (25 September field test) ─────
#
# The scan read "just eavesdrop" as the solo-mode disarm: room mode went off,
# everyone left the roster, automatic re-arming stopped, and nothing in the
# chat said so. Every spoken change now posts one line with what changed and
# the words that undo it, and a heard command that changed nothing keeps its
# own "nothing changed" line.

def _system_lines(chat_id):
    con = db.connect()
    try:
        return [m["content"] for m in db.get_chat_messages(con, chat_id)
                if m["speaker"] == "system"]
    finally:
        con.close()


def test_spoken_disarm_says_what_changed_and_how_to_undo_it(app, utility):
    utility["reply"] = {"mode_command": "off"}
    with TestClient(app, base_url="http://127.0.0.1") as c:
        chat = _make_chat(c)
        introductions.apply_scan(
            chat["id"], {"introductions": ["Sam"], "departures": []}, CFG)
        _send(c, chat["id"], "sorry, go to listening mode, we're chatting")
        lines = _wait_for(lambda: _system_lines(chat["id"]))
    assert len(lines) == 1, lines
    line = lines[0]
    assert "room mode off" in line and "it's off now" in line
    assert "nobody is listed in the room" in line
    assert "won't switch itself back on" in line
    assert 'Say "room mode on"' in line
    # built from what changed, never from the turn's words
    for leak in ("Sam", "listening", "chatting", "sorry"):
        assert leak not in line


def test_spoken_arm_says_what_changed_and_how_to_undo_it(app, utility):
    utility["reply"] = {"mode_command": "on"}
    with TestClient(app, base_url="http://127.0.0.1") as c:
        chat = _make_chat(c)
        _send(c, chat["id"], "group mode please")
        lines = _wait_for(lambda: _system_lines(chat["id"]))
    assert len(lines) == 1, lines
    assert "room mode on" in lines[0] and "it's on now" in lines[0]
    assert 'Say "room mode off"' in lines[0]


def test_disarm_in_a_room_already_off_still_says_re_arming_stopped(app):
    """Off but still re-arming by itself ("listening"): the command stops
    the automatic re-arm, which is a change, so the line says exactly
    that and nothing about a room list."""
    with TestClient(app, base_url="http://127.0.0.1"):
        chat_id = _bare_chat()
        assert introductions.apply_command(chat_id, "disarm", CFG) \
            == "disarmed_by_command"
        lines = _system_lines(chat_id)
    assert len(lines) == 1
    assert "already off" in lines[0]
    assert "won't switch itself back on" in lines[0]
    assert "listed in the room" not in lines[0]


def test_a_disarm_that_changes_nothing_keeps_the_nothing_changed_line(
        app, utility, caplog):
    utility["reply"] = {"mode_command": "off"}
    with caplog.at_level(logging.INFO, logger="crossband.introductions"):
        with TestClient(app, base_url="http://127.0.0.1") as c:
            chat = _make_chat(c)
            introductions.apply_command(chat["id"], "disarm", CFG)
            before = _system_lines(chat["id"])
            _send(c, chat["id"], "solo mode")
            lines = _wait_for(lambda: len(_system_lines(chat["id"]))
                              > len(before) and _system_lines(chat["id"]))
            assert _wait_for(lambda: any(
                f"chat={chat['id']} outcome=no_change" in m
                for m in _verdict_lines(caplog)))
    assert len(lines) == len(before) + 1
    assert "nothing changed" in lines[-1] and "already off" in lines[-1]


def test_an_arm_that_changes_nothing_logs_no_change_not_rejected(
        app, utility, caplog):
    """The 25 September log showed a heard "turn room mode on" in a room
    already on as model_rejected, which read as "nothing heard". It is
    no_change, beside its "nothing changed" line."""
    utility["reply"] = {"mode_command": "on"}
    with caplog.at_level(logging.INFO, logger="crossband.introductions"):
        with TestClient(app, base_url="http://127.0.0.1") as c:
            chat = _make_chat(c)
            introductions.apply_command(chat["id"], "arm", CFG)
            _send(c, chat["id"], "turn room mode on")
            assert _wait_for(lambda: any(
                f"chat={chat['id']} outcome=no_change" in m
                for m in _verdict_lines(caplog)))
            lines = _wait_for(lambda: len(_system_lines(chat["id"])) == 2
                              and _system_lines(chat["id"]))
    assert "it's on now" in lines[0]
    assert "nothing changed" in lines[1] and "already on" in lines[1]
    assert not any(f"chat={chat['id']} outcome=model_rejected" in m
                   for m in _verdict_lines(caplog))


# ── 3. apply_command, the seam ──────────────────────────────────────────────

def _bare_chat():
    con = db.connect()
    cur = con.execute("INSERT INTO chats(title, created_at, updated_at) "
                      "VALUES('t', 0, 0)")
    con.commit()
    chat_id = cur.lastrowid
    con.close()
    return chat_id


def test_arm_flips_and_rosters_the_owner_anchor_pending(app):
    with TestClient(app, base_url="http://127.0.0.1"):
        chat_id = _bare_chat()
        assert introductions.apply_command(chat_id, "arm", CFG) \
            == "armed_by_command"
        assert _chat_room_mode(chat_id) is True
        assert diarize.room_enabled(chat_id) is True
        roster = _roster(chat_id)
        assert [p["name"] for p in roster] == ["Shawn"]
        assert roster[0]["person_id"] == ""  # nothing heard yet
        # arm while already armed changes nothing
        assert introductions.apply_command(chat_id, "arm", CFG) == "no_change"


def test_spoken_arm_seeds_the_owner_anchor_from_the_stash(app):
    """The voice that spoke "group mode, please" is the owner's by design -
    exactly the introduction's rule, same stash, same claim-once."""
    with TestClient(app, base_url="http://127.0.0.1"):
        chat_id = _bare_chat()
        diarize.stash_utterance(chat_id, loud_pcm(2.0), 16000)
        assert introductions.apply_command(chat_id, "arm", CFG) \
            == "armed_by_command"
        people = anchors.store().people()
        assert [p["name"] for p in people] == ["Shawn"]
        assert people[0]["clip_count"] == 1
        roster = _roster(chat_id)
        assert [p["name"] for p in roster] == ["Shawn"]
        assert roster[0]["person_id"] == people[0]["person_id"]
        assert diarize.take_stashed_utterance(chat_id) is None  # claimed


def test_arm_links_a_remembered_owner(app):
    """An owner with remembered anchors rosters LINKED, so the pass runs
    anchored from the first utterance - re-identification, not re-learning."""
    with TestClient(app, base_url="http://127.0.0.1"):
        store = anchors.store()
        pid = store.ensure_person("Shawn")
        for _ in range(3):
            assert store.add_clip(pid, loud_pcm(2.0), 16000,
                                  source="accumulated")
        chat_id = _bare_chat()
        assert introductions.apply_command(chat_id, "arm", CFG) \
            == "armed_by_command"
        roster = _roster(chat_id)
        assert roster[0]["person_id"] == pid


def test_disarm_marks_everyone_left_and_resolves_the_ask(app):
    """Disarm is "back to solo": present people marked left (the cap frees,
    the chip disappears), the open unknown-voice ask resolved as moot, and
    mismatch flags kept - they doubt past turns, which going solo answers
    nothing about."""
    with TestClient(app, base_url="http://127.0.0.1"):
        chat_id = _bare_chat()
        introductions.apply_scan(
            chat_id, {"introductions": ["Alex", "Dave"], "departures": []},
            CFG)
        con = db.connect()
        msg = db.insert_message(con, chat_id, "user", "hello", notify=False)
        db.insert_room_flag(con, chat_id, "unknown_voice")
        db.insert_room_flag(con, chat_id, "mismatch",
                            message_id=msg["id"], label="Alex")
        con.close()
        assert introductions.apply_command(chat_id, "disarm", CFG) \
            == "disarmed_by_command"
        assert _chat_room_mode(chat_id) is False
        assert diarize.room_enabled(chat_id) is False
        assert _roster(chat_id) == []
        gone = _roster(chat_id, present_only=False)
        assert sorted(p["name"] for p in gone) == ["Alex", "Dave"]
        assert all(p["status"] == "left" and p["left_at"] for p in gone)
        assert [f["kind"] for f in _flags(chat_id)] == ["mismatch"]
        con = db.connect()
        try:
            assert db.get_chat_ambient_off(con, chat_id) is True
        finally:
            con.close()
        # A second disarm finds the room off AND automatic re-arming already
        # stopped: nothing changes, so it reports no_change and the scan's
        # "nothing changed" line speaks for it. (A disarm in a room that is
        # off but still re-arming IS a change - see the already-off test.)
        assert introductions.apply_command(chat_id, "disarm", CFG) \
            == "no_change"


def test_a_departure_left_person_can_rejoin_by_introduction(app):
    """Disarm does not burn identities: re-introducing a marked-left person
    re-marks them present with their person link intact."""
    with TestClient(app, base_url="http://127.0.0.1"):
        store = anchors.store()
        pid = store.ensure_person("Alex")
        for _ in range(3):
            assert store.add_clip(pid, loud_pcm(2.0), 16000,
                                  source="accumulated")
        chat_id = _bare_chat()
        introductions.apply_scan(
            chat_id, {"introductions": ["Alex"], "departures": []}, CFG)
        introductions.apply_command(chat_id, "disarm", CFG)
        assert _roster(chat_id) == []
        introductions.apply_scan(
            chat_id, {"introductions": ["Alex"], "departures": []}, CFG)
        assert _chat_room_mode(chat_id) is True
        roster = _roster(chat_id)
        assert [p["name"] for p in roster] == ["Alex"]
        assert roster[0]["person_id"] == pid


def test_apply_command_is_defensive():
    assert introductions.apply_command(999999, "arm", CFG) == "no_change"
    assert introductions.apply_command(999999, "disarm", CFG) == "no_change"


def test_unknown_direction_changes_nothing(app):
    with TestClient(app, base_url="http://127.0.0.1"):
        chat_id = _bare_chat()
        assert introductions.apply_command(chat_id, "sideways", CFG) \
            == "no_change"
        assert _chat_room_mode(chat_id) is False


# ── 4. grounded seat awareness: the engine's inputs ─────────────────────────

def test_round_state_carries_room_mode_and_present_names(app):
    """_load_round_state hands each seat the chat's present roster names
    exactly when room mode is on - the volatile room-state line's inputs.
    Present only: a marked-left person must not be told to the seats."""
    with TestClient(app, base_url="http://127.0.0.1"):
        chat_id = _bare_chat()
        state = engine._load_round_state(chat_id, None, 0)
        assert state["room_names"] == []
        introductions.apply_scan(
            chat_id, {"introductions": ["Alex", "Dave"], "departures": []},
            CFG)
        con = db.connect()
        db.mark_room_person_left(con, chat_id, "Dave")
        con.close()
        state = engine._load_round_state(chat_id, None, 0)
        assert bool(state["chat"]["room_mode"]) is True
        assert state["room_names"] == ["Alex"]
        introductions.apply_command(chat_id, "disarm", CFG)
        state = engine._load_round_state(chat_id, None, 0)
        assert state["room_names"] == []


# ── pure rules ──────────────────────────────────────────────────────────────

def test_command_prefilter_truth_table():
    yes = [
        "Group mode, please",
        "group mode",
        "room mode on",
        "turn on room mode",
        "multi-user mode",
        "multiuser mode",
        "we're in group mode",
        "switch to multi-person mode",
        "solo mode",
        "solo mode please",
        "room mode off",
        "back to solo",
        "just me now",
        "it's just me now",
        "is group mode on right now?",   # about-talk still prefilters;
                                         # the model is the judge
    ]
    no = [
        "let's plan the weekend",
        "what do you both think about the plan?",
        "the room was groovy",
        "a solo performance",
        "modes of transport",
        "there's a group of us going out later",
        "",
        None,
    ]
    for t in yes:
        assert introductions.command_prefilter(t), t
    for t in no:
        assert not introductions.command_prefilter(t), t


def test_command_prefilter_does_not_widen_the_introduction_prefilter():
    """The two prefilters stay independent gates: a bare mode command is not
    introduction-shaped, and an ordinary turn matches neither."""
    assert not introductions.prefilter("Group mode, please")
    assert not introductions.command_prefilter("say hi to Dave")
    assert not introductions.prefilter("let's plan the weekend")
    assert not introductions.command_prefilter("let's plan the weekend")


def test_parse_command_verdict_is_defensive():
    assert introductions.parse_command_verdict(
        'Sure! {"mode_command": "on"}') == "arm"
    assert introductions.parse_command_verdict(
        '{"mode_command": "off"}') == "disarm"
    for bad in (None, "", "not json", "{}", '{"mode_command": "none"}',
                '{"mode_command": 42}', '{"mode_command": ["on"]}',
                '[1, 2]', '{"mode": "on"}'):
        assert introductions.parse_command_verdict(bad) == "", bad


def test_command_prompt_documents_both_directions_and_about_talk():
    """The confirmation prompt is a regression surface, like the
    introduction prompt: it must state both directions, and that talking
    ABOUT the mode is neither."""
    p = introductions.build_command_prompt("group mode please")
    assert "mode_command" in p
    assert "ON" in p and "OFF" in p
    assert "ABOUT" in p
    assert "group mode please" in p


def test_command_outcomes_are_allowlisted():
    assert "armed_by_command" in introductions.SCAN_OUTCOMES
    assert "disarmed_by_command" in introductions.SCAN_OUTCOMES


# ── 5. the research cue, same merged scan (#253/#417) ───────────────────────

def _research_state(chat_id):
    con = db.connect()
    try:
        row = con.execute(
            "SELECT research_mode, research_set_by FROM chats WHERE id=?",
            (chat_id,)).fetchone()
        return bool(row["research_mode"]), row["research_set_by"]
    finally:
        con.close()


def test_scan_turns_research_mode_on_and_verdict_says_so(app, utility, caplog):
    """"Research more" reaches the merged call same as any turn; the mocked
    verdict applies through the research branch and the single verdict line
    reports it, same shape as a command or a depth change."""
    utility["reply"] = {"mode_command": "none", "introductions": [],
                        "departures": [], "research": "more"}
    with caplog.at_level(logging.INFO, logger="crossband.introductions"):
        with TestClient(app, base_url="http://127.0.0.1") as c:
            chat = _make_chat(c)
            _send(c, chat["id"], "research more please")
            assert _wait_for(lambda: _research_state(chat["id"])[0])
            assert _wait_for(lambda: any(
                "outcome=research_set" in m for m in _verdict_lines(caplog)))


def test_scan_research_already_on_gets_the_nothing_changed_line(app, utility,
                                                                caplog):
    utility["reply"] = {"mode_command": "none", "introductions": [],
                        "departures": [], "research": "more"}
    with TestClient(app, base_url="http://127.0.0.1") as c:
        chat = _make_chat(c)
        con = db.connect()
        try:
            db.set_chat_research(con, chat["id"], True, set_by="Shawn")
        finally:
            con.close()
        with caplog.at_level(logging.INFO, logger="crossband.introductions"):
            _send(c, chat["id"], "research more please")
            assert _wait_for(lambda: any(
                "outcome=no_change" in m for m in _verdict_lines(caplog)))
        con = db.connect()
        try:
            lines = [m["content"] for m in db.get_chat_messages(con, chat["id"])
                    if m["speaker"] == "system"]
        finally:
            con.close()
        assert any("already on" in ln for ln in lines)
