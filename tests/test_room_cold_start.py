"""The first meeting (#28, #482): a voice nobody knows, in a room holding
one person with no voice saved yet, is named as them by elimination.

THE DEADLOCK. Every door into the anchor bank needed something an empty bank
does not have. A confident match needs clips to match against. The
introduction scan needs an introduction-shaped sentence. Tap-to-correct needs
a label to tap. So once a bank was emptied, every turn stayed unnamed and the
seats were told "identity pending" over and over about the only person in
the room.

THE WAY OUT is elimination rather than recognition: in an ARMED room where
exactly ONE present person has no learnt voice, a new voice can only be
theirs. That is enough to name the turn - marked, honestly, as still being
learned. Nothing is saved from it until someone confirms it: tapping the
turn, or the person saying their own name.

What these tests pin, in order:

1. The rule, pure (voice_pass.decide): a new voice with exactly one
   unlearnt person present is named learning; two unlearnt people, a
   voice still listening, nothing to name, solo and a confident match
   never are; and a turn with two voices in it is a crosstalk label, never
   a learning one.
2. The plan (voice_pass._plan) derives the unlearnt people from the roster
   it already read, and stops listing someone the moment their bank is
   sufficient.
3. The pass end to end: the turn is labelled learning, nothing is saved,
   the decision is local, and no ElevenLabs call fires; without anyone to
   eliminate to, the turn names nobody and says why.
4. The learning state reaches the seats: the projection heads the turn
   "<name> (learning this voice)" - not the pending head, not voice
   confirmed - and the stable-block explainer says what it means.

Synthetic roster throughout (Alex/Sam/Dave), keyless like every suite here.
"""

import asyncio
import json
import time

import pytest

from backend import anchors, crosstalk, db, diarize, voice_pass
from backend.app import create_app
from backend.config import Settings
from backend.providers import (LEARNING_SUFFIX, PENDING_IDENTITY_HEAD,
                               VOICE_CONFIRMED_SUFFIX,
                               build_anthropic_messages, split_system_prompt)
from tests.conftest import make_msg
from tests.test_projection import PARTICIPANT, ROSTER
from roomkit import fake_naming, loud_pcm, naming_answer


@pytest.fixture
def app(tmp_path):
    settings = Settings(data_dir=str(tmp_path / "data"),
                        memory_url="http://127.0.0.1:1", user_name="Alex")
    return create_app(settings)


def _plan(**kw):
    base = {"room_on": True, "solo": False, "present": ["Sam", "Dave"],
            "unlearnt": ["Dave"], "owner_known": True,
            "is_owner": lambda n: n == "Sam"}
    base.update(kw)
    return base


# ── 1. the rule, pure ───────────────────────────────────────────────────────

def test_a_new_voice_with_one_unlearnt_person_is_named_learning():
    d = voice_pass.decide(naming_answer("new"), _plan())
    assert d["labels"] == ["Dave"] and d["uncertain"] == ["Dave"]
    assert d["learning"] is True
    assert d["arm"] is None and d["seat"] is None and not d["ask"]


def test_two_or_more_unlearnt_people_never_qualify():
    """With two unlearnt people in the room the voice could be either,
    which is what the ask exists for."""
    d = voice_pass.decide(naming_answer("new"),
                          _plan(unlearnt=["Dave", "Mateo"]))
    assert d["labels"] == [] and d["unresolved"] == "new_voice"
    assert d["ask"] is True


def test_a_confident_match_never_qualifies():
    """A named turn already has a better answer than elimination."""
    d = voice_pass.decide(naming_answer(name="Mateo", pid="p3"), _plan())
    assert d["labels"] == ["Mateo"] and not d["learning"]


def test_listening_nothing_to_name_and_solo_never_qualify():
    """A voice with too little speech to judge, a turn the naming had
    nothing for (not speech, or the model not ready), and solo each leave
    the turn unnamed: elimination needs a clear voice and an armed room."""
    for got, plan in ((naming_answer("listening"), _plan()),
                      (None, _plan()),
                      (naming_answer("new"), _plan(solo=True)),
                      (naming_answer("new"), _plan(room_on=False))):
        d = voice_pass.decide(got, plan)
        assert not d["learning"] and d["labels"] == [], (got, plan)


