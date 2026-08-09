"""Anchored identification (#28 phase 2): remembered voices become NAMES.

Driven end to end through the realtime relay with the same FakeEleven /
httpx-mock doubles as tests/test_room_mode.py - phase 1's latency pins stay
in that file and still hold; this file pins what phase 2 adds ON TOP.

#28 PR-B: the anchored EL pass these tests exercise now has exactly ONE
door - the local matcher's "multi" (overlapping speech) verdict - so the
relay-driven tests stub the matcher to that verdict (the `multi_matcher`
fixture). The machinery under test (prefix cluster naming, elimination,
the ask-fallback, the mismatch cross-check) is unchanged behind that door.

1. With a roster, the diarization request grows the anchor prefix and the
   num_speakers = roster+1 hint; a cluster matching a person's prefix segment
   labels the turn with their NAME.
2. A fresh session re-identifies a remembered voice with no introduction.
3. Exactly one anchor-pending person + exactly one unmatched cluster names
   them by elimination (uncertain), and their anchor set accumulates from
   those single-speaker utterances until it crosses the sufficiency bar.
4. An unmatched cluster with no unambiguous elimination stays UNCERTAIN with
   an ordinal and raises the 'unknown_voice' ask-fallback - one open ask at a
   time - which a later introduction resolves.
5. The LLM mismatch cross-check raises a content-free flag and NEVER mutates
   the label it doubts.
6. Tap-to-correct rewrites the label, resolves the turn's flags, and feeds
   the cached single-speaker audio to the person's anchors as ground truth.
7. Roster/flag changes ride the live-events stream content-free; the roster
   snapshot and remembered-voices endpoints serve the UI; forget deletes.
"""

import asyncio
import base64
import json
import threading
import time

import httpx
import pytest
from fastapi.testclient import TestClient

from backend import anchors, db, diarize, events, introductions
from backend.app import create_app
from backend.config import Settings
from backend.routers import voice as voice_router


@pytest.fixture
def app(tmp_path):
    diarize._ROOM_ENABLED.clear()
    diarize._STASHED.clear()
    anchors.clear_recent_audio()
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
    return fake


@pytest.fixture
def multi_matcher(monkeypatch):
    """Stub the local matcher to the "multi" verdict - since #28 PR-B the
    only verdict that runs the anchored ElevenLabs pass under test here."""
    from backend import voiceid
    monkeypatch.setattr(
        voiceid, "identify_utterance",
        lambda *a, **k: {"status": "defer", "person_id": None, "name": None,
                         "score": 0.4, "reason": "multi"})


@pytest.fixture
def batch_stt(monkeypatch):
    state = {"calls": [], "responses": []}

    def fake_post(url, headers=None, data=None, files=None, timeout=None):
        state["calls"].append({"url": url, "data": dict(data or {}),
                               "audio": files["file"][1]})
        nxt = state["responses"].pop(0) if state["responses"] else _resp()
        if isinstance(nxt, Exception):
            raise nxt
        return httpx.Response(200, json=nxt,
                              request=httpx.Request("POST", url))

    monkeypatch.setattr(voice_router.voice.httpx, "post", fake_post)
    return state


def _resp(*entries):
    """A diarized batch response: entries are (speaker_id, start, end)."""
    return {"language_code": "en", "text": "hello world",
            "words": [{"text": f"w{i}", "type": "word", "speaker_id": s,
                       "start": a, "end": b}
                      for i, (s, a, b) in enumerate(entries)]}


def loud_pcm(seconds, sample_rate=16000):
    return b"\x00\x40" * int(seconds * sample_rate)


def _frame(data, commit=False):
    return {"audio": base64.b64encode(data).decode(),
            "sample_rate": 16000, "commit": commit}


def _wait_for(pred, timeout=6.0, interval=0.02):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        v = pred()
        if v:
            return v
        time.sleep(interval)
    return pred()


def _message_labels(msg_id):
    con = db.connect()
    try:
        row = con.execute("SELECT voice_labels FROM messages WHERE id=?",
                          (msg_id,)).fetchone()
        return row["voice_labels"] if row else None
    finally:
        con.close()


def _stt_usage_rows():
    con = db.connect()
    try:
        return con.execute(
            "SELECT COUNT(*) FROM voice_usage WHERE kind='stt'").fetchone()[0]
    finally:
        con.close()


