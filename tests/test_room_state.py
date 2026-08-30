"""backend/room_state.py: the single write path's own suite (#239).

What these tests pin, in order: the ordering law (durable commit before
the bell rings), the flip return each caller derives its outcome from,
the sacred ambient-off rules, the roster steps behind their kwargs, and
the cap derived from one place. The six call sites keep their
end-to-end coverage in the existing suites - test_room_ambient.py,
test_room_commands.py, test_room_intro.py, test_room_identify.py and
test_room_remembered_first.py passing unchanged is the real regression
net for the refactor. conftest's autouse _room_state_clean fixture
resets diarize's mirrors between tests."""

import pytest
from fastapi.testclient import TestClient

from backend import db, diarize, room_state
from backend.app import create_app
from backend.config import Settings
from roomkit import _remember

CFG = {"user_name": "Alex", "room_roster_max": 6}


@pytest.fixture
def app(tmp_path):
    settings = Settings(data_dir=str(tmp_path / "data"),
                        memory_url="http://127.0.0.1:1",
                        user_name="Alex")
    return create_app(settings)


@pytest.fixture
def make_chat(app):
    def _make():
        with TestClient(app, base_url="http://127.0.0.1") as c:
            return c.post("/api/chats", json={}).json()["id"]
    return _make


def _chat_flags(chat_id):
    con = db.connect()
    try:
        row = con.execute("SELECT room_mode, ambient_off FROM chats "
                          "WHERE id=?", (chat_id,)).fetchone()
        return bool(row["room_mode"]), bool(row["ambient_off"])
    finally:
        con.close()


def _present(chat_id):
    con = db.connect()
    try:
        return db.get_room_roster(con, chat_id, present_only=True)
    finally:
        con.close()


def _open_flags(chat_id):
    con = db.connect()
    try:
        return db.get_room_flags(con, chat_id)
    finally:
        con.close()


def _set_ambient(chat_id, off):
    """Place the sacred flag on both planes, as the suites already do."""
    con = db.connect()
    try:
        db.set_chat_ambient_off(con, chat_id, off)
    finally:
        con.close()
    diarize.set_ambient_off(chat_id, off)


# ── arm ─────────────────────────────────────────────────────────────────

def test_arm_flips_durable_and_mirror_and_reports_the_flip(app, make_chat):
    chat_id = make_chat()
    assert room_state.arm(chat_id, CFG, source="command",
                          clear_ambient=True, seat_owner="never") is True
    assert _chat_flags(chat_id) == (True, False)
    assert diarize.room_enabled(chat_id) is True
    # Idempotent, and the second call reports no flip - the command
    # path's "no_change" outcome depends on exactly this.
    assert room_state.arm(chat_id, CFG, source="command",
                          clear_ambient=True, seat_owner="never") is False


def test_the_bell_rings_after_the_durable_commit(app, make_chat, monkeypatch):
    """The ordering law, and the closed gap: an arm that touches no
    roster row still rings, and by ring time the durable row already
    holds the new state (commit BEFORE notify)."""
    chat_id = make_chat()
    seen = []
    from backend import events
    monkeypatch.setattr(events, "notify_room_update",
                        lambda: seen.append(_chat_flags(chat_id)))
    room_state.arm(chat_id, CFG, source="manual toggle",
                   clear_ambient=True, seat_owner="never")
    assert seen and seen[-1] == (True, False)


def test_an_explicit_re_enable_clears_ambient_even_when_already_armed(
        app, make_chat):
    """apply_command's early-return shape: the ambient clear lands on
    the no-flip path too, on both planes."""
    chat_id = make_chat()
    room_state.arm(chat_id, CFG, source="command",
                   clear_ambient=True, seat_owner="never")
    _set_ambient(chat_id, True)
    assert room_state.arm(chat_id, CFG, source="command",
                          clear_ambient=True, seat_owner="never") is False
    assert _chat_flags(chat_id) == (True, False)
    assert diarize.ambient_off(chat_id) is False


