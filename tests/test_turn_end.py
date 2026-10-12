"""The end-of-turn trial (#661): two guesses at every pause, kept with what
you did next and never acted on.

Pins: Smart Turn's input is Whisper's 80 by 800 log-mel layout of the last
8 seconds, with silence in front of a shorter turn; its chance comes back
as a plain number; without onnxruntime the local reader is left out; Jev
reads the words so far and the last AI line and answers mid-thought,
finished or unsure, and nothing without a key; a pause is settled as
resumed when the partial transcript grows and as ended on the commit, and
written down with counts and guesses, never words; the setting off means
nothing is asked; the relay asks at the pause hint and settles at the
commit; and the report counts what each reader would have ended sooner
and where it would have cut you off.
"""

import asyncio
import base64
import json
import sqlite3

import httpx
import numpy as np
import pytest
from fastapi.testclient import TestClient

from backend import auth, db, turn_end
from backend.app import create_app
from backend.config import Settings
from backend.routers import voice as voice_router
from tests.test_stt_relay import FakeEleven, _frame

RATE = turn_end.RATE


# ---------- Smart Turn ----------

def test_features_are_whispers_layout_of_the_last_eight_seconds():
    t = np.arange(3 * RATE) / RATE
    tone = (0.2 * np.sin(2 * np.pi * 220 * t)).astype(np.float32)
    f = turn_end.features(tone)
    assert f.shape == (80, 800) and f.dtype == np.float32
    assert np.array_equal(f, turn_end.features(tone))
    fitted = turn_end.fit(tone)
    assert len(fitted) == 8 * RATE and not fitted[:5 * RATE].any()
    long = np.arange(10 * RATE, dtype=np.float32)
    assert turn_end.fit(long)[0] == 2 * RATE


def test_the_mel_bank_covers_the_spectrum_with_80_triangles():
    bank = turn_end.mel_filters()
    assert bank.shape == (201, 80) and (bank >= 0).all()
    assert (bank.sum(axis=0) > 0).all()


def test_a_chance_comes_back_as_a_plain_number():
    class Session:
        def run(self, outputs, feeds):
            assert feeds["input_features"].shape == (1, 80, 800)
            return [np.array([[0.875]], dtype=np.float32)]
    model = object.__new__(turn_end.SmartTurn)
    model.session = Session()
    prob, ms = model.predict(np.zeros(RATE, dtype=np.float32))
    assert prob == pytest.approx(0.875) and ms >= 0


def test_without_onnxruntime_the_local_reader_is_left_out(monkeypatch):
    monkeypatch.setattr(turn_end, "_model", None)
    monkeypatch.setattr(turn_end, "_model_failed", False)
    monkeypatch.setattr(turn_end, "onnxruntime_present", lambda: False)
    assert turn_end.local_model() is None
    got = asyncio.run(turn_end.ask_local(np.zeros(RATE, dtype=np.float32)))
    assert got == {"local": None, "local_ms": None}


# ---------- Jev ----------

def test_jev_reads_the_words_so_far_and_the_last_ai_line():
    state = turn_end.describe("Alex", ["Claude"], "Chill them for twenty minutes.",
                              "For dinner tonight I was thinking")
    assert 'The last thing an assistant said: "Chill them for twenty minutes."' in state
    assert 'Alex has just paused after saying: "For dinner tonight I was thinking"' in state
    q = turn_end.jev_questions()["status"]
    assert list(q["criteria"]) == ["mid", "finished", "unsure"]
    assert turn_end.parse_jev({"status": {"choice": "finished", "confidence": 0.9}}) == \
        ("finished", 0.9)
    assert turn_end.parse_jev({"status": {"choice": "maybe"}}) == ("", None)
    assert turn_end.parse_jev(None) == ("", None)


def test_no_key_means_jev_is_not_asked(monkeypatch):
    monkeypatch.delenv("TYPESAFE_API_KEY", raising=False)
    got = asyncio.run(turn_end.ask_jev(1, "hello there", {}))
    assert got == {"jev": "", "jev_conf": None, "jev_ms": None}


# ---------- the shadow ----------

@pytest.fixture
def app(tmp_path):
    return create_app(Settings(data_dir=str(tmp_path / "data"),
                               memory_url="http://127.0.0.1:1",
                               turn_end_shadow=True))


def _rows():
    con = db.connect()
    try:
        return [dict(r) for r in con.execute("SELECT * FROM turn_end_shadow ORDER BY id")]
    finally:
        con.close()


def _guess(words, audio):
    async def read():
        return {"jev": "finished" if words.endswith("?") else "mid",
                "jev_conf": 0.9, "jev_ms": 300, "local": 0.2, "local_ms": 60.0}
    return read()


def test_a_pause_is_settled_by_what_you_did_next(app):
    with TestClient(app, base_url="http://127.0.0.1") as c:
        chat = c.post("/api/chats", json={}).json()["id"]

        async def go():
            s = turn_end.Shadow(chat, {"turn_end_shadow": True}, readers=_guess)
            s.audio(b"\x01\x00" * RATE)
            s.pause("for dinner tonight I was thinking")
            s.pause("for dinner tonight I was thinking")      # one guess a pause
            s.partial("for dinner tonight I was thinking")     # no new words yet
            s.partial("for dinner tonight I was thinking we could")
            s.pause("for dinner tonight I was thinking we could do meatballs?")
            s.commit()
            await asyncio.gather(*list(s._tasks))
            await asyncio.sleep(0.05)
            return s
        s = asyncio.run(go())
    rows = sorted(_rows(), key=lambda r: r["words"])   # written as each settles
    assert [(r["outcome"], r["words"], r["jev"]) for r in rows] == [
        ("resumed", 6, "mid"), ("ended", 10, "finished")]
    assert rows[0]["local_prob"] == 0.2 and rows[0]["chat_id"] == chat
    assert not s._audio
    assert "dinner" not in json.dumps(rows)