def _insert_user_message(chat_id, text="hello world"):
    con = db.connect()
    try:
        return db.insert_message(con, chat_id, "user", text)
    finally:
        con.close()


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
    """A chat with room mode on (durably + mirrored) and a roster: `sufficient`
    people get 3x2s anchors (over the bar), `pending` people get roster rows
    with no anchors."""
    chat = client.post("/api/chats", json={"participant_ids": []}).json()
    con = db.connect()
    db.set_chat_room_mode(con, chat["id"], True)
    diarize.set_room_enabled(chat["id"], True)
    store = anchors.store()
    for name in sufficient:
        pid = store.ensure_person(name)
        for _ in range(3):
            assert store.add_clip(pid, loud_pcm(2.0), 16000,
                                  source="accumulated")
        db.add_room_person(con, chat["id"], name, person_id=pid)
    for name in pending:
        db.add_room_person(con, chat["id"], name)
    con.close()
    return chat


# One sufficient person's prefix is exactly PREFIX_PERSON_SECONDS long
# (2s + 2s best clips, capped) - where mocked utterance timestamps start.
PREFIX_1 = anchors.PREFIX_PERSON_SECONDS


# ── 1. anchored request + name labels ───────────────────────────────────────

def test_matched_cluster_gets_the_persons_name(app, relay, batch_stt, multi_matcher):
    """The whole point: prefix cluster -> Shawn's segment -> the utterance
    cluster that matches it is labelled 'Shawn', and the request carried the
    anchor prefix and the roster+1 num_speakers hint."""
    batch_stt["responses"] = [_resp(("speaker_0", 0.4, 0.9),
                                    ("speaker_0", PREFIX_1 + 0.5,
                                     PREFIX_1 + 0.9))]
    with TestClient(app, base_url="http://127.0.0.1") as c:
        chat = _setup_room(c, sufficient=["Shawn"])
        utter = loud_pcm(1.5)
        with c.websocket_connect("/api/voice/stt-stream") as ws:
            ws.send_json({"chat_id": chat["id"]})  # NO client toggle - server flag
            ws.send_json(_frame(utter, commit=True))
            assert ws.receive_json() == {"final": "hello world"}
            msg = _insert_user_message(chat["id"])
            labels = _wait_for(lambda: _message_labels(msg["id"]))
            ws.send_json({"done": True})
    assert json.loads(labels) == {"clusters": ["speaker_0"],
                                  "labels": ["Shawn"], "uncertain": []}
    call = batch_stt["calls"][0]
    assert call["data"]["num_speakers"] == "2"  # roster (1) + 1
    pid = anchors.store().find_by_name("Shawn")["person_id"]
    prefix_pcm, _ = anchors.store().build_prefix([pid], 16000)
    assert call["audio"] == diarize.pcm16_wav(prefix_pcm + utter, 16000)


def test_remembered_voice_reidentifies_in_a_fresh_session(app, relay, batch_stt, multi_matcher):
    """A NEW websocket session (fresh RoomSession, fresh ordinals) still
    names the remembered voice - anchors, not session state, carry identity.
    No introduction happened in either session."""
    batch_stt["responses"] = [
        _resp(("speaker_0", 0.4, 0.9),
              ("speaker_0", PREFIX_1 + 0.5, PREFIX_1 + 0.9)),
        _resp(("speaker_3", 0.4, 0.9),   # per-request ids differ - that's the point
              ("speaker_3", PREFIX_1 + 0.5, PREFIX_1 + 0.9)),
    ]
    with TestClient(app, base_url="http://127.0.0.1") as c:
        chat = _setup_room(c, sufficient=["Shawn"])
        for expect_rows in (1, 2):
            with c.websocket_connect("/api/voice/stt-stream") as ws:
                ws.send_json({"chat_id": chat["id"]})
                ws.send_json(_frame(loud_pcm(1.5), commit=True))
                assert ws.receive_json() == {"final": "hello world"}
                msg = _insert_user_message(chat["id"])
                labels = _wait_for(lambda: _message_labels(msg["id"]))
                assert json.loads(labels)["labels"] == ["Shawn"]
                ws.send_json({"done": True})


# ── 2. elimination + anchor accumulation for the introduced person ──────────

