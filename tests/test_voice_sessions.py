"""The session naming (#482): follow each voice through a voice session and
name each voice from everything it has said.

What these tests pin, in order:

1. THE DIARISER IS LOOPBACK ONLY. Every non-loopback URL is refused, a
   refused URL starts no feed and sends nothing, the refusal is logged
   once, and the calls ignore proxies and redirects. With no diariser, or
   the matcher off, the feed stays off.
2. NAMING A VOICE FROM POOLED EVIDENCE. Listening under 1.5 s of clean
   speech, named over the bar and the margin, one person per voice, new
   once a voice has 4 s and matches nobody, and the pooled fingerprint
   weights by length. The multi scorer's maths: the mean of each
   person's best clips, its impostor statistics and its z-matched bar.
3. ONE TRACKING SESSION PER CHAT. Opened once, every turn pushed then
   ended in order, spans moved from session time to turn time, a fresh
   session after the idle limit (the old one closed), and a failed call
   dropping the session so the next turn reopens.
4. ONLY CLEAN SPEECH IS FINGERPRINTED. Overlapped and short spans never
   are, and evidence builds across turns so a short turn is named from
   what the voice said before.
5. NO SCORING AGAINST ITSELF. A clip banked after the session opened is
   left out of the comparison, every kept clip is embedded once, and the
   feed never writes to the store.
6. CONTENT-FREE ROWS, AND THE VIEW. No words, no audio. The view gives the
   name at the time and the name at the end, the route serves it, and the
   file is cut back once full.
7. FILLING IN. A named voice's unnamed turns take its name, with the
   owner marker for the owner; a name a person or the pass gave, a
   correction and crosstalk are never touched; nothing but the label
   changes.
8. THE FEED. The relay feeds the tracker as audio arrives, a later turn is
   named from the voice's whole session, a failed push gives no name, and
   the relay feeds every chunk and ends the turn before the check.
9. WARMING. A warm embeds every clip so the first turn embeds none, runs
   once at a time and only with the matcher on, and a new voice chat
   starts one.
10. WHAT THE CROSSTALK SPLIT READS. Every voice heard in a turn, with its
   spans in turn time.
11. LONG TURNS (#469). A piece that names the piece before it answers for
   the whole turn: its voices over every piece, end to end. Pieces whose
   voices disagree list both, a piece too short to judge adds nothing, a
   new turn stands alone, and tap-to-correct names the turn's voice.
   Pieces named on their own are joined by their verdicts by the same
   rules, and the links are bounded.
12. THE SPAN CHECK. A clean span that plainly sounds like another session
   voice moves there before it's added, which catches the tracker mixing
   two people or splitting one. The bar is strict: enough evidence behind
   the voice it joins, a high score, and a clear lead over every rival.
   A normal session moves nothing, and every move is on the row.
13. THE END-OF-SESSION PASS. When a session ends, on the feed thread and
   before it's closed, every voice is named once more and each turn
   whose name changed is relabelled, the last turn included. A voice
   that ends unnamed takes no name off, corrections and crosstalk are
   never touched, a failed session gets no pass, and the pass writes one
   content-free row that the view counts apart from the turns.

Keyless and offline: the diariser and the speaker model are fakes.
Synthetic roster (Alex, Sam).
"""

import asyncio
import json
import logging
import math

import pytest

from backend import anchors, voice_sessions as vss, voiceid
from backend.app import create_app
from backend.config import Settings
from roomkit import _wait_for
from tests.conftest import speech_pcm

SR = 16000
CFG = {"user_name": "Alex", "diarize_shadow_url": "http://127.0.0.1:8910"}
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
    monkeypatch.setattr(vss, "gate", lambda pcm, sr: None)
    monkeypatch.setattr(vss, "embed_live", lambda pcm, sr, cfg: ALEX)
    monkeypatch.setattr(vss, "embed_eres_live", lambda pcm, sr, cfg: None)
    monkeypatch.setattr(vss, "_live_candidates", lambda chat_id: [])
    monkeypatch.setattr(vss, "bank", lambda cands, sr, cfg, before,
                        embed_fn=None: {
        "alex": {"name": "Alex", "clips": [ALEX]},
        "sam": {"name": "Sam", "clips": [SAM]}})
    monkeypatch.setattr(vss, "_bar", lambda *a, **k: dict(BAR, source="t"))
    return diar


def _turn(seconds):
    return speech_pcm(seconds, amp=6000)


def _chunks(seconds, size=0.1):
    pcm = _turn(seconds)
    step = int(size * SR) * 2
    return [pcm[i:i + step] for i in range(0, len(pcm), step)]


def _live(chat_id, turn_id, seconds, cfg=CFG, after=None):
    """One turn through the feed, as the relay drives it: every chunk as it
    arrives, then the commit (naming the piece before it, `after`, when a
    long turn was cut). Returns the turn's result once its row is written
    too (the pass gets the result first, then the row lands)."""
    for chunk in _chunks(seconds):
        vss.feed(chat_id, chunk, SR, cfg)
    vss.end_turn(chat_id, turn_id, cfg, after=after)
    got = vss.wait_turn(turn_id, timeout=3)
    assert _wait_for(lambda: _row(turn_id))
    return got


def _row(turn_id):
    return next((r for r in vss.read_rows() if r["turn_id"] == turn_id),
                None)


# ---------- 1. the diariser is loopback only ----------

@pytest.mark.parametrize("url,base", [
    ("http://127.0.0.1:8910", "http://127.0.0.1:8910"),
    ("http://127.0.0.1:8910/", "http://127.0.0.1:8910"),
    ("http://127.0.0.1:8910/diarize", "http://127.0.0.1:8910"),
    ("http://localhost:8910", "http://localhost:8910"),
    ("http://[::1]:8910", "http://[::1]:8910"),
    ("http://127.0.0.2:9000", "http://127.0.0.2:9000"),
])
def test_loopback_urls_are_accepted(url, base):
    assert vss.loopback_base_url(url) == base


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
    assert vss.loopback_base_url(url) is None


def test_the_feed_is_off_without_a_loopback_diariser_or_the_matcher():
    assert not vss.enabled({})
    assert not vss.enabled({"diarize_shadow_url": "http://10.0.0.5:8910"})
    assert not vss.enabled(dict(CFG, voice_id_enabled=False))
    assert vss.enabled(CFG)
    assert Settings().diarize_shadow_url == ""


def test_a_refused_url_sends_nothing_and_warns_once(app, monkeypatch,
                                                    caplog):
    monkeypatch.setattr(vss, "_request", lambda *a, **k: pytest.fail(
        "audio must never go to a refused URL"))
    caplog.set_level(logging.WARNING, logger="crossband.voice_sessions")
    cfg = dict(CFG, diarize_shadow_url="http://192.168.1.20:8910")
    assert vss.diariser_url(cfg) is None
    assert vss.diariser_url(cfg) is None
    vss.feed(3, _turn(0.5), SR, cfg)
    vss.end_turn(3, "t1", cfg)
    assert vss._feeds == {} and vss.wait_turn("t1", timeout=0.01) is None
    refusals = [r for r in caplog.records if "refused" in r.getMessage()]
    assert len(refusals) == 1
    assert vss.status(cfg)["diariser_refused"] is True
    assert vss.status(cfg)["on"] is False


def test_the_http_client_ignores_proxies_and_redirects(monkeypatch):
    seen = {}

    class FakeClient:
        def __init__(self, **kwargs):
            seen.update(kwargs)

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def request(self, method, url, content=None, headers=None):
            seen["url"] = url

            class R:
                content = b"{}"

                def raise_for_status(self):
                    pass

                def json(self):
                    return {"session": "s1"}
            return R()

    import httpx
    monkeypatch.setattr(httpx, "Client", FakeClient)
    assert vss._request("POST", "http://127.0.0.1:8910/sessions") == {
        "session": "s1"}
    assert seen["trust_env"] is False and seen["follow_redirects"] is False
    assert seen["url"] == "http://127.0.0.1:8910/sessions"
    assert seen["timeout"] == vss.REQUEST_TIMEOUT_S


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


