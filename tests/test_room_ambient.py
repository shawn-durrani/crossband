"""A room that switches itself on (#28, #461, #482): no trigger phrase.

With room mode OFF every committed utterance still gets the voice check
(backend/voice_pass.py), on this computer. What these tests prove, in
order:

1. THE LATENCY PIN: the committed transcript arrives while the check is
   deliberately wedged open - the live path never waits on it.
2. The rules, end to end through the relay: the owner's voice labels the
   turn as the owner, voice-confirmed, and arms NOTHING; a remembered
   non-owner arms room mode, joins the roster, names the turn and gets
   the mismatch cross-check; a clear new voice (decidable only because
   the owner is enrolled) arms, says "new voice", and raises the ask; with
   the owner not enrolled a new voice changes nothing.
3. THE CHECK IS LOCAL-ONLY: none of those makes an ElevenLabs batch call.
4. DISARM IS SACRED: "solo mode" sets a durable ambient-off (even with room
   mode already off), and every explicit re-enable (arm command,
   introduction, manual toggle) clears it. Solo still checks every turn, so
   the turn says who spoke, but in solo nothing arms, seats or asks, and a
   mid-session disarm is honoured at the next commit.
5. THE ASK FIRES WHEN /send CLAIMS THE LABEL (#461): labels ride the
   insert, so the check nearly always found its label already on the row
   and stopped before raising the ask.
6. A SESSION THAT OPENED ARMED STILL CHECKS ONCE THE ROOM GOES SOLO
   (#461): the room state is read per turn, never frozen at session open.

The session naming is stood in for by roomkit.fake_naming.
"""

import base64
import json
import threading
import time

import pytest
from fastapi.testclient import TestClient

from backend import anchors, auth, db, diarize
from backend.app import create_app
from backend.config import Settings
from backend.routers import voice as voice_router
from roomkit import (_insert_user_message, _message_labels, _remember,
                     _wait_for, fake_naming, loud_pcm, naming_answer)


@pytest.fixture
def app(tmp_path):
    settings = Settings(data_dir=str(tmp_path / "data"),
                        memory_url="http://127.0.0.1:1",
                        user_name="Alex")
    return create_app(settings)


class FakeEleven:
    def __init__(self):
        self.sent = []
        self.queue = __import__("asyncio").Queue()

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
def batch_calls(monkeypatch):
    """Counts EL batch STT calls - the check must make NONE."""
    state = {"calls": 0}

    def fake_post(url, headers=None, data=None, files=None, timeout=None):
        state["calls"] += 1
        import httpx
        return httpx.Response(200, json={"language_code": "en",
                                         "text": "hello world", "words": []},
                              request=httpx.Request("POST", url))

    monkeypatch.setattr(voice_router.voice.httpx, "post", fake_post)
    return state


@pytest.fixture
def naming(monkeypatch):
    return fake_naming(monkeypatch)


def _frame(data, commit=False):
    return {"audio": base64.b64encode(data).decode(),
            "sample_rate": 16000, "commit": commit}


def _chat_state(chat_id):
    con = db.connect()
    try:
        row = con.execute("SELECT room_mode, ambient_off FROM chats WHERE id=?",
                          (chat_id,)).fetchone()
        return bool(row["room_mode"]), bool(row["ambient_off"])
    finally:
        con.close()


def _roster(chat_id):
    con = db.connect()
    try:
        return db.get_room_roster(con, chat_id, present_only=True)
    finally:
        con.close()


def _flags(chat_id):
    con = db.connect()
    try:
        return db.get_room_flags(con, chat_id, open_only=True)
    finally:
        con.close()


def _new_chat(c):
    return c.post("/api/chats", json={"participant_ids": []}).json()


# ── pure rules ──────────────────────────────────────────────────────────────

