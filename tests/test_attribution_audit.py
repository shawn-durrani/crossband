"""The non-blocking DIAGNOSTIC half of the source-provenance fix. The
stable-prompt rule in providers.py (prevention) asks a model never to call
instruction or application context something the user "said" -- but prompt text
can't be mechanically enforced, so this observes it: a deterministic, non-LLM
scan of a COMPLETED reply for "you said/told me/asked/..." claims, cross-checked
against the raw speaker=="user" transcript turns.

A hit logs `result=no_verbatim_user_match` -- meaning ONLY "not found verbatim
in the raw User turns available in this window", NOT "the model fabricated
this". A true, well-grounded claim can land here via compression (the grounding
turn folded into the rolling summary) or paraphrase; the log line is honest
about that and the check never blocks a reply. The log is also CONTENT-FREE by
construction (a one-way fingerprint + lengths/offsets, never the claim text) --
see test_log_line_is_content_free.

These tests are fixed-fixture and fully deterministic -- no model call, no
network, no dependence on what an actual model happens to say -- exercising
`_check_attribution` directly against hand-written reply text, exactly the
acceptance criteria: a reply that attributes assistant-side
guidance ("keep replies concise", "don't pile on") to the user must be flagged,
and a reply correctly quoting something the user actually said must not be."""

import logging

from backend import providers
from backend.config import Settings
from tests.conftest import make_msg

PARTICIPANT = {"name": "Claude", "slug": "claude", "model": "claude-opus-4-8"}


def _run(caplog, reply_text, transcript, cfg=None):
    caplog.set_level(logging.INFO, logger="mmc.providers")
    providers._check_attribution(reply_text, transcript, PARTICIPANT, cfg or {})
    return [r for r in caplog.records if "attribution_audit" in r.getMessage()]


# ---------- the exact acceptance-criteria shape ----------

def test_flags_assistant_side_instruction_misattributed_as_user_speech(caplog):
    """The literal acceptance criteria: assistant-side instructions
    like "keep replies concise" and "do not pile on" (both real phrases from
    providers.py's own stable rules) must never be laundered as user speech."""
    transcript = [make_msg(1, "user", "how's the weather looking this weekend?")]
    reply = "Sure — you told me to keep replies concise, so here's the short version."
    hits = _run(caplog, reply, transcript)
    assert len(hits) == 1
    # Honest semantics: "not found verbatim in raw User turns", never a
    # fabrication verdict -- the message must not imply the model made it up.
    msg = hits[0].getMessage()
    assert "result=no_verbatim_user_match" in msg
    assert "grounded" not in msg
    assert "fabricat" not in msg.lower()


def test_flags_second_acceptance_phrase(caplog):
    transcript = [make_msg(1, "user", "what do you think of GPT's answer?")]
    reply = "Well, you said not to pile on, so I'll just add one thing."
    hits = _run(caplog, reply, transcript)
    assert len(hits) == 1


# ---------- grounded claims are never flagged ----------

def test_does_not_flag_claim_actually_grounded_in_a_real_user_turn(caplog):
    transcript = [make_msg(1, "user", "I'd rather wait until Friday to ship this.")]
    reply = "Got it — you said you'd rather wait until Friday to ship this."
    hits = _run(caplog, reply, transcript)
    assert hits == []


def test_grounded_check_is_case_and_punctuation_insensitive(caplog):
    transcript = [make_msg(1, "user", "Please don't email the WHOLE team, just Sam.")]
    reply = "Understood — you said please don't email the whole team, just SAM."
    hits = _run(caplog, reply, transcript)
    assert hits == []


def test_grounded_check_handles_second_to_first_person_pronoun_flip(caplog):
    """The realistic shape: a model reports the user's own words back in the
    second person ("you said you'd..."), while the original turn was phrased
    in the first person ("I'd..."). Must still count as grounded."""
    transcript = [make_msg(1, "user", "I'd rather wait until Friday to ship this.")]
    reply = "Got it — you said you'd rather wait until Friday to ship this."
    hits = _run(caplog, reply, transcript)
    assert hits == []


# ---------- non-attribution text and edge cases never trip it ----------

def test_no_claim_phrase_produces_no_log(caplog):
    transcript = [make_msg(1, "user", "hello")]
    reply = "Happy to help with that today."
    hits = _run(caplog, reply, transcript)
    assert hits == []


def test_empty_reply_is_a_noop(caplog):
    hits = _run(caplog, "", [make_msg(1, "user", "hi")])
    assert hits == []


