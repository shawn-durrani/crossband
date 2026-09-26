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
            "people", "ms", "filled"}


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


# ---------- 7. filling in unnamed turns (voice_session_labels) ----------

LABELS_CFG = dict(CFG, voice_session_labels=True)


def _msg(chat_id, turn_id, labels=None):
    from roomkit import _insert_user_message
    from backend import db
    m = _insert_user_message(chat_id, "a turn", voice_turn_id=turn_id)
    if labels is not None:
        con = db.connect()
        try:
            db.set_message_voice_labels(con, m["id"], labels)
        finally:
            con.close()
    return m["id"]


def _labels(mid):
    from roomkit import _message_labels
    raw = _message_labels(mid)
    return json.loads(raw) if raw else {}


def _chat(app):
    from fastapi.testclient import TestClient
    with TestClient(app, base_url="http://127.0.0.1") as c:
        return c.post("/api/chats", json={"participant_ids": []}).json()["id"]


def test_filling_in_is_off_unless_its_own_switch_is_on(app, fakes):
    chat = _chat(app)
    mid = _msg(chat, "t1", {"clusters": ["local"], "labels": [],
                            "uncertain": [], "unresolved": "below_threshold"})
    fakes.script = [[{"slot": 1, "start": 0.0, "end": 2.0}]]
    row = vss.observe(chat, "t1", _turn(2.0), SR, CFG, _today(()))
    assert "filled" not in row
    assert _labels(mid)["labels"] == []
    assert not vss.labels_enabled({"voice_session_labels": True})


def test_an_unnamed_turn_takes_its_voices_name(app, fakes, monkeypatch):
    monkeypatch.setattr(voice_shadow, "embed", lambda m, p, s, c: SAM)
    chat = _chat(app)
    mid = _msg(chat, "t1", {"clusters": ["local"], "labels": [],
                            "uncertain": [], "unresolved": "below_threshold"})
    fakes.script = [[{"slot": 1, "start": 0.0, "end": 2.0}]]
    row = vss.observe(chat, "t1", _turn(2.0), SR, LABELS_CFG, _today(()))
    assert row["filled"] == 1
    got = _labels(mid)
    assert got["labels"] == ["Sam"] and got["source"] == "session"
    assert "unresolved" not in got and not got.get("owner")
    # memory reads it as the weakest method, which membro never binds on
    from backend.memory_client import speaker_identity
    assert speaker_identity({"voice_labels": got}, "guest:Sam",
                            {})["method"] == "by-elimination"


def test_the_owners_name_carries_the_owner_marker(app, fakes):
    chat = _chat(app)
    mid = _msg(chat, "t1", {"clusters": ["local"], "labels": [],
                            "uncertain": [], "unresolved": "below_threshold"})
    fakes.script = [[{"slot": 1, "start": 0.0, "end": 2.0}]]
    vss.observe(chat, "t1", _turn(2.0), SR, LABELS_CFG, _today(()))
    got = _labels(mid)
    assert got["labels"] == ["Alex"] and got["owner"] is True


def test_a_name_a_correction_or_crosstalk_is_never_touched(app, fakes,
                                                           monkeypatch):
    """The voice is Sam, but each of these turns already says something a
    person or the live pass decided, so none changes."""
    monkeypatch.setattr(voice_shadow, "embed", lambda m, p, s, c: SAM)
    chat = _chat(app)
    keep = {
        "t1": {"clusters": ["local"], "labels": ["Alex"], "uncertain": [],
               "source": "local", "score": 0.6},
        "t2": {"clusters": [], "labels": ["Alex"], "uncertain": [],
               "corrected": True, "source": "correction"},
        "t3": {"clusters": ["s0", "s1"], "labels": [], "uncertain": [],
               "crosstalk": True},
    }
    ids = {t: _msg(chat, t, labels) for t, labels in keep.items()}
    fakes.script = [[{"slot": 1, "start": 0.0, "end": 2.0}],
                    [{"slot": 1, "start": 2.0, "end": 4.0}],
                    [{"slot": 1, "start": 4.0, "end": 6.0}]]
    for t in ("t1", "t2", "t3"):
        vss.observe(chat, t, _turn(2.0), SR, LABELS_CFG, _today(()))
    for t, labels in keep.items():
        assert _labels(ids[t]) == labels