def test_owner_sufficient_rules():
    """Only an enrolled owner lets a new voice mean "not the owner"."""
    somebody = [{"name": "Sam", "sufficient": True}]
    assert diarize.owner_sufficient(
        [{"name": "Alex", "sufficient": True}], "Alex") is True
    assert diarize.owner_sufficient(
        [{"name": "Alex", "sufficient": False}], "Alex") is False
    assert diarize.owner_sufficient(somebody, "Alex") is False


# ── 1. the latency pin ──────────────────────────────────────────────────────

def test_committed_transcript_arrives_while_the_check_is_wedged(
        app, relay, batch_calls, naming):
    pid = _remember("Sam")
    naming["gate"] = threading.Event()
    naming["answers"] = [naming_answer(name="Sam", pid=pid)]
    with TestClient(app, base_url="http://127.0.0.1") as c:
        chat = _new_chat(c)
        with c.websocket_connect("/api/voice/stt-stream") as ws:
            ws.send_json({"chat_id": chat["id"]})
            assert ws.receive_json()["session"]  # #134 handshake
            ws.send_json(_frame(loud_pcm(1.5), commit=True))
            # the live path completes while the check is blocked
            assert ws.receive_json() == {"final": "hello world"}
            assert _chat_state(chat["id"])[0] is False
            naming["gate"].set()
            assert _wait_for(lambda: _chat_state(chat["id"])[0])
            ws.send_json({"done": True})
    assert batch_calls["calls"] == 0  # local-only, even on the arm


# ── 2. the decision table, end to end ───────────────────────────────────────

def test_owner_voice_labels_but_never_arms(app, relay, batch_calls, naming):
    """#28 PR-C: the owner's identity is shown, not hidden. A confident
    owner naming in a room-off session writes an owner-marked confident
    label on the turn - so solo chats can answer "who is speaking?" - and
    nothing else: no arm, no roster, no ElevenLabs call."""
    _remember("Alex")  # the owner, sufficiently enrolled
    pid_owner = anchors.store().find_by_name("Alex")["person_id"]
    naming["answers"] = [naming_answer(name="Alex", pid=pid_owner)]
    with TestClient(app, base_url="http://127.0.0.1") as c:
        chat = _new_chat(c)
        with c.websocket_connect("/api/voice/stt-stream") as ws:
            ws.send_json({"chat_id": chat["id"]})
            assert ws.receive_json()["session"]  # #134 handshake
            ws.send_json(_frame(loud_pcm(1.5), commit=True))
            assert ws.receive_json() == {"final": "hello world"}
            msg = _insert_user_message(chat["id"])
            labels = _wait_for(lambda: _message_labels(msg["id"]))
            ws.send_json({"done": True})
    on, _ = _chat_state(chat["id"])
    assert on is False
    assert _roster(chat["id"]) == []
    parsed = json.loads(labels)
    assert parsed["labels"] == ["Alex"]
    assert parsed["uncertain"] == []
    assert parsed["owner"] is True   # the chips' voice-confirmed marker
    assert batch_calls["calls"] == 0
    # #237: the owner-by-voice guard and tap-to-correct need this turn's
    # audio in the remembered ring, on solo chats too.
    assert anchors.peek_audio(msg["id"]) is not None


def test_remembered_voice_arms_names_and_rosters(app, relay, batch_calls,
                                                 naming, monkeypatch):
    pid = _remember("Sam")
    naming["answers"] = [naming_answer(name="Sam", pid=pid)]
    # #237: the turn that ARMS the room gets the mismatch cross-check, as
    # every named guest's turn does.
    checks = []
    monkeypatch.setattr("backend.mismatch.schedule_check",
                        lambda *a, **k: checks.append(a))
    with TestClient(app, base_url="http://127.0.0.1") as c:
        chat = _new_chat(c)
        with c.websocket_connect("/api/voice/stt-stream") as ws:
            ws.send_json({"chat_id": chat["id"]})
            assert ws.receive_json()["session"]  # #134 handshake
            ws.send_json(_frame(loud_pcm(1.5), commit=True))
            assert ws.receive_json() == {"final": "hello world"}
            msg = _insert_user_message(chat["id"])
            labels = _wait_for(lambda: _message_labels(msg["id"]))
            assert _wait_for(lambda: _chat_state(chat["id"])[0])
            ws.send_json({"done": True})
    assert [(p["name"], p["person_id"]) for p in _roster(chat["id"])] \
        == [("Sam", pid)]
    assert json.loads(labels)["labels"] == ["Sam"]
    assert json.loads(labels)["uncertain"] == []
    assert batch_calls["calls"] == 0
    assert len(checks) == 1 and checks[0][2] == "Sam", checks


