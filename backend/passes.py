"""The sanctioned pass (#98): a structural outlet for "stay silent when
redundant".

The July diagnosis, both standing agents on the record: prompted to stay
silent when redundant AND to be helpful, models invent angles instead of
obeying the silence rule. No wording fixes that tension - passing has to
be a real, cheap, honourable move the APP enforces.

Owner decisions (2026-08-14, on the issue): a pass is INVISIBLE - nothing
persisted, nothing spoken, no transcript marker - and a model selected
FIRST to respond to a direct user question may not pass, so every direct
question gets at least one substantive answer. Same spirit, one addition:
a seat addressed by name may not pass either - being summoned IS the
demand for an answer (addressing detection lives in engine.py, reusing
pick_responders' own mention and vocative rules - one summoning grammar,
never two).

Pure module: the engine consumes it, tests drive it without I/O.
"""

PASS_TOKEN = "[pass]"

GUARD_NOTE = (
    "Your [pass] was refused: {user} asked a direct question and you are "
    "first to answer it (or you were addressed by name), so at least one "
    "substantive reply is owed. Answer now - as briefly as you like, but "
    "with actual content."
)


def is_pass(text: str) -> bool:
    """A bare [pass] - surrounding whitespace tolerated, case-insensitive,
    nothing else. A reply that merely CONTAINS the token chose to speak."""
    return (text or "").strip().lower() == PASS_TOKEN


def is_direct_question(text: str) -> bool:
    """The guard trigger: the user's turn asks something."""
    return "?" in (text or "")


def may_pass(idx: int, addressed: bool, user_text: str) -> bool:
    """The guard (#98): first responder to a direct question, or any
    explicitly-addressed seat, may not pass."""
    if addressed:
        return False
    return not (idx == 0 and is_direct_question(user_text))
