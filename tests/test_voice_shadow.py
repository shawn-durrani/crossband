"""The room-mode shadow test (#465 stage 1): measure, never act.

What these tests pin, in order:

1. OFF BY DEFAULT. Both settings ship empty, and with them empty an armed
   voice turn writes no row and calls nothing.
2. ON, IT RECORDS AND CHANGES NOTHING. One row per armed voice turn, on
   every live path (named, unresolved, cold start), with the turn id, the
   message id, today's label and timings. The live label is identical
   with the shadow on or off, and the shadow never writes to the anchor
   store.
3. NEVER DELAYS THE TURN. A diariser that hangs past the timeout, or isn't
   there at all, leaves run_pass as fast as ever; the row records the
   error, and the failure is logged once across many turns.
4. LOOPBACK ONLY. Every non-loopback diariser URL is refused, and a
   refused URL sends nothing.
5. CONTENT-FREE ROWS. No words, no audio: only allowlisted keys, and the
   turn's message text appears nowhere.
6. TWO MODELS. Agreement, disagreement, one model missing, the impostor
   statistics, the fused-score arithmetic, the z-matched bars, and the
   decision rule's parity with the live classify_utterance.
7. SPLIT, THEN NAME. A two-voice turn split by the (fake) diariser names
   each segment; overlapped stretches are cut out before naming.
8. READING IT. The compare view puts today's label beside every method,
   and the route serves it behind the usual session gate.
9. THE SECOND MODEL'S FETCH. Pinned and verified by the same code as the
   primary, and started only when the setting names it.

Keyless and offline: the diariser is a fake (or a local socket that
misbehaves on purpose), and both speaker models are one fake embedding
function keyed on the audio's loudness. Synthetic roster (Alex, Sam).
"""

import asyncio
import hashlib
import http.server
import json
import logging
import math
import random
import socket
import struct
import threading
import time

import pytest
from fastapi.testclient import TestClient

from backend import anchors, db, diarize, voice_shadow, voiceid
from backend.app import create_app
from backend.config import Settings
from roomkit import _insert_user_message
from tests.conftest import speech_pcm

ALEX_AMP, SAM_AMP = 9000, 3000
RMS_SPLIT = 4500  # speech_pcm's RMS is about 0.84 x its amplitude

BASE_CFG = {"user_name": "Alex", "room_roster_max": 6}
SHADOW_CFG = dict(BASE_CFG, diarize_shadow_url="http://127.0.0.1:8910",
                  voice_shadow_model="titanet_large")

# The fake models. Each 0.1 s frame of audio is heard as Alex or Sam by its
# loudness, and the embedding is the normalised mean of the frame vectors,
# so a mixed turn embeds as a blend. Large hears the same people in a
# slightly different space.
VECTORS = {
    "small": {"alex": [1.0, 0.0, 0.2, 0.0], "sam": [0.0, 1.0, 0.0, 0.2]},
    "large": {"alex": [0.9, 0.1, 0.0, 0.3], "sam": [0.1, 0.9, 0.3, 0.0]},
}


def _frames(pcm, sample_rate=16000):
    n = int(0.1 * sample_rate) * 2
    for i in range(0, len(pcm) - n + 1, n):
        frame = pcm[i:i + n]
        count = len(frame) // 2
        vals = struct.unpack(f"<{count}h", frame)
        yield math.sqrt(sum(v * v for v in vals) / count)


def fake_embed_factory(calls=None, missing=(), swap=()):
    """A stand-in for voice_shadow.embed. `missing` models return None
    (not ready); `swap` models hear Alex as Sam and Sam as Alex."""
    def fake(model, pcm, sample_rate, cfg):
        if calls is not None:
            calls.append((model, len(pcm)))
        if model in missing:
            return None
        acc = [0.0] * 4
        n = 0
        for rms in _frames(pcm, sample_rate):
            who = "alex" if rms > RMS_SPLIT else "sam"
            if model in swap:
                who = "sam" if who == "alex" else "alex"
            acc = [a + b for a, b in zip(acc, VECTORS[model][who])]
            n += 1
        return voiceid.l2_normalize(acc) if n else None
    return fake


def _remember(name, amp, clips=3):
    store = anchors.store()
    pid = store.ensure_person(name)
    assert store.add_clip(pid, speech_pcm(2.0, amp=amp), 16000,
                          source="introduction")
    for _ in range(clips - 1):
        assert store.add_clip(pid, speech_pcm(2.0, amp=amp), 16000,
                              source="accumulated")
    return pid


@pytest.fixture
def app(tmp_path):
    settings = Settings(data_dir=str(tmp_path / "data"),
                        memory_url="http://127.0.0.1:1", user_name="Alex")
    return create_app(settings)


