"""The session shadow (#482 stage 2): the redesign's session tracking and
pooled naming, measured beside today's, changing nothing.

What these tests pin, in order:

1. OFF BY DEFAULT. Without its own switch, or without the shadow's
   loopback diariser, the shadow's worker never calls it.
2. NAMING A VOICE FROM POOLED EVIDENCE. Listening under 1.5 s of clean
   speech, named over the bar and the margin, one person per voice, new
   once a voice has 4 s and matches nobody, and the pooled fingerprint
   weights by length.
3. ONE TRACKING SESSION PER CHAT. Opened once, every turn pushed then
   ended in order, spans moved from session time to turn time, a fresh
   session after the idle limit (the old one closed), and a failed call
   dropping the session so the next turn reopens.
4. ONLY CLEAN SPEECH IS FINGERPRINTED. Overlapped and short spans never
   are, and evidence builds across turns so a short turn is named from
   what the voice said before.
5. NO SCORING AGAINST ITSELF. A clip banked after the session opened is
   left out of the comparison.
6. CONTENT-FREE ROWS, AND THE COMPARE VIEW. No words, no audio. The view
   gives today's label, the name at the time and the name at the end.

Keyless and offline: the diariser and the speaker model are fakes.
Synthetic roster (Alex, Sam).
"""

import asyncio
import json

import pytest

from backend import anchors, voice_session_shadow as vss, voice_shadow, voiceid
from backend.app import create_app
from backend.config import Settings
from tests.conftest import speech_pcm

SR = 16000
CFG = {"user_name": "Alex", "diarize_shadow_url": "http://127.0.0.1:8910",
       "voice_session_shadow": True}
ALEX = [1.0, 0.0, 0.0, 0.0]
SAM = [0.0, 1.0, 0.0, 0.0]
BAR = {"threshold": 0.5, "margin": 0.1}


@pytest.fixture
def app(tmp_path):
    settings = Settings(data_dir=str(tmp_path / "data"),
                        memory_url="http://127.0.0.1:1", user_name="Alex")
    a = create_app(settings)
    vss._reset_for_tests()
    yield a
    vss._reset_for_tests()


class FakeDiariser:
    """Stands in for diarserve's /sessions routes. `script` is a list of
    span lists, one per turn, in SESSION time; each turn answers them on
    end-turn."""

    def __init__(self, script=(), fail_on=None):
        self.script = list(script)
        self.calls = []
        self.opened = 0
        self.fail_on = fail_on

    def __call__(self, method, url, content=None):
        self.calls.append((method, url.split("8910", 1)[1],
                           len(content or b"")))
        if self.fail_on and self.fail_on in url:
            import httpx
            raise httpx.ConnectError("down")
        if method == "POST" and url.endswith("/sessions"):
            self.opened += 1
            return {"session": f"s{self.opened}"}
        if url.endswith("/audio"):
            return {"spans": []}
        if url.endswith("/end-turn"):
            return {"spans": self.script.pop(0) if self.script else []}
        return {}


@pytest.fixture
def fakes(app, monkeypatch):
    diar = FakeDiariser()
    monkeypatch.setattr(vss, "_request", diar)
    monkeypatch.setattr(voice_shadow, "gate", lambda pcm, sr: None)
    monkeypatch.setattr(voice_shadow, "embed", lambda m, p, s, c: ALEX)
    monkeypatch.setattr(vss, "bank", lambda cands, sr, cfg, before: {
        "alex": {"name": "Alex", "clips": [ALEX]},
        "sam": {"name": "Sam", "clips": [SAM]}})
    monkeypatch.setattr(vss, "_bar", lambda *a, **k: dict(BAR, source="t"))
    return diar


def _turn(seconds):
    return speech_pcm(seconds, amp=6000)


def _today(labels=("Alex",)):
    return {"path": "local", "labels": list(labels), "uncertain": [],
            "reason": "", "score": 0.7, "ms": 50.0,
            "candidates": [{"person_id": "alex", "name": "Alex"},
                           {"person_id": "sam", "name": "Sam"}]}


# ---------- 1. off by default ----------

