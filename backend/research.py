"""Spoken research mode (#253, #417): "research more", "look into that
properly" or "go deeper" turns on a bigger tool budget and a research
routine for the REST of the chat - plan before the first search, weigh
sources, say plainly when the evidence isn't enough, cite what was found,
memory included. "Back to normal" turns it off. A new chat starts at the
defaults. Per chat, never per seat (the owner's decision of 17 September):
one tool-budget pool, and everyone in the room shares it.

The cue also asks for a stronger model for every seat in the chat (the
owner's decision of 4 September): introductions.scan_user_turn runs that
after this module has applied the mode (model_step.step_up, #254).

Same shape as depth.py: the merged intent scan (backend/intent.py) is the
judge, introductions.scan_user_turn applies the confirmed verdict on a
worker thread, and this module owns the durable state, the transcript
notice and the volatile prompt note. Speaker resolution is depth's own
rule (depth.resolve_speaker) - the same ledger-backed lookup, never the
owner as a fallback (#255)."""

import logging

from . import db
from .depth import resolve_speaker

log = logging.getLogger("crossband.research")

ROUTINE = (
    "the seats plan before they search, weigh sources, say when the "
    "evidence isn't enough, and cite what they find, with a larger tool "
    "budget"
)


def _on_notice(user: str) -> str:
    by = f", set by {user}" if user else ""
    return (f"Research mode on for this chat{by}: {ROUTINE}. Anyone can "
            "say back to normal.")


def apply_research(chat_id, cfg, message_id=None) -> str:
    """Apply a confirmed research cue (synchronous; worker thread). Already
    on is an honest no-change - the mode is per chat, so a second "research
    more" changes nothing and gets the "nothing changed" line, not a second
    notice. Returns the scan outcome word."""
    con = db.connect()
    try:
        chat = con.execute("SELECT * FROM chats WHERE id=?",
                           (chat_id,)).fetchone()
        if not chat:
            return "no_change"
        if chat["research_mode"]:
            return "no_change"
        user = resolve_speaker(con, chat_id, message_id, cfg)
        db.set_chat_research(con, chat_id, True, set_by=user)
        db.insert_message(con, chat_id, "system", _on_notice(user))
    finally:
        con.close()
    return "research_set"


def clear_research(con, chat_id) -> bool:
    """Turn research mode off, posting the notice only when it was actually
    on - a "back to normal" that clears depth alone must not also claim a
    research change that never happened. Caller owns `con` (depth.apply_depth
    calls this mid-transaction, same connection, same worker thread)."""
    chat = con.execute("SELECT * FROM chats WHERE id=?",
                       (chat_id,)).fetchone()
    if not chat or not chat["research_mode"]:
        return False
    db.set_chat_research(con, chat_id, False)
    db.insert_message(con, chat_id, "system", "Research mode off for this chat.")
    return True


def research_note(set_by: str) -> str:
    """The volatile prompt note (engine.py threads it per round, never the
    cached stable block - cache layout law, same as depth_note). `set_by` is
    who spoke the cue, or '' when the app could not say (#255)."""
    who = set_by or "Someone in this chat"
    return (
        "\n## Research mode (this chat)\n"
        f"{who} turned research mode on for this chat. Four rules while "
        "it's on: plan your searches before the first call, instead of "
        "searching as you go; sort what comes back by how much to trust it "
        "- a primary document outranks a forum post; assert nothing the "
        "evidence doesn't back, and say plainly when it doesn't reach far "
        "enough; and end with a written answer that lists its sources. "
        "Searching your own memory - recall_memory and search_history - "
        "counts as research too. Your tool budget is larger for this "
        "chat. Anyone in this chat can turn it off by saying back to "
        "normal; you cannot change it yourself, so never claim you have."
    )
