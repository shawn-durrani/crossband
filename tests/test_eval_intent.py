"""Tests for the spoken intent harness itself (crossband#258): the fixture
schema, the merged prompt and parser, today's path over the real prefilters
and parsers, the dropped-turn count, the scoring, and the keyless mock run.
No live model call anywhere; whether the utility model hears more under the
merged prompt is what `python -m eval_intent` measures against a key."""

import asyncio
import json

import pytest

from backend.intent import build_merged_prompt, parse_merged
from eval_intent import runner
from eval_intent.fixtures_loader import load_fixtures
from eval_intent.mock import MockCaller
from eval_intent.schema import Fixture, FixtureError, empty_verdict
from eval_intent.scoring import Result, aggregate, normalise
from eval_intent.today import merge_today, prefilters_fired, silent_misses, today_prompts


def _fx(fid):
    return next(f for f in load_fixtures() if f.id == fid)


# ---------- fixtures ----------

def test_builtin_corpus_loads_and_covers_the_named_cases():
    fixtures = load_fixtures()
    ids = [f.id for f in fixtures]
    assert len(ids) == len(set(ids))
    cats = {f.category for f in fixtures}
    assert {"depth_missed_wording", "depth_negative", "research",
            "research_negative", "two_intents", "plain_chat"} <= cats
    assert any(not f.has_intent for f in fixtures)
    assert any(ch["once"] for f in fixtures for ch in f.expected["depth"])


def test_corpus_grades_hold_back_requests_as_no_instruction():
    """The 25 September field test: "just eavesdrop until asked" was heard as
    room mode on, and "go to eavesdropping mode, we're just talking",
    mid-sentence, as the solo disarm. The corpus carries those shapes in
    made-up words, each graded as no instruction at all, plus the plain
    statements the disarm still has to hear."""
    hold = [f for f in load_fixtures() if f.category == "room_hold_back"]
    assert len(hold) >= 8
    assert all(not f.has_intent for f in hold)
    texts = " ".join(f.text.lower() for f in hold)
    for wording in ("eavesdrop", "just listen", "stay quiet", "silent mode",
                    "don't respond unless"):
        assert wording in texts, wording
    mid = _fx("hold_eavesdrop_mid_sentence")
    assert not mid.text[0].isupper()  # lands mid-sentence, as spoken
    assert _fx("room_off_alone_after_departure").expected["mode_command"] == "off"
    assert _fx("room_on_and_listen").expected["mode_command"] == "on"


def test_fixture_validation_rejects_bad_shapes():
    base = {"id": "x", "category": "c", "text": "hi", "expected": {}}
    Fixture.from_dict(base)
    with pytest.raises(FixtureError):
        Fixture.from_dict({**base, "expected": {"mode_command": "maybe"}})
    with pytest.raises(FixtureError):
        Fixture.from_dict({**base, "expected": {"depth": [{"seat": "all", "depth": "huge"}]}})
    with pytest.raises(FixtureError):
        Fixture.from_dict({**base, "expected": {"tier": "opus"}})
    with pytest.raises(FixtureError):
        Fixture.from_dict({"id": "y", "category": "c", "expected": {}})


def test_load_fixtures_rejects_duplicates_and_missing_dirs(tmp_path):
    (tmp_path / "a.json").write_text(json.dumps(
        [{"id": "plain_recipe", "category": "c", "text": "t", "expected": {}}]))
    with pytest.raises(FixtureError):
        load_fixtures(dirs=[str(tmp_path)])
    with pytest.raises(FixtureError):
        load_fixtures(dirs=[str(tmp_path / "nope")], include_builtin=False)


# ---------- today's path: the real lists and parsers ----------

def test_todays_lists_drop_the_named_wordings():
    for fid in ("depth_return_to_defaults", "depth_go_faster", "depth_keep_it_short",
                "depth_quicker_all", "depth_once_quick"):
        assert "depth" in silent_misses(_fx(fid)), fid
    assert silent_misses(_fx("depth_back_to_normal")) == []
    assert silent_misses(_fx("research_more")) == ["research"]
    assert silent_misses(_fx("plain_recipe")) == []
    assert "depth" in prefilters_fired("think harder please")


