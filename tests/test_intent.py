"""The merged intent scan (#258, #412): backend/intent.py's prompt, parser
and "heard but changed nothing" wording, tested without a model.

1. build_merged_prompt carries every input and ends with the message.
2. parse_merged reads every axis off one reply and degrades to nothing
   heard on bad JSON, the same defensive posture as the four parsers it
   delegates to.
3. nothing_changed_line, for each no-op case #412 names: a room-mode
   command that named the state the room was already in, an introduction of
   someone already present, a correction that resolved to nobody, a depth
   instruction that named no known seat, and a research cue heard while the
   mode is already on. A real change on any axis, or nothing confirmed at
   all, gets no line.
4. schedule_scan's two cheap guards: an empty turn and a `/` command never
   reach the model.
5. The 25 September field test: asking the seats to hold back ("just
   eavesdrop", "go to eavesdropping mode, we're just talking") was read as
   a room-mode command twice. The prompt now says a hold back request is no
   instruction at all and that "off" needs a plain statement, and a spoken
   room change posts mode_changed_line. Whether the model obeys is measured
   by eval_intent's room_hold_back fixtures, not here.
6. The 26 September field test (#474): a word spelt out letter by letter
   to fix the transcript was heard as a name correction. The prompt now
   says spelling a word out is no correction unless the word is a
   person's name. Whether the model obeys is measured by eval_intent's
   spelling fixtures. Since #494 a plain rule after the parse holds the
   line too, pinned in tests/test_spelling_guard.py.
"""

import json

from backend import intent, introductions


# ---------- build_merged_prompt ----------

def test_merged_prompt_carries_every_input_and_ends_with_the_message():
    p = intent.build_merged_prompt("her name is spelt Aleks", "Alex",
                                   ["Claude", "GPT"], ["Sam"], ["Sam", "Dave"])
    assert "Alex" in p
    assert "Claude, GPT" in p
    assert "Sam" in p and "Dave" in p
    assert p.endswith("her name is spelt Aleks")


def test_merged_prompt_handles_empty_lists():
    p = intent.build_merged_prompt("hello", "Alex", [], [], [])
    assert "(none)" in p           # no seats
    assert "(nobody yet)" in p     # no present, no known


def test_merged_prompt_says_a_hold_back_request_is_no_instruction():
    """The regression surface for the two misreads: the wordings the owner
    used are named as hold back requests, they count on no axis wherever
    they sit in a turn, and a named command beside one still counts."""
    p = intent.build_merged_prompt("hello", "Alex", ["Claude"], [], [])
    assert "HOLD BACK" in p
    for wording in ("eavesdrop", "just listen", "stay quiet",
                    "you don't need to respond", "eavesdropping mode"):
        assert wording in p, wording
    assert "wherever it sits in the message" in p
    assert "Anything else the same message asks still counts" in p


def test_merged_prompt_makes_both_directions_need_a_plain_statement():
    """"On" needs the mode named (#413's rule, now covering people talking
    among themselves too). The sacred disarm empties the room and stops
    automatic re-arming, so "off" needs solo or room mode off by name, or
    the owner saying they are alone, and "we're just talking" is named as
    the opposite."""
    p = intent.build_merged_prompt("hello", "Alex", ["Claude"], [], [])
    assert "return \"none\" unless the mode is named" in p
    assert "\"off\" needs an unambiguous statement" in p
    assert "alone now" in p
    assert "we're just talking" in p and "the opposite of alone" in p
    assert "When unsure, \"none\"" in p


def test_merged_prompt_says_a_spelt_out_word_is_no_correction():
    p = intent.build_merged_prompt("hello", "Alex", ["Claude"], [], [])
    assert "spoken and transcribed" in p
    assert "spelling one out is a transcript fix, never a correction" in p
    assert "whoever the message is spoken to" in p
    assert "does not make the spelt word their name" in p
    assert "a word the message calls a thing" in p
    assert "Only a word that is a person's name counts" in p
    assert "with their name spelt out is an introduction" in p
    assert "spelt out letters joined" in p
    assert "never the same name spelt out" in p


# ---------- parse_merged ----------

def test_parse_merged_reads_every_axis():
    text = json.dumps({
        "mode_command": "off", "introductions": ["Sam"], "departures": [],
        "aliases": {"Sam": "Sammy"},
        "corrections": [{"who": "owner", "name": "Aleks", "also": ""}],
        "depth": [{"seat": "Claude", "depth": "deep", "once": True}],
        "research": "more"})
    heard = intent.parse_merged("Sure: " + text)
    assert heard["mode_command"] == "off"
    assert heard["introductions"] == ["Sam"]
    assert heard["aliases"] == {"Sam": "Sammy"}
    assert heard["corrections"][0]["name"] == "Aleks"
    assert heard["depth"] == [{"seat": "Claude", "depth": "deep", "once": True}]
    assert heard["research"] == "more"


def test_parse_merged_degrades_to_nothing_on_bad_json():
    assert intent.parse_merged("no json here") == intent.empty_verdict()
    assert intent.parse_merged(None) == intent.empty_verdict()
    assert intent.parse_merged("") == intent.empty_verdict()
    out = intent.parse_merged('{"depth": "not a list", "research": "lots"}')
    assert out["depth"] == []
    assert out["research"] == "none"


# ---------- nothing_changed_line ----------

def test_line_for_a_room_mode_command_that_named_the_current_state():
    verdict = {**intent.empty_verdict(), "mode_command": "on"}
    line = intent.nothing_changed_line(verdict, {"mode_command": "no_change"})
    assert "room mode" in line and "nothing changed" in line