def test_topk_mean_is_the_mean_of_the_best_clips():
    q = [1.0, 0.0]
    clips = [[1.0, 0.0], [0.6, 0.8], [0.0, 1.0]]
    assert vss.topk_mean(q, clips, k=2) == pytest.approx(0.8)
    assert vss.topk_mean(q, clips, k=1) == pytest.approx(1.0)
    assert vss.topk_mean(q, clips[:1], k=2) == pytest.approx(1.0)
    assert vss.topk_mean(q, [], k=2) is None
    assert vss.MULTI_TOP_K == 2


def test_every_clip_reaches_a_voice_the_average_misses():
    """Alex was recorded in two rooms. The average of his clips sits
    between them, so a turn from either room scores lower against it than
    against his clips from that room."""
    phone = voiceid.l2_normalize([1.0, 0.0, 1.0, 0.0])
    laptop = voiceid.l2_normalize([1.0, 0.0, 0.0, 1.0])
    clips = [phone, phone, laptop, laptop]
    average = voiceid.cosine(phone, voiceid.average_embeddings(clips))
    assert average == pytest.approx(0.866, abs=1e-3)
    assert vss.topk_mean(phone, clips) == pytest.approx(1.0)


def _anchors(spec):
    """{pid: {"name", "emb", "clips"}} from {pid: [clip vectors]}."""
    return {pid: {"name": pid.title(), "clips": clips,
                  "emb": voiceid.average_embeddings(clips)}
            for pid, clips in spec.items()}


def test_impostor_statistics_use_cross_speaker_scores_only():
    a = _anchors({"alex": [[1.0, 0.0], [0.8, 0.6]],
                  "sam": [[0.0, 1.0], [0.6, 0.8]]})
    stats = vss.impostor_stats(a)
    expected = [voiceid.cosine(c, a[o]["emb"])
                for p in a for c in a[p]["clips"] for o in a if o != p]
    mean = sum(expected) / 4
    std = math.sqrt(sum((x - mean) ** 2 for x in expected) / 4)
    assert stats["n"] == 4
    assert stats["mean"] == round(mean, 4)
    assert stats["std"] == round(max(std, vss.SPREAD_FLOOR), 4)
    # one person has nobody to be an impostor against
    assert vss.impostor_stats(_anchors({"alex": [[1, 0]] * 3})) is None


def test_the_multi_scorer_gets_its_own_z_matched_bar():
    small = {"mean": 0.2, "std": 0.1, "n": 10}
    multi = {"mean": 0.3, "std": 0.05, "n": 10}
    bar = vss.multi_bar({}, small, multi)
    # The matcher's threshold 0.5 is z = 3 on its scale; the same z here
    assert bar["threshold"] == pytest.approx(0.3 + 3 * 0.05)
    assert bar["margin"] == pytest.approx(0.12 * 0.5)
    assert bar["source"] == "matched" and bar["k"] == 2
    live = vss.multi_bar({}, small, None)
    assert live["source"] == "live"
    assert (live["threshold"], live["margin"]) == (0.5, 0.12)
    stats = vss.multi_impostor_stats(
        {"a": {"clips": [[1.0, 0.0], [0.8, 0.6]]},
         "b": {"clips": [[0.0, 1.0], [0.6, 0.8]]}})
    expected = [vss.topk_mean(c, o) for c, o in (
        ([1.0, 0.0], [[0.0, 1.0], [0.6, 0.8]]),
        ([0.8, 0.6], [[0.0, 1.0], [0.6, 0.8]]),
        ([0.0, 1.0], [[1.0, 0.0], [0.8, 0.6]]),
        ([0.6, 0.8], [[1.0, 0.0], [0.8, 0.6]]))]
    assert stats["n"] == 4
    assert stats["mean"] == round(sum(expected) / 4, 4)


# ---------- 3. one tracking session per chat ----------

def test_one_session_per_chat_turns_pushed_then_ended_in_order(fakes):
    fakes.script = [[{"slot": 0, "start": 0.0, "end": 2.0}],
                    [{"slot": 0, "start": 2.0, "end": 5.0}]]
    r1 = _live(3, "t1", 2.0)
    r2 = _live(3, "t2", 3.0)
    assert fakes.opened == 1
    paths = [c[1] for c in fakes.calls]
    assert paths[0] == "/sessions"
    ends = [i for i, p in enumerate(paths) if p.endswith("/end-turn")]
    assert len(ends) == 2 and ends[-1] == len(paths) - 1
    assert all(p == "/sessions/s1/audio" for p in paths[1:ends[0]])
    assert sum(c[2] for c in fakes.calls[:ends[0]]) == len(_turn(2.0))
    # session time moves to turn time
    row = _row("t2")
    assert row["offset"] == 2.0
    assert row["spans"] == [{"slot": 0, "start": 0.0, "end": 3.0,
                             "overlap": False}]
    assert r1["name"] == "Alex" and r2["name"] == "Alex" and row["turn"] == 2


def test_a_quiet_chat_gets_a_fresh_session(fakes, monkeypatch):
    fakes.script = [[{"slot": 0, "start": 0.0, "end": 2.0}],
                    [{"slot": 0, "start": 0.0, "end": 2.0}]]
    _live(3, "t1", 2.0)
    now = [vss.time.time() + vss.SESSION_IDLE_S + 5]
    monkeypatch.setattr(vss.time, "time", lambda: now[0])
    _live(3, "t2", 2.0)
    assert fakes.opened == 2
    assert ("DELETE", "/sessions/s1", 0) in fakes.calls


def test_a_failed_call_drops_the_session_and_the_next_turn_reopens(
        fakes, caplog):
    fakes.fail_on = "/end-turn"
    assert _live(3, "t1", 2.0) is None
    assert _row("t1")["error"] == "unreachable"
    assert _live(3, "t2", 2.0) is None
    assert _row("t2")["error"] == "unreachable"
    assert fakes.opened == 2
    warnings = [m for m in caplog.messages if "tracking session failed" in m]
    assert len(warnings) <= 1
    fakes.fail_on = None
    fakes.script = [[{"slot": 0, "start": 0.0, "end": 2.0}]]
    assert _live(3, "t3", 2.0)["name"] == "Alex"
    assert "error" not in _row("t3")


def test_the_wrong_sample_rate_or_no_audio_does_nothing(fakes):
    vss.feed(3, _turn(1.0), 8000, CFG)
    vss.feed(3, b"", SR, CFG)
    assert vss._feeds == {} and fakes.calls == []


# ---------- 4. only clean speech is fingerprinted ----------

def test_overlap_and_short_spans_are_never_fingerprinted(fakes, monkeypatch):
    lengths = []

    def embed(pcm, sr, cfg):
        lengths.append(len(pcm) / 2 / sr)
        return ALEX
    monkeypatch.setattr(vss, "embed_live", embed)
    fakes.script = [[{"slot": 0, "start": 0.0, "end": 2.0},
                     {"slot": 1, "start": 2.0, "end": 3.5, "overlap": True},
                     {"slot": 1, "start": 3.5, "end": 4.0}]]
    _live(3, "t1", 4.0)
    row = _row("t1")
    assert lengths == [2.0]
    assert row["embedded"] == 1
    assert row["voices"]["1"]["clean_s"] == 0.0


def test_evidence_builds_so_a_short_turn_is_named_from_earlier_turns(
        fakes, monkeypatch):
    """1 s of Sam can't be named alone; after a 2 s turn from the same
    voice it is, because the voice's evidence is pooled."""
    monkeypatch.setattr(vss, "embed_live", lambda pcm, sr, cfg: SAM)
    fakes.script = [[{"slot": 1, "start": 0.0, "end": 1.0}],
                    [{"slot": 1, "start": 1.0, "end": 3.0}],
                    [{"slot": 1, "start": 3.0, "end": 4.0}]]
    assert _live(3, "t1", 1.0)["state"] == "listening"
    _live(3, "t2", 2.0)
    third = _live(3, "t3", 1.0)
    assert third["name"] == "Sam"
    assert _row("t3")["voices"]["1"]["clean_s"] == 4.0


# ---------- 5. no scoring against itself ----------