def test_todays_path_builds_only_the_prompts_whose_list_fires():
    prompts = today_prompts(_fx("both_group_and_intro"))
    assert set(prompts) == {"mode_command", "introductions"}
    assert today_prompts(_fx("depth_go_faster")) == {}
    heard = merge_today({"mode_command": '{"mode_command": "on"}',
                         "introductions": '{"introductions": ["Dave"], "departures": [], "aliases": {}}'})
    assert heard["mode_command"] == "on" and heard["introductions"] == ["Dave"]
    assert heard["research"] == "none"


# ---------- the merged prompt and parser ----------

def test_merged_prompt_carries_the_context_and_the_message():
    fx = _fx("correction_call_her")
    prompt = build_merged_prompt(fx.text, fx.user_name, fx.seats, fx.present,
                                 fx.known)
    assert "Samantha" in prompt and "Alex" in prompt and "Claude, GPT" in prompt
    assert prompt.endswith(fx.text)


def test_parse_merged_uses_the_apps_parsers_and_degrades_to_nothing():
    text = json.dumps({"mode_command": "off", "introductions": ["Sam"], "departures": [],
                       "aliases": {"Sam": "Sammy"},
                       "corrections": [{"who": "owner", "name": "Aleks", "also": ""}],
                       "depth": [{"seat": "Claude", "depth": "deep", "once": True}],
                       "research": "more"})
    heard = parse_merged("Sure: " + text)
    assert heard["mode_command"] == "off"
    assert heard["introductions"] == ["Sam"] and heard["aliases"] == {"Sam": "Sammy"}
    assert heard["corrections"][0]["name"] == "Aleks"
    assert heard["depth"] == [{"seat": "Claude", "depth": "deep", "once": True}]
    assert heard["research"] == "more"
    assert parse_merged("no json here") == empty_verdict()
    assert parse_merged('{"depth": "not a list", "research": "lots"}')["depth"] == []


# ---------- scoring ----------

def test_normalise_ignores_case_and_order_and_result_names_wrong_axes():
    fx = _fx("both_think_and_research")
    same = {**empty_verdict(), "depth": [{"seat": "claude", "depth": "deep", "once": False}],
            "research": "more"}
    assert normalise(same) == normalise(fx.expected)
    r = Result(fixture=fx, strategy="merged", heard={**same, "research": "none"})
    assert r.wrong_axes() == ["research"]


def test_aggregate_reports_the_dropped_turns_and_per_strategy_accuracy():
    fixtures = load_fixtures()
    silent = {f.id: silent_misses(f) for f in fixtures}
    results = [Result(fixture=f, strategy="merged", heard=f.expected, calls=1,
                      cost_usd=0.001, latency_s=0.5) for f in fixtures]
    report = aggregate(results, fixtures, silent)
    assert report["silent_today"]["turns"] >= 15
    assert report["silent_today"]["by_axis"]["research"] >= 6
    st = report["strategies"]["merged"]
    assert st["all_axes_right"] == 1.0 and st["calls_per_turn"] == 1.0
    assert st["cost_per_turn_usd"] == pytest.approx(0.001)
    assert "today" not in report["strategies"]


# ---------- the mock run ----------

def test_mock_run_compares_both_paths_and_renders(capsys):
    args = runner.build_arg_parser().parse_args(["--mock"])
    report = asyncio.run(runner.run(args))
    today, merged = report["strategies"]["today"], report["strategies"]["merged"]
    assert today["per_axis"]["research"] < 1.0        # no research path today
    assert today["per_category"]["depth_missed_wording"] == 0.0
    assert merged["per_axis"]["depth"] == 1.0
    assert merged["misses"], "the stand-in is wrong on a fixed few"
    assert runner.main(["--mock", "--format", "json"]) == 0
    json.loads(capsys.readouterr().out)


def test_mock_caller_answers_each_live_prompt_for_its_axis():
    fixtures = load_fixtures()
    caller = MockCaller(fixtures)
    fx = _fx("room_on_please")
    done = asyncio.run(caller(today_prompts(fx)["mode_command"], "mode_command"))
    assert json.loads(done.text) == {"mode_command": "on"}


def test_env_flag_loads_keys_over_a_blank(tmp_path, monkeypatch):
    env = tmp_path / ".env"
    env.write_text("ANTHROPIC_API_KEY=sk-made-up\n")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "")   # what a guest inherits
    runner.load_env(str(env))
    import os
    assert os.environ["ANTHROPIC_API_KEY"] == "sk-made-up"
    with pytest.raises(SystemExit):
        runner.load_env(str(tmp_path / "missing.env"))