def test_a_two_voice_turn_is_crosstalk_never_learning():
    """Overlapping speech is the one thing elimination cannot survive: two
    people spoke, so the label lists both voices, and the unlearnt person's
    name is on neither."""
    got = naming_answer("new", voices_in_turn=2, voices={
        0: {"state": "new", "name": "", "seconds": 2.0, "first": 0.0},
        1: {"state": "listening", "name": "", "seconds": 1.5,
            "first": 1.0}}, spans=[], turn_s=3.5)
    listed = crosstalk.listed_voices(got)
    assert len(listed) == 2
    payload = crosstalk.label(got, listed, None)
    assert payload["crosstalk"] is True and "learning" not in payload
    assert "Dave" not in payload["labels"]


# ── 2. the plan derives the unlearnt people ─────────────────────────────────

def test_the_plan_lists_the_unlearnt_and_stops_once_the_bank_is_sufficient(
        app):
    from fastapi.testclient import TestClient
    with TestClient(app, base_url="http://127.0.0.1") as c:
        chat = c.post("/api/chats", json={"participant_ids": []}).json()
        con = db.connect()
        try:
            db.set_chat_room_mode(con, chat["id"], True)
            db.add_room_person(con, chat["id"], "Dave")
        finally:
            con.close()
        plan = voice_pass._plan(chat["id"], {"user_name": "Alex"})
        assert plan["unlearnt"] == ["Dave"] and plan["room_on"] is True
        store = anchors.store()
        pid = store.ensure_person("Dave")
        store.add_clip(pid, loud_pcm(anchors.SUFFICIENT_SECONDS + 1), 16000,
                       source="introduction")
        con = db.connect()
        try:
            db.link_room_person(con, chat["id"], "Dave", pid)
        finally:
            con.close()
        # Dave can be named now, so elimination has nothing left to do -
        # this is the exit condition, and it is what ends the deadlock.
        assert voice_pass._plan(chat["id"],
                                {"user_name": "Alex"})["unlearnt"] == []


# ── 3. the pass end to end ──────────────────────────────────────────────────

def _room(app, *people):
    from fastapi.testclient import TestClient
    with TestClient(app, base_url="http://127.0.0.1") as c:
        chat = c.post("/api/chats", json={"participant_ids": []}).json()
    con = db.connect()
    try:
        db.set_chat_room_mode(con, chat["id"], True)
        for name, pid in people:
            db.add_room_person(con, chat["id"], name, person_id=pid)
    finally:
        con.close()
    return chat["id"]


def _run(chat_id, monkeypatch, got, written):
    fake_naming(monkeypatch)["answers"] = [got]
    monkeypatch.setattr(
        "backend.voice.httpx.post",
        lambda *a, **k: pytest.fail("the check must make NO ElevenLabs call"))

    async def capture(chat_id, commit_ts, payload, session, turn_id=None):
        written.append(payload)
        return None
    monkeypatch.setattr(diarize, "_attach_until_deadline", capture)
    asyncio.run(voice_pass.run(chat_id, loud_pcm(3.0), 16000, time.time(),
                               diarize.RoomSession(), {"user_name": "Alex"},
                               None))


def test_the_first_meeting_labels_learning_saves_nothing_and_stays_local(
        app, monkeypatch):
    from roomkit import _remember
    owner = _remember("Alex")
    chat = _room(app, ("Alex", owner), ("Dave", ""))
    written = []
    _run(chat, monkeypatch, naming_answer("new"), written)
    payload = written[0]
    assert payload["learning"] is True
    assert payload["labels"] == ["Dave"]
    # The name rides `uncertain` too, so every consumer written before the
    # learning marker existed keeps treating it as a guess rather than a
    # confident identification.
    assert payload["uncertain"] == ["Dave"]
    assert payload["source"] == "session"
    # Nothing is saved until someone confirms it.
    dave = anchors.store().find_by_name("Dave")
    assert dave is None or dave["clip_count"] == 0
    # A by-elimination decision IS a decision the health strip should show,
    # and it came from this device, so it stamps a 'local' pulse.
    assert diarize.last_decision(chat)["path"] == "local"


def test_without_anyone_to_eliminate_to_a_new_voice_names_nobody(
        app, monkeypatch):
    """Outside the first-meeting shape a new voice leaves the turn unnamed
    - no name, nothing saved, no cloud call - and the row carries the
    reason, so the seats and memory can read that it looked and could not
    tell."""
    chat = _room(app)
    written = []
    _run(chat, monkeypatch, naming_answer("new"), written)
    decision = diarize.last_decision(chat)
    assert decision["path"] == diarize.DECISION_UNRESOLVED
    assert decision["reason"] == "new_voice"
    assert anchors.store().people() == []
    assert written == [{"clusters": ["session"], "labels": [],
                        "uncertain": [], "source": "session",
                        "unresolved": "new_voice"}]