def test_clips_banked_after_the_session_opened_are_left_out(app,
                                                            monkeypatch):
    store = anchors.store()
    pid = store.ensure_person("Sam")
    for _ in range(4):
        assert store.add_clip(pid, speech_pcm(2.0, amp=3000), SR,
                              source="introduction")
    embed = (lambda pcm: SAM)
    cands = [{"person_id": pid, "name": "Sam"}]
    opened = vss.time.time()
    before_count = len(vss.bank(cands, SR, {}, before=opened,
                                embed_fn=embed)[pid]["clips"])
    vss.time.sleep(0.02)
    assert store.add_clip(pid, speech_pcm(2.5, amp=3100), SR,
                          source="accumulated")
    kept = vss.bank(cands, SR, {}, before=opened, embed_fn=embed)[pid]["clips"]
    everything = vss.bank(cands, SR, {}, before=vss.time.time() + 10,
                          embed_fn=embed)
    assert len(kept) == before_count
    assert len(everything[pid]["clips"]) == before_count + 1


def test_every_kept_clip_is_embedded_once(app):
    calls = []
    embed = (lambda pcm: calls.append(len(pcm)) or SAM)
    store = anchors.store()
    alex = store.ensure_person("Alex")
    for n in range(5):                     # five long clips, all kept
        assert store.add_clip(alex, speech_pcm(2.5 + n / 10, amp=9000), SR,
                              source="introduction")
    cands = [{"person_id": alex, "name": "Alex"}]
    enrolled = vss.build_anchors(cands, SR, {}, embed_fn=embed)
    assert len(enrolled[alex]["clips"]) == anchors.ENROLL_CLIPS
    calls.clear()
    people = vss.bank(cands, SR, {}, before=vss.time.time() + 1,
                      embed_fn=embed)
    assert len(people[alex]["clips"]) == 5          # every kept clip
    assert len(calls) == 5
    vss.bank(cands, SR, {}, before=vss.time.time() + 1, embed_fn=embed)
    assert len(calls) == 5                          # cached per clip
    assert store.add_clip(alex, speech_pcm(1.5, amp=9000), SR,
                          source="accumulated")
    again = vss.bank(cands, SR, {}, before=vss.time.time() + 1,
                     embed_fn=embed)
    assert len(again[alex]["clips"]) == 6
    assert len(calls) == 6                          # only the new clip


def test_the_feed_never_writes_to_the_store(fakes, monkeypatch):
    def boom(*a, **k):
        raise AssertionError("the session naming wrote to the store")
    for name in ("add_clip", "ensure_person"):
        monkeypatch.setattr(anchors.AnchorStore, name, boom)
    monkeypatch.setattr(anchors, "remember_audio", boom)
    fakes.script = [[{"slot": 0, "start": 0.0, "end": 2.0}]]
    assert _live(3, "t1", 2.0)["name"] == "Alex"
    assert "error" not in _row("t1")


# ---------- 6. rows and the view ----------

ROW_KEYS = {"v", "at", "chat_id", "turn_id", "message_id", "seconds",
            "session", "turn", "offset", "spans", "main", "main_state",
            "main_name", "voices", "bar", "embedded", "people", "ms",
            "filled", "method", "error", "pieces", "moved", "span_lead", "end", "turns",
            "renamed"}


def test_rows_are_content_free_and_owner_only(fakes):
    fakes.script = [[{"slot": 0, "start": 0.0, "end": 2.0}]]
    _live(3, "t1", 2.0)
    path = vss.rows_path()
    assert oct(path.stat().st_mode & 0o777) == "0o600"
    row = json.loads(path.read_text().splitlines()[0])
    assert set(row) <= ROW_KEYS
    text = json.dumps(row)
    assert "prints" not in text and "pcm" not in text


def test_the_view_gives_the_name_then_and_at_the_end(fakes, monkeypatch):
    """The first turn is too short to name, so it reads listening at the
    time. By the end of the session its voice is Sam, and the view says
    so."""
    monkeypatch.setattr(vss, "embed_live", lambda pcm, sr, cfg: SAM)
    fakes.script = [[{"slot": 2, "start": 0.0, "end": 1.0}],
                    [{"slot": 2, "start": 1.0, "end": 4.0}]]
    _live(3, "t1", 1.0)
    _live(3, "t2", 3.0)
    view = vss.view(vss.read_rows())
    first = next(line for line in view["lines"] if line["turn_id"] == "t1")
    assert first["then"] == "listening"
    assert first["at_end"] == "Sam"
    assert view["tally"]["turns"] == 2
    assert view["tally"]["named_then"] == 1
    assert view["tally"]["named_at_end"] == 2


def test_the_route_serves_the_view(app, fakes):
    from fastapi.testclient import TestClient
    fakes.script = [[{"slot": 0, "start": 0.0, "end": 2.0}]]
    _live(3, "t1", 2.0)
    with TestClient(app, base_url="http://127.0.0.1") as c:
        body = c.get("/api/voice/sessions?rows=true").json()
        assert c.get("/api/voice/shadow/sessions").status_code == 404
        assert c.get("/api/voice/shadow").status_code == 404
    assert body["tally"]["turns"] == 1
    assert body["rows"][0]["main_name"] == "Alex"
    assert "status" in body


def test_rows_are_cut_back_once_the_file_is_full(tmp_path, monkeypatch):
    from backend import db
    monkeypatch.setattr(db, "DATA_DIR", str(tmp_path))
    monkeypatch.setattr(vss, "ROWS_MAX", 300)
    monkeypatch.setattr(vss, "ROWS_KEEP", 100)
    for n in range(400):
        vss.write_row({"chat_id": 1, "turn_id": f"t{n}", "message_id": n})
    rows = vss.read_rows(limit=1000)
    assert 100 <= len(rows) <= 300
    assert rows[0]["turn_id"] == "t399"


# ---------- 7. filling in unnamed turns ----------

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


UNNAMED = {"clusters": ["session"], "labels": [], "uncertain": [],
           "source": "session", "unresolved": "listening"}


def test_an_unnamed_turn_takes_its_voices_name(app, fakes, monkeypatch):
    monkeypatch.setattr(vss, "embed_live", lambda pcm, sr, cfg: SAM)
    chat = _chat(app)
    mid = _msg(chat, "t1", UNNAMED)
    fakes.script = [[{"slot": 1, "start": 0.0, "end": 1.0}],
                    [{"slot": 1, "start": 1.0, "end": 3.0}]]
    _live(chat, "t1", 1.0)
    _live(chat, "t2", 2.0)
    assert _row("t2")["filled"] == 1
    got = _labels(mid)
    assert got["labels"] == ["Sam"] and got["source"] == "session"
    assert "unresolved" not in got and not got.get("owner")
    # memory reads it as a voice match, with the naming's score as the
    # confidence: membro binds only at 0.8 or more (#482 stage 3)
    from backend.memory_client import speaker_identity
    ident = speaker_identity({"voice_labels": got}, "guest:Sam", {})
    assert ident["method"] == "voice-match"
    assert ident["confidence"] == got["score"]


def test_the_owners_name_carries_the_owner_marker(app, fakes):
    chat = _chat(app)
    mid = _msg(chat, "t1", UNNAMED)
    fakes.script = [[{"slot": 1, "start": 0.0, "end": 1.0}],
                    [{"slot": 1, "start": 1.0, "end": 3.0}]]
    _live(chat, "t1", 1.0)
    _live(chat, "t2", 2.0)
    got = _labels(mid)
    assert got["labels"] == ["Alex"] and got["owner"] is True


def test_a_correction_or_crosstalk_is_never_touched(app, fakes, monkeypatch):
    """The voice is Sam, but each of these turns already says something a
    person decided or held two voices, so none changes."""
    monkeypatch.setattr(vss, "embed_live", lambda pcm, sr, cfg: SAM)
    chat = _chat(app)
    keep = {
        "t1": {"clusters": ["local"], "labels": ["Alex"], "uncertain": [],
               "source": "local", "score": 0.6},
        "t2": {"clusters": [], "labels": ["Alex"], "uncertain": [],
               "corrected": True, "source": "correction"},
        "t3": {"clusters": ["session"], "labels": ["Voice 1", "Voice 2"],
               "uncertain": ["Voice 1", "Voice 2"], "source": "session",
               "crosstalk": True},
    }
    ids = {t: _msg(chat, t, labels) for t, labels in keep.items()}
    fakes.script = [[{"slot": 1, "start": 0.0, "end": 2.0}],
                    [{"slot": 1, "start": 2.0, "end": 4.0}],
                    [{"slot": 1, "start": 4.0, "end": 6.0}]]
    for t in ("t1", "t2", "t3"):
        _live(chat, t, 2.0)
    for t, labels in keep.items():
        assert _labels(ids[t]) == labels