@pytest.fixture
def live_fakes(monkeypatch):
    """The live side, faked the way the room suites fake it: a matcher that
    names Alex for a loud turn and defers otherwise, no ElevenLabs call and
    no mismatch check."""
    def identify(pcm, sample_rate, candidates, cfg, pending_present=False):
        loud = max(_frames(pcm, sample_rate), default=0) > RMS_SPLIT
        alex = next((c for c in candidates if c["name"] == "Alex"), None)
        if loud and alex:
            return {"status": voiceid.MATCH, "person_id": alex["person_id"],
                    "name": "Alex", "score": 0.81, "reason": "match"}
        return {"status": voiceid.DEFER, "person_id": None, "name": None,
                "score": 0.31, "reason": "below_threshold"}
    monkeypatch.setattr(voiceid, "identify_utterance", identify)
    monkeypatch.setattr("backend.voice.transcribe_diarized",
                        lambda *a, **k: pytest.fail("no EL call expected"))
    monkeypatch.setattr("backend.mismatch.schedule_check",
                        lambda *a, **k: None)


@pytest.fixture
def fake_models(monkeypatch):
    calls = []
    monkeypatch.setattr(voice_shadow, "embed", fake_embed_factory(calls))
    return calls


@pytest.fixture
def fake_diariser(monkeypatch):
    """Answers like diarserve: the first 3 s are slot 1, the rest slot 2."""
    sent = []

    def post(base_url, pcm):
        sent.append((base_url, len(pcm)))
        seconds = len(pcm) / 2 / 16000
        segs = [{"start": 0.0, "end": min(3.0, seconds), "speaker_slot": 1}]
        if seconds > 3.0:
            segs.append({"start": 3.0, "end": seconds, "speaker_slot": 2})
        return {"model": "fake/diariser", "segments": segs}
    monkeypatch.setattr(voice_shadow, "_post_diarise", post)
    return sent


def _armed_chat(client, roster=()):
    chat = client.post("/api/chats", json={"participant_ids": []}).json()
    con = db.connect()
    db.set_chat_room_mode(con, chat["id"], True)
    diarize.set_room_enabled(chat["id"], True)
    for name, pid in roster:
        db.add_room_person(con, chat["id"], name, person_id=pid)
    con.close()
    return chat


def _labels(msg_id):
    con = db.connect()
    try:
        row = con.execute("SELECT voice_labels FROM messages WHERE id=?",
                          (msg_id,)).fetchone()
        return row["voice_labels"] if row else None
    finally:
        con.close()


def _turn(chat_id, pcm, turn_id, cfg):
    """Drive one armed-room turn, then let the shadow finish. Returns how
    long run_pass itself took."""
    async def drive():
        t0 = time.perf_counter()
        await diarize.run_pass(chat_id, pcm, 16000, time.time(),
                               diarize.RoomSession(enabled=True), cfg,
                               turn_id=turn_id)
        took = time.perf_counter() - t0
        pending = list(voice_shadow._TASKS)
        if pending:
            await asyncio.gather(*pending)
        return took
    return asyncio.run(drive())


def _rows():
    return voice_shadow.read_rows(limit=1000)


def _setup(client):
    alex = _remember("Alex", ALEX_AMP)
    sam = _remember("Sam", SAM_AMP)
    chat = _armed_chat(client, roster=[("Alex", alex), ("Sam", sam)])
    return chat, alex, sam


# ── 1. off by default ───────────────────────────────────────────────────────

def test_both_settings_ship_empty_and_the_shadow_is_off():
    s = Settings()
    assert s.diarize_shadow_url == "" and s.voice_shadow_model == ""
    assert voice_shadow.active(s.as_cfg()) is False


def test_off_means_no_row_and_no_call(app, live_fakes, monkeypatch):
    monkeypatch.setattr(voice_shadow, "embed", lambda *a: pytest.fail(
        "the shadow must not embed while off"))
    monkeypatch.setattr(voice_shadow, "_post_diarise", lambda *a: pytest.fail(
        "the shadow must not call a diariser while off"))
    with TestClient(app, base_url="http://127.0.0.1") as c:
        chat, _, _ = _setup(c)
        _insert_user_message(chat["id"], voice_turn_id="t1")
        _turn(chat["id"], speech_pcm(3.0, amp=ALEX_AMP), "t1", BASE_CFG)
    assert _rows() == []
    assert not voice_shadow.rows_path().exists()


# ── 2. on, it records and changes nothing ───────────────────────────────────

def test_one_row_per_armed_turn_with_ids_today_and_timings(
        app, live_fakes, fake_models, fake_diariser):
    with TestClient(app, base_url="http://127.0.0.1") as c:
        chat, _, _ = _setup(c)
        m1 = _insert_user_message(chat["id"], voice_turn_id="t1")
        m2 = _insert_user_message(chat["id"], voice_turn_id="t2")
        _turn(chat["id"], speech_pcm(3.0, amp=ALEX_AMP), "t1", SHADOW_CFG)
        _turn(chat["id"], speech_pcm(3.0, amp=SAM_AMP), "t2", SHADOW_CFG)
        rows = _rows()
    assert [r["turn_id"] for r in rows] == ["t2", "t1"]  # newest first
    assert [r["message_id"] for r in rows] == [m2["id"], m1["id"]]
    alex_row, sam_row = rows[1], rows[0]
    assert alex_row["today"]["path"] == "local"
    assert alex_row["today"]["labels"] == ["Alex"]
    assert sam_row["today"]["path"] == "unresolved"
    assert sam_row["today"]["reason"] == "below_threshold"
    assert sam_row["today"]["labels"] == []
    for row in rows:
        assert row["today"]["ms"] is not None       # end of speech to label
        assert row["ms"]["diarise"] >= 0
        assert set(row["ms"]["whole"]) == {"small", "large"}
        assert set(row["ms"]["split"]) == {"small", "large"}
    # the shadow named Sam's turn where today's path left it unresolved
    assert sam_row["whole"]["small"]["named"] == "Sam"
    assert sam_row["whole"]["large"]["named"] == "Sam"
    assert sam_row["whole"]["consensus"]["named"] == "Sam"
    assert len(fake_diariser) == 2
    assert fake_diariser[0][0] == "http://127.0.0.1:8910"