def test_an_automatic_arm_never_clears_the_sacred_flag(app, make_chat):
    """clear_ambient=False leaves a set ambient-off untouched in both
    stores. Production gates keep ambient arms from firing while the
    flag is set; the module holds the rule by construction anyway, so a
    future caller cannot inherit the gap unguarded."""
    chat_id = make_chat()
    _set_ambient(chat_id, True)
    room_state.arm(chat_id, CFG, source="ambient (known voice)",
                   clear_ambient=False, seat_owner="never")
    assert _chat_flags(chat_id) == (True, True)
    assert diarize.ambient_off(chat_id) is True


def test_seat_owner_on_arm_seats_only_on_a_genuine_flip(app, make_chat):
    armed_first = make_chat()
    room_state.arm(armed_first, CFG, source="introduction",
                   clear_ambient=True, seat_owner="never")
    # A room some other path armed first: on_arm seats nobody.
    room_state.arm(armed_first, CFG, source="ambient (unknown voice)",
                   clear_ambient=False, seat_owner="on_arm")
    assert _present(armed_first) == []
    # A genuine flip: the owner is seated under the user_name setting.
    fresh = make_chat()
    room_state.arm(fresh, CFG, source="ambient (unknown voice)",
                   clear_ambient=False, seat_owner="on_arm")
    assert [p["name"] for p in _present(fresh)] == ["Alex"]
    assert _present(fresh)[0]["seated_via"] == "owner"


def test_seat_owner_always_reseats_and_links_an_armed_room(app, make_chat):
    """The manual toggle's shape: a re-enable on an armed room still
    seats the owner, and links them once their bank exists."""
    chat_id = make_chat()
    room_state.arm(chat_id, CFG, source="command",
                   clear_ambient=True, seat_owner="never")
    pid = _remember("Alex")
    room_state.arm(chat_id, CFG, source="manual toggle",
                   clear_ambient=True, seat_owner="always")
    rows = _present(chat_id)
    assert [p["name"] for p in rows] == ["Alex"]
    assert rows[0]["person_id"] == pid


def test_seat_owner_rejects_a_typo(app, make_chat):
    with pytest.raises(ValueError):
        room_state.arm(make_chat(), CFG, source="command",
                       clear_ambient=True, seat_owner="on-arm")


# ── disarm ──────────────────────────────────────────────────────────────

def test_disarm_solo_shape_sets_ambient_marks_left_and_resolves_asks(
        app, make_chat):
    chat_id = make_chat()
    room_state.arm(chat_id, CFG, source="command",
                   clear_ambient=True, seat_owner="on_arm")
    room_state.seat(chat_id, "Rina", CFG, via="introduction",
                    enforce_cap=True)
    con = db.connect()
    try:
        db.insert_room_flag(con, chat_id, "unknown_voice")
    finally:
        con.close()
    assert room_state.disarm(chat_id, source="command",
                             set_ambient_off=True, clear_roster=True,
                             resolve_asks=True) is True
    assert _chat_flags(chat_id) == (False, True)
    assert diarize.room_enabled(chat_id) is False
    assert diarize.ambient_off(chat_id) is True
    assert _present(chat_id) == []
    assert _open_flags(chat_id) == []


def test_disarm_is_sacred_even_when_room_already_off(app, make_chat):
    """"Solo mode" in a room that never armed still writes the durable
    preference and its mirror, and reports no flip."""
    chat_id = make_chat()
    assert room_state.disarm(chat_id, source="command",
                             set_ambient_off=True, clear_roster=True,
                             resolve_asks=True) is False
    assert _chat_flags(chat_id) == (False, True)
    assert diarize.ambient_off(chat_id) is True


def test_disarm_toggle_off_means_off(app, make_chat):
    """The manual toggle-off does the full solo-mode disarm (#294, ruled
    on #239): ambient goes quiet, everyone present is marked left, the
    open ask closes. A switched-off room cannot re-arm itself from the
    next voice it hears."""
    chat_id = make_chat()
    room_state.arm(chat_id, CFG, source="manual toggle",
                   clear_ambient=True, seat_owner="always")
    room_state.seat(chat_id, "Rina", CFG, via="introduction",
                    enforce_cap=True)
    con = db.connect()
    try:
        db.insert_room_flag(con, chat_id, "unknown_voice")
    finally:
        con.close()
    assert room_state.disarm(chat_id, source="manual toggle",
                             set_ambient_off=True, clear_roster=True,
                             resolve_asks=True) is True
    assert _chat_flags(chat_id) == (False, True)
    assert diarize.room_enabled(chat_id) is False
    assert _present(chat_id) == []
    assert _open_flags(chat_id) == []