def test_a_turn_named_late_is_filled_when_its_voice_is_named(app, fakes,
                                                             monkeypatch):
    """1 s is too little to name, so the first turn stays unnamed. The
    second turn names the voice, and the first turn takes the name too.
    A turn whose message wasn't saved yet at its own turn's end is filled
    on a later one."""
    monkeypatch.setattr(vss, "embed_live", lambda pcm, sr, cfg: SAM)
    chat = _chat(app)
    first = _msg(chat, "t1", UNNAMED)
    fakes.script = [[{"slot": 1, "start": 0.0, "end": 1.0}],
                    [{"slot": 1, "start": 1.0, "end": 3.0}],
                    [{"slot": 1, "start": 3.0, "end": 5.0}]]
    _live(chat, "t1", 1.0)
    assert _row("t1")["filled"] == 0
    assert _labels(first)["labels"] == []
    _live(chat, "t2", 2.0)
    assert _labels(first)["labels"] == ["Sam"]
    second = _msg(chat, "t2")            # saved after its own turn's end
    _live(chat, "t3", 2.0)
    assert _labels(second)["labels"] == ["Sam"]


def test_nothing_but_the_label_changes(app, fakes, monkeypatch):
    def boom(*a, **k):
        raise AssertionError("filling in must not seat or bank")
    from backend import room_state
    monkeypatch.setattr(room_state, "seat", boom)
    monkeypatch.setattr(anchors.AnchorStore, "add_clip", boom)
    chat = _chat(app)
    _msg(chat, "t1", {"clusters": ["local"], "labels": [], "uncertain": []})
    fakes.script = [[{"slot": 1, "start": 0.0, "end": 1.0}],
                    [{"slot": 1, "start": 1.0, "end": 3.0}]]
    _live(chat, "t1", 1.0)
    _live(chat, "t2", 2.0)
    assert _row("t2")["filled"] == 1


def test_the_turn_being_named_is_never_filled(app, fakes):
    """The pass labels the turn being named right now, from the whole
    answer: it may hold two voices. Its message may already be saved when
    the feed names the voice, and filling it then would put one voice's
    name on a two-voice turn before the pass's crosstalk label lands."""
    chat = _chat(app)
    mid = _msg(chat, "t1")
    fakes.script = [[{"slot": 1, "start": 0.0, "end": 2.0}]]
    assert _live(chat, "t1", 2.0)["name"] == "Alex"
    assert _row("t1")["filled"] == 0
    assert _labels(mid) == {}


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


def test_naming_a_turn_by_hand_fills_its_voices_other_turns(app, fakes,
                                                            monkeypatch):
    """Tap-to-correct names the voice that spoke the turn, and its other
    unnamed turns in the session take the name at once."""
    monkeypatch.setattr(vss, "embed_live",
                        lambda pcm, sr, cfg: [0.0, 0.0, 1.0, 0.0])
    chat = _chat(app)
    m1 = _msg(chat, "t1", UNNAMED)
    m2 = _msg(chat, "t2", UNNAMED)
    fakes.script = [[{"slot": 4, "start": 0.0, "end": 2.0}],
                    [{"slot": 4, "start": 2.0, "end": 4.0}]]
    _live(chat, "t1", 2.0)
    _live(chat, "t2", 2.0)
    assert _labels(m2)["labels"] == []            # nobody known
    assert vss.human_named(chat, "t1", "Dave", "p-dave", CFG)
    assert _labels(m2)["labels"] == ["Dave"]
    assert not vss.human_named(chat, "t-unknown", "Dave", "p-dave", CFG)


# ---------- 8. the feed ----------

def test_the_feed_pushes_as_audio_arrives_and_names_the_turn(fakes):
    fakes.script = [[{"slot": 1, "start": 0.0, "end": 1.0}]]
    got = _live(3, "t1", 1.0)
    assert {k: got[k] for k in ("voice", "state", "name", "score")} == {
        "voice": 1, "state": "listening", "name": "", "score": 1.0}
    # 1 s alone is too little to name
    paths = [c[1] for c in fakes.calls]
    assert paths[0] == "/sessions" and paths[-1] == "/sessions/s1/end-turn"
    audio = [c for c in fakes.calls if c[1].endswith("/audio")]
    # quarter-second pieces while the turn runs, the rest before end-turn
    sizes = [c[2] for c in audio]
    assert sizes == [9600, 9600, 9600, 3200]   # 0.1 s chunks, pushed at 0.25 s
    assert sum(sizes) == len(_turn(1.0))
    assert vss.read_rows()[0]["turn_id"] == "t1"


def test_a_later_turn_is_named_from_the_voices_whole_session(fakes):
    fakes.script = [[{"slot": 1, "start": 0.0, "end": 1.0}],
                    [{"slot": 1, "start": 1.0, "end": 3.0}]]
    assert _live(3, "t1", 1.0)["state"] == "listening"
    got = _live(3, "t2", 2.0)
    assert got["state"] == "named" and got["name"] == "Alex"
    row = _row("t2")
    assert row["offset"] == 1.0          # session time moved to turn time
    assert row["spans"] == [{"slot": 1, "start": 0.0, "end": 2.0,
                             "overlap": False}]


def test_a_failed_push_gives_no_name_and_the_next_turn_reopens(fakes):
    fakes.fail_on = "/audio"
    assert _live(3, "t1", 0.5) is None
    assert vss.read_rows()[0]["error"] == "unreachable"
    fakes.fail_on = None
    fakes.script = [[{"slot": 1, "start": 0.0, "end": 2.0}]]
    assert _live(3, "t2", 2.0)["name"] == "Alex"
    assert fakes.opened == 2


def test_waiting_on_a_turn_that_never_ends_gives_up(fakes):
    vss.feed(3, _turn(0.3), SR, CFG)
    vss._result_slot("never")
    t0 = vss.time.monotonic()
    assert vss.wait_turn("never", timeout=0.05) is None
    assert vss.time.monotonic() - t0 < 1.0
    assert vss.wait_turn("unknown-turn", timeout=5) is None   # no slot: no wait


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
    monkeypatch.setattr(vss, "end_turn", lambda chat_id, tid, cfg, after:
                        order.append(("end", tid, after)))
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
            # the next piece of a long turn names the piece before it
            ws.send_json(dict(_frame(commit=True), turn_id="t10", after="t9"))
            ws.receive_json()
            ws.send_json({"done": True})
    assert order == [("feed", 320), ("feed", 320), ("end", "t9", None),
                     ("check", "t9"), ("feed", 320), ("end", "t10", "t9"),
                     ("check", "t10")]
    # the link is ours alone: nothing about it goes to the transcriber
    assert all("after" not in m for m in fake.sent)


def test_the_async_wait_polls_without_a_thread():
    vss._reset_for_tests()
    assert asyncio.run(vss.await_turn("nobody", timeout=5)) is None  # no slot
    vss._result_slot("slow")
    t0 = vss.time.monotonic()
    assert asyncio.run(vss.await_turn("slow", timeout=0.06)) is None
    assert vss.time.monotonic() - t0 < 1.0
    vss._resolve("done", {"state": "named", "name": "Sam"})
    assert asyncio.run(vss.await_turn("done"))["name"] == "Sam"
    vss._reset_for_tests()


# ---------- 9. warming: the first turn finds the caches full ----------

def _one_person_store():
    store = anchors.store()
    pid = store.ensure_person("Sam")
    for amp in (3000, 3100, 3200, 3300):
        assert store.add_clip(pid, speech_pcm(2.0, amp=amp), SR,
                              source="introduction")
    return pid