def test_elimination_names_the_single_pending_person_and_builds_their_anchor(
        app, relay, batch_stt, multi_matcher):
    """After 'my wife Alex is here', Alex speaks: her cluster matches no
    anchor, but she is the ONLY pending person - elimination names her
    (uncertain), and each clean single-speaker utterance feeds her anchor set
    until it crosses the sufficiency bar. The bar is TWO-PART since #28 PR-B
    (seconds AND short clips), so her utterances mix lengths - two long, two
    short - exactly the mix real speech provides."""
    batch_stt["responses"] = [
        _resp(("speaker_0", 0.4, 0.9),
              ("speaker_1", PREFIX_1 + 0.3, PREFIX_1 + 0.7))
        for _ in range(4)]
    with TestClient(app, base_url="http://127.0.0.1") as c:
        chat = _setup_room(c, sufficient=["Shawn"], pending=["Alex"])
        with c.websocket_connect("/api/voice/stt-stream") as ws:
            ws.send_json({"chat_id": chat["id"]})
            first_labels = None
            for n, secs in enumerate((2.5, 2.5, 1.5, 1.5), start=1):
                ws.send_json(_frame(loud_pcm(secs), commit=True))
                assert ws.receive_json() == {"final": "hello world"}
                msg = _insert_user_message(chat["id"], f"turn {n}")
                labels = _wait_for(lambda: _message_labels(msg["id"]))
                if first_labels is None:
                    first_labels = labels
            ws.send_json({"done": True})
    assert json.loads(first_labels) == {"clusters": ["speaker_1"],
                                        "labels": ["Alex"],
                                        "uncertain": ["Alex"]}
    alex = anchors.store().find_by_name("Alex")
    assert alex is not None
    # 2 x 2.5s + 2 x 1.5s = 8s with two short clips: both halves of the bar
    assert alex["sufficient"] is True
    assert alex["short_clips"] == 2
    # the roster row linked the moment her first clip was accepted
    row = next(p for p in _roster(chat["id"]) if p["name"] == "Alex")
    assert row["person_id"] == alex["person_id"]
    assert _flags(chat["id"]) == []          # elimination was unambiguous - no ask


def test_num_speakers_tracks_roster_size(app, relay, batch_stt, multi_matcher):
    with TestClient(app, base_url="http://127.0.0.1") as c:
        chat = _setup_room(c, sufficient=["Shawn"], pending=["Ana", "Ben"])
        with c.websocket_connect("/api/voice/stt-stream") as ws:
            ws.send_json({"chat_id": chat["id"]})
            ws.send_json(_frame(loud_pcm(1.0), commit=True))
            ws.receive_json()
            assert _wait_for(lambda: batch_stt["calls"])
            ws.send_json({"done": True})
    assert batch_stt["calls"][0]["data"]["num_speakers"] == "4"  # 3 present + 1


# ── 3. the ask-fallback ─────────────────────────────────────────────────────

def test_ambiguous_unknown_cluster_stays_uncertain_and_asks_once(
        app, relay, batch_stt, multi_matcher):
    """Two pending people, one unknown cluster: no elimination possible. The
    turn keeps an uncertain ordinal, ONE 'unknown_voice' flag opens (not one
    per utterance), and a later introduction resolves it."""
    batch_stt["responses"] = [
        _resp(("speaker_0", 0.4, 0.9),
              ("speaker_1", PREFIX_1 + 0.3, PREFIX_1 + 0.7))
        for _ in range(2)]
    with TestClient(app, base_url="http://127.0.0.1") as c:
        chat = _setup_room(c, sufficient=["Shawn"], pending=["Ana", "Ben"])
        with c.websocket_connect("/api/voice/stt-stream") as ws:
            ws.send_json({"chat_id": chat["id"]})
            for n in (1, 2):
                ws.send_json(_frame(loud_pcm(1.5), commit=True))
                assert ws.receive_json() == {"final": "hello world"}
                msg = _insert_user_message(chat["id"], f"turn {n}")
                labels = _wait_for(lambda: _message_labels(msg["id"]))
            ws.send_json({"done": True})
        assert json.loads(labels)["uncertain"] == ["Voice 1"]
        flags = _flags(chat["id"])
        assert len(flags) == 1               # one open ask, not one per turn
        assert flags[0]["kind"] == "unknown_voice"
        assert flags[0]["message_id"] is not None
        # nobody's anchor was fed - an ambiguous voice is not ground truth
        assert anchors.store().find_by_name("Ana") is None
        assert anchors.store().find_by_name("Ben") is None
        # answering in chat (a confirmed introduction) closes the ask
        introductions.apply_scan(chat["id"],
                                 {"introductions": ["Cass"], "departures": []},
                                 {"user_name": "Shawn", "room_roster_max": 6})
        assert _flags(chat["id"]) == []


# ── 4. the mismatch cross-check ─────────────────────────────────────────────