# ── seat ────────────────────────────────────────────────────────────────

def test_seat_enforces_the_cap_from_one_place(app, make_chat):
    chat_id = make_chat()
    cfg = dict(CFG, room_roster_max=1)
    assert room_state.seat(chat_id, "Ana", cfg, via="introduction",
                           enforce_cap=True) is not None
    assert room_state.seat(chat_id, "Ben", cfg, via="voice-match",
                           enforce_cap=True) is None
    assert [p["name"] for p in _present(chat_id)] == ["Ana"]


def test_seat_dedupes_and_links_instead_of_reseating(app, make_chat):
    chat_id = make_chat()
    row = room_state.seat(chat_id, "Ana", CFG, via="introduction",
                          enforce_cap=True)
    assert row["person_id"] == ""
    assert room_state.seat(chat_id, "Ana", CFG, via="voice-match",
                           person_id="p-123", enforce_cap=True,
                           link_existing=True) is None
    rows = _present(chat_id)
    assert len(rows) == 1 and rows[0]["person_id"] == "p-123"
    # Provenance never drifts under an idempotent re-seat (#84).
    assert rows[0]["seated_via"] == "introduction"


def test_seat_records_provenance_at_write_time(app, make_chat):
    chat_id = make_chat()
    con = db.connect()
    try:
        mid = db.insert_message(con, chat_id, "user", "it was Ana")["id"]
    finally:
        con.close()
    row = room_state.seat(chat_id, "Ana", CFG, via="owner",
                          message_id=mid, enforce_cap=False)
    assert row["seated_via"] == "owner"
    assert row["seated_by_message_id"] == mid


def test_resolve_ask_only_after_a_genuine_seat(app, make_chat):
    """A refused seat answers nothing: the cap-refused call leaves the
    ask open (site 5's shape), and the admitted one closes it."""
    chat_id = make_chat()
    con = db.connect()
    try:
        db.insert_room_flag(con, chat_id, "unknown_voice")
    finally:
        con.close()
    cfg = dict(CFG, room_roster_max=1)
    room_state.seat(chat_id, "Ana", cfg, via="introduction",
                    enforce_cap=True)
    assert room_state.seat(chat_id, "Ben", cfg, via="voice-match",
                           enforce_cap=True, resolve_ask=True) is None
    assert len(_open_flags(chat_id)) == 1
    assert room_state.seat(chat_id, "Ben", CFG, via="voice-match",
                           enforce_cap=True, resolve_ask=True) is not None
    assert _open_flags(chat_id) == []


def test_seat_refuses_a_participant_name(app, make_chat):
    """The #65 boundary holds under the new path: an AI participant's
    name never becomes a roster person, whatever site asked."""
    chat_id = make_chat()
    con = db.connect()
    try:
        row = con.execute("SELECT name FROM participants LIMIT 1").fetchone()
    finally:
        con.close()
    if not row:
        pytest.skip("no seeded participants in this build")
    assert room_state.seat(chat_id, row["name"], CFG, via="owner",
                           enforce_cap=False) is None
    assert _present(chat_id) == []


# ── the cap helper and the seed ─────────────────────────────────────────

def test_roster_cap_one_key_one_default_one_falsy_rule(app):
    """Missing, None and 0 all mean the Settings default (6 today,
    preserved bug-for-bug - whether 0 should mean "no guests" is Shawn's
    open question); a configured value wins."""
    assert room_state.roster_cap({}) == 6
    assert room_state.roster_cap(None) == 6
    assert room_state.roster_cap({"room_roster_max": 0}) == 6
    assert room_state.roster_cap({"room_roster_max": 3}) == 3


def test_seed_mirrors_touches_no_durable_state(app, make_chat):
    chat_id = make_chat()
    room_state.seed_mirrors(chat_id, enabled=True, ambient_disarmed=True)
    assert diarize.room_enabled(chat_id) is True
    assert diarize.ambient_off(chat_id) is True
    assert _chat_flags(chat_id) == (False, False)