def test_no_real_user_turns_in_window_is_a_noop_not_a_false_positive(caplog):
    """No speaker=='user' turn at all (e.g. a model-only continuation round) --
    nothing to ground against, so this must stay silent rather than flag
    every claim as ungrounded."""
    transcript = [make_msg(1, "claude", "you said we should try again")]
    hits = _run(caplog, "you said we should try again", transcript)
    assert hits == []


def test_short_claim_below_the_noise_floor_is_not_flagged(caplog):
    transcript = [make_msg(1, "user", "ok")]
    reply = "you said ok"
    hits = _run(caplog, reply, transcript)
    assert hits == []


def test_addressing_another_participant_as_you_can_still_be_flagged_but_stays_informational(caplog):
    """Documents the known false-positive class (paraphrase / addressing a
    different "you") rather than hiding it -- this is why the check is
    informational-only, never blocking (see providers.py docstring)."""
    transcript = [make_msg(1, "user", "let's hear GPT's take")]
    reply = "GPT, you mentioned the deploy window was tight, right?"
    hits = _run(caplog, reply, transcript)
    # Ungrounded in a user turn, so it DOES flag -- proving the check runs on
    # any "you <verb>" claim, not just ones literally addressed to the human.
    # That's a deliberate false-positive tradeoff for a non-blocking signal.
    assert len(hits) == 1


# ---------- provenance-sourced text (persona/summary/memory) never counts as grounding ----------

def test_system_only_content_never_grounds_a_claim(caplog):
    """A claim is only grounded by a real speaker=='user' transcript turn --
    persona, project instructions, memory, and chat-summary text never enter
    `transcript` at all (see tests/test_projection.py), so they can never
    accidentally satisfy the substring check here either."""
    transcript = [
        make_msg(1, "user", "hi there"),
        make_msg(2, "claude", "Reminder to myself: keep replies concise per my "
                               "persona settings."),
    ]
    reply = "You told me to keep replies concise."
    hits = _run(caplog, reply, transcript)
    assert len(hits) == 1  # the phrase only appears in Claude's OWN turn, never a user turn


# ---------- privacy: the log line carries no conversation content ----------

def test_log_line_is_content_free(caplog):
    """From privacy review: the audit must never write conversation text to the
    log -- only a one-way fingerprint plus content-free counts. Neither the
    claim text, the trigger phrase, nor the user's own words may appear."""
    # Distinctive tokens chosen so a real leak is unambiguous (none are
    # substrings of the log's own field names).
    secret_claim = "zephyr quokka nimbus wandering"
    transcript = [make_msg(1, "user", "pomegranate lattice discussion")]
    reply = f"Right, you said {secret_claim}, so I'll prepare for it."
    hits = _run(caplog, reply, transcript)
    assert len(hits) == 1
    msg = hits[0].getMessage().lower()
    # No claim/trigger/user text, verbatim OR normalized, anywhere in the line.
    for word in secret_claim.split() + ["pomegranate", "lattice"]:
        assert word not in msg
    assert "you said" not in msg
    # But it IS a useful, structured, content-free diagnostic.
    assert "claim_fp=" in msg
    assert "claim_norm_len=" in msg
    assert "claim_offset=" in msg
    assert "model=claude-opus-4-8" in msg


def test_fingerprint_is_stable_and_deterministic():
    """Same normalized claim -> same fingerprint (correlatable across lines);
    different claim -> different fingerprint. No text, fixed short width."""
    a = providers._claim_fingerprint("keep replies concise")
    b = providers._claim_fingerprint("keep replies concise")
    c = providers._claim_fingerprint("do not pile on")
    assert a == b and a != c
    assert len(a) == 12 and a.isalnum()


# ---------- the config off-switch (a real, documented Settings boolean) ----------

def test_attribution_audit_can_be_disabled_via_cfg(caplog):
    transcript = [make_msg(1, "user", "hello")]
    reply = "You told me to keep replies concise."
    hits = _run(caplog, reply, transcript, cfg={"attribution_audit": False})
    assert hits == []


def test_attribution_audit_is_a_real_setting_defaulting_on():
    """From config review: the off-switch must be a genuine Settings field that
    flows through as_cfg (not a phantom key that _check_attribution's default
    happens to honor). Default is on."""
    cfg = Settings().as_cfg()
    assert cfg["attribution_audit"] is True


def test_attribution_audit_setting_is_env_overridable():
    """It must actually be configurable through the existing env mechanism, so
    the off-switch documented in config.py works."""
    from backend.config import load_settings
    s = load_settings(environ={"MMC_ATTRIBUTION_AUDIT": "false"})
    assert s.attribution_audit is False
    assert s.as_cfg()["attribution_audit"] is False
