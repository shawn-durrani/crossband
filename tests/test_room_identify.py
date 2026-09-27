"""Remembered voices become NAMES (#28 phase 2, #482).

Driven end to end through the realtime relay with the same FakeEleven
double as tests/test_room_mode.py, and the session naming stood in for by
roomkit.fake_naming - the latency pins stay in that file and still hold;
this file pins what naming adds ON TOP.

1. A named voice labels the turn with the person's NAME, in any session:
   banks, not session state, carry identity.
2. A new voice with no unambiguous elimination stays unnamed and raises
   the 'unknown_voice' ask - one open ask at a time - which a later
   introduction resolves. Nobody's bank is fed from it.
3. The LLM mismatch cross-check raises a content-free flag and NEVER mutates
   the label it doubts.
4. Tap-to-correct rewrites the label, resolves the turn's flags, and feeds
   the cached single-speaker audio to the person's anchors as ground truth.
5. Roster/flag changes ride the live-events stream content-free; the roster
   snapshot and remembered-voices endpoints serve the UI; forget deletes.
6. With the room off, a turn's audio is remembered under its message for an
   introduction to claim, and no cloud call is made.
"""

import asyncio
import base64
import json
import threading
import time

import httpx
import pytest
from fastapi.testclient import TestClient

from backend import anchors, auth, db, events, introductions
from backend.app import create_app
from backend.config import Settings
from backend.routers import voice as voice_router
from roomkit import (_insert_user_message, _message_labels, _wait_for,
                     as_utility_completion, fake_naming, loud_pcm,
                     naming_answer)


@pytest.fixture
def app(tmp_path):
    settings = Settings(data_dir=str(tmp_path / "data"),
                        memory_url="http://127.0.0.1:1")
    return create_app(settings)


class FakeEleven:
    def __init__(self):
        self.sent = []
        self.queue = asyncio.Queue()

    async def __aenter__(self):
        return self

    async def __aexit__(self, *a):
        return False

    async def send(self, raw):
        msg = json.loads(raw)
        self.sent.append(msg)
        if msg.get("commit"):
            self.queue.put_nowait(json.dumps(
                {"message_type": "committed_transcript", "text": "hello world"}))
        elif msg.get("audio_base_64"):
            self.queue.put_nowait(json.dumps(
                {"message_type": "partial_transcript", "text": "hello"}))

    def __aiter__(self):
        return self

    async def __anext__(self):
        return await self.queue.get()


@pytest.fixture
def relay(app, monkeypatch):
    fake = FakeEleven()
    monkeypatch.setattr(voice_router.websockets, "connect",
                        lambda *a, **kw: fake)
    monkeypatch.setattr(voice_router.voice, "enabled", lambda: True)
    monkeypatch.setattr(voice_router.voice, "api_key", lambda: "test-key")
    monkeypatch.setattr(voice_router.engine, "prewarm_recall",
                        lambda *a, **kw: None)
    app.state.allowed_hosts = {"testserver", "127.0.0.1", "localhost", "::1"}
    # These suites drive the relays as a loopback browser would. The
    # TestClient's websocket host is "testserver", so the gate must read it
    # as this machine; otherwise it is a trusted non-loopback host and the
    # pre-enrolment gate correctly holds it (see test_auth_gate).
    monkeypatch.setattr(auth, "GATE_LOOPBACK_HOSTS",
                        auth.GATE_LOOPBACK_HOSTS | {"testserver"})
    return fake


@pytest.fixture
def naming(monkeypatch):
    return fake_naming(monkeypatch)


@pytest.fixture
def cloud(monkeypatch):
    """Every batch call to ElevenLabs, recorded. Naming makes none."""
    calls = []
    monkeypatch.setattr(voice_router.voice.httpx, "post",
                        lambda url, *a, **k: calls.append(url))
    return calls


def _frame(data, commit=False):
    return {"audio": base64.b64encode(data).decode(),
            "sample_rate": 16000, "commit": commit}


def _flags(chat_id, open_only=True):
    con = db.connect()
    try:
        return db.get_room_flags(con, chat_id, open_only=open_only)
    finally:
        con.close()


def _roster(chat_id):
    con = db.connect()
    try:
        return db.get_room_roster(con, chat_id, present_only=True)
    finally:
        con.close()


