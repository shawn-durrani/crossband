"""The interruption judge trial (eval_bargein), keyless.

Pins: the made-up moments load, each with a right answer, a real seat and
a speaker who's in the room; they name only the made-up roster; every
judge reads the same description of a moment; a written answer and Jev's
answer are read the same way however they come back; the scores count a
wrong cut and a missed interruption apart; "rules, then X" asks the judge
only where the rules cut on word count alone; and a run with the stand-in
judge reports every moment right. The word rules themselves run through
Node, so that check is skipped where Node isn't installed.
"""

import json
import re
import shutil

import pytest

from eval_bargein import judge, runner


def test_the_moments_load_with_answers_seats_and_speakers():
    cases = judge.load_cases()
    assert len(cases) >= 40
    assert {c.expected for c in cases} == set(judge.LABELS)
    assert all(c.seat in judge.SEATS for c in cases)


def test_the_moments_name_only_the_made_up_roster():
    text = judge.CASES.read_text()
    for c in judge.load_cases():
        for name in c.room:
            assert name in judge.ROSTER
    # Any capitalised word that follows "said" or opens a remark and is a
    # name must come from the roster; spot-check the people fields.
    names = set(re.findall(r'"speaker": "([A-Z][a-z]+)"', text))
    assert names <= set(judge.ROSTER)


def test_every_judge_reads_the_same_description():
    c = judge.load_cases()[0]
    d = judge.describe(c)
    assert c.ai_said in d and c.words in d
    assert d in judge.prompt(c)
    assert judge.jev_body(c)["state"] == d
    q = judge.jev_body(c)["questions"]["verdict"]
    assert q["type"] == "choice" and set(q["criteria"]) == set(judge.LABELS)


def test_a_written_answer_is_read_whatever_comes_with_it():
    assert judge.parse_label("interrupt") == "interrupt"
    assert judge.parse_label("Aside.") == "aside"
    assert judge.parse_label("<think>maybe interrupt</think>\nlistening") == "listening"
    assert judge.parse_label("I'd say aside, not interrupt") == "aside"
    assert judge.parse_label("") == ""
    assert judge.parse_label(None) == ""


def test_jevs_answer_is_read_or_left_blank():
    ok = {"answers": {"verdict": {"type": "choice", "choice": "aside",
                                  "confidence": 0.91, "probabilities": {}}}}
    assert judge.parse_jev(ok) == ("aside", 0.91)
    assert judge.parse_jev({"answers": {"verdict": {"choice": "maybe"}}}) == ("", None)
    assert judge.parse_jev({}) == ("", None)
    assert judge.parse_jev(None) == ("", None)


def _a(id, expected, label, unclear=False, latency=0.1):
    return {"id": id, "category": "x", "expected": expected, "label": label,
            "latency_s": latency, "cost_usd": 0.0, "confidence": None,
            "error": "", "unclear": unclear}


def test_scores_count_wrong_cuts_and_missed_interruptions_apart():
    s = runner.summarise([_a("1", "aside", "interrupt"), _a("2", "interrupt", "aside"),
                          _a("3", "listening", "aside"), _a("4", "interrupt", "interrupt")])
    assert (s["right"], s["cut_right"]) == (1, 2)
    assert (s["false_cuts"], s["of_not_cut"]) == (1, 2)
    assert (s["missed_cuts"], s["of_cut"]) == (1, 2)


def test_behind_the_rules_the_judge_is_asked_only_about_word_count_cuts():
    rules = [_a("1", "aside", "interrupt", unclear=True),
             _a("2", "interrupt", "interrupt", unclear=False)]
    judged = [_a("1", "aside", "aside", latency=0.5), _a("2", "interrupt", "aside")]
    got = runner.with_rules(rules, judged)
    assert [g["label"] for g in got] == ["aside", "interrupt"]
    assert [g["latency_s"] for g in got] == [0.5, 0.0]


def test_a_stand_in_run_reports_every_moment_right(tmp_path, capsys):
    out = tmp_path / "r.json"
    assert runner.main(["--mock", "--json-out", str(out)]) == 0
    report = json.loads(out.read_text())
    s = report["mock"]
    assert s["right"] == s["cases"] and s["false_cuts"] == 0 and s["missed_cuts"] == 0
    assert "# Judge trial" in capsys.readouterr().out


def test_jev_without_a_key_reports_errors_not_answers(monkeypatch):
    import asyncio
    monkeypatch.delenv("TYPESAFE_API_KEY", raising=False)
    cases = judge.load_cases()[:2]
    got = asyncio.run(runner.judge_jev(cases, 5))
    assert all(a["error"] == "no TYPESAFE_API_KEY" and a["label"] == "" for a in got)


@pytest.mark.skipif(not shutil.which("node"), reason="Node isn't installed")
def test_the_word_rules_never_miss_a_real_interruption_here():
    s = runner.summarise(runner.judge_rules(judge.load_cases()))
    assert s["missed_cuts"] == 0
    assert s["errors"] == 0