def test_a_warm_embeds_every_clip_so_the_first_turn_embeds_none(
        app, monkeypatch):
    _one_person_store()
    calls = []
    monkeypatch.setattr(voiceid, "matcher_status", lambda cfg: "ready")
    monkeypatch.setattr(vss, "embed_live",
                        lambda pcm, sr, cfg: calls.append(len(pcm)) or SAM)
    vss._reset_for_tests()
    vss._warm(CFG)
    warmed = len(calls)
    assert warmed >= 4
    from backend import diarize
    cands = diarize.remembered_candidates()
    people = vss.bank(cands, SR, CFG, before=vss.time.time() + 1,
                      embed_fn=lambda p: vss.embed_live(p, SR, CFG))
    vss._bar(people, cands, SR, CFG)
    assert len(calls) == warmed          # nothing left to embed
    assert not vss._warming.is_set()


def test_a_warm_never_starts_the_model_loading_itself(app, monkeypatch):
    """A warm waits on the matcher's state and gives up; it never claims
    the model fetch, which startup and the first voice check own."""
    claimed = []
    monkeypatch.setattr(voiceid, "_get_extractor",
                        lambda cfg: claimed.append(1))
    vss._warm(CFG)
    assert claimed == [] and not vss._warming.is_set()


def test_warming_is_off_with_the_matcher_off_and_runs_once_at_a_time(
        monkeypatch):
    started = []
    monkeypatch.setattr(vss.threading, "Thread",
                        lambda **kw: type("T", (), {
                            "start": lambda self: started.append(kw["name"])})())
    vss._warming.clear()
    assert vss.start_warm({"voice_id_enabled": False}) is False
    assert started == []
    assert vss.start_warm({}) is True             # no diariser needed
    assert vss.start_warm(CFG) is False           # one at a time
    assert started == ["voice-session-warm"]
    vss._warming.clear()


def test_a_new_voice_chat_starts_a_warm(fakes, monkeypatch):
    warmed = []
    monkeypatch.setattr(vss, "start_warm", lambda cfg: warmed.append(1))
    vss.feed(3, _turn(0.3), SR, CFG)
    vss.feed(3, _turn(0.3), SR, CFG)
    assert warmed == [1]                          # once per new feed


def test_clean_spans_are_fingerprinted_while_the_turn_runs(fakes,
                                                          monkeypatch):
    """A span the tracker calls final mid-turn is fingerprinted then, so
    the turn's end only names; it isn't fingerprinted a second time."""
    calls = []
    monkeypatch.setattr(vss, "embed_live",
                        lambda pcm, sr, cfg: calls.append(len(pcm)) or ALEX)
    pushes = {"n": 0}
    base = fakes

    def diariser(method, url, content=None):
        if url.endswith("/audio"):
            base.calls.append((method, url.split("8910", 1)[1],
                               len(content or b"")))
            pushes["n"] += 1
            if pushes["n"] == 4:          # 1.2 s pushed by now
                return {"spans": [{"slot": 1, "start": 0.0, "end": 1.0}]}
            return {"spans": []}
        return base(method, url, content)
    monkeypatch.setattr(vss, "_request", diariser)
    got = _live(3, "t1", 2.0)
    assert got is not None and got["voice_clean_s"] == 1.0
    assert calls == [SR * 2]              # once, during the turn


# ---------- 10. what the crosstalk split reads (#482 item D) ----------

def test_the_live_result_lists_every_voice_with_its_spans_in_turn_time(
        fakes):
    """A turn after 1 s of earlier audio: the spans come back in session
    time and the result gives them in turn time, with every voice heard,
    its seconds and when it first spoke, and the turn's length. Names,
    numbers and times only."""
    fakes.script = [[{"slot": 1, "start": 0.0, "end": 1.0}],
                    [{"slot": 1, "start": 1.0, "end": 2.6},
                     {"slot": 1, "start": 2.6, "end": 2.9, "overlap": True},
                     {"slot": 2, "start": 2.6, "end": 2.9, "overlap": True},
                     {"slot": 2, "start": 2.9, "end": 4.0}]]
    assert _live(3, "t1", 1.0)["voices_in_turn"] == 1
    got = _live(3, "t2", 3.0)
    assert got["turn_s"] == 3.0
    assert got["spans"] == [
        {"slot": 1, "start": 0.0, "end": 1.6, "overlap": False},
        {"slot": 1, "start": 1.6, "end": 1.9, "overlap": True},
        {"slot": 2, "start": 1.6, "end": 1.9, "overlap": True},
        {"slot": 2, "start": 1.9, "end": 3.0, "overlap": False}]
    assert {slot: (v["seconds"], v["first"], v["state"])
            for slot, v in got["voices"].items()} == {
        1: (1.9, 0.0, "named"), 2: (1.4, 1.6, "listening")}
    assert got["voices"][1]["name"] == "Alex"
    allowed = {"state", "name", "pid", "score", "prob", "human", "seconds",
               "first"}
    assert all(set(v) == allowed for v in got["voices"].values())


def test_a_turn_named_on_its_own_is_one_voice(fakes):
    got = vss.name_single_turn(3, _turn(2.0), SR, dict(CFG))
    assert list(got["voices"]) == [0] and got["turn_s"] == 2.0
    assert got["single"] is True and got["voices_in_turn"] == 1
    from backend import crosstalk
    assert crosstalk.listed_voices(got) == [0]


# ---------- 11. a long turn is named from all its pieces (#469) ----------

def test_a_short_last_piece_takes_the_voice_its_pieces_had(fakes,
                                                           monkeypatch):
    """The 25 September shape: a long remark cut into a 12 s piece and a
    tail too short to track. The message carries the tail's id, and the
    tail used to come back with no answer, so the whole turn read "still
    listening". The tail now answers with the voice of the whole turn."""
    monkeypatch.setattr(vss, "embed_live", lambda pcm, sr, cfg: SAM)
    fakes.script = [[{"slot": 1, "start": 0.0, "end": 12.0}], []]
    assert _live(3, "t1", 12.0)["name"] == "Sam"
    got = _live(3, "t2", 0.4, after="t1")
    assert (got["state"], got["name"], got["voice"]) == ("named", "Sam", 1)
    assert got["pieces"] == 2 and got["voices_in_turn"] == 1
    assert got["turn_s"] == 12.4
    assert got["spans"] == [{"slot": 1, "start": 0.0, "end": 12.0,
                             "overlap": False}]
    assert got["clean_spans"] == []     # none of Sam's speech in the tail
    row = _row("t2")
    assert row["pieces"] == 2 and row["main_name"] == "Sam"
    assert "pieces" not in _row("t1")


def test_pieces_whose_voices_disagree_make_a_two_voice_turn(fakes,
                                                            monkeypatch):
    """Sam speaks the first piece and Alex the second, each clearly. The
    turn never gets one name: it lists both voices, by their time in the
    whole turn, which makes it crosstalk."""
    from backend import crosstalk
    seq = [SAM, ALEX]
    monkeypatch.setattr(vss, "embed_live", lambda pcm, sr, cfg: seq.pop(0))
    fakes.script = [[{"slot": 1, "start": 0.0, "end": 10.0}],
                    [{"slot": 2, "start": 10.0, "end": 16.0}]]
    _live(3, "t1", 10.0)
    got = _live(3, "t2", 6.0, after="t1")
    assert crosstalk.listed_voices(got) == [1, 2]
    assert {s: (v["name"], v["seconds"], v["first"])
            for s, v in got["voices"].items()} == {
        1: ("Sam", 10.0, 0.0), 2: ("Alex", 6.0, 10.0)}
    assert got["voices_in_turn"] == 2 and got["voice"] == 1
    # the clean spans are this piece's own, of the turn's main voice, and
    # Sam isn't in this piece (a two-voice turn saves no clip anyway)
    assert got["clean_spans"] == []


def test_a_piece_too_short_to_judge_changes_nothing(fakes, monkeypatch):
    """Half a second of another voice at the end of a long turn is too
    short to fingerprint or to list: the turn stays Sam's alone."""
    from backend import crosstalk
    monkeypatch.setattr(vss, "embed_live", lambda pcm, sr, cfg: SAM)
    fakes.script = [[{"slot": 1, "start": 0.0, "end": 12.0}],
                    [{"slot": 2, "start": 12.0, "end": 12.5}]]
    _live(3, "t1", 12.0)
    got = _live(3, "t2", 0.5, after="t1")
    assert got["name"] == "Sam" and crosstalk.listed_voices(got) == [1]