def _setup_room(client, sufficient=(), pending=()):
    """A chat with room mode on and a roster: `sufficient` people get 3x2s
    anchors (over the bar), `pending` people get roster rows with no
    anchors."""
    chat = client.post("/api/chats", json={"participant_ids": []}).json()
    con = db.connect()
    db.set_chat_room_mode(con, chat["id"], True)
    store = anchors.store()
    for name in sufficient:
        pid = store.ensure_person(name)
        # #83: a remembered-sufficient person IS an introduced person - the
        # first clip carries the introduction that vouched the bank.
        assert store.add_clip(pid, loud_pcm(2.0), 16000,
                              source="introduction")
        for _ in range(2):
            assert store.add_clip(pid, loud_pcm(2.0), 16000,
                                  source="accumulated")
        db.add_room_person(con, chat["id"], name, person_id=pid)
    for name in pending:
        db.add_room_person(con, chat["id"], name)
    con.close()
    return chat


def _pid(name):
    return anchors.store().find_by_name(name)["person_id"]


# ── 1. names ────────────────────────────────────────────────────────────────

def test_a_named_voice_labels_the_turn_with_the_persons_name(
        app, relay, naming, cloud):
    """The whole point: the naming names the voice, and the turn carries
    that person's name - with no cloud call."""
    with TestClient(app, base_url="http://127.0.0.1") as c:
        chat = _setup_room(c, sufficient=["Alex"])
        naming["answers"] = [naming_answer(name="Alex", pid=_pid("Alex"),
                                           score=0.95)]
        with c.websocket_connect("/api/voice/stt-stream") as ws:
            ws.send_json({"chat_id": chat["id"]})  # NO client toggle
            assert ws.receive_json()["session"]  # #134 handshake
            ws.send_json(_frame(loud_pcm(1.5), commit=True))
            assert ws.receive_json() == {"final": "hello world"}
            msg = _insert_user_message(chat["id"])
            labels = _wait_for(lambda: _message_labels(msg["id"]))
            ws.send_json({"done": True})
    assert json.loads(labels) == {"clusters": ["session"],
                                  "labels": ["Alex"], "uncertain": [],
                                  "source": "session", "score": 0.95}
    assert cloud == []


def test_remembered_voice_reidentifies_in_a_fresh_session(
        app, relay, naming, cloud):
    """A NEW websocket session still names the remembered voice - banks,
    not session state, carry identity. No introduction happened in either
    session."""
    with TestClient(app, base_url="http://127.0.0.1") as c:
        chat = _setup_room(c, sufficient=["Alex"])
        naming["answers"] = [naming_answer(name="Alex", pid=_pid("Alex")),
                             naming_answer(name="Alex", pid=_pid("Alex"))]
        for _ in (1, 2):
            with c.websocket_connect("/api/voice/stt-stream") as ws:
                ws.send_json({"chat_id": chat["id"]})
                assert ws.receive_json()["session"]  # #134 handshake
                ws.send_json(_frame(loud_pcm(1.5), commit=True))
                assert ws.receive_json() == {"final": "hello world"}
                msg = _insert_user_message(chat["id"])
                labels = _wait_for(lambda: _message_labels(msg["id"]))
                assert json.loads(labels)["labels"] == ["Alex"]
                ws.send_json({"done": True})


# ── 2. the ask ──────────────────────────────────────────────────────────────

def test_an_unknown_voice_stays_unnamed_and_asks_once(app, relay, naming,
                                                      cloud):
    """Two unlearnt people, one new voice: no elimination possible. The
    turn stays unnamed and says why, ONE 'unknown_voice' flag opens (not
    one per turn), nobody's bank is fed, and a later introduction resolves
    the ask. The owner (the default `user_name`) is learnt, so a new voice
    can't be them."""
    with TestClient(app, base_url="http://127.0.0.1") as c:
        chat = _setup_room(c, sufficient=["User"], pending=["Sam", "Dave"])
        naming["answers"] = [naming_answer("new"), naming_answer("new")]
        with c.websocket_connect("/api/voice/stt-stream") as ws:
            ws.send_json({"chat_id": chat["id"]})
            assert ws.receive_json()["session"]  # #134 handshake
            for n in (1, 2):
                ws.send_json(_frame(loud_pcm(1.5), commit=True))
                assert ws.receive_json() == {"final": "hello world"}
                msg = _insert_user_message(chat["id"], f"turn {n}")
                labels = _wait_for(lambda: _message_labels(msg["id"]))
                assert _wait_for(lambda: _flags(chat["id"]))
            ws.send_json({"done": True})
        assert json.loads(labels)["unresolved"] == "new_voice"
        flags = _flags(chat["id"])
        assert len(flags) == 1               # one open ask, not one per turn
        assert flags[0]["kind"] == "unknown_voice"
        assert flags[0]["message_id"] is not None
        # nobody's anchor was fed - an unknown voice is not ground truth
        assert anchors.store().find_by_name("Sam") is None
        assert anchors.store().find_by_name("Dave") is None
        # answering in chat (a confirmed introduction) closes the ask
        introductions.apply_scan(chat["id"],
                                 {"introductions": ["Mateo"],
                                  "departures": []},
                                 {"user_name": "Alex", "room_roster_max": 6})
        assert _flags(chat["id"]) == []
    assert cloud == []


