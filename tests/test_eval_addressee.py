"""The addressee trial (eval_addressee), keyless.

Pins: the made-up turns load, each with a right answer on all three
questions, and name only the made-up roster; Jev and Claude read the same
description of a turn, and Jev's request asks the three choices; the
answers are read however they come back and hang together, so a turn not
for the AIs owes them nothing and names no seat; today's rules send every
turn to the AIs and owe a reply on a "?" or a seat's name; the scores count
a skipped question, a needless round, a forced reply and a missed one
apart; and a run with the stand-in judge reports every turn right.
"""

import asyncio
import re

from backend import addressee
from eval_addressee import runner


def test_the_turns_load_with_answers_on_all_three():
    cases = runner.load_cases()
    assert len(cases) >= 40
    assert {c["expected"]["for"] for c in cases} == set(addressee.FOR)
    assert {c["expected"]["asks"] for c in cases} == set(addressee.ASKS)
    assert {"all", "none"} <= {c["expected"]["seat"] for c in cases}


def test_the_turns_name_only_the_made_up_roster():
    text = runner.CASES.read_text()
    for c in runner.load_cases():
        assert set(c["room"]) <= set(runner.ROSTER)
        assert c["who"] in {*runner.ROSTER, "tv", "unknown"}
    speakers = set(re.findall(r'"who": "([A-Z][a-z]+)"', text))
    assert speakers <= {*runner.ROSTER, *runner.SEATS}


def test_both_models_read_the_same_description():
    c = runner.load_cases()[0]
    state = runner.state_of(c)
    assert c["text"] in state and "Alex, the owner" in state
    assert state in addressee.claude_prompt(state, list(runner.SEATS))
    qs = addressee.jev_questions(list(runner.SEATS))
    assert set(qs) == {"for", "asks", "seat"}
    assert set(qs["for"]["criteria"]) == set(addressee.FOR)
    assert set(qs["seat"]["criteria"]) == {"all", "none", *runner.SEATS}
    assert addressee.describe("Alex", ["Claude"], ["Alex"], [], "tv", "news") \
        .endswith('Now a voice the app knows is the TV says: "news"')


def test_the_answers_hang_together():
    seats = list(runner.SEATS)

    def jev(f, a, s):
        return addressee.parse_jev({"for": {"choice": f}, "asks": {"choice": a},
                                    "seat": {"choice": s}}, seats)
    assert jev("someone", "yes", "Claude") == {"for": "someone", "asks": "no",
                                               "seat": "none"}
    assert jev("ais", "yes", "none") == {"for": "ais", "asks": "yes", "seat": "all"}
    assert jev("ais", "no", "GPT") == {"for": "ais", "asks": "no", "seat": "GPT"}
    assert addressee.parse_jev({}, seats) == {"for": "", "asks": "", "seat": ""}
    assert addressee.parse_jev({"for": {"choice": "maybe"}}, seats)["for"] == ""


def test_a_written_answer_is_read_whatever_comes_with_it():
    seats = list(runner.SEATS)
    got = addressee.parse_claude('Sure.\n{"for": "ais", "asks": "yes", "seat": "GPT"}',
                                 seats)
    assert got == {"for": "ais", "asks": "yes", "seat": "GPT"}
    assert addressee.parse_claude("not json", seats) == {"for": "", "asks": "",
                                                         "seat": ""}
    assert addressee.parse_claude(None, seats)["for"] == ""


def test_todays_rules_send_every_turn_to_the_ais():
    cases = [
        {"id": "a", "category": "x", "text": "Claude, what's next?",
         "expected": {"for": "ais", "asks": "yes", "seat": "Claude"}},
        {"id": "b", "category": "x", "text": "Is the oven on?",
         "expected": {"for": "someone", "asks": "no", "seat": "none"}},
        {"id": "c", "category": "x", "text": "Wow, that smells good",
         "expected": {"for": "nobody", "asks": "no", "seat": "none"}},
    ]
    got = {a["id"]: a["got"] for a in runner.judge_rules(cases)}
    assert got["a"] == {"for": "ais", "asks": "yes", "seat": "Claude"}
    assert got["b"] == {"for": "ais", "asks": "yes", "seat": "all"}
    assert got["c"] == {"for": "ais", "asks": "no", "seat": "all"}


def _a(expected, got):
    return {"id": "x", "category": "k", "expected": expected, "got": got,
            "latency_s": 0.2, "cost_usd": 0.001, "error": ""}


def test_the_scores_count_each_mistake_apart():
    ais = {"for": "ais", "asks": "yes", "seat": "all"}
    person = {"for": "someone", "asks": "no", "seat": "none"}
    s = runner.summarise([
        _a(ais, dict(ais)),
        _a(ais, dict(person)),                                   # a skipped question
        _a(person, {"for": "ais", "asks": "no", "seat": "all"}),  # a needless round
        _a(person, {"for": "ais", "asks": "yes", "seat": "all"}),  # and a forced reply
    ])
    assert (s["all_right"], s["cases"]) == (1, 4)
    assert (s["wrong_skips"], s["of_for_ais"]) == (1, 2)
    assert (s["needless_rounds"], s["of_not_ais"]) == (2, 2)
    assert (s["forced_replies"], s["of_not_owed"]) == (1, 2)
    assert (s["missed_replies"], s["of_owed"]) == (1, 2)
    assert s["p50_ms"] == 200 and s["cost_usd"] == 0.004
    assert len(s["wrong"]) == 3


def test_a_stand_in_run_reports_every_turn_right(tmp_path):
    args = runner.build_arg_parser().parse_args(["--mock"])
    report = asyncio.run(runner.run(args))
    s = report["mock"]
    assert s["all_right"] == s["cases"] and s["wrong"] == []
    md = runner.render(report)
    assert md.startswith("# Is a turn for the AIs, and which of them?")
    assert f"| mock | {s['cases']}/{s['cases']} |" in md