def test_a_turn_joins_only_the_piece_it_names(fakes, monkeypatch):
    """A new turn names no piece and stands alone, and so does a piece
    naming a turn that isn't the last one the feed saw."""
    monkeypatch.setattr(vss, "embed_live", lambda pcm, sr, cfg: SAM)
    fakes.script = [[{"slot": 1, "start": 0.0, "end": 12.0}], [], []]
    _live(3, "t1", 12.0)
    assert _live(3, "t2", 0.4) is None
    assert _live(3, "t3", 0.4, after="t1") is None


def test_three_pieces_run_end_to_end(fakes, monkeypatch):
    """A 30 s monologue in three pieces: each piece's answer covers every
    piece so far, in the first piece's time, whichever id the message
    ends up carrying."""
    monkeypatch.setattr(vss, "embed_live", lambda pcm, sr, cfg: SAM)
    fakes.script = [[{"slot": 1, "start": 0.0, "end": 12.0}],
                    [{"slot": 1, "start": 12.0, "end": 24.0}],
                    [{"slot": 1, "start": 24.0, "end": 30.0}]]
    _live(3, "t1", 12.0)
    assert _live(3, "t2", 12.0, after="t1")["turn_s"] == 24.0
    got = _live(3, "t3", 6.0, after="t2")
    assert got["pieces"] == 3 and got["turn_s"] == 30.0
    assert [(s["start"], s["end"]) for s in got["spans"]] == [
        (0.0, 12.0), (12.0, 24.0), (24.0, 30.0)]
    assert got["voices"][1]["seconds"] == 30.0
    assert got["clean_spans"] == [(0.0, 6.0)]   # this piece's own time
    assert got["piece_voices"][1]["seconds"] == 6.0


def test_a_piece_the_feed_failed_is_joined_by_what_each_piece_heard(
        fakes, monkeypatch):
    """The feed named the first piece, then failed on the tail. The pass
    names the tail on its own (nothing, it's too short) and joins it with
    what the first piece heard."""
    monkeypatch.setattr(vss, "embed_live", lambda pcm, sr, cfg: SAM)
    fakes.script = [[{"slot": 1, "start": 0.0, "end": 12.0}]]
    first = _live(3, "t1", 12.0)
    assert vss.join_pieces("t1", first, 12.0) is first   # the pass's call
    fakes.fail_on = "/end-turn"
    assert _live(3, "t2", 0.4, after="t1") is None
    got = vss.join_pieces("t2", None, 0.4)
    assert (got["name"], got["pieces"], got["clean_spans"]) == ("Sam", 2, [])


def test_naming_a_long_turn_by_hand_names_the_voice_of_its_pieces(
        app, fakes, monkeypatch):
    """Tap-to-correct on a long turn whose message carries its short tail's
    id names the voice that spoke the turn, not nobody."""
    monkeypatch.setattr(vss, "embed_live",
                        lambda pcm, sr, cfg: [0.0, 0.0, 1.0, 0.0])
    chat = _chat(app)
    fakes.script = [[{"slot": 4, "start": 0.0, "end": 12.0}], []]
    _live(chat, "t1", 12.0)
    _live(chat, "t2", 0.4, after="t1")
    assert vss.human_named(chat, "t2", "Dave", "p-dave", CFG)
    assert vss._sessions[chat]["voices"][4]["human"]["name"] == "Dave"


# What pieces named on their own heard (no diariser, or it failed).

def _v(state="named", name="Sam", score=0.95, pid=None, seconds=0.0):
    return {"state": state, "name": name if state == "named" else "",
            "pid": (pid or f"p-{name}") if state == "named" else "",
            "score": score, "prob": score, "human": False,
            "method": "calibrated", "seconds": seconds}


def _p(seconds, *voices):
    """One piece: its length, and each voice it heard for all of it."""
    return (seconds, [dict(v, seconds=v["seconds"] or seconds)
                      for v in voices])


def test_verdicts_that_agree_name_the_turn_weighted_by_length():
    got = vss.join_verdicts([_p(12.0, _v(score=0.95)), _p(6.0, _v(score=0.99)),
                             _p(8.0, _v("listening", score=0.4)),
                             (0.3, [])])
    assert (got["state"], got["name"], got["pid"]) == ("named", "Sam",
                                                       "p-Sam")
    assert got["prob"] == pytest.approx((12 * 0.95 + 6 * 0.99) / 18,
                                        abs=1e-4)
    assert got["voices_in_turn"] == 1 and got["pieces"] == 4
    assert got["turn_s"] == 26.3 and got["clean_spans"] == []


def test_verdicts_that_disagree_never_give_one_name():
    from backend import crosstalk
    for pieces in ([_p(10.0, _v()), _p(6.0, _v(name="Alex"))],
                   [_p(10.0, _v()), _p(6.0, _v("new", score=0.02))],
                   # one piece that heard two voices keeps both
                   [_p(10.0, _v()), _p(8.0, _v(seconds=6.0),
                                       _v(name="Alex", seconds=2.0))]):
        got = vss.join_verdicts(pieces)
        assert got["name"] == "Sam" and got["voices_in_turn"] == 2
        assert crosstalk.listed_voices(got) == [0, 1]
    # the longer voice leads
    got = vss.join_verdicts([_p(3.0, _v()), _p(9.0, _v(name="Alex"))])
    assert got["name"] == "Alex" and crosstalk.listed_voices(got) == [1, 0]


def test_verdicts_with_nothing_judged_leave_the_answer_as_it_was():
    mine = {"state": "listening"}
    assert vss.join_verdicts([_p(12.0, _v("listening")), (0.4, [])],
                             mine) is mine
    assert vss.join_verdicts([], None) is None
    got = vss.join_verdicts([_p(5.0, _v("new", score=0.01)),
                             _p(5.0, _v("new", score=0.03))])
    assert got["state"] == "new" and got["voices_in_turn"] == 1


def test_what_a_piece_heard():
    assert vss.piece_voices(None, 3.0) == []
    one = vss.piece_voices({"voice": 0, "state": "named", "name": "Sam",
                            "pid": "p-Sam", "score": 0.9, "method": "multi"},
                           3.0)
    assert one == [{"state": "named", "name": "Sam", "pid": "p-Sam",
                    "score": 0.9, "prob": None, "human": None,
                    "seconds": 3.0, "method": "multi"}]
    # a feed's answer over several pieces gives this piece's voices only
    got = {"voices": {1: _v(seconds=20.0)}, "method": "calibrated",
           "piece_voices": {1: _v(seconds=0.4)}}
    assert [v["seconds"] for v in vss.piece_voices(got, 0.4)] == [0.4]


def test_pieces_named_on_their_own_are_joined_by_the_pass(app):
    """No feed answered either piece: the pass joins what each piece was
    named, through the links the relay recorded as the pieces arrived."""
    vss.end_turn(3, "t1", {})             # no diariser: links only
    vss.end_turn(3, "t2", {}, after="t1")
    assert vss.pieces_before("t2") == ["t1"]
    first = vss.join_pieces("t1", dict(_v(), voice=0, single=True), 12.0)
    assert first["name"] == "Sam" and "pieces" not in first
    got = vss.join_pieces("t2", None, 0.4)
    assert (got["state"], got["name"], got["pieces"]) == ("named", "Sam", 2)
    # a feed's answer that already covers every piece is left alone
    covered = {"state": "named", "name": "Sam", "pieces": 2}
    assert vss.join_pieces("t2", covered, 0.4) is covered
    # a turn of its own is its own answer
    vss.note_piece("t9")
    assert vss.join_pieces("t9", None, 0.4) is None


def test_the_links_are_bounded_and_never_loop(app):
    vss.note_piece("a", "b")
    vss.note_piece("b", "a")
    assert vss.pieces_before("a") == ["b"]
    for i in range(1, 20):
        vss.note_piece(f"p{i}", f"p{i - 1}")
    assert len(vss.pieces_before("p19")) == vss.MAX_PIECES - 1
    vss.note_piece("p19")                 # a later copy without the link
    assert vss.pieces_before("p19")       # keeps it
    for i in range(vss.RESULTS_MAX + 10):
        vss.note_piece(f"x{i}")
    assert len(vss._pieces) == vss.RESULTS_MAX


# ---------- 12. the span check ----------