# ── 4. the mismatch cross-check ─────────────────────────────────────────────

def test_mismatch_flags_but_never_mutates_the_label(app, relay, naming,
                                                    monkeypatch):
    """The cross-check doubts a named turn: a 'mismatch' flag opens carrying
    names only, and the voice label is EXACTLY what it was - there is no code
    path from the check to the label."""
    async def fake_utility(prompt, cfg, max_tokens=2000):
        return json.dumps({"mismatch": True, "suspected": "Sam"})
    monkeypatch.setattr("backend.llm_util.utility_complete_with_usage",
                        as_utility_completion(fake_utility))
    with TestClient(app, base_url="http://127.0.0.1") as c:
        chat = _setup_room(c, sufficient=["Dave", "Sam"])
        naming["answers"] = [naming_answer(name="Dave", pid=_pid("Dave"))]
        with c.websocket_connect("/api/voice/stt-stream") as ws:
            ws.send_json({"chat_id": chat["id"]})
            assert ws.receive_json()["session"]  # #134 handshake
            ws.send_json(_frame(loud_pcm(1.5), commit=True))
            assert ws.receive_json() == {"final": "hello world"}
            msg = _insert_user_message(chat["id"], "my husband thinks so too")
            labels = _wait_for(lambda: _message_labels(msg["id"]))
            flag = _wait_for(lambda: _flags(chat["id"]))
            ws.send_json({"done": True})
    assert flag[0]["kind"] == "mismatch"
    assert flag[0]["label"] == "Dave"
    assert flag[0]["suspected"] == "Sam"
    assert flag[0]["message_id"] == msg["id"]
    # THE PIN: the label is untouched, before and after the flag
    assert json.loads(_message_labels(msg["id"]))["labels"] == json.loads(
        labels)["labels"] == ["Dave"]


def test_mismatch_parse_verdict_is_defensive():
    from backend import mismatch
    ok = mismatch.parse_verdict('{"mismatch": true, "suspected": "Alex"}')
    assert ok == {"mismatch": True, "suspected": "Alex"}
    assert mismatch.parse_verdict(
        '{"mismatch": false, "suspected": "Alex"}') == {
        "mismatch": False, "suspected": ""}
    for bad in (None, "", "yes", "{}", '{"mismatch": "true"}', "[1]"):
        assert mismatch.parse_verdict(bad)["mismatch"] is False, bad


def test_mismatch_keyless_is_a_quiet_no_op(app, relay, naming):
    """No utility key: the check degrades to nothing - no flag, no error, and
    obviously no label change."""
    with TestClient(app, base_url="http://127.0.0.1") as c:
        chat = _setup_room(c, sufficient=["Dave"])
        naming["answers"] = [naming_answer(name="Dave", pid=_pid("Dave"))]
        with c.websocket_connect("/api/voice/stt-stream") as ws:
            ws.send_json({"chat_id": chat["id"]})
            assert ws.receive_json()["session"]  # #134 handshake
            ws.send_json(_frame(loud_pcm(1.5), commit=True))
            ws.receive_json()
            msg = _insert_user_message(chat["id"])
            _wait_for(lambda: _message_labels(msg["id"]))
            ws.send_json({"done": True})
        time.sleep(0.3)
        assert _flags(chat["id"]) == []


# ── 5. tap-to-correct ───────────────────────────────────────────────────────