def test_line_for_an_introduction_of_someone_already_present():
    verdict = {**intent.empty_verdict(), "introductions": ["Alex"]}
    line = intent.nothing_changed_line(verdict, {"introductions": "no_change"})
    assert "introduction" in line and "already present" in line


def test_line_for_a_departure_that_matched_nobody_present():
    verdict = {**intent.empty_verdict(), "departures": ["Alex"]}
    line = intent.nothing_changed_line(verdict, {"introductions": "no_change"})
    assert "left" in line and "nothing changed" in line


def test_line_for_a_correction_that_resolved_to_nobody():
    verdict = {**intent.empty_verdict(),
              "corrections": [{"who": "", "name": "Aleks"}]}
    line = intent.nothing_changed_line(
        verdict, {"corrections": "correction_unmatched"})
    assert "correction" in line and "nothing changed" in line


def test_line_for_a_depth_instruction_that_named_no_known_seat():
    verdict = {**intent.empty_verdict(),
              "depth": [{"seat": "Mysteron", "depth": "deep", "once": False}]}
    line = intent.nothing_changed_line(verdict, {"depth": "no_change"})
    assert "thinking depth" in line and "nothing changed" in line


def test_line_for_research_heard_while_already_on():
    verdict = {**intent.empty_verdict(), "research": "more"}
    line = intent.nothing_changed_line(verdict, {"research": "no_change"})
    assert "research" in line and "already on" in line


def test_no_line_when_something_actually_changed():
    verdict = {**intent.empty_verdict(), "mode_command": "on"}
    assert intent.nothing_changed_line(
        verdict, {"mode_command": "armed_by_command"}) == ""


def test_no_line_when_nothing_was_confirmed_at_all():
    assert intent.nothing_changed_line(intent.empty_verdict(), {}) == ""


def test_no_line_when_one_axis_changed_and_another_did_not():
    """A real change anywhere means the verdict line already reports it -
    the no-op on the other axis gets no separate mention."""
    verdict = {**intent.empty_verdict(), "mode_command": "on",
              "depth": [{"seat": "all", "depth": "deep", "once": False}]}
    outcomes = {"mode_command": "armed_by_command", "depth": "no_change"}
    assert intent.nothing_changed_line(verdict, outcomes) == ""


def test_line_names_the_first_no_op_axis_when_two_axes_are_both_no_ops():
    verdict = {**intent.empty_verdict(), "mode_command": "on",
              "depth": [{"seat": "all", "depth": "deep", "once": False}]}
    outcomes = {"mode_command": "no_change", "depth": "no_change"}
    line = intent.nothing_changed_line(verdict, outcomes)
    assert "room mode" in line and "thinking depth" not in line


# ---------- mode_changed_line (the 25 September field test) ----------

def test_changed_line_for_an_arm_names_the_undo():
    line = intent.mode_changed_line("on")
    assert "room mode on" in line and "it's on now" in line
    assert 'Say "room mode off"' in line


def test_changed_line_for_a_disarm_that_emptied_the_room():
    line = intent.mode_changed_line("off", was_on=True, cleared_roster=True)
    assert "it's off now" in line
    assert "nobody is listed in the room" in line
    assert "won't switch itself back on" in line
    assert 'Say "room mode on"' in line


def test_changed_line_for_a_disarm_with_nobody_seated_says_nothing_of_a_list():
    line = intent.mode_changed_line("off", was_on=True, cleared_roster=False)
    assert "it's off now" in line and "listed" not in line


def test_changed_line_for_a_disarm_in_a_room_already_off():
    line = intent.mode_changed_line("off", was_on=False)
    assert "already off" in line and "won't switch itself back on" in line
    assert 'Say "room mode on"' in line


def test_changed_lines_are_plain_words():
    """House style for a line the owner reads: no dashes, no semicolons."""
    for line in (intent.mode_changed_line("on"),
                 intent.mode_changed_line("off", was_on=True,
                                          cleared_roster=True),
                 intent.mode_changed_line("off", was_on=False)):
        assert "\u2014" not in line and " - " not in line and ";" not in line


# ---------- schedule_scan's two cheap guards ----------

def test_empty_turn_never_schedules_the_scan(caplog):
    import logging
    with caplog.at_level(logging.INFO, logger="crossband.introductions"):
        assert introductions.schedule_scan(1, None, "   ", {}) is None
        assert introductions.schedule_scan(1, None, "", {}) is None
    lines = [r.getMessage() for r in caplog.records
             if "introduction scan verdict" in r.getMessage()]
    assert all("outcome=no_prefilter_match" in m for m in lines)
    assert len(lines) == 2


def test_slash_command_never_schedules_the_scan(caplog):
    import logging
    with caplog.at_level(logging.INFO, logger="crossband.introductions"):
        assert introductions.schedule_scan(
            1, None, "/deploy crossband", {}) is None
    lines = [r.getMessage() for r in caplog.records
             if "introduction scan verdict" in r.getMessage()]
    assert lines and "outcome=no_prefilter_match" in lines[0]


def test_an_ordinary_turn_is_not_caught_by_either_guard(monkeypatch):
    """Everything else is scheduled - proven by NOT hitting the guard, since
    scheduling the real coroutine needs a running loop this sync test has
    none of."""
    scheduled = []
    monkeypatch.setattr(introductions.asyncio, "get_running_loop",
                        lambda: (_ for _ in ()).throw(RuntimeError("no loop")))
    try:
        introductions.schedule_scan(1, None, "let's plan the weekend", {})
    except RuntimeError as e:
        scheduled.append(e)
    assert scheduled, "an ordinary turn must reach the scheduling step"