def test_the_live_label_is_identical_with_the_shadow_on_or_off(
        app, live_fakes, fake_models, fake_diariser):
    pcm = speech_pcm(3.0, amp=ALEX_AMP)
    with TestClient(app, base_url="http://127.0.0.1") as c:
        chat, _, _ = _setup(c)
        off = _insert_user_message(chat["id"], voice_turn_id="off")
        on = _insert_user_message(chat["id"], voice_turn_id="on")
        _turn(chat["id"], pcm, "off", BASE_CFG)
        _turn(chat["id"], pcm, "on", SHADOW_CFG)
        assert _labels(off["id"]) and _labels(on["id"]) == _labels(off["id"])
        con = db.connect()
        roster = sorted(p["name"] for p in db.get_room_roster(
            con, chat["id"], present_only=True))
        con.close()
        assert roster == ["Alex", "Sam"]
        assert diarize.room_enabled(chat["id"])
    assert len(_rows()) == 1


def test_the_shadow_never_writes_to_the_anchor_store(
        app, fake_models, fake_diariser, monkeypatch):
    """Scored directly, with every store writer booby-trapped: the shadow
    reads enrolment clips and close pairs, and writes nothing."""
    with TestClient(app, base_url="http://127.0.0.1") as c:
        _setup(c)
        store = anchors.store()
        for name in ("_save", "_write_clip", "_delete_file", "add_clip",
                     "ensure_person", "set_hygiene", "delete_clip", "forget",
                     "move_clip", "merge_people", "retract_utterance_clips"):
            monkeypatch.setattr(store, name, lambda *a, **k: pytest.fail(
                "the shadow wrote to the anchor store"))
        today = {"path": "local", "labels": ["Alex"],
                 "candidates": diarize.remembered_candidates()}
        pcm = speech_pcm(3.0, amp=ALEX_AMP) + speech_pcm(2.0, amp=SAM_AMP)
        split = voice_shadow.diarise("http://127.0.0.1:8910", pcm, 16000)
        row = voice_shadow.score_turn(1, "t1", pcm, 16000, SHADOW_CFG, today,
                                      split)
    assert row["whole"]["small"]["best"] == "Alex"


def test_cold_start_and_unresolved_turns_are_recorded_too(
        app, live_fakes, fake_models, fake_diariser):
    """A roster with one unlearnt person makes today's path a cold start."""
    with TestClient(app, base_url="http://127.0.0.1") as c:
        alex = _remember("Alex", ALEX_AMP)
        chat = _armed_chat(c, roster=[("Alex", alex), ("Mateo", "")])
        _insert_user_message(chat["id"], voice_turn_id="t1")
        _turn(chat["id"], speech_pcm(3.0, amp=SAM_AMP), "t1", SHADOW_CFG)
        row = _rows()[0]
    assert row["today"]["path"] == "cold_start"
    assert row["today"]["labels"] == []            # a guess, not a name
    assert row["today"]["uncertain"] == ["Mateo"]
    assert row["pending"] is True


# ── 3. never delays the turn ────────────────────────────────────────────────

class _SlowHandler(http.server.BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def do_POST(self):
        self.rfile.read(int(self.headers.get("Content-Length") or 0))
        time.sleep(2.5)
        body = b'{"model": "slow", "segments": []}'
        try:
            self.send_response(200)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
        except OSError:
            pass


def test_a_slow_diariser_neither_delays_the_turn_nor_blocks_the_row(
        app, live_fakes, fake_models, monkeypatch):
    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), _SlowHandler)
    server.daemon_threads = True
    threading.Thread(target=server.serve_forever, daemon=True).start()
    # The timeout sits well past the bound on run_pass, so a shadow that
    # blocked the turn would fail the bound, and short of the hang, so the
    # row still records a timeout.
    monkeypatch.setattr(voice_shadow, "DIARISE_TIMEOUT_S", 1.5)
    cfg = dict(SHADOW_CFG, diarize_shadow_url=
               f"http://127.0.0.1:{server.server_address[1]}")
    try:
        with TestClient(app, base_url="http://127.0.0.1") as c:
            chat, _, _ = _setup(c)
            msg = _insert_user_message(chat["id"], voice_turn_id="t1")
            took = _turn(chat["id"], speech_pcm(3.0, amp=ALEX_AMP), "t1",
                         cfg)
            assert json.loads(_labels(msg["id"]))["labels"] == ["Alex"]
            row = _rows()[0]
    finally:
        server.shutdown()
        server.server_close()
    assert took < 0.8, f"run_pass took {took:.2f}s behind a hung diariser"
    assert row["split"] == {"error": "timeout"}
    assert row["whole"]["small"]["named"] == "Alex"   # the rest still ran