def _ev(emb, secs, eres=None):
    """A session voice with `secs` of clean speech behind it."""
    v = {"prints": [(emb, secs)], "clean_s": secs}
    if eres is not None:
        v["prints_eres"] = [(eres, secs)]
    return v


EMPTY = {"prints": [], "clean_s": 0.0}


def test_a_span_given_to_the_wrong_voice_moves_to_its_own():
    """The tracker mixing two people: one of Sam's spans comes back under
    Alex's voice."""
    voices = {0: _ev(ALEX, 10.0), 1: _ev(SAM, 10.0)}
    home, scores = vss.span_home(voices, 0, SAM)
    assert home == 1
    assert scores == {"sim": 1.0, "own": 0.0, "gap": 1.0}


def test_a_split_voice_with_no_evidence_yet_moves_to_the_voice_it_is():
    """The tracker splitting one person: Alex's voice changes and the
    tracker starts a new voice for it. The new voice has nothing to
    compare, so the bar and the lead over the rest decide."""
    voices = {0: _ev(ALEX, 10.0), 1: _ev(SAM, 10.0), 3: dict(EMPTY)}
    home, scores = vss.span_home(voices, 3, ALEX)
    assert home == 0 and scores["own"] is None and scores["gap"] == 1.0


def test_the_span_check_bar_is_strict():
    near = voiceid.l2_normalize
    # the voice it would join has too little speech behind it
    assert vss.span_home({0: _ev(ALEX, 5.9), 1: _ev(SAM, 10.0)}, 1,
                         ALEX)[0] is None
    # not close enough to it: 0.65
    home, scores = vss.span_home({0: _ev(ALEX, 10.0), 1: _ev(SAM, 10.0)},
                                 1, near([0.65, 0.0, 0.76, 0.0]))
    assert home is None and scores["sim"] == 0.65
    # its own voice is too close behind: a lead of 0.2
    home, scores = vss.span_home({0: _ev(ALEX, 10.0), 1: _ev(SAM, 10.0)},
                                 1, near([0.8, 0.6, 0.0, 0.0]))
    assert home is None and scores["gap"] == pytest.approx(0.2, abs=1e-3)
    # two voices it sounds like: plainly neither
    also_alex = near([0.95, 0.0, 0.31, 0.0])
    assert vss.span_home({0: _ev(ALEX, 10.0), 2: _ev(also_alex, 10.0),
                          1: _ev(SAM, 10.0)}, 1, ALEX)[0] is None
    # a span that sounds like its own voice stays, and says by how much
    home, scores = vss.span_home({0: _ev(ALEX, 10.0), 1: _ev(SAM, 10.0)},
                                 1, SAM)
    assert home is None and scores["gap"] == -1.0
    # nobody else to go to
    assert vss.span_home({1: _ev(SAM, 10.0)}, 1, ALEX) == (None, {})
    assert (vss.SPAN_MOVE_MIN_S, vss.SPAN_MOVE_SIM,
            vss.SPAN_MOVE_MARGIN) == (6.0, 0.7, 0.25)


def test_the_span_check_averages_both_models_where_both_have_prints():
    """TitaNet-Small says Sam plainly, ERes2Net says it's between the two:
    the average (0.75 against Sam, 0.35 against Alex) still moves it. With
    ERes2Net saying Alex, it stays."""
    between = voiceid.l2_normalize([0.4, 0.0, 0.0, 0.5])
    voices = {0: _ev(ALEX, 10.0, eres=[1.0, 0.0, 0.0, 0.0]),
              1: _ev(SAM, 10.0, eres=[0.0, 0.0, 0.0, 1.0])}
    home, scores = vss.span_home(voices, 0, SAM, between)
    assert home == 1 and scores["sim"] == pytest.approx(
        (1.0 + voiceid.cosine(between, [0, 0, 0, 1.0])) / 2, abs=1e-3)
    assert vss.span_home(voices, 0, SAM, [1.0, 0.0, 0.0, 0.0])[0] is None


def test_a_pool_is_kept_until_the_voice_gains_a_fingerprint():
    v = _ev(ALEX, 2.0)
    first = vss.pool_of(v)
    assert vss.pool_of(v) is first
    v["prints"].append((SAM, 2.0))
    assert vss.pool_of(v) is not first
    assert vss.pool_of(v, "prints_eres") is None


def test_the_feed_moves_a_mixed_up_span_and_names_the_turn_by_it(
        fakes, monkeypatch):
    seq = [ALEX, SAM, SAM]
    monkeypatch.setattr(vss, "embed_live", lambda pcm, sr, cfg: seq.pop(0))
    fakes.script = [[{"slot": 0, "start": 0.0, "end": 10.0}],
                    [{"slot": 1, "start": 10.0, "end": 20.0}],
                    [{"slot": 0, "start": 20.0, "end": 23.0}]]
    _live(3, "t1", 10.0)
    _live(3, "t2", 10.0)
    got = _live(3, "t3", 3.0)
    assert (got["voice"], got["name"]) == (1, "Sam")
    row = _row("t3")
    assert row["moved"] == [{"start": 0.0, "end": 3.0, "from": 0, "to": 1,
                             "sim": 1.0, "own": 0.0, "gap": 1.0}]
    assert row["spans"][0]["slot"] == 1
    voices = vss._sessions[3]["voices"]
    assert (voices[0]["clean_s"], voices[1]["clean_s"]) == (10.0, 13.0)


def test_the_feed_folds_a_split_voice_back_into_its_person(fakes,
                                                          monkeypatch):
    monkeypatch.setattr(vss, "embed_live", lambda pcm, sr, cfg: ALEX)
    fakes.script = [[{"slot": 0, "start": 0.0, "end": 10.0}],
                    [{"slot": 5, "start": 10.0, "end": 13.0}]]
    _live(3, "t1", 10.0)
    got = _live(3, "t2", 3.0)
    assert (got["voice"], got["name"], got["voices_in_turn"]) == (0, "Alex",
                                                                  1)
    assert _row("t2")["moved"][0]["from"] == 5
    assert vss._sessions[3]["voices"][5]["clean_s"] == 0.0


def test_a_normal_session_moves_nothing(fakes, monkeypatch):
    """Two people, each heard as themselves with ordinary variation, and a
    newcomer whose voice is nobody's: nothing moves, and the rows say by
    how much each span preferred its own voice."""
    alex_ish = [voiceid.l2_normalize([1.0, 0.1 * i, 0.05, 0.0])
                for i in range(3)]
    sam_ish = [voiceid.l2_normalize([0.1 * i, 1.0, 0.0, 0.05])
               for i in range(3)]
    dave = voiceid.l2_normalize([0.2, 0.1, 1.0, 0.0])
    seq = [alex_ish[0], sam_ish[0], alex_ish[1], sam_ish[1], dave,
           alex_ish[2], sam_ish[2]]
    monkeypatch.setattr(vss, "embed_live", lambda pcm, sr, cfg: seq.pop(0))
    slots = [0, 1, 0, 1, 2, 0, 1]
    fakes.script = [[{"slot": slot, "start": 4.0 * i, "end": 4.0 * i + 4.0}]
                    for i, slot in enumerate(slots)]
    for i in range(len(slots)):
        _live(3, f"t{i}", 4.0)
    rows = [_row(f"t{i}") for i in range(len(slots))]
    assert not any("moved" in r for r in rows)
    leads = [r["span_lead"] for r in rows if "span_lead" in r]
    assert leads and max(leads) < 0
    assert vss._sessions[3]["voices"][2]["clean_s"] == 4.0


# ---------- 13. the end-of-session pass ----------

def _stale(monkeypatch):
    """The clock moves past the idle limit: the next turn finds the
    session stale and it ends."""
    now = [vss.time.time() + vss.SESSION_IDLE_S + 5]
    monkeypatch.setattr(vss.time, "time", lambda: now[0])


def _end_row(session):
    return next((r for r in vss.read_rows()
                 if r.get("end") and r.get("session") == session), None)


SURE = {"clusters": ["session"], "labels": ["Sam"], "uncertain": [],
        "source": "session", "score": 0.97}