def test_off_without_its_switch_or_without_a_loopback_diariser():
    assert not vss.enabled({})
    assert not vss.enabled({"voice_session_shadow": True})
    assert not vss.enabled({"diarize_shadow_url": "http://127.0.0.1:8910"})
    assert not vss.enabled({"voice_session_shadow": True,
                            "diarize_shadow_url": "http://10.0.0.5:8910"})
    assert vss.enabled(CFG)
    assert Settings().voice_session_shadow is False


def test_the_shadow_worker_calls_it_only_when_on(app, monkeypatch):
    seen = []
    monkeypatch.setattr(vss, "observe", lambda *a: seen.append(a[0]))
    monkeypatch.setattr(voice_shadow, "diarise",
                        lambda url, pcm, sr: ({"error": "x"}, "", 0.0))
    monkeypatch.setattr(voice_shadow, "score_turn", lambda *a, **k: {})
    monkeypatch.setattr(voice_shadow, "write_row", lambda row: None)

    async def go(cfg):
        await voice_shadow._run(7, b"\x00\x00" * SR, SR, cfg, "t1",
                                _today(), 0.0)
    asyncio.run(go(dict(CFG, voice_session_shadow=False)))
    assert seen == []
    asyncio.run(go(CFG))
    assert seen == [7]


# ---------- 2. naming a voice from pooled evidence ----------

def _voice(emb, secs):
    return {"prints": [(emb, secs)], "clean_s": secs}


PEOPLE = {"alex": {"name": "Alex", "clips": [ALEX]},
          "sam": {"name": "Sam", "clips": [SAM]}}


def test_a_voice_listens_until_it_has_enough_clean_speech():
    out = vss.name_voices({0: _voice(ALEX, 1.2)}, PEOPLE, BAR)
    assert out[0]["state"] == "listening" and out[0]["name"] == ""
    out = vss.name_voices({0: _voice(ALEX, 1.6)}, PEOPLE, BAR)
    assert out[0]["state"] == "named" and out[0]["name"] == "Alex"


def test_one_person_per_voice():
    """Two voices that both sound most like Alex: only the closer one is
    Alex, and the other doesn't become Sam by default."""
    near_alex = voiceid.l2_normalize([0.9, 0.3, 0.0, 0.0])
    out = vss.name_voices({0: _voice(near_alex, 3.0), 1: _voice(ALEX, 3.0)},
                          PEOPLE, BAR)
    assert out[1]["name"] == "Alex"
    assert out[0]["state"] == "listening" and out[0]["name"] == ""


def test_two_voices_two_people():
    out = vss.name_voices({0: _voice(ALEX, 2.0), 1: _voice(SAM, 2.0)},
                          PEOPLE, BAR)
    assert (out[0]["name"], out[1]["name"]) == ("Alex", "Sam")


def test_the_margin_holds_a_voice_between_two_people():
    between = voiceid.l2_normalize([1.0, 0.95, 0.0, 0.0])
    out = vss.name_voices({0: _voice(between, 5.0)}, PEOPLE, BAR)
    assert out[0]["state"] == "listening"


def test_a_voice_matching_nobody_becomes_new_after_four_seconds():
    stranger = [0.0, 0.0, 1.0, 0.0]
    assert vss.name_voices({0: _voice(stranger, 3.5)}, PEOPLE,
                           BAR)[0]["state"] == "listening"
    assert vss.name_voices({0: _voice(stranger, 4.0)}, PEOPLE,
                           BAR)[0]["state"] == "new"


def test_the_pooled_fingerprint_weights_by_length():
    pool = vss.pooled([(ALEX, 3.0), (SAM, 1.0)])
    assert pool[0] > pool[1] * 2.9
    assert vss.pooled([]) is None


def test_spans_and_the_main_voice():
    assert vss.clean_spans({"spans": [{"slot": 1, "start": 0, "end": 1}]}) \
        == [{"slot": 1, "start": 0.0, "end": 1.0, "overlap": False}]
    assert vss.clean_spans({"spans": [{"slot": "x"}]}) is None
    assert vss.clean_spans({"nope": 1}) is None
    spans = [{"slot": 0, "start": 0.0, "end": 1.0, "overlap": False},
             {"slot": 1, "start": 1.0, "end": 4.0, "overlap": True},
             {"slot": 0, "start": 4.0, "end": 5.5, "overlap": False}]
    assert vss.main_voice(spans) == 0       # time alone wins
    assert vss.main_voice([]) is None


