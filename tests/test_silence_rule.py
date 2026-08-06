"""The "don't pile on" rule was reworded away from judging redundancy
by informational content alone (which wrongly let a model pass with a bare
"…" on a plain group-directed greeting) to a general relational-cost-of-
silence principle: pass only when BOTH the informational value of speaking
AND the relational cost of staying silent are low. No hardcoded greeting/
phrase detector anywhere -- see eval_silence/ for the fixture-style
behavioral matrix this rule is meant to satisfy."""

from backend.providers import group_chat_system, split_system_prompt

PARTICIPANT = {"name": "Claude", "slug": "claude", "system_prompt": ""}
ROSTER = [{"name": "Claude", "slug": "claude"}, {"name": "GPT", "slug": "gpt"}]


def _prompt(cfg):
    return group_chat_system(PARTICIPANT, ROSTER, cfg, None, "", False)


def test_pile_on_rule_states_the_two_axes(cfg):
    text = _prompt(cfg)
    assert "informational value of speaking" in text
    assert "relational cost of staying quiet" in text
    assert "BOTH are low" in text


def test_pile_on_rule_has_no_hardcoded_greeting_phrases(cfg):
    """A phrase/keyword detector was explicitly rejected -- the rule must
    reason about redundancy vs. relational cost generally, never by matching
    literal greeting words like "hi"/"hello"/"how's it going"."""
    text = _prompt(cfg).lower()
    for banned in ("\"hi\"", "\"hello\"", "how's it going", "how are you"):
        assert banned not in text


def test_pile_on_rule_names_high_relational_cost_situations(cfg):
    text = _prompt(cfg)
    assert "check-in or greeting to the whole" in text
    assert "roll call" in text
    assert "question aimed at you directly" in text


def test_pile_on_rule_still_permits_a_genuine_pass(cfg):
    text = _prompt(cfg)
    assert "bare \"…\" only when BOTH are low" in text
    assert "already been correctly answered" in text or "already correctly answered" in text


def test_pile_on_rule_lives_in_stable_block(cfg):
    """A standing rule, not per-turn volatile content -- must sit in the
    prompt-cache-stable block like the sibling attribution/action
    rules it sits next to."""
    stable, volatile = split_system_prompt(PARTICIPANT, ROSTER, dict(cfg), None, "", False)
    assert "informational value of speaking" in stable
    assert "informational value of speaking" not in volatile


def test_old_pile_on_wording_is_gone(cfg):
    text = _prompt(cfg)
    assert "never just rephrase what another member already said" not in text
    assert "manufactured micro-angle" not in text
