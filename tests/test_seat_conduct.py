"""Seat-conduct rules added after the 2026-08-17 voice incident (#172):
fabricated dispatch claims, promised merges, and attribution-label
fixation all happened in one live session, each with no prompt backstop.
These pin the stable-block lines that now exist, so a prompt refactor
cannot drop them silently."""

from backend.providers import group_chat_system

PARTICIPANT = {"name": "Claude", "slug": "claude", "system_prompt": ""}
ROSTER = [{"name": "Claude", "slug": "claude"}, {"name": "GPT", "slug": "gpt"}]


def _prompt(cfg):
    return group_chat_system(PARTICIPANT, ROSTER, dict(cfg), None, "", False)


def test_dispatch_claims_require_a_tool_result(cfg):
    # The field failure: a seat said "I've sent Claude Code to check the
    # live deployment" with no dispatch recorded anywhere.
    text = _prompt(cfg)
    assert "Never claim you dispatched" in text
    assert "tool row and a status chip" in text


def test_nobody_may_promise_a_merge(cfg):
    # The field failure: a seat offered "merging it if green" - the guest
    # never merges (backend/guest.py deny-list) and neither can a seat.
    text = _prompt(cfg)
    assert "NEVER merges" in text
    assert "never offer a merge" in text


def test_attribution_heads_are_metadata_not_a_topic(cfg):
    # The field failure: a seat raised "that turn shows Identity pending"
    # round after round, after being told to drop it.
    text = _prompt(cfg)
    assert "attribution metadata" in text
    assert "drop the subject" in text