# ---------- 3. one tracking session per chat ----------

def test_one_session_per_chat_turns_pushed_then_ended_in_order(fakes):
    fakes.script = [[{"slot": 0, "start": 0.0, "end": 2.0}],
                    [{"slot": 0, "start": 2.0, "end": 5.0}]]
    r1 = vss.observe(3, "t1", _turn(2.0), SR, CFG, _today())
    r2 = vss.observe(3, "t2", _turn(3.0), SR, CFG, _today())
    assert fakes.opened == 1
    assert [c[1] for c in fakes.calls] == [
        "/sessions", "/sessions/s1/audio", "/sessions/s1/end-turn",
        "/sessions/s1/audio", "/sessions/s1/end-turn"]
    assert fakes.calls[1][2] == len(_turn(2.0))
    # session time moves to turn time
    assert r2["offset"] == 2.0
    assert r2["spans"] == [{"slot": 0, "start": 0.0, "end": 3.0,
                            "overlap": False}]
    assert r1["main_name"] == "Alex" and r2["turn"] == 2


def test_a_quiet_chat_gets_a_fresh_session(fakes, monkeypatch):
    vss.observe(3, "t1", _turn(2.0), SR, CFG, _today())
    now = [vss.time.time() + vss.SESSION_IDLE_S + 5]
    monkeypatch.setattr(vss.time, "time", lambda: now[0])
    vss.observe(3, "t2", _turn(2.0), SR, CFG, _today())
    assert fakes.opened == 2
    assert ("DELETE", "/sessions/s1", 0) in fakes.calls


def test_a_failed_call_drops_the_session_and_the_next_turn_reopens(
        fakes, caplog):
    fakes.fail_on = "/end-turn"
    r = vss.observe(3, "t1", _turn(2.0), SR, CFG, _today())
    assert r["error"] == "unreachable"
    r = vss.observe(3, "t2", _turn(2.0), SR, CFG, _today())
    assert r["error"] == "unreachable"
    assert fakes.opened == 2
    warnings = [m for m in caplog.messages if "tracking session failed" in m]
    assert len(warnings) <= 1
    fakes.fail_on = None
    assert "error" not in vss.observe(3, "t3", _turn(2.0), SR, CFG, _today())


def test_the_wrong_sample_rate_or_no_audio_does_nothing(fakes):
    assert vss.observe(3, "t1", _turn(1.0), 8000, CFG, _today()) is None
    assert vss.observe(3, "t1", b"", SR, CFG, _today()) is None
    assert fakes.calls == []


# ---------- 4. only clean speech is fingerprinted ----------

def test_overlap_and_short_spans_are_never_fingerprinted(fakes, monkeypatch):
    lengths = []

    def embed(model, pcm, sr, cfg):
        lengths.append(len(pcm) / 2 / sr)
        return ALEX
    monkeypatch.setattr(voice_shadow, "embed", embed)
    fakes.script = [[{"slot": 0, "start": 0.0, "end": 2.0},
                     {"slot": 1, "start": 2.0, "end": 3.5, "overlap": True},
                     {"slot": 1, "start": 3.5, "end": 4.0}]]
    r = vss.observe(3, "t1", _turn(4.0), SR, CFG, _today())
    assert lengths == [2.0]
    assert r["embedded"] == 1
    assert r["voices"]["1"]["clean_s"] == 0.0


def test_evidence_builds_so_a_short_turn_is_named_from_earlier_turns(
        fakes, monkeypatch):
    """1 s of Sam can't be named alone; after a 2 s turn from the same
    voice it is, because the voice's evidence is pooled."""
    monkeypatch.setattr(voice_shadow, "embed", lambda m, p, s, c: SAM)
    fakes.script = [[{"slot": 1, "start": 0.0, "end": 1.0}],
                    [{"slot": 1, "start": 1.0, "end": 3.0}],
                    [{"slot": 1, "start": 3.0, "end": 4.0}]]
    first = vss.observe(3, "t1", _turn(1.0), SR, CFG, _today(()))
    assert first["main_state"] == "listening"
    vss.observe(3, "t2", _turn(2.0), SR, CFG, _today(()))
    third = vss.observe(3, "t3", _turn(1.0), SR, CFG, _today(()))
    assert third["main_name"] == "Sam"
    assert third["voices"]["1"]["clean_s"] == 4.0