def test_clear_stranger_arms_and_asks(app, relay, batch_calls, naming):
    _remember("Alex")  # owner enrolled: a new voice means NOT the owner
    naming["answers"] = [naming_answer("new")]
    with TestClient(app, base_url="http://127.0.0.1") as c:
        chat = _new_chat(c)
        with c.websocket_connect("/api/voice/stt-stream") as ws:
            ws.send_json({"chat_id": chat["id"]})
            assert ws.receive_json()["session"]  # #134 handshake
            ws.send_json(_frame(loud_pcm(1.5), commit=True))
            assert ws.receive_json() == {"final": "hello world"}
            msg = _insert_user_message(chat["id"])
            labels = _wait_for(lambda: _message_labels(msg["id"]))
            assert _wait_for(lambda: _chat_state(chat["id"])[0])
            assert _wait_for(lambda: _flags(chat["id"]))
            ws.send_json({"done": True})
    # armed, the owner rostered beside the new voice
    assert [p["name"] for p in _roster(chat["id"])] == ["Alex"]
    # the new voice's turn names nobody and says why, never the owner
    parsed = json.loads(labels)
    assert parsed["labels"] == []
    assert parsed["unresolved"] == "new_voice"
    # and exactly one open ask
    flags = _flags(chat["id"])
    assert [f["kind"] for f in flags] == ["unknown_voice"]
    assert batch_calls["calls"] == 0


def test_stranger_without_owner_enrolment_defers(app, relay, batch_calls,
                                                 naming):
    _remember("Sam")  # a guest is remembered, but the OWNER is not enrolled
    naming["answers"] = [naming_answer("new")]
    with TestClient(app, base_url="http://127.0.0.1") as c:
        chat = _new_chat(c)
        with c.websocket_connect("/api/voice/stt-stream") as ws:
            ws.send_json({"chat_id": chat["id"]})
            assert ws.receive_json()["session"]  # #134 handshake
            ws.send_json(_frame(loud_pcm(1.5), commit=True))
            assert ws.receive_json() == {"final": "hello world"}
            assert _wait_for(lambda: naming["calls"] >= 1)
            ws.send_json({"done": True})
    assert _chat_state(chat["id"])[0] is False
    assert _flags(chat["id"]) == []


# ── 4. disarm is sacred ─────────────────────────────────────────────────────

def test_solo_command_sets_ambient_off_even_when_room_already_off(app):
    from backend import introductions
    with TestClient(app, base_url="http://127.0.0.1") as c:
        chat = _new_chat(c)
        cfg = app.state.settings.as_cfg()
        outcome = introductions.apply_command(chat["id"],
                                              introductions.COMMAND_DISARM,
                                              cfg)
    assert outcome == "disarmed_by_command"
    on, ambient_off = _chat_state(chat["id"])
    assert on is False and ambient_off is True


def _claim_insert(chat_id, turn_id, text="hello world"):
    """What /send does with a voice turn: the parked label rides the
    insert (#28, twelfth field test)."""
    con = db.connect()
    try:
        return db.insert_message(con, chat_id, "user", text,
                                 voice_turn_id=turn_id,
                                 voice_labels=diarize.claim_label(turn_id))
    finally:
        con.close()


def _commit(ws, turn_id):
    frame = _frame(loud_pcm(1.5), commit=True)
    frame["turn_id"] = turn_id
    ws.send_json(frame)
    got = ws.receive_json()
    assert got.get("final") == "hello world", got