def test_a_dead_diariser_is_logged_once_across_turns(
        app, live_fakes, fake_models, caplog):
    sock = socket.socket()
    sock.bind(("127.0.0.1", 0))
    port = sock.getsockname()[1]
    sock.close()                                       # nothing listens now
    cfg = dict(SHADOW_CFG, diarize_shadow_url=f"http://127.0.0.1:{port}")
    caplog.set_level(logging.WARNING, logger="crossband.voice_shadow")
    with TestClient(app, base_url="http://127.0.0.1") as c:
        chat, _, _ = _setup(c)
        for n in range(3):
            _insert_user_message(chat["id"], voice_turn_id=f"t{n}")
            took = _turn(chat["id"], speech_pcm(2.0, amp=ALEX_AMP), f"t{n}",
                         cfg)
            assert took < 1.5
        rows = _rows()
    assert [r["split"] for r in rows] == [{"error": "unreachable"}] * 3
    failures = [r for r in caplog.records if "diariser" in r.getMessage()]
    assert len(failures) == 1


def test_a_full_queue_drops_and_counts_instead_of_waiting(monkeypatch):
    monkeypatch.setattr(voice_shadow, "MAX_IN_FLIGHT", 0)
    task = voice_shadow.schedule(1, b"\x00\x01" * 16000, 16000, SHADOW_CFG,
                                 "t1", {"path": "local"})
    assert task is None
    assert voice_shadow.status(SHADOW_CFG)["dropped_busy"] == 1


# ── 4. loopback only ────────────────────────────────────────────────────────

@pytest.mark.parametrize("url,base", [
    ("http://127.0.0.1:8910", "http://127.0.0.1:8910"),
    ("http://127.0.0.1:8910/", "http://127.0.0.1:8910"),
    ("http://127.0.0.1:8910/diarize", "http://127.0.0.1:8910"),
    ("http://localhost:8910", "http://localhost:8910"),
    ("http://[::1]:8910", "http://[::1]:8910"),
    ("http://127.0.0.2:9000", "http://127.0.0.2:9000"),
])
def test_loopback_urls_are_accepted(url, base):
    assert voice_shadow.loopback_base_url(url) == base


@pytest.mark.parametrize("url", [
    "http://192.168.1.20:8910", "http://10.0.0.5:8910",
    "http://100.101.102.103:8910", "http://my-mac.my-tailnet.ts.net:8910",
    "https://diarise.example.com", "http://0.0.0.0:8910",
    "http://127.0.0.1.nip.io:8910", "http://user:pw@127.0.0.1:8910",
    "http://127.0.0.1:8910/?next=http://evil", "ftp://127.0.0.1:8910",
    "127.0.0.1:8910", "http://[fd7a:115c:a1e0::1]:8910", "http://:8910",
    "http://127.0.0.1:99999",
])
def test_non_loopback_urls_are_refused(url):
    assert voice_shadow.loopback_base_url(url) is None


def test_a_refused_url_sends_nothing_and_warns_once(monkeypatch, caplog):
    monkeypatch.setattr(voice_shadow, "_post_diarise", lambda *a: pytest.fail(
        "audio must never go to a refused URL"))
    caplog.set_level(logging.WARNING, logger="crossband.voice_shadow")
    cfg = dict(BASE_CFG, diarize_shadow_url="http://192.168.1.20:8910")
    assert voice_shadow.diariser_url(cfg) is None
    assert voice_shadow.diariser_url(cfg) is None
    assert voice_shadow.active(cfg) is False          # nothing else is on
    assert voice_shadow.schedule(1, b"\x00\x01" * 16000, 16000, cfg, "t",
                                 {"path": "local"}) is None
    refusals = [r for r in caplog.records if "refused" in r.getMessage()]
    assert len(refusals) == 1
    assert voice_shadow.status(cfg)["diariser"]["refused"] is True


def test_the_http_client_ignores_proxies_and_redirects(monkeypatch):
    seen = {}

    class FakeClient:
        def __init__(self, **kwargs):
            seen.update(kwargs)

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def post(self, url, content, headers):
            seen["url"] = url

            class R:
                def raise_for_status(self):
                    pass

                def json(self):
                    return {"model": "m", "segments": []}
            return R()

    import httpx
    monkeypatch.setattr(httpx, "Client", FakeClient)
    voice_shadow._post_diarise("http://127.0.0.1:8910", b"\x00\x00")
    assert seen["trust_env"] is False and seen["follow_redirects"] is False
    assert seen["url"] == "http://127.0.0.1:8910/diarize"
    assert seen["timeout"] == voice_shadow.DIARISE_TIMEOUT_S


# ── 5. content-free rows ────────────────────────────────────────────────────

ROW_KEYS = {"v", "at", "chat_id", "turn_id", "message_id", "seconds",
            "candidates", "pending", "today", "models", "bars", "whole",
            "split", "ms"}
UNIT_KEYS = {"small", "large", "agree", "fused", "consensus", "reason"}
VIEW_KEYS = {"best", "pid", "score", "second", "named", "reason", "ms"}


