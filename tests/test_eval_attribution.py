"""Tests for the attribution replay harness itself (crossband#212): fixture
loading, the three projection shapes, answer parsing, scoring, and the
keyless mock run. No live model call anywhere; whether a real model answers
correctly under each shape is what `python -m eval_attribution --model`
measures against a key."""

import asyncio

import pytest

from backend import providers
from backend.llm_util import UtilityCompletion
from eval_attribution import projection, runner
from eval_attribution.fixtures_loader import load_fixtures
from eval_attribution.prompt import build_call, seat_cfg, timed_transcript
from eval_attribution.schema import Fixture, FixtureError
from eval_attribution.scoring import aggregate, parse_answer, score


def _fixture(fid="remark_ownership_photos"):
    return next(f for f in load_fixtures() if f.id == fid)


# ---------- fixtures ----------

def test_builtin_corpus_loads_with_unique_ids_and_valid_probes():
    fixtures = load_fixtures()
    assert len(fixtures) >= 6
    ids = [f.id for f in fixtures]
    assert len(ids) == len(set(ids))
    kinds = {p.kind for f in fixtures for p in f.probes}
    assert kinds == {"who", "yesno"}
    assert any(p.expected == "self" for f in fixtures for p in f.probes)
    assert {f.category for f in fixtures} >= {"remark_ownership", "addressed_you",
                                               "own_words_deep", "exclusivity",
                                               "owner_words"}


def test_every_who_probe_expects_a_speaker_who_actually_spoke():
    for f in load_fixtures():
        spoke = {t["speaker"] for t in f.transcript}
        for p in f.probes:
            if p.kind == "who":
                assert p.resolve_expected() in spoke, (f.id, p.tag)


def test_fixture_validation_rejects_bad_shapes():
    base = {"id": "x", "category": "c", "user_name": "Alex",
            "roster": [{"slug": "claude", "name": "Claude"}],
            "transcript": [{"speaker": "claude", "content": "hi"}],
            "probes": [{"seat": "claude", "question": "q", "expected": "gpt"}]}
    with pytest.raises(FixtureError):
        Fixture.from_dict(base)
    base["probes"] = [{"seat": "claude", "question": "q", "expected": "yes", "kind": "yesno"}]
    Fixture.from_dict(base)
    base["transcript"] = [{"speaker": "gpt", "content": "hi"}]
    with pytest.raises(FixtureError):
        Fixture.from_dict(base)


# ---------- projection shapes ----------

def _setup(cfg, fid="remark_ownership_photos", seat="gpt"):
    fx = _fixture(fid)
    c = seat_cfg(fx, cfg)
    transcript = timed_transcript(fx, start=1_700_000_000.0)
    return fx, c, transcript, seat


def test_current_shape_is_exactly_the_live_builders(cfg):
    fx, c, transcript, seat = _setup(cfg)
    for family, builder in (("anthropic", providers.build_anthropic_messages),
                            ("openai", providers.build_openai_input)):
        assert projection.project("current", family, seat, transcript, fx.names, c) \
            == builder(seat, transcript, fx.names, c)


def test_current_walk_reproduces_the_live_builders_byte_for_byte(cfg):
    """The local walk is what the other two shapes are built on, so it must
    agree with the real builders when asked for the current shape."""
    fx, c, transcript, seat = _setup(cfg)
    for family, builder in (("anthropic", providers.build_anthropic_messages),
                            ("openai", providers.build_openai_input)):
        assert projection._walk("current", family, seat, transcript, fx.names, c) \
            == builder(seat, transcript, fx.names, c)


def test_self_labelled_heads_own_turns_and_leaves_others_unchanged(cfg):
    fx, c, transcript, seat = _setup(cfg)
    cur = projection.project("current", "anthropic", seat, transcript, fx.names, c)
    lab = projection.project("self_labelled", "anthropic", seat, transcript, fx.names, c)
    assert len(cur) == len(lab)
    own = [(a, b) for a, b in zip(cur, lab) if a["role"] == "assistant"]
    assert own and all(b["content"].startswith("[GPT · ") for _, b in own)
    assert all(b["content"].endswith(a["content"]) for a, b in own)
    assert [a for a in cur if a["role"] == "user"] == [b for b in lab if b["role"] == "user"]