def test_reassign_rewrites_label_resolves_flags_and_feeds_the_anchor(app):
    with TestClient(app, base_url="http://127.0.0.1") as c:
        chat = c.post("/api/chats", json={"participant_ids": []}).json()
        msg = _insert_user_message(chat["id"], "actually I disagree")
        con = db.connect()
        db.set_message_voice_labels(con, msg["id"],
                                    {"clusters": ["s0"], "labels": ["Shawn"],
                                     "uncertain": []})
        db.insert_room_flag(con, chat["id"], "mismatch",
                            message_id=msg["id"], label="Shawn",
                            suspected="Alex")
        con.close()
        anchors.remember_audio(msg["id"], loud_pcm(2.0), 16000, 1)
        r = c.post(f"/api/chats/{chat['id']}/messages/{msg['id']}/speaker",
                   json={"name": "Alex"})
        assert r.status_code == 200
        assert r.json()["learned"] is True
        data = json.loads(_message_labels(msg["id"]))
        assert data["labels"] == ["Alex"] and data["corrected"] is True
        # memory reads the owner's answer as owner-correction, which membro
        # always binds on, not as the weakest claim (#484)
        assert data["source"] == "correction"
        from backend.memory_client import speaker_identity
        assert speaker_identity({"voice_labels": data}, "guest:Alex",
                                {})["method"] == "owner-correction"
        assert _flags(chat["id"]) == []                  # doubt answered
        alex = anchors.store().find_by_name("Alex")
        assert alex and alex["clip_count"] == 1          # ground truth stored
        row = next(p for p in _roster(chat["id"]) if p["name"] == "Alex")
        assert row["person_id"] == alex["person_id"]


def test_reassign_without_cached_audio_still_corrects_but_learns_nothing(app):
    with TestClient(app, base_url="http://127.0.0.1") as c:
        chat = c.post("/api/chats", json={"participant_ids": []}).json()
        msg = _insert_user_message(chat["id"])
        r = c.post(f"/api/chats/{chat['id']}/messages/{msg['id']}/speaker",
                   json={"name": "Alex"})
        assert r.status_code == 200 and r.json()["learned"] is False
        assert json.loads(_message_labels(msg["id"]))["labels"] == ["Alex"]
        alex = anchors.store().find_by_name("Alex")
        assert alex and alex["clip_count"] == 0


def test_reassign_never_feeds_a_multi_voice_utterance(app):
    """A two-cluster utterance is not clean evidence of anyone's voice: the
    correction applies, the audio is discarded."""
    with TestClient(app, base_url="http://127.0.0.1") as c:
        chat = c.post("/api/chats", json={"participant_ids": []}).json()
        msg = _insert_user_message(chat["id"])
        anchors.remember_audio(msg["id"], loud_pcm(2.0), 16000, 2)
        r = c.post(f"/api/chats/{chat['id']}/messages/{msg['id']}/speaker",
                   json={"name": "Alex"})
        assert r.status_code == 200 and r.json()["learned"] is False
        assert anchors.store().find_by_name("Alex")["clip_count"] == 0


def test_reassign_guards_its_inputs(app):
    with TestClient(app, base_url="http://127.0.0.1") as c:
        chat = c.post("/api/chats", json={"participant_ids": []}).json()
        con = db.connect()
        ai_msg = db.insert_message(con, chat["id"], "claude", "a reply")
        con.close()
        assert c.post(f"/api/chats/{chat['id']}/messages/{ai_msg['id']}/speaker",
                      json={"name": "Alex"}).status_code == 400
        assert c.post(f"/api/chats/{chat['id']}/messages/999999/speaker",
                      json={"name": "Alex"}).status_code == 404
        msg = _insert_user_message(chat["id"])
        assert c.post(f"/api/chats/{chat['id']}/messages/{msg['id']}/speaker",
                      json={"name": "  "}).status_code == 400


# ── 6. snapshots, people, forget, override ──────────────────────────────────

def test_roster_snapshot_carries_sufficiency_and_flags(app):
    with TestClient(app, base_url="http://127.0.0.1") as c:
        chat = _setup_room(c, sufficient=["Shawn"], pending=["Alex"])
        con = db.connect()
        db.insert_room_flag(con, chat["id"], "unknown_voice")
        con.close()
        snap = c.get(f"/api/chats/{chat['id']}/roster").json()
        assert snap["room_mode"] is True
        assert snap["cap"] == 6
        by_name = {p["name"]: p for p in snap["roster"]}
        assert by_name["Shawn"]["sufficient"] is True
        assert by_name["Alex"]["sufficient"] is False    # anchor pending
        assert [f["kind"] for f in snap["flags"]] == ["unknown_voice"]