def test_a_turn_named_late_is_filled_when_its_voice_is_named(app, fakes,
                                                             monkeypatch):
    """1 s is too little to name, so the first turn stays unnamed. The
    second turn names the voice, and the first turn takes the name too.
    A turn whose message wasn't saved yet at its own pass is filled on a
    later one."""
    monkeypatch.setattr(voice_shadow, "embed", lambda m, p, s, c: SAM)
    chat = _chat(app)
    first = _msg(chat, "t1", {"clusters": ["local"], "labels": [],
                              "uncertain": [], "unresolved": "too_short"})
    fakes.script = [[{"slot": 1, "start": 0.0, "end": 1.0}],
                    [{"slot": 1, "start": 1.0, "end": 3.0}],
                    [{"slot": 1, "start": 3.0, "end": 5.0}]]
    assert vss.observe(chat, "t1", _turn(1.0), SR, LABELS_CFG,
                       _today(()))["filled"] == 0
    assert _labels(first)["labels"] == []
    vss.observe(chat, "t2", _turn(2.0), SR, LABELS_CFG, _today(()))
    assert _labels(first)["labels"] == ["Sam"]
    second = _msg(chat, "t2")            # saved after its own pass
    vss.observe(chat, "t3", _turn(2.0), SR, LABELS_CFG, _today(()))
    assert _labels(second)["labels"] == ["Sam"]


def test_nothing_but_the_label_changes(app, fakes, monkeypatch):
    def boom(*a, **k):
        raise AssertionError("filling in must not seat or bank")
    from backend import room_state
    monkeypatch.setattr(room_state, "seat", boom)
    monkeypatch.setattr(anchors.AnchorStore, "add_clip", boom)
    chat = _chat(app)
    _msg(chat, "t1", {"clusters": ["local"], "labels": [], "uncertain": []})
    fakes.script = [[{"slot": 1, "start": 0.0, "end": 2.0}]]
    assert vss.observe(chat, "t1", _turn(2.0), SR, LABELS_CFG,
                       _today(()))["filled"] == 1


def test_fillable():
    assert vss.fillable({})
    assert vss.fillable({"labels": [], "unresolved": "multi"})
    assert vss.fillable({"labels": ["Sam"], "source": "session"})
    assert not vss.fillable({"labels": ["Sam"], "source": "local"})
    assert not vss.fillable({"labels": ["Sam"], "uncertain": ["Sam"],
                             "learning": True, "source": "cold-start"})
    assert not vss.fillable({"labels": [], "corrected": True})
    assert not vss.fillable({"labels": [], "crosstalk": True})
    assert not vss.fillable(None)


# ---------- 8. live: the relay feeds the tracker, the check asks it ----------

LIVE_CFG = dict(CFG, voice_session_live=True)


@pytest.fixture
def live(fakes, monkeypatch):
    monkeypatch.setattr(vss, "embed_live", lambda pcm, sr, cfg: ALEX)
    monkeypatch.setattr(vss, "_live_candidates", lambda chat_id: [])
    return fakes


def _chunks(seconds, size=0.1):
    pcm = _turn(seconds)
    step = int(size * SR) * 2
    return [pcm[i:i + step] for i in range(0, len(pcm), step)]


def test_live_is_off_unless_its_own_switch_is_on(fakes):
    vss.feed(3, _turn(0.5), SR, CFG)
    vss.end_turn(3, "t1", CFG)
    assert fakes.calls == [] and vss._feeds == {}
    assert vss.wait_turn("t1", timeout=0.01) is None
    assert not vss.live_enabled({"voice_session_live": True})