def test_envelope_wraps_other_seats_only_and_keeps_the_owner_plain(cfg):
    fx, c, transcript, seat = _setup(cfg)
    env = projection.project("envelope", "openai", seat, transcript, fx.names, c)
    texts = [m["content"][0]["text"] for m in env if m["role"] == "user"]
    assert texts[0].startswith("(Only Alex speaks as the user")
    assert any(t.startswith('<member name="Claude"') for t in texts)
    assert any(t.startswith("[Alex · ") for t in texts)
    assert not any(t.startswith("[Claude · ") for t in texts)
    assert all(m["content"] == t["content"] for m, t in
               zip([m for m in env if m["role"] == "assistant"],
                   [t for t in transcript if t["speaker"] == seat]))


def test_probe_rides_as_a_final_owner_turn_with_the_answer_rule(cfg):
    fx, c, _, seat = _setup(cfg)
    probe = fx.probes[0]
    system, messages = build_call(fx, probe, "current", "anthropic", c, start=1_700_000_000.0)
    last = messages[-1]["content"][0]["text"]
    assert last.startswith("[Alex · ") and probe.question in last
    assert "exactly one name from this list: Alex, Claude, GPT" in last
    assert "attribute it to whoever actually made it" in system


# ---------- parsing and scoring ----------

def test_parse_answer_maps_names_self_words_and_yes_no():
    fx = _fixture()
    who = next(p for p in fx.probes if p.kind == "who" and p.expected == "claude")
    assert parse_answer("Claude.", fx, who) == "claude"
    assert parse_answer("That was GPT, not Claude", fx, who) == "gpt"
    assert parse_answer("Alex did", fx, who) == "user"
    assert parse_answer("That was me.", fx, who) == who.seat
    assert parse_answer("", fx, who) is None
    yn = next(p for p in fx.probes if p.kind == "yesno")
    assert parse_answer("No, that was Claude.", fx, yn) == "no"
    assert parse_answer("Yes.", fx, yn) == "yes"


def test_score_records_failure_modes_without_counting_them_as_wrong(cfg):
    fx = _fixture()
    probe = fx.probes[0]
    keyless = score(fx, probe, "current", "claude-haiku-4-5",
                    UtilityCompletion(text=None), cfg)
    assert keyless.failure_mode == "missing_key" and keyless.correct is False
    timed = score(fx, probe, "current", "claude-haiku-4-5",
                  UtilityCompletion(text=None, timed_out=True), cfg)
    assert timed.failure_mode == "timeout"
    ok = score(fx, probe, "current", "claude-haiku-4-5",
               UtilityCompletion(text="Claude", input_tokens=10, output_tokens=2), cfg)
    assert ok.failure_mode == "ok" and ok.correct and ok.cost_usd is not None
    report = aggregate([keyless, timed, ok])
    assert report["accuracy"]["current|claude-haiku-4-5"] == 1.0
    assert report["missing_key"] == 1 and report["timeouts"] == 1


# ---------- the whole pipeline, keyless ----------

def test_mock_run_covers_every_variant_and_acts_out_the_hypothesis():
    args = runner.build_arg_parser().parse_args(["--mock"])
    report, models, variants = asyncio.run(runner.run(args))
    assert variants == list(projection.VARIANTS)
    for v in variants:
        assert report["accuracy"][f"{v}|{models[0]}"] is not None
    cur = report["self_accuracy"]["current|claude-haiku-4-5"]
    lab = report["self_accuracy"]["self_labelled|claude-haiku-4-5"]
    assert cur == 0.0 and lab > 0.9
    assert report["missing_key"] == 0


def test_keyless_real_run_reports_missing_key_instead_of_crashing(monkeypatch):
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    args = runner.build_arg_parser().parse_args(["--variant", "current",
                                                 "--model", "claude-haiku-4-5"])
    report, _, _ = asyncio.run(runner.run(args))
    assert report["missing_key"] == report["n_results"] > 0
    assert report["accuracy"]["current|claude-haiku-4-5"] is None