def test_people_endpoint_and_forget_deletes_audio_and_unlinks(app, tmp_path):
    import os
    with TestClient(app, base_url="http://127.0.0.1") as c:
        chat = _setup_room(c, sufficient=["Alex"])
        people = c.get("/api/voice/people").json()["people"]
        assert people[0]["name"] == "Alex" and people[0]["sufficient"]
        pid = people[0]["person_id"]
        root = anchors.store().root
        assert any(f.endswith(".wav") for f in os.listdir(root))
        assert c.delete(f"/api/voice/people/{pid}").json() == {"ok": True}
        assert not any(f.endswith(".wav") for f in os.listdir(root))
        assert c.get("/api/voice/people").json()["people"] == []
        # the roster row survives but drops back to anchor-pending
        row = next(p for p in _roster(chat["id"]) if p["name"] == "Alex")
        assert row["person_id"] == ""
        assert c.delete(f"/api/voice/people/{pid}").status_code == 404


def test_rename_sets_preferred_name_and_roster_shows_it(app):
    """The correctable display name (#28 phase 3): renaming a remembered
    voice changes what the roster snapshot and people endpoint SHOW, while
    `name` stays the identity key the voice labels keep matching."""
    with TestClient(app, base_url="http://127.0.0.1") as c:
        chat = _setup_room(c, sufficient=["Lex"])
        pid = c.get("/api/voice/people").json()["people"][0]["person_id"]
        assert c.post(f"/api/voice/people/{pid}/name",
                      json={"name": "  Alex  "}).json() == {"ok": True}
        person = c.get("/api/voice/people").json()["people"][0]
        assert person["preferred_name"] == "Alex"
        assert person["name"] == "Lex"  # identity untouched
        row = next(p for p in c.get(f"/api/chats/{chat['id']}/roster")
                   .json()["roster"] if p["name"] == "Lex")
        assert row["display_name"] == "Alex"
        # guards: unknown person 404s; a letterless name 400s
        assert c.post("/api/voice/people/nope/name",
                      json={"name": "Alex"}).status_code == 404
        assert c.post(f"/api/voice/people/{pid}/name",
                      json={"name": "!!!"}).status_code == 400


def test_room_mode_patch_override_updates_the_flag(app):
    with TestClient(app, base_url="http://127.0.0.1") as c:
        chat = c.post("/api/chats", json={"participant_ids": []}).json()
        r = c.patch(f"/api/chats/{chat['id']}", json={"room_mode": True}).json()
        assert r["room_mode"] == 1
        r = c.patch(f"/api/chats/{chat['id']}", json={"room_mode": False}).json()
        assert r["room_mode"] == 0


def test_flag_dismissal_endpoint(app):
    with TestClient(app, base_url="http://127.0.0.1") as c:
        chat = c.post("/api/chats", json={"participant_ids": []}).json()
        con = db.connect()
        flag = db.insert_room_flag(con, chat["id"], "unknown_voice")
        con.close()
        assert c.post(f"/api/chats/{chat['id']}/flags/{flag['id']}/resolve"
                      ).json() == {"ok": True}
        assert _flags(chat["id"]) == []
        assert c.post(f"/api/chats/{chat['id']}/flags/{flag['id']}/resolve"
                      ).status_code == 404


def test_a_room_off_turn_remembers_its_audio_for_an_introduction(
        app, relay, naming, cloud):
    """Room mode OFF: the commit still slices the tee and the turn still
    gets its check, and its audio is remembered under its message for a
    later introduction to claim as the owner's first clip. No cloud call."""
    with TestClient(app, base_url="http://127.0.0.1") as c:
        chat = c.post("/api/chats", json={"participant_ids": []}).json()
        utter = loud_pcm(1.0)
        with c.websocket_connect("/api/voice/stt-stream") as ws:
            ws.send_json({"chat_id": chat["id"]})
            assert ws.receive_json()["session"]  # #134 handshake
            ws.send_json({**_frame(utter, commit=True), "turn_id": "t1"})
            assert ws.receive_json() == {"final": "hello world",
                                         "turn_id": "t1"}
            msg = _insert_user_message(chat["id"], voice_turn_id="t1")
            entry = _wait_for(lambda: anchors.peek_audio(msg["id"]))
            ws.send_json({"done": True})
        assert entry[0] == utter and entry[2] == 1
        assert cloud == []