# ---------- 5. no scoring against itself ----------

def test_clips_banked_after_the_session_opened_are_left_out(app,
                                                            monkeypatch):
    store = anchors.store()
    pid = store.ensure_person("Sam")
    for _ in range(4):
        assert store.add_clip(pid, speech_pcm(2.0, amp=3000), SR,
                              source="introduction")
    monkeypatch.setattr(voice_shadow, "embed", lambda m, p, s, c: SAM)
    cands = [{"person_id": pid, "name": "Sam"}]
    opened = vss.time.time()
    before_count = len(vss.bank(cands, SR, {}, before=opened)[pid]["clips"])
    vss.time.sleep(0.02)
    assert store.add_clip(pid, speech_pcm(2.5, amp=3100), SR,
                          source="accumulated")
    kept = vss.bank(cands, SR, {}, before=opened)[pid]["clips"]
    everything = vss.bank(cands, SR, {}, before=vss.time.time() + 10)
    assert len(kept) == before_count
    assert len(everything[pid]["clips"]) == before_count + 1


def test_the_live_store_is_never_written(fakes, monkeypatch):
    def boom(*a, **k):
        raise AssertionError("the session shadow wrote to the store")
    for name in ("add_clip", "ensure_person", "remember_audio"):
        if hasattr(anchors.AnchorStore, name):
            monkeypatch.setattr(anchors.AnchorStore, name, boom)
    fakes.script = [[{"slot": 0, "start": 0.0, "end": 2.0}]]
    assert "error" not in vss.observe(3, "t1", _turn(2.0), SR, CFG, _today())


# ---------- 6. rows and the compare view ----------

ROW_KEYS = {"v", "at", "chat_id", "turn_id", "message_id", "seconds",
            "today", "session", "turn", "offset", "spans", "main",
            "main_state", "main_name", "voices", "bar", "embedded",
            "people", "ms"}


def test_rows_are_content_free_and_owner_only(fakes):
    fakes.script = [[{"slot": 0, "start": 0.0, "end": 2.0}]]
    vss.observe(3, "t1", _turn(2.0), SR, CFG, _today())
    path = vss.rows_path()
    assert oct(path.stat().st_mode & 0o777) == "0o600"
    row = json.loads(path.read_text().splitlines()[0])
    assert set(row) <= ROW_KEYS
    text = json.dumps(row)
    assert "prints" not in text and "pcm" not in text


def test_compare_gives_today_then_and_at_the_end(fakes, monkeypatch):
    """The first turn is too short to name, so it reads listening at the
    time. By the end of the session its voice is Sam, and the view says
    so beside today's label."""
    monkeypatch.setattr(voice_shadow, "embed", lambda m, p, s, c: SAM)
    fakes.script = [[{"slot": 2, "start": 0.0, "end": 1.0}],
                    [{"slot": 2, "start": 1.0, "end": 4.0}]]
    vss.observe(3, "t1", _turn(1.0), SR, CFG, _today(("Alex",)))
    vss.observe(3, "t2", _turn(3.0), SR, CFG, _today(()))
    view = vss.compare(vss.read_rows())
    first = next(line for line in view["lines"] if line["turn_id"] == "t1")
    assert first["today"] == "Alex"
    assert first["then"] == "listening"
    assert first["at_end"] == "Sam"
    assert view["tally"]["at_end_differs_from_today"] == 1
    assert view["tally"]["turns"] == 2


def test_the_route_serves_the_view(app, fakes):
    from fastapi.testclient import TestClient
    fakes.script = [[{"slot": 0, "start": 0.0, "end": 2.0}]]
    vss.observe(3, "t1", _turn(2.0), SR, CFG, _today())
    with TestClient(app, base_url="http://127.0.0.1") as c:
        body = c.get("/api/voice/shadow/sessions?rows=true").json()
    assert body["tally"]["turns"] == 1
    assert body["rows"][0]["main_name"] == "Alex"
    assert "status" in body