def test_the_feed_pushes_as_audio_arrives_and_names_the_turn(live):
    live.script = [[{"slot": 1, "start": 0.0, "end": 1.0}]]
    for chunk in _chunks(1.0):
        vss.feed(3, chunk, SR, LIVE_CFG)
    vss.end_turn(3, "t1", LIVE_CFG)
    got = vss.wait_turn("t1", timeout=3)
    assert got == {"voice": 1, "state": "listening", "name": "",
                   "score": 1.0}     # 1 s alone is too little to name
    paths = [c[1] for c in live.calls]
    assert paths[0] == "/sessions" and paths[-1] == "/sessions/s1/end-turn"
    audio = [c for c in live.calls if c[1].endswith("/audio")]
    # quarter-second pieces while the turn runs, the rest before end-turn
    sizes = [c[2] for c in audio]
    assert sizes == [9600, 9600, 9600, 3200]   # 0.1 s chunks, pushed at 0.25 s
    assert sum(sizes) == len(_turn(1.0))
    row = vss.read_rows()[0]
    assert row["live"] is True and row["turn_id"] == "t1"


def test_a_later_turn_is_named_from_the_voices_whole_session(live):
    live.script = [[{"slot": 1, "start": 0.0, "end": 1.0}],
                   [{"slot": 1, "start": 1.0, "end": 3.0}]]
    for chunk in _chunks(1.0):
        vss.feed(3, chunk, SR, LIVE_CFG)
    vss.end_turn(3, "t1", LIVE_CFG)
    assert vss.wait_turn("t1", timeout=3)["state"] == "listening"
    for chunk in _chunks(2.0):
        vss.feed(3, chunk, SR, LIVE_CFG)
    vss.end_turn(3, "t2", LIVE_CFG)
    got = vss.wait_turn("t2", timeout=3)
    assert got["state"] == "named" and got["name"] == "Alex"
    rows = vss.read_rows()
    assert rows[0]["offset"] == 1.0          # session time moved to turn time
    assert rows[0]["spans"] == [{"slot": 1, "start": 0.0, "end": 2.0,
                                 "overlap": False}]


def test_a_failed_push_gives_no_name_and_the_next_turn_reopens(live):
    live.fail_on = "/audio"
    for chunk in _chunks(0.5):
        vss.feed(3, chunk, SR, LIVE_CFG)
    vss.end_turn(3, "t1", LIVE_CFG)
    assert vss.wait_turn("t1", timeout=3) is None
    assert vss.read_rows()[0]["error"] == "unreachable"
    live.fail_on = None
    live.script = [[{"slot": 1, "start": 0.0, "end": 2.0}]]
    for chunk in _chunks(2.0):
        vss.feed(3, chunk, SR, LIVE_CFG)
    vss.end_turn(3, "t2", LIVE_CFG)
    assert vss.wait_turn("t2", timeout=3)["name"] == "Alex"
    assert live.opened == 2


def test_waiting_on_a_turn_that_never_ends_gives_up(live):
    vss.feed(3, _turn(0.3), SR, LIVE_CFG)
    vss._result_slot("never")
    t0 = vss.time.monotonic()
    assert vss.wait_turn("never", timeout=0.05) is None
    assert vss.time.monotonic() - t0 < 1.0
    assert vss.wait_turn("unknown-turn", timeout=5) is None   # no slot: no wait


def test_the_shadow_never_pushes_a_turn_twice_when_live(live):
    assert vss.observe(3, "t1", _turn(2.0), SR, LIVE_CFG, _today()) is None
    assert live.calls == []


# ---------- 9. live: the check names a turn it would leave unnamed ----------

from tests.test_voice_shadow import live_fakes  # noqa: E402,F401  (fixture)


def _room_turn(app, monkeypatch, got, loud=False):
    """Drive one armed-room turn through run_pass with the session's answer
    faked as `got`, and return the turn's labels and the wait calls."""
    from fastapi.testclient import TestClient
    from roomkit import _insert_user_message
    from tests import test_voice_shadow as tvs
    waited = []

    async def wait(turn_id, timeout=vss.LIVE_WAIT_S, step=0.02):
        waited.append(turn_id)
        return got
    monkeypatch.setattr(vss, "await_turn", wait)
    monkeypatch.setattr(voice_shadow, "schedule", lambda *a, **k: None)
    cfg = dict(tvs.BASE_CFG, **LIVE_CFG)
    with TestClient(app, base_url="http://127.0.0.1") as c:
        chat, _, _ = tvs._setup(c)
        m = _insert_user_message(chat["id"], voice_turn_id="t1")
        tvs._turn(chat["id"], speech_pcm(3.0, amp=tvs.ALEX_AMP if loud
                                         else tvs.SAM_AMP), "t1", cfg)
        return json.loads(tvs._labels(m["id"]) or "{}"), waited