def test_the_end_pass_names_the_sessions_last_turn(app, fakes, monkeypatch):
    """No later turn ever fills a session's last turn, so a message whose
    label never landed stayed unnamed. The pass at the end names it."""
    monkeypatch.setattr(vss, "embed_live", lambda pcm, sr, cfg: SAM)
    chat = _chat(app)
    fakes.script = [[{"slot": 1, "start": 0.0, "end": 3.0}],
                    [{"slot": 1, "start": 0.0, "end": 2.0}]]
    assert _live(chat, "t1", 3.0)["name"] == "Sam"
    mid = _msg(chat, "t1", UNNAMED)       # its label never landed
    _stale(monkeypatch)
    _live(chat, "t2", 2.0)                # the next session's first turn
    row = _wait_for(lambda: _end_row("s1"))
    assert (row["end"], row["turns"], row["filled"]) == ("idle", 1, 1)
    assert row["voices"]["1"]["name"] == "Sam" and row["renamed"] == []
    assert _labels(mid)["labels"] == ["Sam"]
    assert fakes.opened == 2 and ("DELETE", "/sessions/s1", 0) in fakes.calls


def test_the_end_pass_writes_only_names_that_changed(app, fakes,
                                                     monkeypatch):
    monkeypatch.setattr(vss, "embed_live", lambda pcm, sr, cfg: SAM)
    chat = _chat(app)
    fakes.script = [[{"slot": 1, "start": 0.0, "end": 3.0}], []]
    _live(chat, "t1", 3.0)
    mid = _msg(chat, "t1", SURE)          # the pass already said Sam
    _stale(monkeypatch)
    _live(chat, "t2", 1.0)
    assert _wait_for(lambda: _end_row("s1"))["filled"] == 0
    assert _labels(mid) == SURE


def test_a_changed_name_is_relabelled_and_nothing_else_moves(app, fakes,
                                                             monkeypatch):
    """By the end of the session the banks name Sam's voice Samuel, and
    Alex's voice is no longer anyone the banks know. Sam's plain turn takes
    the new name. Alex's turn keeps its name: the pass never takes one off.
    A corrected turn and a two-voice turn on Sam's voice are left alone."""
    seq = [SAM, ALEX, SAM, SAM]
    monkeypatch.setattr(vss, "embed_live", lambda pcm, sr, cfg: seq.pop(0))
    chat = _chat(app)
    fakes.script = [[{"slot": 1, "start": 0.0, "end": 3.0}],
                    [{"slot": 2, "start": 3.0, "end": 6.0}],
                    [{"slot": 1, "start": 6.0, "end": 8.0}],
                    [{"slot": 1, "start": 8.0, "end": 10.0}], []]
    for n, secs in enumerate((3.0, 3.0, 2.0, 2.0), start=1):
        _live(chat, f"t{n}", secs)
    alex = dict(SURE, labels=["Alex"], owner=True)
    corrected = {"clusters": [], "labels": ["Dave"], "uncertain": [],
                 "corrected": True, "source": "correction"}
    crosstalk = {"clusters": ["session"], "labels": ["Sam", "Voice 2"],
                 "uncertain": ["Voice 2"], "source": "session",
                 "crosstalk": True}
    ids = {"t1": _msg(chat, "t1", SURE), "t2": _msg(chat, "t2", alex),
           "t3": _msg(chat, "t3", corrected),
           "t4": _msg(chat, "t4", crosstalk)}
    monkeypatch.setattr(vss, "bank", lambda cands, sr, cfg, before,
                        embed_fn=None: {
        "sam": {"name": "Samuel", "clips": [SAM]}})
    _stale(monkeypatch)
    _live(chat, "t5", 1.0)
    row = _wait_for(lambda: _end_row("s1"))
    assert row["filled"] == 1 and row["renamed"] == [1, 2]
    assert row["voices"]["2"]["state"] == "listening"
    assert _labels(ids["t1"])["labels"] == ["Samuel"]
    assert _labels(ids["t2"]) == alex
    assert _labels(ids["t3"]) == corrected
    assert _labels(ids["t4"]) == crosstalk


def test_the_end_pass_runs_on_the_feed_thread_before_the_close(
        app, fakes, monkeypatch):
    import threading
    seen = []
    real_fill, real_close = vss.fill_labels, vss._close

    def fill(*a, **k):
        seen.append(("fill", threading.current_thread().name))
        return real_fill(*a, **k)

    def close(sess):
        seen.append(("close", sess["id"]))
        real_close(sess)
    monkeypatch.setattr(vss, "fill_labels", fill)
    monkeypatch.setattr(vss, "_close", close)
    chat = _chat(app)
    fakes.script = [[{"slot": 1, "start": 0.0, "end": 3.0}], []]
    _live(chat, "t1", 3.0)
    _stale(monkeypatch)
    got = _live(chat, "t2", 1.0)          # its answer comes first
    assert got is None and _wait_for(lambda: ("close", "s1") in seen)
    feed = f"voice-session-feed-{chat}"
    assert seen[-2:] == [("fill", feed), ("close", "s1")]
    assert all(who == feed for what, who in seen if what == "fill")


def test_a_quiet_feed_ends_its_session_with_the_pass(app, fakes,
                                                     monkeypatch):
    """The feed thread exits after the idle limit, and the session it
    held gets its pass before it's closed."""
    monkeypatch.setattr(vss, "SESSION_IDLE_S", 0.4)
    chat = _chat(app)
    fakes.script = [[{"slot": 1, "start": 0.0, "end": 3.0}]]
    _live(chat, "t1", 3.0)
    row = _wait_for(lambda: _end_row("s1"), timeout=5)
    assert row["end"] == "idle" and row["voices"]["1"]["name"] == "Alex"
    assert _wait_for(lambda: ("DELETE", "/sessions/s1", 0) in fakes.calls)
    assert _wait_for(lambda: chat not in vss._feeds)


def test_a_new_diariser_url_ends_the_session_for_the_pass(app, fakes):
    fakes.script = [[{"slot": 1, "start": 0.0, "end": 3.0}]]
    _live(3, "t1", 3.0)
    ended = []
    sess = vss._sessions[3]
    fresh = vss._session_for(3, "http://localhost:8910",
                             vss.time.time(), ended=ended)
    assert ended == [(sess, "diariser_changed")] and fresh is not sess
    assert ("DELETE", "/sessions/s1", 0) not in fakes.calls   # the feed's


def test_a_failed_session_and_an_empty_one_get_no_pass(app, fakes):
    fakes.fail_on = "/end-turn"
    assert _live(3, "t1", 2.0) is None
    assert not any(r.get("end") for r in vss.read_rows())
    assert vss.end_session(3, {"id": "s9", "turn_voice": []}, CFG,
                           "idle") is None


def test_the_end_row_is_content_free_and_the_view_counts_it_apart(
        app, fakes, monkeypatch):
    monkeypatch.setattr(vss, "embed_live", lambda pcm, sr, cfg: SAM)
    chat = _chat(app)
    fakes.script = [[{"slot": 1, "start": 0.0, "end": 3.0}], []]
    _live(chat, "t1", 3.0)
    _msg(chat, "t1", UNNAMED)
    _stale(monkeypatch)
    _live(chat, "t2", 1.0)
    row = _wait_for(lambda: _end_row("s1"))
    assert set(row) <= ROW_KEYS
    text = json.dumps(row)
    assert "prints" not in text and "pcm" not in text
    view = vss.view(vss.read_rows())
    assert view["tally"]["sessions_ended"] == 1
    assert view["tally"]["relabelled_at_end"] == 1
    assert [line["turn_id"] for line in view["lines"]] == ["t2", "t1"]
    first = next(line for line in view["lines"] if line["turn_id"] == "t1")
    assert first["at_end"] == "Sam"


def test_the_end_pass_changes_nothing_but_labels(app, fakes, monkeypatch):
    def boom(*a, **k):
        raise AssertionError("the end-of-session pass must not seat or bank")
    from backend import room_state
    monkeypatch.setattr(room_state, "seat", boom)
    monkeypatch.setattr(anchors.AnchorStore, "add_clip", boom)
    chat = _chat(app)
    fakes.script = [[{"slot": 1, "start": 0.0, "end": 3.0}], []]
    _live(chat, "t1", 3.0)
    _msg(chat, "t1", UNNAMED)
    _stale(monkeypatch)
    _live(chat, "t2", 1.0)
    assert _wait_for(lambda: _end_row("s1"))["filled"] == 1