# ── 3b. an app's name is nobody to eliminate to (#602) ──────────────────────
#
# Field failure: an app's short name was seated from a mention, the only
# unlearnt seat in a room of two, and a remembered person's turns were
# named after it by elimination. The seat leaves before the pass plans the
# turn, so the voice is asked about instead. "kingfisher" is invented.

APPS = {"user_name": "Alex", "mcp_servers": {"kingfisher": {"command": "x"}}}


def _run_with(chat_id, monkeypatch, cfg, written):
    fake_naming(monkeypatch)["answers"] = [naming_answer("new")]

    async def capture(chat_id, commit_ts, payload, session, turn_id=None):
        written.append(payload)
        return None
    monkeypatch.setattr(diarize, "_attach_until_deadline", capture)
    asyncio.run(voice_pass.run(chat_id, loud_pcm(3.0), 16000, time.time(),
                               diarize.RoomSession(), cfg, None))


def _present(chat_id):
    con = db.connect()
    try:
        return [r["name"] for r in db.get_room_roster(con, chat_id,
                                                      present_only=True)]
    finally:
        con.close()


def test_an_app_named_seat_leaves_and_the_voice_is_asked_about(
        app, monkeypatch):
    from roomkit import _remember
    owner = _remember("Alex")
    sam = _remember("Sam")
    chat = _room(app, ("Alex", owner), ("Sam", sam), ("Fisher", ""))
    written = []
    _run_with(chat, monkeypatch, APPS, written)
    assert _present(chat) == ["Alex", "Sam"]
    assert written[0]["labels"] == [] and "learning" not in written[0]
    assert written[0]["unresolved"] == "new_voice"
    con = db.connect()
    try:
        asks = [f for f in db.get_room_flags(con, chat, open_only=True)
                if f["kind"] == "unknown_voice"]
    finally:
        con.close()
    assert len(asks) == 1


def test_a_seat_the_owner_made_by_hand_stays_whatever_its_name(
        app, monkeypatch):
    """A real guest who shares a name with an app gets their name from a
    tap, and the tap's seat is never taken away. Elimination still names a
    new voice after them, as for any unlearnt guest."""
    from roomkit import _remember
    owner = _remember("Alex")
    chat = _room(app, ("Alex", owner))
    con = db.connect()
    try:
        db.add_room_person(con, chat, "Fisher", seated_via="owner")
    finally:
        con.close()
    written = []
    _run_with(chat, monkeypatch, APPS, written)
    assert _present(chat) == ["Alex", "Fisher"]
    assert written[0]["labels"] == ["Fisher"] and written[0]["learning"]


def test_with_no_apps_configured_the_first_meeting_is_unchanged(
        app, monkeypatch):
    from roomkit import _remember
    owner = _remember("Alex")
    chat = _room(app, ("Alex", owner), ("Fisher", ""))
    written = []
    _run_with(chat, monkeypatch, {"user_name": "Alex"}, written)
    assert _present(chat) == ["Alex", "Fisher"]
    assert written[0]["labels"] == ["Fisher"] and written[0]["learning"]


def test_unseat_apps_keeps_a_seat_whose_voice_is_learnt(app):
    """A learnt voice is a real person by definition, so a remembered guest
    named like an app is never taken off. Otherwise the voice match would
    seat them and the next turn would take them off again."""
    from backend import room_state
    from roomkit import _remember
    owner = _remember("Alex")
    fisher = _remember("Fisher", clips=6)
    store = anchors.store()
    store.add_clip(fisher, loud_pcm(anchors.SUFFICIENT_SECONDS + 1), 16000,
                   source="introduction")
    assert store.find_by_name("Fisher")["sufficient"]
    chat = _room(app, ("Alex", owner), ("Fisher", fisher))
    assert room_state.unseat_apps(chat, APPS) == 0
    assert _present(chat) == ["Alex", "Fisher"]
    assert room_state.unseat_apps(chat, {"user_name": "Alex"}) == 0


# ── 4. the learning state reaches the seats ─────────────────────────────────

def _learning_msg(name="Alex", **extra):
    m = make_msg(1, "user", "we should get going")
    m["voice_labels"] = json.dumps(
        {"clusters": ["local"], "labels": [name], "uncertain": [name],
         "learning": True, "source": "cold-start", **extra})
    m["voice_turn_id"] = "t1"
    return m