def test_a_deferred_turn_takes_its_session_name(app, live_fakes,
                                                monkeypatch):
    labels, waited = _room_turn(app, monkeypatch, {
        "voice": 1, "state": "named", "name": "Sam", "score": 0.71})
    assert waited == ["t1"]
    assert labels["labels"] == ["Sam"] and labels["source"] == "session"
    assert "unresolved" not in labels and not labels.get("owner")


def test_no_session_name_in_time_leaves_todays_unnamed_marker(
        app, live_fakes, monkeypatch):
    labels, waited = _room_turn(app, monkeypatch, None)
    assert waited == ["t1"]
    assert labels["labels"] == [] and labels["unresolved"] == \
        "below_threshold"


def test_a_voice_still_listening_leaves_the_marker_too(app, live_fakes,
                                                       monkeypatch):
    labels, _ = _room_turn(app, monkeypatch, {
        "voice": 1, "state": "listening", "name": "", "score": 0.4})
    assert labels["labels"] == [] and "unresolved" in labels


def test_a_turn_the_matcher_names_never_waits(app, live_fakes, monkeypatch):
    labels, waited = _room_turn(app, monkeypatch, None, loud=True)
    assert waited == []
    assert labels["labels"] == ["Alex"] and labels["source"] == "local"


def test_the_relay_feeds_every_chunk_and_ends_the_turn_before_the_check(
        app, monkeypatch):
    from fastapi.testclient import TestClient
    from backend import auth, diarize
    from backend.routers import voice as voice_router
    from tests.test_stt_relay import FakeEleven, _frame
    fake = FakeEleven()
    monkeypatch.setattr(voice_router.websockets, "connect",
                        lambda *a, **kw: fake)
    monkeypatch.setattr(voice_router.voice, "enabled", lambda: True)
    monkeypatch.setattr(voice_router.voice, "api_key", lambda: "test-key")
    monkeypatch.setattr(auth, "GATE_LOOPBACK_HOSTS",
                        auth.GATE_LOOPBACK_HOSTS | {"testserver"})
    app.state.allowed_hosts = {"testserver", "127.0.0.1", "localhost", "::1"}
    voice_router._captures.clear()
    order = []
    monkeypatch.setattr(vss, "feed", lambda chat_id, pcm, sr, cfg:
                        order.append(("feed", len(pcm))))
    monkeypatch.setattr(vss, "end_turn", lambda chat_id, tid, cfg:
                        order.append(("end", tid)))
    monkeypatch.setattr(diarize, "schedule_turn_check",
                        lambda *a, **k: order.append(("check",
                                                      k.get("turn_id"))))
    with TestClient(app, base_url="http://127.0.0.1") as c:
        chat = c.post("/api/chats", json={}).json()
        with c.websocket_connect("/api/voice/stt-stream") as ws:
            ws.send_json({"chat_id": chat["id"]})
            assert ws.receive_json()["session"]
            ws.send_json(_frame())
            ws.receive_json()
            ws.send_json(dict(_frame(commit=True), turn_id="t9"))
            ws.receive_json()
            ws.send_json({"done": True})
    assert order == [("feed", 320), ("feed", 320), ("end", "t9"),
                     ("check", "t9")]


def test_the_async_wait_polls_without_a_thread():
    import asyncio
    vss._reset_for_tests()
    assert asyncio.run(vss.await_turn("nobody", timeout=5)) is None  # no slot
    vss._result_slot("slow")
    t0 = vss.time.monotonic()
    assert asyncio.run(vss.await_turn("slow", timeout=0.06)) is None
    assert vss.time.monotonic() - t0 < 1.0
    vss._resolve("done", {"state": "named", "name": "Sam"})
    assert asyncio.run(vss.await_turn("done"))["name"] == "Sam"
    vss._reset_for_tests()
