"""Tests for the silence/speak-vs-pass fixture eval (`eval_silence/`) --
no live API calls, no model judgment: this pins fixture schema validation and
the internal consistency of the contrast matrix (group check-in, resolved
factual question, roll-call, mid-debate paraphrase, direct address, and the
quiet family: asked to stay quiet, a stale answered question, room chatter,
and a named seat after a quiet request) against the general
relational-cost-of-silence principle now in backend/providers.py. It does
NOT test what a real model would actually say -- that needs live
conversations. tests/test_pass.py replays the quiet family through a real
round, to hold the pass guard to the same verdicts."""

import json

import pytest

from eval_silence.fixtures_loader import load_fixtures
from eval_silence.policy import decide
from eval_silence.schema import Fixture, FixtureError


# ---------- fixture loading ----------

def test_builtin_seed_corpus_loads_and_validates():
    fixtures = load_fixtures()
    ids = [fx.id for fx in fixtures]
    assert len(ids) == len(set(ids)), "fixture ids must be unique"
    categories = {fx.category for fx in fixtures}
    for expected in ("group_checkin", "resolved_factual_question", "roll_call",
                     "mid_debate_paraphrase", "direct_address",
                     "asked_to_stay_quiet", "stale_answered_question",
                     "room_chatter", "quiet_request_named_seat"):
        assert expected in categories, f"missing seed category {expected}"


def test_load_fixtures_missing_dir_raises(tmp_path):
    with pytest.raises(FixtureError):
        load_fixtures(dirs=[str(tmp_path / "nope")])


def test_load_fixtures_duplicate_id_raises(tmp_path):
    fx = {"id": "dup", "category": "c", "responder": "gpt",
          "conversation": [{"speaker": "user", "content": "hi"}],
          "informational_value": "low", "relational_cost_of_silence": "low",
          "expected_verdict": "pass"}
    (tmp_path / "a.json").write_text(json.dumps([fx]))
    (tmp_path / "b.json").write_text(json.dumps([fx]))
    with pytest.raises(FixtureError, match="duplicate"):
        load_fixtures(dirs=[str(tmp_path)], include_builtin=False)


def test_load_fixtures_external_dir_only(tmp_path):
    fx = {"id": "external-1", "category": "c", "responder": "gpt",
          "conversation": [{"speaker": "user", "content": "hi"}],
          "informational_value": "low", "relational_cost_of_silence": "high",
          "expected_verdict": "speak"}
    (tmp_path / "one.json").write_text(json.dumps(fx))
    fixtures = load_fixtures(dirs=[str(tmp_path)], include_builtin=False)
    assert [fx.id for fx in fixtures] == ["external-1"]


def test_missing_required_field_rejected():
    with pytest.raises(FixtureError, match="missing required field"):
        Fixture.from_dict({"id": "x", "category": "c", "responder": "gpt",
                           "conversation": [{"speaker": "user", "content": "hi"}],
                           "informational_value": "low",
                           "expected_verdict": "pass"})


def test_bad_verdict_enum_rejected():
    with pytest.raises(FixtureError):
        Fixture.from_dict({"id": "x", "category": "c", "responder": "gpt",
                           "conversation": [{"speaker": "user", "content": "hi"}],
                           "informational_value": "low",
                           "relational_cost_of_silence": "low",
                           "expected_verdict": "maybe"})


def test_bad_axis_value_rejected():
    with pytest.raises(FixtureError, match="informational_value"):
        Fixture.from_dict({"id": "x", "category": "c", "responder": "gpt",
                           "conversation": [{"speaker": "user", "content": "hi"}],
                           "informational_value": "medium",
                           "relational_cost_of_silence": "low",
                           "expected_verdict": "pass"})


def test_empty_conversation_rejected():
    with pytest.raises(FixtureError, match="conversation"):
        Fixture.from_dict({"id": "x", "category": "c", "responder": "gpt",
                           "conversation": [],
                           "informational_value": "low",
                           "relational_cost_of_silence": "low",
                           "expected_verdict": "pass"})


# ---------- policy consistency ----------

def test_decide_passes_only_when_both_axes_low():
    assert decide("low", "low") == "pass"
    assert decide("high", "low") == "speak"
    assert decide("low", "high") == "speak"
    assert decide("high", "high") == "speak"


def test_decide_rejects_bad_axis_value():
    with pytest.raises(ValueError):
        decide("medium", "low")


def test_every_seed_fixture_matches_the_general_principle():
    """The load-bearing assertion: each fixture's human-graded axes must
    imply its human-graded expected_verdict under the policy -- pass
    only when BOTH informational value and relational cost of silence are
    low. A fixture author changing one field without the other goes red
    here instead of silently drifting the matrix out of sync with the rule
    it illustrates."""
    for fx in load_fixtures():
        assert decide(fx.informational_value, fx.relational_cost_of_silence) == \
            fx.expected_verdict, fx.id


def test_redundancy_alone_does_not_decide_it():
    """The core point, pinned directly: the group check-in and the
    resolved factual question are both scenarios where the responder's
    honest reply would be functionally identical to one already given
    (would_be_redundant in spirit for both) -- yet they resolve oppositely,
    because whether the responder was addressed as part of the group is
    what matters, not the redundancy."""
    fixtures = {fx.id: fx for fx in load_fixtures()}
    checkin = fixtures["group_checkin_all_speak"]
    resolved = fixtures["resolved_factual_question_pass"]
    assert checkin.informational_value == resolved.informational_value == "low"
    assert checkin.expected_verdict == "speak"
    assert resolved.expected_verdict == "pass"
    assert checkin.relational_cost_of_silence != resolved.relational_cost_of_silence


def test_a_quiet_request_holds_until_a_seat_is_named():
    """The quiet family's contrast, pinned the way the check-in pair is:
    the same quiet request, the same nothing-new-to-say, and opposite
    verdicts. A question mark between two people in the room leaves the
    seats' silence unremarkable; naming a seat is the invitation back in.
    A quiet stretch the owner asked for never raises the cost of silence on
    its own."""
    fixtures = {fx.id: fx for fx in load_fixtures()}
    chatter = fixtures["asked_to_stay_quiet_pass"]
    named = fixtures["quiet_then_named_speak"]
    assert chatter.informational_value == named.informational_value == "low"
    assert chatter.expected_verdict == "pass"
    assert named.expected_verdict == "speak"
    assert chatter.relational_cost_of_silence == "low"
    assert named.relational_cost_of_silence == "high"


def test_an_answered_question_stays_answered():
    """A question both seats already answered doesn't come back as owed
    when a later turn happens to hold a question mark."""
    stale = {fx.id: fx for fx in load_fixtures()}["stale_question_pass"]
    assert stale.already_answered_by == ["claude", "gpt"]
    assert stale.expected_verdict == "pass"
    assert "?" in stale.conversation[-1]["content"]