def test_the_setting_off_asks_nothing():
    called = []
    s = turn_end.Shadow(1, {}, readers=lambda w, a: called.append(w))
    s.audio(b"\x01\x00" * 100)
    s.pause("hello there")
    assert called == [] and not s._audio and s._pending is None


def test_the_audio_kept_is_the_last_eight_seconds():
    s = turn_end.Shadow(1, {"turn_end_shadow": True}, readers=_guess)
    s.audio(b"\x00\x00" * (5 * RATE))
    s.audio(b"\x01\x00" * (5 * RATE))
    assert len(s._audio) == 8 * RATE * 2
    assert s._audio[:2] == b"\x00\x00" and s._audio[-2:] == b"\x01\x00"


def test_the_relay_asks_at_the_pause_hint_and_settles_at_the_commit(app, monkeypatch):
    voice_router._captures.clear()
    fake = FakeEleven()
    monkeypatch.setattr(voice_router.websockets, "connect", lambda *a, **kw: fake)
    monkeypatch.setattr(voice_router.voice, "enabled", lambda: True)
    monkeypatch.setattr(voice_router.voice, "api_key", lambda: "test-key")
    monkeypatch.setattr(voice_router.engine, "prewarm_recall", lambda *a, **kw: None)
    app.state.allowed_hosts = {"testserver", "127.0.0.1", "localhost", "::1"}
    monkeypatch.setattr(auth, "GATE_LOOPBACK_HOSTS", auth.GATE_LOOPBACK_HOSTS | {"testserver"})
    heard = []

    async def read(self, words, audio):
        heard.append((words, len(audio)))
        return {"jev": "mid", "jev_conf": 0.8, "jev_ms": 250}
    monkeypatch.setattr(turn_end.Shadow, "_read", read)
    with TestClient(app, base_url="http://127.0.0.1") as c:
        chat = c.post("/api/chats", json={}).json()["id"]
        with c.websocket_connect("/api/voice/stt-stream") as ws:
            ws.send_json({"chat_id": chat})
            assert ws.receive_json()["session"]
            ws.send_json(_frame())
            assert ws.receive_json() == {"partial": "hello"}
            ws.send_json({"speculative": True})
            ws.send_json(_frame(commit=True))
            assert ws.receive_json() == {"final": "hello world"}
            ws.send_json({"done": True})
        for _ in range(50):
            if _rows():
                break
            asyncio.run(asyncio.sleep(0.02))
    assert heard == [("hello", 160)]
    assert [(r["outcome"], r["jev"], r["words"]) for r in _rows()] == [("ended", "mid", 1)]
    assert "speculative" not in json.dumps(fake.sent)


# ---------- the report ----------

def _script():
    import importlib.util
    from pathlib import Path
    path = Path(__file__).resolve().parents[1] / "scripts" / "turn_end_shadow_report.py"
    spec = importlib.util.spec_from_file_location("turn_end_shadow_report", path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def test_the_report_counts_sooner_endings_and_cut_offs(tmp_path):
    rep = _script()
    data = tmp_path / "data"
    data.mkdir()
    con = sqlite3.connect(data / "chat.db")
    con.execute("CREATE TABLE turn_end_shadow(id INTEGER PRIMARY KEY, chat_id INTEGER, "
                "created_at REAL, words INTEGER, jev TEXT, jev_conf REAL, jev_ms INTEGER, "
                "local_prob REAL, local_ms REAL, outcome TEXT, gap_ms INTEGER, error TEXT)")
    rows = [("finished", 0.95, "ended"), ("finished", 0.4, "ended"), ("mid", 0.2, "ended"),
            ("finished", 0.8, "resumed"), ("mid", 0.1, "resumed"), ("unsure", 0.6, "resumed")]
    for jev_says, prob, outcome in rows:
        con.execute("INSERT INTO turn_end_shadow(chat_id, created_at, words, jev, jev_ms, "
                    "local_prob, local_ms, outcome, gap_ms, error) VALUES "
                    "(1, ?, 5, ?, 300, ?, 60, ?, 1200, '')",
                    (1_700_000_000.0, jev_says, prob, outcome))
    con.commit()
    con.close()
    out = []
    rep.main(["--since", "2023-11-01", "--until", "2023-11-30"],
             settings=Settings(data_dir=str(data)), out=out.append)
    text = "\n".join(out)
    assert "Pauses in spoken turns" in out[0] and ": 6. 3 ended the turn" in out[0]
    assert "said finished: ended sooner 2 of 3, by about 0.9s each. Cut you off 1 of 3." in text
    assert "said unsure on 1" in text
    assert "at 0.5: ended sooner 1 of 3" in text and "Cut you off 2 of 3" in text
    assert "at 0.9: ended sooner 1 of 3, by about 1.1s each. Cut you off 0 of 3." in text
