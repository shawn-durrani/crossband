"""Guardrail (#239): room and roster state has ONE write path -
backend/room_state.py. Six call sites used to flip chats.room_mode /
chats.ambient_off and write roster rows, each with its own hand-rolled
ceremony, in the subsystem where an undocumented seat path minted the
#65 phantom people. A new direct writer anywhere else in backend/ is
exactly the bug this guard exists to prevent: a durable flip with no
live mirror, no roster step, or no bell, invisible to a running STT
session or a connected client.

Mirrors tests/test_insert_message_guard.py: source-scanning, no runtime.
Tests live outside backend/ and are not scanned - suites may place state
directly through db's setters and diarize's accessors."""

import re
from pathlib import Path

BACKEND = Path(__file__).resolve().parents[1] / "backend"

# (pattern, exempt file names, what to do instead)
RULES = [
    (re.compile(r"\bset_chat_(room_mode|ambient_off|room_state)\s*\("),
     {"db.py", "room_state.py"},
     "flip room state through room_state.arm()/disarm(), so the live "
     "mirror, the roster steps and the bell can never be forgotten"),
    (re.compile(r"UPDATE\s+chats\s+SET\s+[^\n]*\b(room_mode|ambient_off)\b",
                re.IGNORECASE),
     {"db.py"},
     "raw room-flag SQL belongs in db.py, called only by room_state.py"),
    (re.compile(r"_ROOM_ENABLED|_AMBIENT_OFF"),
     {"diarize.py"},
     "the live mirror is touched only through diarize's accessors, and "
     "those only from room_state.py"),
    (re.compile(r"\bset_room_enabled\s*\(|\bset_ambient_off\s*\("),
     {"diarize.py", "room_state.py"},
     "mirror writes ride room_state (arm/disarm/seed_mirrors) so the "
     "durable row and the live dict cannot diverge"),
    (re.compile(r"\badd_room_person\s*\("),
     {"db.py", "room_state.py"},
     "seat people through room_state.seat() - it owns the dedupe, the "
     "cap and the provenance; undocumented seat paths are how the #65 "
     "phantoms were minted"),
    (re.compile(r"\broom_roster_max\b"),
     {"config.py", "db.py", "room_state.py"},
     "derive the cap with room_state.roster_cap(cfg) - hand copies of "
     "the default are how the number forks"),
]


def _offenders(pattern, exempt):
    offenders = []
    for path in sorted(BACKEND.rglob("*.py")):
        if path.name in exempt:
            continue
        for i, line in enumerate(path.read_text().splitlines(), 1):
            if line.strip().startswith("#"):
                continue
            if pattern.search(line):
                offenders.append(
                    f"{path.relative_to(BACKEND.parent)}:{i}: {line.strip()}")
    return offenders


def test_room_state_is_the_only_writer():
    problems = []
    for pattern, exempt, fix in RULES:
        offenders = _offenders(pattern, exempt)
        if offenders:
            problems.append(fix + ":\n" + "\n".join(offenders))
    assert not problems, "\n\n".join(problems)


def test_patch_route_no_longer_folds_room_flags_into_the_generic_update():
    """chats.py's PATCH used to write room_mode/ambient_off inside one
    dynamic `UPDATE chats SET {sets}` statement the SQL rule above cannot
    see (the column names only exist at runtime). Pin the reroute
    directly, so the invisible writer cannot quietly return."""
    text = (BACKEND / "routers" / "chats.py").read_text()
    assert 'updates["room_mode"]' not in text
    assert 'updates["ambient_off"]' not in text
    assert "room_state" in text, "the PATCH toggle must route through room_state"


def test_the_durable_writer_stays_documented():
    """db.set_chat_room_state's single-caller rule is deliberate and
    written down, not an oversight that happened to slip past the rules
    above - regression check for a refactor dropping the justification."""
    text = (BACKEND / "db.py").read_text()
    assert "room_state.py is the only caller" in text


def test_the_registry_comment_names_the_single_writer():
    """diarize's registry comment listed its writers by hand and went
    stale once already (it omitted both ambient arms and apply_command).
    It now names room_state as the writer; keep it true."""
    text = (BACKEND / "diarize.py").read_text()
    head = text.split("_ROOM_ENABLED: dict")[0]
    assert "room_state" in head, (
        "the _ROOM_ENABLED comment must say every writer goes through "
        "backend/room_state.py")
