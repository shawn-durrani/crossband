"""Single-primary-human invariant guard.

Crossband's projection collapses every non-self speaker into an
indistinguishable `role: user` turn, so the ONLY durable signal that a turn was
really the human is the reserved speaker sentinel `providers.PRIMARY_HUMAN_SPEAKER`
== "user". The current architecture has EXACTLY ONE primary human: every
human-typed message is stored with speaker == "user"; AI participants use their
slug; external producers are namespaced `ext:<source>`.

The attribution audit grounds "the User said X" against those turns and
those turns only. That is correct while there is one human - and would silently
mean something else the moment a second human existed. These tests pin the
assumption so that any future move toward multi-human identity (a `user:<id>`
speaker, a set of human speakers, a projection that labels more than one speaker
as the primary human) fails here and forces a deliberate redesign, rather than
quietly widening the ground-truth bucket. This suite does NOT add multi-human
support; it documents its absence and makes widening it loud.
"""

from backend import providers
from tests.conftest import make_msg


def test_primary_human_speaker_is_a_single_scalar_sentinel():
    """Exactly one reserved value for the one human -- a scalar string, not a
    set/list/tuple (which would be the natural shape of multi-human support)."""
    assert providers.PRIMARY_HUMAN_SPEAKER == "user"
    assert isinstance(providers.PRIMARY_HUMAN_SPEAKER, str)


def test_ground_truth_is_exactly_the_single_user_speaker():
    """`_user_turns_text` collects raw turns by EXACT equality with the sentinel
    -- so it is precisely the one primary human's words, nothing else."""
    transcript = [
        make_msg(1, "user", "alpha bravo charlie"),
        make_msg(2, "claude", "delta echo foxtrot"),
        make_msg(3, "gpt", "golf hotel india"),
    ]
    ground = providers._user_turns_text(transcript)
    assert "alpha bravo charlie" in ground
    # No AI participant's words are ever ground truth for "the User said X".
    assert "delta" not in ground and "golf" not in ground


def test_a_second_humanlike_speaker_does_not_silently_widen_the_bucket():
    """The regression that matters: if a future schema introduced a SECOND human
    under a namespaced speaker (e.g. `user:alice`, `user2`, `human:bob`), the
    exact-equality match must NOT absorb it into the one-primary-human ground
    truth. It doesn't -- those turns simply don't ground, so the audit stays
    valid-for-one-human instead of quietly attributing a second person's words
    to the primary User."""
    for other_human in ("user:alice", "user2", "human", "human:bob", "User"):
        transcript = [make_msg(1, other_human, "second human secret phrase here")]
        ground = providers._user_turns_text(transcript)
        assert ground == "", (
            f"speaker {other_human!r} silently widened the single-human bucket -- "
            "multi-human support needs a deliberate redesign of _user_turns_text "
            "and PRIMARY_HUMAN_SPEAKER, not this exact-match to keep passing")


def test_audit_is_not_valid_for_multi_human_attribution(caplog):
    """A claim grounded ONLY in a second human's turn is still flagged, proving
    the audit does not treat any speaker but the one primary human as ground
    truth. (This is the correct behavior for a single-human architecture; it is
    ALSO the tripwire that would make a naive multi-human rollout visibly wrong,
    forcing reconsideration rather than a silent broadening.)"""
    import logging
    caplog.set_level(logging.INFO, logger="crossband.providers")
    transcript = [
        make_msg(1, "user", "let's talk about the roadmap"),
        make_msg(2, "user:alice", "I'd rather ship on Friday"),  # a hypothetical 2nd human
    ]
    reply = "Got it — you said you'd rather ship on Friday."
    providers._check_attribution(reply, transcript,
                                 {"slug": "claude", "model": "claude-opus-4-8"}, {})
    hits = [r for r in caplog.records if "attribution_audit" in r.getMessage()]
    assert len(hits) == 1  # alice's turn is NOT the primary User's ground truth


def test_projection_labels_only_the_exact_user_speaker_as_the_human(names, cfg):
    """Belt-and-suspenders on the projection side: only the exact sentinel gets
    the human's display name. A namespaced second-human speaker falls through to
    the generic slug label, so the transcript never presents two speakers as the
    one primary human -- another place a silent multi-human widening would surface."""
    cfg = dict(cfg, user_name="Alex")
    transcript = [
        make_msg(1, "user", "primary human here"),
        make_msg(2, "user:alice", "second human here"),
        make_msg(3, "gpt", "a model here"),
    ]
    msgs = providers.build_anthropic_messages("claude", transcript, names, cfg)
    blob = " ".join(b["text"] for m in msgs for b in m["content"] if b.get("type") == "text")
    assert "[Alex ·" in blob  # the one primary human is labelled by user_name
    # The namespaced second human is NOT rendered under the primary human's name.
    assert blob.count("[Alex ·") == 1
    assert "[user:alice ·" in blob