def _walk_strings(value):
    if isinstance(value, str):
        yield value
    elif isinstance(value, dict):
        for k, v in value.items():
            yield k
            yield from _walk_strings(v)
    elif isinstance(value, list):
        for v in value:
            yield from _walk_strings(v)


def test_rows_hold_no_words_and_no_audio(app, live_fakes, fake_models,
                                         fake_diariser):
    secret = "the spare key is under the blue pot"
    with TestClient(app, base_url="http://127.0.0.1") as c:
        chat, _, _ = _setup(c)
        con = db.connect()
        db.insert_message(con, chat["id"], "user", secret,
                          voice_turn_id="t1")
        con.close()
        pcm = speech_pcm(3.0, amp=ALEX_AMP) + speech_pcm(2.0, amp=SAM_AMP)
        _turn(chat["id"], pcm, "t1", SHADOW_CFG)
        raw = voice_shadow.rows_path().read_text()
    assert secret not in raw and "spare" not in raw
    row = json.loads(raw.splitlines()[0])
    assert set(row) == ROW_KEYS
    units = [row["whole"]] + row["split"]["segments"]
    for unit in units:
        assert set(unit) - {"start", "end", "slot", "used_s"} <= UNIT_KEYS
        for model in ("small", "large"):
            assert set(unit[model]) <= VIEW_KEYS
    # nothing long enough to be encoded audio, and no field that holds any
    assert max(len(s) for s in _walk_strings(row)) < 80
    for banned in ("audio", "pcm", "text", "transcript", "words", "content"):
        assert banned not in raw.lower()
    assert len(raw) < 12000
    import os
    assert oct(os.stat(voice_shadow.rows_path()).st_mode)[-3:] == "600"


# ── 6. two models ───────────────────────────────────────────────────────────

def _anchors(spec):
    """{pid: {"name", "emb", "clips"}} from {pid: [clip vectors]}."""
    return {pid: {"name": pid.title(), "clips": clips,
                  "emb": voiceid.average_embeddings(clips)}
            for pid, clips in spec.items()}


def test_agreement_names_the_person_in_every_view(app, fake_models,
                                                  fake_diariser):
    with TestClient(app, base_url="http://127.0.0.1") as c:
        _setup(c)
        today = {"path": "local", "labels": ["Alex"],
                 "candidates": diarize.remembered_candidates()}
        row = voice_shadow.score_turn(1, "t", speech_pcm(3.0, amp=ALEX_AMP),
                                      16000, SHADOW_CFG, today, None)
    whole = row["whole"]
    assert whole["small"]["named"] == whole["large"]["named"] == "Alex"
    assert whole["agree"] is True
    assert whole["consensus"] == {"named": "Alex", "reason": "agree"}
    assert whole["fused"]["named"] == "Alex"
    assert row["split"] is None                        # that part is off here


def test_disagreement_leaves_the_consensus_unsure(app, monkeypatch):
    """Large builds its anchors from the stored clips like Small, then hears
    this one turn as the other person."""
    plain, swapped = fake_embed_factory(), fake_embed_factory(swap=("large",))
    turn = speech_pcm(3.0, amp=ALEX_AMP)

    def one_ear(model, pcm, sr, cfg):
        if model == "large" and pcm == turn:
            return swapped(model, pcm, sr, cfg)
        return plain(model, pcm, sr, cfg)
    monkeypatch.setattr(voice_shadow, "embed", one_ear)
    with TestClient(app, base_url="http://127.0.0.1") as c:
        _setup(c)
        today = {"path": "local", "labels": ["Alex"],
                 "candidates": diarize.remembered_candidates()}
        row = voice_shadow.score_turn(1, "t", turn, 16000, SHADOW_CFG, today,
                                      None)
    whole = row["whole"]
    assert whole["small"]["named"] == "Alex"
    assert whole["large"]["named"] == "Sam"
    assert whole["agree"] is False
    assert whole["consensus"] == {"named": None, "reason": "disagree"}
    assert row["today"]["labels"] == ["Alex"]


def test_one_model_missing_keeps_the_other_and_skips_the_pair(app,
                                                              monkeypatch):
    monkeypatch.setattr(voice_shadow, "embed",
                        fake_embed_factory(missing=("large",)))
    with TestClient(app, base_url="http://127.0.0.1") as c:
        _setup(c)
        today = {"path": "local", "labels": ["Alex"],
                 "candidates": diarize.remembered_candidates()}
        row = voice_shadow.score_turn(1, "t", speech_pcm(3.0, amp=ALEX_AMP),
                                      16000, SHADOW_CFG, today, None)
    whole = row["whole"]
    assert whole["small"]["named"] == "Alex"
    assert whole["large"] == {"reason": "unavailable"}
    assert whole["agree"] is None and whole["consensus"] is None
    assert whole["fused"] is None
    assert row["bars"]["large"]["source"] == "fallback"