def test_mismatch_flags_but_never_mutates_the_label(app, relay, batch_stt, multi_matcher,
                                                    monkeypatch):
    """The cross-check doubts a named turn: a 'mismatch' flag opens carrying
    names only, and the voice label is EXACTLY what it was - there is no code
    path from the check to the label."""
    async def fake_utility(prompt, cfg, max_tokens=2000):
        return json.dumps({"mismatch": True, "suspected": "Alex"})
    monkeypatch.setattr("backend.llm_util.utility_complete", fake_utility)
    two = 2 * PREFIX_1  # two sufficient people tile the prefix
    batch_stt["responses"] = [_resp(
        ("speaker_0", 0.4, 0.9),                  # Shawn's anchor segment
        ("speaker_9", PREFIX_1 + 0.4, PREFIX_1 + 0.9),  # Alex's anchor segment
        ("speaker_0", two + 0.1, two + 0.2),
        ("speaker_0", two + 0.3, two + 0.4),
        ("speaker_0", two + 0.5, two + 0.6),
        ("speaker_0", two + 0.7, two + 0.8))]
    with TestClient(app, base_url="http://127.0.0.1") as c:
        chat = _setup_room(c, sufficient=["Shawn", "Alex"])
        with c.websocket_connect("/api/voice/stt-stream") as ws:
            ws.send_json({"chat_id": chat["id"]})
            ws.send_json(_frame(loud_pcm(1.5), commit=True))
            assert ws.receive_json() == {"final": "hello world"}
            msg = _insert_user_message(chat["id"], "my husband thinks so too")
            labels = _wait_for(lambda: _message_labels(msg["id"]))
            flag = _wait_for(lambda: _flags(chat["id"]))
            ws.send_json({"done": True})
    assert flag[0]["kind"] == "mismatch"
    assert flag[0]["label"] == "Shawn"
    assert flag[0]["suspected"] == "Alex"
    assert flag[0]["message_id"] == msg["id"]
    # THE PIN: the label is untouched, before and after the flag
    assert json.loads(_message_labels(msg["id"]))["labels"] == json.loads(
        labels)["labels"] == ["Shawn"]


def test_mismatch_parse_verdict_is_defensive():
    from backend import mismatch
    ok = mismatch.parse_verdict('{"mismatch": true, "suspected": "Alex"}')
    assert ok == {"mismatch": True, "suspected": "Alex"}
    assert mismatch.parse_verdict(
        '{"mismatch": false, "suspected": "Alex"}') == {
        "mismatch": False, "suspected": ""}
    for bad in (None, "", "yes", "{}", '{"mismatch": "true"}', "[1]"):
        assert mismatch.parse_verdict(bad)["mismatch"] is False, bad


def test_mismatch_keyless_is_a_quiet_no_op(app, relay, batch_stt, multi_matcher):
    """No utility key: the check degrades to nothing - no flag, no error, and
    obviously no label change."""
    batch_stt["responses"] = [_resp(
        ("speaker_0", 0.4, 0.9),
        ("speaker_0", PREFIX_1 + 0.1, PREFIX_1 + 0.2),
        ("speaker_0", PREFIX_1 + 0.3, PREFIX_1 + 0.4),
        ("speaker_0", PREFIX_1 + 0.5, PREFIX_1 + 0.6),
        ("speaker_0", PREFIX_1 + 0.7, PREFIX_1 + 0.8))]
    with TestClient(app, base_url="http://127.0.0.1") as c:
        chat = _setup_room(c, sufficient=["Shawn"])
        with c.websocket_connect("/api/voice/stt-stream") as ws:
            ws.send_json({"chat_id": chat["id"]})
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


def test_room_mode_patch_override_updates_flag_and_mirror(app):
    with TestClient(app, base_url="http://127.0.0.1") as c:
        chat = c.post("/api/chats", json={"participant_ids": []}).json()
        r = c.patch(f"/api/chats/{chat['id']}", json={"room_mode": True}).json()
        assert r["room_mode"] == 1
        assert diarize.room_enabled(chat["id"]) is True
        r = c.patch(f"/api/chats/{chat['id']}", json={"room_mode": False}).json()
        assert r["room_mode"] == 0
        assert diarize.room_enabled(chat["id"]) is False


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


def test_off_session_stashes_the_utterance_for_the_owner_anchor(
        app, relay, batch_stt):
    """Room mode OFF: the commit still slices the tee, but the audio only
    lands in the in-memory stash (for a later introduction to claim) - no
    batch call, no task, no label. The phase-1 pins hold with the stash."""
    with TestClient(app, base_url="http://127.0.0.1") as c:
        chat = c.post("/api/chats", json={"participant_ids": []}).json()
        utter = loud_pcm(1.0)
        with c.websocket_connect("/api/voice/stt-stream") as ws:
            ws.send_json({"chat_id": chat["id"]})
            ws.send_json(_frame(utter, commit=True))
            assert ws.receive_json() == {"final": "hello world"}
            ws.send_json({"done": True})
        time.sleep(0.2)
        assert batch_stt["calls"] == []
        assert diarize._TASKS == set()
        stashed = diarize.take_stashed_utterance(chat["id"])
        assert stashed is not None and stashed[0] == utter


# ── 7. the live-events stream ───────────────────────────────────────────────

def test_roster_and_flag_changes_ride_the_stream_content_free(tmp_path):
    diarize._ROOM_ENABLED.clear()
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
