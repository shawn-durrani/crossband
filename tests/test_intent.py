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