def test_solo_labels_but_never_arms_seats_or_asks(app, relay, batch_calls,
                                                  naming):
    """#461: 15 of one evening's 19 unlabelled turns came after a spoken
    "solo mode", and a guest's words then reached memory as the owner's.
    Every turn in solo is checked. The owner is labelled as in listening,
    a remembered guest is named, a clear new voice is marked "a new
    voice", and a turn the naming had nothing for is marked "still
    listening". None of them arms, seats or asks."""
    from backend import introductions
    owner = _remember("Alex")
    guest = _remember("Sam")
    naming["answers"] = [naming_answer(name="Alex", pid=owner),
                         naming_answer(name="Sam", pid=guest),
                         naming_answer("new"), None]
    with TestClient(app, base_url="http://127.0.0.1") as c:
        chat = _new_chat(c)
        introductions.apply_command(chat["id"], introductions.COMMAND_DISARM,
                                    app.state.settings.as_cfg())
        msgs = []
        with c.websocket_connect("/api/voice/stt-stream") as ws:
            ws.send_json({"chat_id": chat["id"]})
            assert ws.receive_json()["session"]  # #134 handshake
            for i, tid in enumerate(("tS1", "tS2", "tS3")):
                _commit(ws, tid)
                assert _wait_for(lambda: tid in diarize._PENDING_LABELS)
                msgs.append(_claim_insert(chat["id"], tid))
            _commit(ws, "tS4")
            assert _wait_for(lambda: "tS4" in diarize._PENDING_LABELS)
            msgs.append(_claim_insert(chat["id"], "tS4"))
            time.sleep(0.3)  # give a wrong arm or ask time to land
            ws.send_json({"done": True})
    heads = [json.loads(m["voice_labels"]) for m in msgs[:3]]
    assert heads[0]["labels"] == ["Alex"] and heads[0]["owner"] is True
    assert heads[1]["labels"] == ["Sam"] and heads[1]["uncertain"] == []
    assert heads[2]["labels"] == []
    assert heads[2]["unresolved"] == "new_voice"
    last = json.loads(msgs[3]["voice_labels"])
    assert last["labels"] == [] and last["unresolved"] == "listening"
    assert _chat_state(chat["id"]) == (False, True)
    assert _roster(chat["id"]) == []
    assert _flags(chat["id"]) == []
    assert batch_calls["calls"] == 0


def test_mid_session_disarm_is_honoured_at_the_next_commit(
        app, relay, batch_calls, naming):
    """#461: the commit after a mid-session "solo mode" is still checked,
    and the naming that would have armed a listening room only names the
    turn."""
    from backend import introductions
    pid = _remember("Sam")
    naming["answers"] = [naming_answer("listening"),
                         naming_answer(name="Sam", pid=pid)]
    with TestClient(app, base_url="http://127.0.0.1") as c:
        chat = _new_chat(c)
        cfg = app.state.settings.as_cfg()
        with c.websocket_connect("/api/voice/stt-stream") as ws:
            ws.send_json({"chat_id": chat["id"]})
            assert ws.receive_json()["session"]  # #134 handshake
            ws.send_json(_frame(loud_pcm(1.5), commit=True))
            assert ws.receive_json() == {"final": "hello world"}
            assert _wait_for(lambda: naming["calls"] == 1)   # ran, deferred
            introductions.apply_command(chat["id"],
                                        introductions.COMMAND_DISARM, cfg)
            _commit(ws, "tM2")
            assert _wait_for(lambda: "tM2" in diarize._PENDING_LABELS)
            msg = _claim_insert(chat["id"], "tM2")
            time.sleep(0.3)
            ws.send_json({"done": True})
    assert naming["calls"] == 2
    assert json.loads(msg["voice_labels"])["labels"] == ["Sam"]
    assert _chat_state(chat["id"]) == (False, True)
    assert _roster(chat["id"]) == []


