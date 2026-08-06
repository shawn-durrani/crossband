"""Guardrail: every LIVE message insert must go through
db.insert_message() so it always wakes the global events bus (see that
function's own docstring — it's the ONE place notify_new_message() is
called). A raw `INSERT INTO messages` anywhere else in backend/ is exactly
the bug this guard exists to prevent: a write with no live-delivery hook,
invisible to an already-connected client until a manual refresh.

backend/importer.py is the one documented, deliberate exemption — historical
bulk backfill from a provider export, not live activity (see its own module
docstring for why notifying per-row there would be actively wrong, not just
unnecessary)."""

import re
from pathlib import Path

BACKEND = Path(__file__).resolve().parents[1] / "backend"
EXEMPT_FILES = {"db.py", "importer.py"}
PATTERN = re.compile(r"INSERT\s+INTO\s+messages", re.IGNORECASE)


def _offenders():
    offenders = []
    for path in sorted(BACKEND.rglob("*.py")):
        if path.name in EXEMPT_FILES:
            continue
        for i, line in enumerate(path.read_text().splitlines(), 1):
            if PATTERN.search(line):
                offenders.append(f"{path.relative_to(BACKEND.parent)}:{i}: {line.strip()}")
    return offenders


def test_no_raw_message_inserts_outside_the_centralized_helper():
    offenders = _offenders()
    assert not offenders, (
        "raw INSERT INTO messages outside db.py/importer.py — route it through "
        "db.insert_message() instead, or connected clients won't be woken"
        ":\n" + "\n".join(offenders)
    )


def test_importer_exemption_stays_documented():
    """The exemption is deliberate and explained, not an oversight that
    happened to slip past the guard above — regression check for a future
    refactor accidentally dropping the justification."""
    text = (BACKEND / "importer.py").read_text()
    assert "Deliberate exemption from db.insert_message()" in text
    assert "historical bulk backfill" in text, "the exemption must state its reason"


def test_insert_message_is_the_only_notifier():
    """The centralization point itself: exactly one call site in the whole
    backend calls `events.notify_new_message()` (the qualified form any
    caller OTHER than events.py itself must use). If this ever needs to
    become two, that's a deliberate design change, not an accident. (Matches
    only the qualified `events.notify_new_message()` call form — not mentions
    of the bare name in events.py's own docstrings/comments about itself.)"""
    call_re = re.compile(r"\bevents\.notify_new_message\(\)")
    hits = []
    for path in sorted(BACKEND.rglob("*.py")):
        for i, line in enumerate(path.read_text().splitlines(), 1):
            stripped = line.strip()
            if stripped.startswith("#"):
                continue
            if call_re.search(line):
                hits.append(f"{path.name}:{i}")
    assert len(hits) == 1, f"expected exactly one call site, found: {hits}"
    assert hits[0].startswith("db.py:")