def test_only_the_second_model_on_runs_no_split(app, fake_models,
                                                monkeypatch):
    monkeypatch.setattr(voice_shadow, "_post_diarise", lambda *a: pytest.fail(
        "no diariser is configured"))
    cfg = dict(BASE_CFG, voice_shadow_model="titanet_large")
    assert voice_shadow.active(cfg)
    with TestClient(app, base_url="http://127.0.0.1") as c:
        chat, _, _ = _setup(c)
        _insert_user_message(chat["id"], voice_turn_id="t1")
        with pytest.MonkeyPatch.context() as mp:
            mp.setattr(voiceid, "identify_utterance", lambda *a, **k: {
                "status": voiceid.DEFER, "person_id": None, "name": None,
                "score": 0.2, "reason": "below_threshold"})
            _turn(chat["id"], speech_pcm(3.0, amp=ALEX_AMP), "t1", cfg)
        row = _rows()[0]
    assert row["split"] is None and "diarise" not in row["ms"]
    assert row["whole"]["large"]["named"] == "Alex"


def test_impostor_statistics_use_cross_speaker_scores_only():
    a = _anchors({"alex": [[1.0, 0.0], [0.8, 0.6]],
                  "sam": [[0.0, 1.0], [0.6, 0.8]]})
    stats = voice_shadow.impostor_stats(a)
    expected = [voiceid.cosine(c, a[o]["emb"])
                for p in a for c in a[p]["clips"] for o in a if o != p]
    mean = sum(expected) / 4
    std = math.sqrt(sum((x - mean) ** 2 for x in expected) / 4)
    assert stats["n"] == 4
    assert stats["mean"] == round(mean, 4)
    assert stats["std"] == round(max(std, voice_shadow.SPREAD_FLOOR), 4)
    # one person has nobody to be an impostor against
    assert voice_shadow.impostor_stats(_anchors({"alex": [[1, 0]] * 3})) is None


def test_fused_score_and_bars_arithmetic():
    small = {"mean": 0.2, "std": 0.1, "n": 10}
    large = {"mean": 0.1, "std": 0.05, "n": 10}
    bars = voice_shadow.model_bars({}, small, large, pending=False)
    # Small's live bar, untouched
    assert bars["small"]["threshold"] == 0.5
    assert bars["small"]["margin"] == 0.12
    # Large at the same z as Small's threshold: (0.5 - 0.2) / 0.1 = 3
    assert bars["large"]["threshold"] == pytest.approx(0.1 + 3 * 0.05)
    assert bars["large"]["margin"] == pytest.approx(0.12 * 0.5)
    assert bars["large"]["source"] == "matched"
    assert bars["fused"]["threshold"] == pytest.approx(3.0)
    assert bars["fused"]["margin"] == pytest.approx(1.2)
    fused = voice_shadow.fused_scores({"a": 0.7, "b": 0.3},
                                      {"a": 0.45, "b": 0.15}, bars)
    # a: (5 + 7) / 2, b: (1 + 1) / 2
    assert fused == pytest.approx({"a": 6.0, "b": 1.0})
    assert voice_shadow.decide(fused, bars["fused"]) == ("a", "match")
    # just under the fused bar names nobody
    assert voice_shadow.decide({"a": 2.99, "b": 0.0}, bars["fused"]) == (
        None, "below_threshold")
    # the pending bump scales the same way
    pend = voice_shadow.model_bars({}, small, large, pending=True)
    assert pend["large"]["pending"] == pytest.approx(0.08 * 0.5)
    assert pend["fused"]["pending"] == pytest.approx(0.8)


def test_bars_fall_back_without_statistics():
    bars = voice_shadow.model_bars({}, None, None, pending=False)
    assert bars["fused"] is None
    assert bars["large"]["source"] == "fallback"
    assert bars["large"]["threshold"] == \
        voice_shadow.LARGE_FALLBACK_BAR["threshold"]


def test_decide_matches_the_live_rule_on_random_voices():
    """One rule for three views: over Small's live bar, decide() must reach
    classify_utterance's verdict exactly, close pairs and the pending bump
    included."""
    rng = random.Random(465)
    for trial in range(400):
        dim = 6
        enrolled = {f"p{i}": {"name": f"P{i}", "emb": voiceid.l2_normalize(
            [rng.gauss(0, 1) for _ in range(dim)])} for i in range(3)}
        query = voiceid.l2_normalize([rng.gauss(0, 1) for _ in range(dim)])
        close = [("p0", "p1")] if trial % 2 else []
        pending = trial % 3 == 0
        live = voiceid.classify_utterance(
            query, [], enrolled, 0.3, margin=0.1, close_pairs=close,
            pending_extra=0.08 if pending else 0.0)
        bar = {"threshold": 0.3, "margin": 0.1,
               "pending": 0.08 if pending else 0.0,
               "close_extra": voiceid.CLOSE_PAIR_EXTRA_MARGIN}
        scores = {p: voiceid.cosine(query, e["emb"])
                  for p, e in enrolled.items()}
        pid, reason = voice_shadow.decide(scores, bar, close)
        assert (pid, reason) == (live["person_id"], live["reason"]), trial


# ── 7. split, then name ─────────────────────────────────────────────────────