# ── 7. the live-events stream ───────────────────────────────────────────────

def test_roster_and_flag_changes_ride_the_stream_content_free(tmp_path):
    create_app(Settings(data_dir=str(tmp_path / "data"),
                        memory_url="http://127.0.0.1:1"))

    async def go():
        events.bind_loop(asyncio.get_running_loop())
        con = db.connect()
        cur = con.execute(
            "INSERT INTO chats(title, created_at, updated_at) VALUES('t', 0, 0)")
        con.commit()
        chat_id = cur.lastrowid
        primer = db.insert_message(con, chat_id, "system", "primer")

        gen = events.stream(since=primer["id"] - 1, heartbeat_secs=25)
        first = await asyncio.wait_for(gen.__anext__(), timeout=1.0)
        assert json.loads(first[6:])["id"] == primer["id"]

        db.add_room_person(con, chat_id, "Alex")
        got = await asyncio.wait_for(gen.__anext__(), timeout=1.0)
        assert json.loads(got[6:]) == {"type": "room_roster",
                                       "chat_id": chat_id}
        assert "Alex" not in got  # content-free: refetch the snapshot instead

        flag = db.insert_room_flag(con, chat_id, "unknown_voice",
                                   message_id=primer["id"])
        got = await asyncio.wait_for(gen.__anext__(), timeout=1.0)
        ev = json.loads(got[6:])
        assert ev == {"type": "room_flag", "chat_id": chat_id,
                      "id": flag["id"], "message_id": primer["id"],
                      "kind": "unknown_voice", "resolved": False}

        db.resolve_room_flags(con, chat_id, flag_id=flag["id"])
        got = await asyncio.wait_for(gen.__anext__(), timeout=1.0)
        assert json.loads(got[6:])["resolved"] is True
        con.close()
        await gen.aclose()

    asyncio.run(go())


# ── the phantom owner-double (#28, tenth field test) ────────────────────────

def test_correcting_a_turn_to_an_owner_alias_never_mints_a_second_person(
        tmp_path, monkeypatch):
    """THE BUG THAT COST A WEEKEND. With `user_name` left at its default, the
    owner was enrolled as one person and a tap-correction to their real name
    minted a SECOND person holding byte-identical copies of the same voice.
    Two perfect matches for one voice means every later identification is
    "ambiguous", so the owner sat on "identity pending" forever. A correction
    naming the owner (or a spelling variant of it) must route to the owner's
    EXISTING record - never mint a twin."""
    anchors.clear_recent_audio()
    app = create_app(Settings(data_dir=str(tmp_path / "data"),
                              memory_url="http://127.0.0.1:1",
                              user_name="Shawn"))
    # The matcher is offline in the keyless suite, so stub the one call the
    # guard makes: this turn's audio IS the owner's voice. That is the
    # reliable half of the guard - "Sean" is two edits from "Shawn", so the
    # spelling half deliberately does not catch it (that is how the live
    # phantom was minted).
    from backend import voiceid
    monkeypatch.setattr(voiceid, "identify_utterance",
                        lambda pcm, sr, cands, cfg: {
                            "status": voiceid.MATCH,
                            "person_id": cands[0]["person_id"],
                            "name": cands[0]["name"], "score": 0.93,
                            "reason": "match"})
    with TestClient(app, base_url="http://127.0.0.1") as c:
        chat = c.post("/api/chats", json={"participant_ids": []}).json()
        store = anchors.store()
        owner_pid = store.ensure_person("Shawn")
        assert store.add_clip(owner_pid, loud_pcm(8.0), 16000,
                              source="introduction")
        # the owner corrects a turn to a MIS-HEARD spelling of their own name
        msg = _insert_user_message(chat["id"], "that was me")
        anchors.remember_audio(msg["id"], loud_pcm(8.0), 16000, 1)
        r = c.post(f"/api/chats/{chat['id']}/messages/{msg['id']}/speaker",
                   json={"name": "Sean"})
        assert r.status_code == 200
    people = anchors.store().people()
    # exactly ONE person: the owner. No "Sean" twin.
    assert [p["name"] for p in people] == ["Shawn"]
    assert people[0]["person_id"] == owner_pid
    assert people[0]["clip_count"] == 2      # the correction fed the OWNER
    # and the turn's label carries the canonical name, not the misheard one
    assert json.loads(_message_labels(msg["id"]))["labels"] == ["Shawn"]
