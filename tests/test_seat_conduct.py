"""Seat-conduct rules added after the 2026-08-17 voice incident (#172):
fabricated dispatch claims, promised merges, and attribution-label
fixation all happened in one live session, each with no prompt backstop.
These pin the stable-block lines that now exist, so a prompt refactor
cannot drop them silently. #80 joins them: the one-message-per-round
truth and the voice-mode written-deliverable channel."""

from backend.providers import WRITTEN_TOKEN, group_chat_system

PARTICIPANT = {"name": "Claude", "slug": "claude", "system_prompt": ""}
ROSTER = [{"name": "Claude", "slug": "claude"}, {"name": "GPT", "slug": "gpt"}]


def _prompt(cfg, voice=False):
    return group_chat_system(PARTICIPANT, ROSTER, dict(cfg), None, "", voice)


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


def test_next_message_promises_are_named_impossible(cfg):
    # The #80 field failure: four rounds of "I'll put the full list in the
    # next message" and the list never arrived - a participant gets exactly
    # one message per round, so the promised follow-up cannot exist.
    text = _prompt(cfg)
    assert "exactly ONE message per round" in text
    assert "Deliver the content in THIS reply" in text


def test_voice_mode_offers_the_written_channel(cfg):
    # #80 shape 2: "too long to say aloud" must not imply "defer" - voice
    # mode names the [written] token and the deferral ban travels with it.
    text = _prompt(cfg, voice=True)
    assert WRITTEN_TOKEN in text
    assert "NEVER a reason to defer" in text
    # and the channel is a voice-mode instruction, not stable-block noise
    assert WRITTEN_TOKEN not in _prompt(cfg)