def test_a_two_voice_turn_is_named_piece_by_piece(app, fake_models,
                                                  fake_diariser):
    pcm = speech_pcm(3.0, amp=ALEX_AMP) + speech_pcm(2.0, amp=SAM_AMP)
    with TestClient(app, base_url="http://127.0.0.1") as c:
        _setup(c)
        today = {"path": "local", "labels": ["Alex"],
                 "candidates": diarize.remembered_candidates()}
        split = voice_shadow.diarise("http://127.0.0.1:8910", pcm, 16000)
        row = voice_shadow.score_turn(1, "t", pcm, 16000, SHADOW_CFG, today,
                                      split)
    segs = row["split"]["segments"]
    assert row["split"]["model"] == "fake/diariser"
    assert row["split"]["count"] == 2 and row["split"]["slots"] == 2
    assert [s["small"]["named"] for s in segs] == ["Alex", "Sam"]
    assert [s["consensus"]["named"] for s in segs] == ["Alex", "Sam"]
    assert segs[1]["used_s"] == pytest.approx(2.0)
    lines = voice_shadow.compare([row])["lines"]
    assert lines[0]["small_split"] == "Alex+Sam"
    assert lines[0]["today"] == "Alex"


def test_overlap_is_cut_out_before_naming():
    segs = [{"start": 0.0, "end": 4.0, "speaker_slot": 1},
            {"start": 3.0, "end": 6.0, "speaker_slot": 2},
            {"start": 5.0, "end": 7.0, "speaker_slot": 1}]
    assert voice_shadow.exclusive_spans(segs[0], segs) == [(0.0, 3.0)]
    assert voice_shadow.exclusive_spans(segs[1], segs) == [(4.0, 5.0)]
    assert voice_shadow.exclusive_spans(segs[2], segs) == [(6.0, 7.0)]
    pcm = struct.pack("<10h", *range(10))
    assert voice_shadow.span_pcm(pcm, 2, [(0.0, 1.0), (3.0, 4.0)]) == \
        struct.pack("<4h", 0, 1, 6, 7)


def test_short_and_surplus_segments_are_recorded_unscored(app, fake_models,
                                                          monkeypatch):
    monkeypatch.setattr(voice_shadow, "MAX_SEGMENTS", 2)
    monkeypatch.setattr(voice_shadow, "_post_diarise", lambda base, pcm: {
        "model": "m", "segments": [
            {"start": 0.0, "end": 2.0, "speaker_slot": 1},
            {"start": 2.0, "end": 2.3, "speaker_slot": 2},
            {"start": 2.3, "end": 4.0, "speaker_slot": 1}]})
    pcm = speech_pcm(4.0, amp=ALEX_AMP)
    with TestClient(app, base_url="http://127.0.0.1") as c:
        _setup(c)
        today = {"path": "local", "labels": ["Alex"],
                 "candidates": diarize.remembered_candidates()}
        split = voice_shadow.diarise("http://127.0.0.1:8910", pcm, 16000)
        row = voice_shadow.score_turn(1, "t", pcm, 16000, SHADOW_CFG, today,
                                      split)
    reasons = [s.get("reason") for s in row["split"]["segments"]]
    assert reasons == [None, "too_short", "skipped"]


def test_a_malformed_diariser_answer_is_an_error_not_a_crash(monkeypatch):
    monkeypatch.setattr(voice_shadow, "_post_diarise", lambda base, pcm: {
        "segments": [{"start": "soon"}]})
    segs, model, _ = voice_shadow.diarise("http://127.0.0.1:8910",
                                          b"\x00\x00" * 16000, 16000)
    assert segs == {"error": "bad_response"} and model == ""
    assert voice_shadow.diarise("http://127.0.0.1:8910", b"\x00\x00" * 8000,
                                8000)[0] == {"error": "sample_rate"}


# ── 8. reading it ───────────────────────────────────────────────────────────

def test_compare_tallies_each_method_against_today():
    rows = [
        {"turn_id": "t1", "today": {"labels": ["Alex"], "path": "local"},
         "whole": {"small": {"pid": "a", "named": "Alex"},
                   "large": {"pid": "a", "named": "Alex"}, "agree": True,
                   "consensus": {"named": "Alex"}, "fused": {"named": "Alex"}},
         "split": {"segments": [{"small": {"pid": "a", "named": "Alex"}},
                                {"small": {"pid": "s", "named": "Sam"}}]}},
        {"turn_id": "t2", "today": {"labels": [], "path": "unresolved",
                                    "reason": "below_threshold"},
         "whole": {"small": {"pid": "s", "named": "Sam"},
                   "large": {"reason": "unavailable"}, "agree": None,
                   "consensus": None, "fused": None},
         "split": {"error": "timeout"}},
    ]
    out = voice_shadow.compare(rows)
    first, second = out["lines"]
    assert first["small_split"] == "Alex+Sam" and first["consensus"] == "Alex"
    assert second["today"] == "" and second["today_reason"] == \
        "below_threshold"
    assert second["large"] is None and second["small_split"] is None
    tally = out["tally"]
    assert tally["small"] == {"turns": 2, "named": 2, "same_as_today": 1,
                              "named_where_today_did_not": 1}
    assert tally["small_split"]["two_or_more"] == 1
    assert tally["small_split"]["differs_from_today"] == 1
    assert "large_split" not in tally


