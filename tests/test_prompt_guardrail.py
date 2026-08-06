"""High-salience personal-claim guardrail.

The system prompt must explicitly name the protected categories and forbid
asserting them as current fact without memory backing. This is the immediate
prompt-layer risk reduction, NOT the finished-draft output verifier the
acceptance criteria still call for.
"""

from backend.providers import group_chat_system

PARTICIPANT = {"name": "Claude", "slug": "claude", "system_prompt": ""}
ROSTER = [{"name": "Claude", "slug": "claude"}, {"name": "GPT", "slug": "gpt"}]


def _prompt(cfg, voice_mode=False):
    return group_chat_system(PARTICIPANT, ROSTER, cfg, None, "", voice_mode)


def test_guardrail_names_every_protected_category(cfg):
    text = _prompt(cfg).lower()
    for category in ("location", "employer", "job status", "family",
                     "health", "money", "legal situation"):
        assert category in text, f"guardrail missing category: {category}"


def test_guardrail_requires_memory_backing_and_uncertainty(cfg):
    text = _prompt(cfg)
    # must forbid invention and point at the memory-backed / ask-or-check path
    assert "must NEVER be invented" in text
    assert "recall_memory/search_history" in text
    assert cfg["user_name"] in text  # addressed to the actual user name


def test_guardrail_present_in_voice_mode(cfg):
    # voice mode appends brevity rules last; the guardrail must still be there
    assert "must NEVER be invented" in _prompt(cfg, voice_mode=True)


def test_error_attribution_rule_present_in_stable_rules(cfg):
    """After a live incident where a seat took ownership of another
    seat's mistake ("I reversed it" for a claim it never made), the group
    rules carry an explicit attribution rule. It must live in the STABLE
    block (it's a standing rule, and the stable block is the cached one)."""
    from backend.providers import split_system_prompt
    stable, volatile = split_system_prompt(PARTICIPANT, ROSTER, dict(cfg), None, "", False)
    assert "attribute it to whoever actually made it" in stable
    assert "never claim another member's statement or mistake as your own" in stable.lower()
    assert "attribute it to whoever" not in volatile


def test_peer_action_rule_present_in_stable_rules(cfg):
    """A same-day recurrence of that incident WITH the rule live: a seat
    announced another seat's save_memory as its own ("Got it — saved") and
    echoed the other's climbdown. The addendum names actions explicitly."""
    from backend.providers import split_system_prompt
    stable, volatile = split_system_prompt(PARTICIPANT, ROSTER, dict(cfg), None, "", False)
    assert "never announce another member's action" in stable
    assert "don't echo their climbdowns as your own" in stable