def test_claimed_stranger_label_still_raises_the_ask(app, relay, batch_calls,
                                                     naming):
    """#461: the who-joined ask waited for the check to write its label,
    and since labels ride the insert the check nearly always found the
    label already there and stopped. No ask was raised from 14 Aug on.
    Here the label is claimed by the insert, as /send does, and the ask
    must still come up, pointing at the turn."""
    _remember("Alex")
    naming["answers"] = [naming_answer("new")]
    with TestClient(app, base_url="http://127.0.0.1") as c:
        chat = _new_chat(c)
        with c.websocket_connect("/api/voice/stt-stream") as ws:
            ws.send_json({"chat_id": chat["id"]})
            assert ws.receive_json()["session"]  # #134 handshake
            _commit(ws, "tU1")
            assert _wait_for(lambda: "tU1" in diarize._PENDING_LABELS)
            msg = _claim_insert(chat["id"], "tU1")
            assert json.loads(msg["voice_labels"])["unresolved"] \
                == "new_voice"
            flags = _wait_for(lambda: _flags(chat["id"]))
            ws.send_json({"done": True})
    assert [(f["kind"], f["message_id"]) for f in flags] \
        == [("unknown_voice", msg["id"])]
    assert _chat_state(chat["id"])[0] is True


def test_switch_off_mid_session_still_checks(app, relay, batch_calls,
                                             naming):
    """#461: the switch in settings takes the chat to solo, as the spoken
    command does. A session that opened with the room on keeps checking
    every turn once it goes solo: the room is read per turn. The owner's
    turn is labelled, and the room stays off."""
    owner = _remember("Alex")
    naming["answers"] = [naming_answer(name="Alex", pid=owner)]
    with TestClient(app, base_url="http://127.0.0.1") as c:
        chat = _new_chat(c)
        c.patch(f"/api/chats/{chat['id']}", json={"room_mode": True})
        with c.websocket_connect("/api/voice/stt-stream") as ws:
            ws.send_json({"chat_id": chat["id"]})
            assert ws.receive_json()["session"]  # #134 handshake
            c.patch(f"/api/chats/{chat['id']}", json={"room_mode": False})
            _commit(ws, "tT1")
            assert _wait_for(lambda: "tT1" in diarize._PENDING_LABELS)
            msg = _claim_insert(chat["id"], "tT1")
            ws.send_json({"done": True})
    parsed = json.loads(msg["voice_labels"])
    assert parsed["labels"] == ["Alex"] and parsed["owner"] is True
    assert _chat_state(chat["id"]) == (False, True)


def test_every_reenable_clears_ambient_off(app):
    with TestClient(app, base_url="http://127.0.0.1") as c:
        cfg = app.state.settings.as_cfg()
        from backend import introductions as intro
        # arm command clears it
        chat = _new_chat(c)
        intro.apply_command(chat["id"], intro.COMMAND_DISARM, cfg)
        assert _chat_state(chat["id"])[1] is True
        intro.apply_command(chat["id"], intro.COMMAND_ARM, cfg)
        assert _chat_state(chat["id"])[1] is False
        # a confirmed introduction clears it
        chat2 = _new_chat(c)
        intro.apply_command(chat2["id"], intro.COMMAND_DISARM, cfg)
        assert _chat_state(chat2["id"])[1] is True
        intro.apply_scan(chat2["id"],
                         {"introductions": ["Sam"], "departures": []},
                         cfg, text="this is Sam")
        assert _chat_state(chat2["id"])[1] is False
        # the manual toggle-on clears it, and (#28, fifth field test) behaves
        # like the arm command: the owner joins the roster
        chat3 = _new_chat(c)
        intro.apply_command(chat3["id"], intro.COMMAND_DISARM, cfg)
        assert _chat_state(chat3["id"])[1] is True
        c.patch(f"/api/chats/{chat3['id']}", json={"room_mode": True})
        assert _chat_state(chat3["id"])[1] is False
        assert [p["name"] for p in _roster(chat3["id"])] == ["Alex"]