def test_the_learning_head_names_the_person_and_says_it_is_learning(names, cfg):
    """The projection's whole job here: stop saying "identity pending" about
    someone the room can only contain, without over-claiming. Not the
    pending head, not voice-confirmed - the name, plus what it is worth."""
    cfg = dict(cfg, room_mode=True)
    text = build_anthropic_messages(
        "claude", [_learning_msg("Sam")], names, cfg)[0]["content"][0]["text"]
    assert text.startswith(f"[Sam{LEARNING_SUFFIX} · ")
    assert PENDING_IDENTITY_HEAD not in text
    assert VOICE_CONFIRMED_SUFFIX not in text
    assert "unidentified speaker" not in text


def test_the_learning_head_speaks_the_preferred_name(names, cfg):
    """#28, naming is law: a learning head resolves through the preferred
    map exactly like every other named head."""
    cfg = dict(cfg, room_mode=True, preferred_names={"dave": "Mateo"})
    text = build_anthropic_messages(
        "claude", [_learning_msg("Dave")], names, cfg)[0]["content"][0]["text"]
    assert text.startswith(f"[Mateo{LEARNING_SUFFIX} · ")


def test_a_learning_marker_without_a_single_label_falls_through(names, cfg):
    """Defensive: the marker only ever accompanies one by-elimination name.
    Anything else takes the ordinary uncertain path rather than inventing a
    head - two names cannot both be the only person in the room."""
    cfg = dict(cfg, room_mode=True)
    two = _learning_msg("Sam")
    two["voice_labels"] = json.dumps(
        {"clusters": ["a", "b"], "labels": ["Sam", "Dave"],
         "uncertain": ["Sam", "Dave"], "learning": True})
    text = build_anthropic_messages(
        "claude", [two], names, cfg)[0]["content"][0]["text"]
    assert LEARNING_SUFFIX not in text
    assert "nidentified speaker" in text


def test_the_learning_explainer_lives_in_the_stable_block(cfg):
    """Constant text (it names nobody and varies with nothing), so it sits
    in the STABLE cached block with the rest of the room-label meanings -
    the cache-split pins in tests/test_cache_split.py depend on that."""
    stable, volatile = split_system_prompt(
        PARTICIPANT, ROSTER, dict(cfg), None, "", False)
    for tell in ("(learning this voice)", "by elimination",
                 "likely rather than certain"):
        assert tell in stable, tell
        assert tell not in volatile


# ── the ordering pin (#28, twelfth field test) ──────────────────────────────

def test_a_finished_verdict_is_on_the_row_at_insert(tmp_path):
    """THE ORDERING BUG. The identity check finishes seconds before /send
    creates the row (it fires at the start of the silence gap), but the label
    could only be written AFTER the row existed - and /send dispatches the
    round that renders the transcript in the same breath. So the model
    answering a turn read "identity pending" on the very turn the browser
    already showed as confirmed. A parked verdict must therefore ride the
    INSERT itself, not a write that follows it."""
    from backend import diarize
    from backend.app import create_app
    from backend.config import Settings
    from fastapi.testclient import TestClient

    diarize._PENDING_LABELS.clear()
    app = create_app(Settings(data_dir=str(tmp_path / "data"),
                              memory_url="http://127.0.0.1:1",
                              user_name="Alex"))
    payload = {"clusters": ["local"], "labels": ["Alex"], "uncertain": []}
    diarize.park_label("turn-abc", payload)
    with TestClient(app, base_url="http://127.0.0.1") as c:
        chat = c.post("/api/chats", json={"participant_ids": []}).json()
        con = db.connect()
        try:
            msg = db.insert_message(con, chat["id"], "user", "hello",
                                    voice_turn_id="turn-abc",
                                    voice_labels=diarize.claim_label("turn-abc"))
            row = con.execute("SELECT voice_labels, labels_updated_at "
                              "FROM messages WHERE id=?",
                              (msg["id"],)).fetchone()
        finally:
            con.close()
    # the label is ON the row from the moment it exists - no later write
    assert json.loads(row["voice_labels"])["labels"] == ["Alex"]
    assert row["labels_updated_at"] > 0      # the live cursor sees it too
    # and the park is single-use, so the pass cannot double-write it
    assert diarize.claim_label("turn-abc") is None


def test_an_unclaimed_park_expires_and_never_leaks_to_another_turn(tmp_path):
    from backend import diarize
    diarize._PENDING_LABELS.clear()
    diarize.park_label("turn-1", {"labels": ["Alex"], "uncertain": []})
    # a different turn never sees it
    assert diarize.claim_label("turn-2") is None
    # bounded: flooding parks evicts oldest rather than growing forever
    for n in range(diarize._PENDING_MAX + 5):
        diarize.park_label(f"flood-{n}", {"labels": ["Sam"], "uncertain": []})
    assert len(diarize._PENDING_LABELS) <= diarize._PENDING_MAX