def test_the_route_serves_the_comparison(app, live_fakes, fake_models,
                                         fake_diariser):
    with TestClient(app, base_url="http://127.0.0.1") as c:
        chat, _, _ = _setup(c)
        _insert_user_message(chat["id"], voice_turn_id="t1")
        _turn(chat["id"], speech_pcm(3.0, amp=ALEX_AMP), "t1", SHADOW_CFG)
        out = c.get("/api/voice/shadow",
                    params={"chat_id": chat["id"]}).json()
        full = c.get("/api/voice/shadow", params={"rows": "true"}).json()
        other = c.get("/api/voice/shadow",
                      params={"chat_id": chat["id"] + 99}).json()
    assert out["lines"][0]["turn_id"] == "t1"
    assert out["lines"][0]["today"] == "Alex"
    assert out["status"]["active"] is False   # the app's own settings: off
    assert "rows" not in out and full["rows"][0]["turn_id"] == "t1"
    assert other["lines"] == []


def test_rows_are_cut_back_once_the_file_is_full(tmp_path, monkeypatch):
    monkeypatch.setattr(db, "DATA_DIR", str(tmp_path))
    monkeypatch.setattr(voice_shadow, "ROWS_MAX", 300)
    monkeypatch.setattr(voice_shadow, "ROWS_KEEP", 100)
    for n in range(400):
        voice_shadow.write_row({"chat_id": 1, "turn_id": f"t{n}",
                                "message_id": n})
    rows = voice_shadow.read_rows(limit=1000)
    assert 100 <= len(rows) <= 300
    assert rows[0]["turn_id"] == "t399"


# ── 9. the second model's fetch ─────────────────────────────────────────────

def test_the_second_model_is_pinned_like_the_primary():
    spec = voice_shadow.SECOND_MODELS["titanet_large"]
    assert spec["url"].startswith("https://github.com/k2-fsa/sherpa-onnx/")
    assert spec["url"].endswith("/" + spec["file"])
    assert len(spec["sha256"]) == 64 and int(spec["sha256"], 16) >= 0
    assert spec["file"] != voiceid.MODEL_FILENAME


def test_the_fetch_starts_only_when_the_setting_names_the_model(monkeypatch):
    started = []
    monkeypatch.setattr(voice_shadow, "_spawn_large_fetch",
                        lambda cfg, key: started.append(key))
    monkeypatch.setattr(voiceid, "sherpa_onnx", object())
    assert voice_shadow._large_extractor(BASE_CFG) is None
    assert started == []
    cfg = dict(BASE_CFG, voice_shadow_model="titanet_large")
    assert voice_shadow._large_extractor(cfg) is None      # fetching
    assert voice_shadow._large_extractor(cfg) is None      # still, once
    assert started == ["titanet_large"]
    assert voice_shadow.status(cfg)["second_model"]["state"] == "fetching"


def test_an_unknown_second_model_stays_off(caplog):
    caplog.set_level(logging.WARNING, logger="crossband.voice_shadow")
    cfg = dict(BASE_CFG, voice_shadow_model="whisper_giant")
    assert voice_shadow.second_model(cfg) is None
    assert voice_shadow.second_model(cfg) is None
    assert voice_shadow.active(cfg) is False
    assert len([r for r in caplog.records
                if "not a known model" in r.getMessage()]) == 1
    assert voice_shadow.second_model(
        dict(BASE_CFG, voice_shadow_model="nemo_en_titanet_large")) == \
        "titanet_large"


class _FakeStream:
    def __init__(self, payload):
        self.payload = payload

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def raise_for_status(self):
        pass

    def iter_bytes(self, n):
        yield self.payload


def test_the_second_model_arrives_through_the_primarys_verified_fetch(
        tmp_path, monkeypatch):
    monkeypatch.setattr(db, "DATA_DIR", str(tmp_path))
    payload = b"large-model-bytes" * 50
    spec = dict(voice_shadow.SECOND_MODELS["titanet_large"],
                sha256=hashlib.sha256(payload).hexdigest())
    monkeypatch.setitem(voice_shadow.SECOND_MODELS, "titanet_large", spec)
    import httpx
    urls = []

    def stream(method, url, **k):
        urls.append(url)
        return _FakeStream(payload)
    monkeypatch.setattr(httpx, "stream", stream)

    class FakeSherpa:
        class SpeakerEmbeddingExtractorConfig:
            def __init__(self, **k):
                self.kw = k

        class SpeakerEmbeddingExtractor:
            def __init__(self, config):
                self.config = config
    monkeypatch.setattr(voiceid, "sherpa_onnx", FakeSherpa)
    voice_shadow._warm_large("titanet_large")
    path = tmp_path / voiceid.MODELS_DIR_NAME / spec["file"]
    assert path.read_bytes() == payload
    assert urls == [spec["url"]]
    assert voice_shadow._large["state"] == "ready"
    # a tampered download is refused and the model stays unavailable
    voice_shadow._reset_for_tests()
    path.unlink()
    monkeypatch.setattr(httpx, "stream", lambda *a, **k: _FakeStream(b"bad"))
    voice_shadow._warm_large("titanet_large")
    assert not path.exists()
    assert voice_shadow._large["state"] == "unavailable"
