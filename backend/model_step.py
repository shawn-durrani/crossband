"""A stronger model for one seat in one chat (#254).

A seat runs the model its settings name. A spoken cue can step it up to a
stronger one for the rest of ONE chat: the step-up lives in chat_seat_state
beside the seat's spoken depth, keyed on chat and seat, so a new chat starts
back on the configured model by construction. "Back to normal" is the only
way down.

This module owns what the round needs to honour a step-up:

- live_step: whether a stored step-up still applies to the seat as it is
  configured now. An owner who edits the seat's model in settings has made
  an explicit choice, and it beats a spoken one: the stored row names the
  model it replaced, and once that stops matching, the step-up is ignored.
- model_note: the volatile prompt note telling the seat which model it is
  running on and why (cache layout law, same as depth_note).
- refused / revert_after_refusal: when the provider refuses the stepped-up
  model outright (a chat too long for it, an id the key can't use), the seat
  goes back to its configured model and the chat says so, instead of failing
  every turn.

Identity never moves with the model. The speaker slug, the memory wire
class, persona, name, voice and colour all come from the participants row,
and the overlay in engine.py replaces the model id and nothing else."""

import logging

from . import db

log = logging.getLogger("crossband.model_step")

# Provider answers that mean "this model will not take this request", as
# opposed to a transient fault. 400 covers a prompt too long for the model's
# window, 403/404 an id the key can't reach, 422 a request shape the model
# rejects. A 429 or a 5xx is weather, and never moves a seat back.
_REFUSAL_STATUS = frozenset({400, 403, 404, 422})


def live_step(participant, row):
    """The step-up that applies to this seat right now, or None. `row` is
    one entry of db.get_chat_seat_models. It applies only while the model it
    replaced is still the seat's configured model, and only when it names a
    different model."""
    if not row or not row.get("model"):
        return None
    configured = participant.get("model") or ""
    if row.get("from") != configured or row["model"] == configured:
        return None
    return row


def model_note(step, seat_name) -> str:
    """The volatile note for a stepped-up seat, or "" for a seat on its
    configured model. Names who asked, or says someone did when the app
    could not tell who (#255's rule)."""
    if not step:
        return ""
    who = step.get("set_by") or "Someone in this chat"
    return (
        "\n## Your model (this chat)\n"
        f"{who} asked for a stronger model, so in this conversation you run "
        f"on {step['label']} instead of your configured {step['from_label']}. "
        f"You're still {seat_name}: your persona, voice and memory don't "
        "change. It lasts until someone says back to normal. You cannot "
        "change it yourself, so never say you have. If asked which model you "
        "are, say this honestly.")


def refused(exc) -> bool:
    """Is this error the provider refusing the model outright? Read from the
    SDK error's status code, so a stall, a network fault or a rate limit
    never counts."""
    status = getattr(exc, "status_code", None)
    return isinstance(status, int) and status in _REFUSAL_STATUS


def revert_notice(seat_name, step) -> str:
    return (f"{seat_name} is back on its configured model, "
            f"{step['from_label']}, for this chat: the provider refused "
            f"{step['label']}.")


def revert_after_refusal(chat_id, slug, seat_name, step) -> bool:
    """Clear a step-up the provider refused and say so in the chat
    (synchronous; worker thread). True when a step-up was cleared, so a
    second refusal racing the first posts nothing twice."""
    con = db.connect()
    try:
        if not db.clear_chat_seat_model(con, chat_id, slug):
            return False
        db.insert_message(con, chat_id, "system",
                          revert_notice(seat_name, step))
    finally:
        con.close()
    log.info("model step-up refused by the provider, seat moved back: "
             "chat=%s seat=%s", chat_id, slug)
    return True
