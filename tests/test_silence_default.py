"""Follow-up to the silence rule: the live incident after its first fix
(f3ce951) was the OPPOSITE failure -- a second participant kept repeating or
paraphrasing an already-adequate answer instead of passing. Root cause: the
stable "pile-on" rule (tested in test_silence_rule.py) states the two-axis
relational-cost principle correctly, but the freshest, most specific
instruction a model reads each turn -- the volatile "Your position THIS
round" block, injected only when `round_predecessors` is non-empty
(backend/providers.py, _volatile_system_parts) -- offered "one short
agreement... or a bare '...'" as two co-equal options with no reminder of
the relational-cost carve-outs. That framing let a model default to the
agreement branch (and drift into restating content) even for a plain
resolved factual question.

This file pins the reworded block: passing is the explicit DEFAULT once a
predecessor has answered adequately, verbal agreement is demoted to a
fallback reserved for the same high-relational-cost situations the stable
rule already names (direct address, group check-in/roll call, first reply
after a quiet stretch, acknowledging a correction) -- covering the three
scenarios called for:
  1. a second participant passes after an adequate factual answer
  2. both participants respond to a group check-in
  3. a directly addressed participant answers
No hardcoded greeting/phrase detector is introduced anywhere."""

from backend.providers import group_chat_system, split_system_prompt

PARTICIPANT = {"name": "GPT", "slug": "gpt", "system_prompt": ""}
ROSTER = [{"name": "Claude", "slug": "claude"}, {"name": "GPT", "slug": "gpt"}]


def _prompt(cfg, **extra):
    live = dict(cfg)
    live.update(extra)
    return group_chat_system(PARTICIPANT, ROSTER, live, None, "", False)


# ---------- the round_predecessors block now defaults to silence ----------

def test_round_predecessors_block_defaults_to_pass(cfg):
    text = _prompt(cfg, round_predecessors=["claude"])
    # #98: the silent default upgraded from a persisted "…" row to
    # the app-suppressed [pass] - same rule, now truly invisible
    assert "DEFAULT to a bare [pass]" in text
    assert "not a fallback" in text


def test_round_predecessors_block_no_longer_offers_agreement_as_coequal(cfg):
    """The exact pre-fix wording that let the model choose "short agreement"
    as freely as passing must be gone."""
    text = _prompt(cfg, round_predecessors=["claude"])
    assert "your entire reply should be one short agreement" not in text
    assert "or a bare \"…\" to pass. Only write" not in text


def test_round_predecessors_block_demotes_agreement_to_a_named_fallback(cfg):
    text = _prompt(cfg, round_predecessors=["claude"])
    assert "is a fallback for case (b)" in text
    assert "never a substitute for passing" in text


def test_round_predecessors_block_still_names_high_relational_cost_carveouts(cfg):
    """Case (1): resolved factual question -- pass. Cases (2)/(3): group
    check-in and direct address still need to override the default -- the
    freshest instruction must keep repeating those carve-outs, not just the
    far-away stable rule."""
    text = _prompt(cfg, round_predecessors=["claude"])
    assert "addressed directly" in text
    assert "whole-group check-in or roll call" in text
    assert "quiet stretch" in text
    assert "acknowledging a correction" in text


def test_round_predecessors_block_lives_in_volatile_not_stable(cfg):
    stable, volatile = split_system_prompt(PARTICIPANT, ROSTER, dict(cfg, round_predecessors=["claude"]),
                                            None, "", False)
    assert "DEFAULT to a bare" not in stable
    assert "DEFAULT to a bare" in volatile


def test_no_round_predecessors_block_when_nobody_has_spoken_yet(cfg):
    """A participant going first this round (e.g. the only one addressed, or
    the opening reply) gets no "position this round" text at all -- the
    default-to-pass framing only applies once someone else has actually
    answered."""
    text = _prompt(cfg)
    assert "Your position THIS round" not in text


# ---------- the three scenarios, read against the assembled prompt ----------

def test_scenario_second_participant_passes_after_adequate_answer(cfg):
    """Claude already answered a resolved factual question; GPT's prompt
    this round must default to passing, with no instruction nudging it
    toward restating the answer."""
    text = _prompt(cfg, round_predecessors=["claude"])
    # #98: the silent default upgraded from a persisted "…" row to
    # the app-suppressed [pass] - same rule, now truly invisible
    assert "DEFAULT to a bare [pass]" in text
    assert "Never restate their content in your own words" in text


def test_scenario_group_checkin_both_may_speak(cfg):
    """Even with a predecessor already having replied to the same
    group-directed message, the relational-cost carve-out for a whole-group
    check-in must still be present and phrased as an override of the
    default, not erased by it."""
    text = _prompt(cfg, round_predecessors=["claude"])
    assert "whole-group check-in or roll call" in text
    # the override must be reachable from the same sentence structure as the
    # default, i.e. explicitly listed as a reason to write actual words
    assert "write actual words instead if at least one of these is true" in text


def test_scenario_direct_address_participant_answers(cfg):
    """A directly addressed participant must still see the carve-out even
    when a predecessor already spoke this round -- both the stable pile-on
    rule and the fresher per-round block name it."""
    stable, volatile = split_system_prompt(PARTICIPANT, ROSTER, dict(cfg, round_predecessors=["claude"]),
                                            None, "", False)
    assert "question aimed at you directly" in stable
    assert "addressed directly" in volatile
