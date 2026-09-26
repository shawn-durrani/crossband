"""The calibrated voice scorer and the readiness test (#482 stage 2).

What these tests pin, in order:

1. OFF BY DEFAULT. The setting ships off, and off means no worker thread,
   no model fetch and no embedding, and the people route says "off".
2. THE PINNED FETCH. ERes2Net is pinned to the sherpa-onnx release's own
   checksum, arrives through voiceid.fetch_verified, is refused when the
   bytes don't match, and is fetched only while the setting is on.
3. THE MATHS. The top-3 score leaves a piece's group out of its own bank
   only; days, clip families and harvested short clips (bytes first,
   then banking time) decide what leaves together; the prior is 1 in N+1.
4. THE FIT ON A KNOWN HOUSEHOLD. A synthetic household where the right
   answer is known: fresh speech names the right person, more speech
   is surer, and a voice whose bank is hidden (or never existed) comes
   out as someone new, never as someone else.
5. READINESS. Pass and fail cases, the 20-piece minimum, one day never
   being enough, seconds counting speech only, and quarantined clips
   counting for nothing.
6. THE CACHE AND THE WORKER. A refit embeds only new clips and fits in
   well under a second; builds happen on the worker thread only, at
   startup and after a bank change; every live-model embedding waits for
   a live check in flight and holds the live lock for one embedding.
7. LIVE NAMING UNCHANGED. The same turns get the same labels with the
   scorer on as off, and a build never writes to the anchor store.
8. THE ROUTE. /api/voice/people carries each person's readiness and the
   test's state beside the unchanged `sufficient`.

Keyless and offline. Voices are synthetic tones: each person has a pitch
and each capture day a faint room tone, and the fake speaker models read
both back, add noise that shrinks with the seconds of audio, and return
a fingerprint. Synthetic roster only (Alex, Sam, Dave, Mateo).
"""

import asyncio
import datetime
import hashlib
import json
import math
import os
import threading
import time
from pathlib import Path

import numpy as np
import pytest
from fastapi.testclient import TestClient

from backend import anchors, db, diarize, voice_calibration as vc, voiceid
from backend.app import create_app
from backend.config import Settings
from roomkit import _insert_user_message

SR = 16000
ON = {"voice_id_enabled": True, "voice_calibrated_scorer": True,
      "user_name": "Alex", "room_roster_max": 6}
OFF = dict(ON, voice_calibrated_scorer=False)

# ---- the synthetic household ------------------------------------------------
PITCH = {"Alex": 110.0, "Sam": 170.0, "Dave": 230.0, "Mateo": 290.0}
ROOM_HZ = (960.0, 1010.0, 1060.0, 1110.0, 1160.0)
DIM = 24
_rng = np.random.default_rng(482)
BASE = {m: {n: _rng.standard_normal(DIM) for n in PITCH} for m in vc.MODELS}
ROOM = {m: [_rng.standard_normal(DIM) for _ in ROOM_HZ] for m in vc.MODELS}
for _m in vc.MODELS:
    BASE[_m] = {n: v / np.linalg.norm(v) for n, v in BASE[_m].items()}
    ROOM[_m] = [v / np.linalg.norm(v) for v in ROOM[_m]]


def voice(name, seconds, day=0, take=0, pause=None):
    """PCM-16 of one synthetic person: harmonics of their pitch, which the
    speech gate hears as a voice, plus a faint tone for the day's room.
    `take` shifts the phase so two clips never share bytes. `pause` is
    (at seconds, length) of silence inserted."""
    t = np.arange(int(seconds * SR)) / SR
    f0, ph = PITCH[name], 0.37 * take
    x = (5200 * np.sin(2 * np.pi * f0 * t + ph)
         + 2600 * np.sin(2 * np.pi * 2 * f0 * t + ph)
         + 1300 * np.sin(2 * np.pi * 3 * f0 * t)
         + 700 * np.sin(2 * np.pi * ROOM_HZ[day] * t))
    if pause:
        at, length = pause
        x = np.concatenate([x[:int(at * SR)], np.zeros(int(length * SR)),
                            x[int(at * SR):]])
    return x.astype("<i2").tobytes()


def _peak(x, lo, hi):
    spec = np.abs(np.fft.rfft(x))
    freqs = np.fft.rfftfreq(len(x), 1 / SR)
    band = (freqs >= lo) & (freqs <= hi)
    return freqs[band][int(np.argmax(spec[band]))]


def fake_embed_factory(calls=None, like=None, noise=1.0, threads=None):
    """A stand-in for voice_calibration.embed: reads the person from the
    pitch and the day from the room tone, and returns their base vector
    plus a room offset plus noise that shrinks with the seconds heard.
    `like` = {name: (other, c)} makes one voice sound like another."""
    like = like or {}

    def fake(model, pcm, sample_rate, cfg):
        if calls is not None:
            calls.append((model, len(pcm)))
        if threads is not None:
            threads.add(threading.current_thread().name)
        x = np.frombuffer(pcm[:len(pcm) // 2 * 2], "<i2").astype(float)
        if len(x) < 800:
            return None
        who = min(PITCH, key=lambda n: abs(PITCH[n] - _peak(x, 80, 400)))
        rhz = _peak(x, 940, 1180)
        day = min(range(len(ROOM_HZ)), key=lambda d: abs(ROOM_HZ[d] - rhz))
        base = BASE[model][who]
        if who in like:
            other, c = like[who]
            base = c * BASE[model][other] + math.sqrt(1 - c * c) * base
        seed = int.from_bytes(hashlib.sha256(model.encode() + pcm)
                              .digest()[:8], "little")
        rng = np.random.default_rng(seed)
        v = (base + 0.35 * ROOM[model][day]
             + noise / math.sqrt(len(x) / SR) * rng.standard_normal(DIM)
             / math.sqrt(DIM))
        return list(v / np.linalg.norm(v))
    return fake


def _day0():
    """Local noon, twenty days ago: clips on day d land on one calendar
    date whatever the time zone."""
    d = datetime.date.today() - datetime.timedelta(days=20)
    return datetime.datetime.combine(d, datetime.time(12)).timestamp()


D0 = _day0()


def bank(store, name, clips, source="accumulated"):
    """Store (seconds, day) clips for a synthetic person; returns their id."""
    pid = store.ensure_person(name)
    for k, clip in enumerate(clips):
        seconds, day = clip[:2]
        pause = clip[2] if len(clip) > 2 else None
        assert store.add_clip(pid, voice(name, seconds, day, take=k, pause=pause),
                              SR, source=source,
                              added_at=D0 + day * 86400 + k * 60)
    return pid


HOUSEHOLD = {
    "Alex": [(8, 0), (7, 0), (6, 1), (9, 1), (5, 2)],     # three days
    "Sam": [(9, 0), (8, 0), (7, 1), (6, 1)],              # two days
    "Dave": [(9, 0), (8, 0), (7, 0)],                     # one day only
    "Mateo": [(3, 0), (3, 1)],                            # a little speech
}


@pytest.fixture
def store(tmp_path, monkeypatch):
    monkeypatch.setattr(db, "DATA_DIR", str(tmp_path))
    anchors._store = None
    return anchors.store()


@pytest.fixture
def household(store):
    return {name: bank(store, name, clips) for name, clips in HOUSEHOLD.items()}


@pytest.fixture
def fake(monkeypatch):
    calls = []
    monkeypatch.setattr(vc, "embed", fake_embed_factory(calls))
    monkeypatch.setattr(vc, "models_state", lambda cfg: "ready")
    return calls


def _fingerprints(name, seconds, day, take=9, like=None):
    f = fake_embed_factory(like=like)
    pcm = voice(name, seconds, day, take=take)
    return {m: f(m, pcm, SR, ON) for m in vc.MODELS}


def _wait(pred, timeout=10.0):
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        if pred():
            return True
        time.sleep(0.02)
    return False


def _calibration_threads():
    return [t for t in threading.enumerate() if t.name == "voice-calibration"]


# ── 1. off by default ───────────────────────────────────────────────────────

def test_the_setting_ships_off_and_off_does_nothing(tmp_path, monkeypatch):
    assert Settings().voice_calibrated_scorer is False
    assert vc.enabled(Settings().as_cfg()) is False
    assert vc.enabled(dict(ON, voice_id_enabled=False)) is False
    for name in ("embed", "build", "_spawn_eres2net_fetch", "models_state"):
        monkeypatch.setattr(vc, name, lambda *a, **k: pytest.fail(
            "the scorer must do nothing while off"))
    settings = Settings(data_dir=str(tmp_path / "data"),
                        memory_url="http://127.0.0.1:1")
    with TestClient(create_app(settings), base_url="http://127.0.0.1") as c:
        assert _calibration_threads() == []
        body = c.get("/api/voice/people").json()
    assert body["readiness"]["state"] == "off"
    assert vc.start(settings.as_cfg()) is False
    assert vc._eres2net_extractor(settings.as_cfg()) is None
    assert not vc.eres2net_path().exists()
    assert vc.readiness(settings.as_cfg()) == {}


# ── 2. the pinned fetch ─────────────────────────────────────────────────────

def test_eres2net_is_pinned_to_the_release_checksum():
    spec = vc.ERES2NET
    assert spec["url"] == ("https://github.com/k2-fsa/sherpa-onnx/releases/"
                           "download/speaker-recongition-models/" + spec["file"])
    assert spec["file"] == "3dspeaker_speech_eres2net_sv_en_voxceleb_16k.onnx"
    # the release's checksum.txt entry for this file
    assert spec["sha256"] == ("c59158379255ad66e161679cca6af8d52d51e389e3224ab7"
                              "d7a7baae295c2db5")
    assert spec["file"] != voiceid.MODEL_FILENAME


def test_the_fetch_starts_once_and_only_with_the_setting_on(monkeypatch):
    started = []
    monkeypatch.setattr(vc, "_spawn_eres2net_fetch",
                        lambda cfg: started.append(cfg))
    monkeypatch.setattr(voiceid, "sherpa_onnx", object())
    assert vc._eres2net_extractor(OFF) is None
    assert started == []
    assert vc._eres2net_extractor(ON) is None      # claims the fetch
    assert vc._eres2net_extractor(ON) is None      # still fetching, once
    assert len(started) == 1


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


def test_eres2net_arrives_through_the_verified_fetch(tmp_path, monkeypatch):
    monkeypatch.setattr(db, "DATA_DIR", str(tmp_path))
    payload = b"eres2net-model-bytes" * 64
    spec = dict(vc.ERES2NET, sha256=hashlib.sha256(payload).hexdigest())
    monkeypatch.setattr(vc, "ERES2NET", spec)
    import httpx
    urls = []

    def stream(method, url, **k):
        urls.append(url)
        return _FakeStream(payload)
    monkeypatch.setattr(httpx, "stream", stream)
    via = []
    real = voiceid.fetch_verified
    monkeypatch.setattr(voiceid, "fetch_verified",
                        lambda url, sha, path: via.append((url, sha)) or
                        real(url, sha, path))

    class FakeSherpa:
        class SpeakerEmbeddingExtractorConfig:
            def __init__(self, **k):
                self.kw = k

        class SpeakerEmbeddingExtractor:
            def __init__(self, config):
                self.config = config
    monkeypatch.setattr(voiceid, "sherpa_onnx", FakeSherpa)
    vc._warm_eres2net()
    path = tmp_path / voiceid.MODELS_DIR_NAME / spec["file"]
    assert path.read_bytes() == payload
    assert oct(os.stat(path).st_mode)[-3:] == "600"
    assert urls == [spec["url"]] and via == [(spec["url"], spec["sha256"])]
    assert vc._eres["state"] == "ready"
    # a download that doesn't match the pin is refused
    vc._reset_for_tests()
    path.unlink()
    monkeypatch.setattr(httpx, "stream", lambda *a, **k: _FakeStream(b"bad"))
    vc._warm_eres2net()
    assert not path.exists()
    assert vc._eres["state"] == "unavailable"


# ── 3. the maths ────────────────────────────────────────────────────────────

def test_top_three_scores_leave_only_the_own_group_out():
    q = np.array([[1.0, 0.0]])
    # three of person 0's clips: two in the query's group, one outside it
    clips = np.array([[1.0, 0.0], [1.0, 0.0], [0.6, 0.8], [0.0, 1.0]])
    person = np.array([0, 0, 0, 1])
    code = np.array([7, 7, 8, 9])
    whole = vc.bank_scores(q, clips, person, 2)
    assert whole[0, 0] == pytest.approx((1 + 1 + 0.6) / 3)
    out = vc.bank_scores(q, clips, person, 2, np.array([7]), code)
    assert out[0, 0] == pytest.approx(0.6)          # its group left out
    assert out[0, 1] == pytest.approx(0.0)          # someone else's, whole
    gone = vc.bank_scores(q, clips[:2], person[:2], 1, np.array([7]),
                          code[:2])
    assert np.isnan(gone[0, 0])                     # nothing left to score


def test_days_families_and_harvested_clips_leave_together():
    day = 86400.0
    long_pcm = bytes(range(256)) * 40
    clips = [
        # Alex, two days: the whole day leaves
        {"pid": "a", "file": "a1", "added_at": D0, "source": "accumulated",
         "pcm": long_pcm},
        # a slice of a1's bytes, stamped after midnight (two in the
        # morning, so a clock change that night can't move it back)
        {"pid": "a", "file": "a2", "added_at": D0 + 14 * 3600,
         "source": "harvested-short", "pcm": long_pcm[512:1536]},
        {"pid": "a", "file": "a3", "added_at": D0 + day,
         "source": "accumulated", "pcm": b"\x01\x02" * 900},
        # Dave, one day: the clip leaves, with the short clip cut from it
        {"pid": "d", "file": "d1", "added_at": D0, "source": "accumulated",
         "pcm": b"\x05\x06" * 3000},
        {"pid": "d", "file": "d2", "added_at": D0 + 0.4,
         "source": "harvested-short", "pcm": b"\x07\x08" * 800},  # trimmed
        {"pid": "d", "file": "d3", "added_at": D0 + 600,
         "source": "harvested-short", "pcm": b"\x09\x0a" * 800},  # orphan
        {"pid": "d", "file": "d4", "added_at": D0 + 900,
         "source": "introduction", "pcm": b"\x0b\x0c" * 3000},
    ]
    assert vc.harvest_parents(clips) == {1: 0, 4: 3}
    units = vc.leave_out_units(clips)
    days = [u["day"] for u in units]
    assert days[1] == days[0] != days[2]        # a2 takes a1's day
    assert units[0]["group"] == units[1]["group"] == ("a", "day", days[0])
    assert units[2]["group"] == ("a", "day", days[2])
    assert units[0]["unit"] == ("a", days[0])
    assert units[3]["group"] == units[4]["group"] == ("d", "clip", "d1")
    assert units[5]["group"] == ("d", "clip", "d3")
    assert units[6]["group"] == ("d", "clip", "d4")
    assert {u["unit"] for u in units[3:]} == {("d", days[3])}


def test_the_prior_is_one_in_n_plus_one():
    b = np.zeros(4)                     # no evidence either way
    for n in (1, 2, 3, 6):
        p = vc.apply_calibration(b, [0.7], 2.0, n)
        assert p[0] == pytest.approx(1 / (n + 1))
    b = np.array([-5.0, 12.0, 0.0, 0.0])
    fewer = vc.apply_calibration(b, [0.6], 2.0, 2)[0]
    more = vc.apply_calibration(b, [0.6], 2.0, 6)[0]
    assert more < fewer                 # the same evidence, more people
    assert np.isnan(vc.apply_calibration(b, [np.nan], 2.0, 2)[0])


def test_the_fit_needs_both_kinds_of_row():
    assert vc.fit_calibration([0.5, 0.6], [2, 2], [1, 1]) is None
    assert vc.fit_calibration([0.5, 0.6], [2, 2], [0, 0]) is None
    b = vc.fit_calibration([0.9, 0.8, 0.2, 0.1], [2, 2, 2, 2], [1, 1, 0, 0])
    assert b is not None and b[1] > 0


# ── 4. the fit on a known household ────────────────────────────────────────

def test_the_fit_names_fresh_speech_right(household, fake):
    snap = vc.build(ON)
    assert snap["calibrated"] and snap["people"] == 4
    assert snap["calibration"][1] > 0   # a higher score, a likelier person
    by_pid = {pid: name for name, pid in household.items()}
    for name in ("Alex", "Sam", "Dave", "Mateo"):
        # a new day, a new room: nothing in any bank was recorded there
        probs = vc.probability(_fingerprints(name, 4, day=4), 4, snap)
        assert set(probs) == set(by_pid)
        best = max(probs, key=probs.get)
        assert by_pid[best] == name and probs[best] >= vc.NAME_BAR
        assert all(p < vc.NAME_BAR for pid, p in probs.items() if pid != best)


def test_more_speech_is_surer(household, monkeypatch):
    monkeypatch.setattr(vc, "embed", fake_embed_factory(noise=3.0))
    snap = vc.build(ON)
    alex = household["Alex"]
    short = [vc.probability(_fingerprints("Alex", 1, day=4, take=t), 1,
                            snap)[alex] for t in range(8)]
    long_ = [vc.probability(_fingerprints("Alex", 8, day=4, take=t), 8,
                            snap)[alex] for t in range(8)]
    assert np.mean(long_) > np.mean(short)


def test_a_voice_with_no_bank_comes_out_new(store, fake):
    for name in ("Alex", "Sam", "Dave"):
        bank(store, name, HOUSEHOLD[name])
    snap = vc.build(ON)
    for seconds in (2, 4, 8):
        probs = vc.probability(_fingerprints("Mateo", seconds, day=4),
                               seconds, snap)
        assert len(probs) == 3
        assert all(p < vc.NEW_BAR for p in probs.values()), probs
    # the build's own check: every person's bank hidden in turn
    assert snap["unknown"]["pieces"] > 0
    assert snap["unknown"]["named"] == 0
    assert snap["unknown"]["new"] == snap["unknown"]["pieces"]


def test_nothing_to_calibrate_without_two_people(store, fake):
    alex = bank(store, "Alex", HOUSEHOLD["Alex"])
    snap = vc.build(ON)
    assert not snap["calibrated"]
    assert vc.probability(_fingerprints("Alex", 4, day=4), 4, snap) is None
    r = snap["readiness"][alex]
    assert r["ready"] is False and r["reason"] == "no_calibration"


# ── 5. readiness ────────────────────────────────────────────────────────────

def test_readiness_on_a_known_household(household, fake):
    ready = vc.build(ON)["readiness"]
    alex = ready[household["Alex"]]
    assert alex["ready"] and alex["reason"] == "ready"
    assert alex["share_right"] == 1.0 and alex["named_as_other"] == 0
    assert alex["pieces"] >= vc.READY_MIN_PIECES and alex["days"] == 3
    # plenty of speech from one day: nothing left to name them by
    dave = ready[household["Dave"]]
    assert dave["reason"] == "one_day" and dave["pieces"] >= 20
    assert dave["named_right"] == 0 and dave["named_as_other"] == 0
    mateo = ready[household["Mateo"]]
    assert mateo["reason"] == "too_few_pieces" and mateo["pieces"] == 4
    assert set(alex) == {"ready", "reason", "pieces", "named_right",
                         "share_right", "named_as_other", "days",
                         "speech_seconds"}


def test_a_voice_named_as_someone_else_is_never_ready(store, fake):
    """A turn of Sam's banked under Alex and never set aside: its pieces
    are named as Sam, so Alex's bank isn't ready, however well the rest
    of it names him."""
    alex = bank(store, "Alex", HOUSEHOLD["Alex"])
    bank(store, "Sam", HOUSEHOLD["Sam"])
    bank(store, "Dave", [(9, 0), (8, 1), (7, 2)])
    assert store.add_clip(alex, voice("Sam", 6, 1, take=40), SR,
                          "accumulated", added_at=D0 + 86400 + 3600)
    r = vc.build(ON)["readiness"][alex]
    assert r["pieces"] >= vc.READY_MIN_PIECES
    assert r["named_as_other"] >= 1
    assert r["ready"] is False and r["reason"] == "named_as_other"


def test_share_below_the_bar_is_not_ready():
    assert vc.verdict(40, 37, 0, 2, True) == (False, "share_below")
    assert vc.verdict(40, 38, 0, 2, True) == (True, "ready")
    assert vc.verdict(40, 40, 1, 2, True) == (False, "named_as_other")
    assert vc.verdict(19, 19, 0, 3, True) == (False, "too_few_pieces")
    assert vc.verdict(20, 20, 0, 1, True) == (False, "one_day")
    assert vc.verdict(20, 20, 0, 2, False) == (False, "no_calibration")


@pytest.mark.parametrize("last_clip, pieces, ready", [(2.0, 19, False),
                                                      (3.0, 20, True)])
def test_twenty_pieces_is_the_minimum(store, fake, last_clip, pieces, ready):
    for name in ("Alex", "Sam"):
        bank(store, name, HOUSEHOLD[name])
    # 9 + 9 two-second pieces on day 0, and 1 or 2 more on day 1
    mateo = bank(store, "Mateo", [(10, 0), (10, 0), (last_clip, 1)])
    r = vc.build(ON)["readiness"][mateo]
    assert r["pieces"] == pieces
    assert r["ready"] is ready
    assert r["reason"] == ("ready" if ready else "too_few_pieces")


def test_seconds_count_speech_only(store, fake):
    bank(store, "Sam", HOUSEHOLD["Sam"])
    alex = bank(store, "Alex", [(4, 0, (2.0, 2.5)), (3, 1)])
    clips = anchors.store().calibration_clips(SR)[alex]["clips"]
    speech = sum(sum(hi - lo for lo, hi in voiceid.speech_spans(c["pcm"], SR))
                 for c in clips) / SR
    recorded = sum(len(c["pcm"]) for c in clips) / 2 / SR
    r = vc.build(ON)["readiness"][alex]
    assert r["speech_seconds"] == pytest.approx(speech, abs=0.05)
    assert r["speech_seconds"] < recorded - 1.5
    # pieces are cut from the speech with the pause taken out: about 4.6 s
    # of it (the pads kept around each stretch) gives three 2 s pieces,
    # and the 3 s clip gives two
    assert r["pieces"] == 5


def test_quarantined_clips_count_for_nothing(store, fake):
    bank(store, "Sam", HOUSEHOLD["Sam"])
    alex = bank(store, "Alex", HOUSEHOLD["Alex"])
    before = vc.build(ON)
    # Sam's voice filed under Alex, then set aside by the hygiene audit
    s = anchors.store()
    assert s.add_clip(alex, voice("Sam", 9, 3, take=5), SR, "accumulated",
                      added_at=D0 + 3 * 86400)
    bad = [c["file"] for c in s.clips_of(alex) if c["added_at"] >= D0 + 3 * 86400]
    s.set_hygiene({alex: bad}, [])
    after = vc.build(ON)
    assert len(after["banks"][alex][vc.SMALL]) == len(HOUSEHOLD["Alex"])
    assert after["readiness"][alex] == before["readiness"][alex]
    assert after["clips"] == before["clips"]
    s.set_hygiene({}, [])                           # reinstated: it counts
    back = vc.build(ON)
    assert back["clips"] == before["clips"] + 1
    assert back["readiness"][alex]["speech_seconds"] > \
        before["readiness"][alex]["speech_seconds"]


# ── 6. the cache and the worker ─────────────────────────────────────────────

def test_a_refit_embeds_only_new_clips(household, fake):
    first = vc.build(ON)
    assert len(fake) == first["embedded"] > 0
    keys = set(vc._cache)
    fake.clear()
    again = vc.build(ON)
    assert fake == [] and again["embedded"] == 0
    assert again["readiness"] == first["readiness"]
    assert again["ms"]["fit"] < 1000                # well under a second
    sam = household["Sam"]
    anchors.store().add_clip(sam, voice("Sam", 6, 2, take=7), SR,
                             "accumulated", added_at=D0 + 2 * 86400)
    third = vc.build(ON)
    new = [e for k, e in vc._cache.items() if k not in keys]
    assert len(new) == 1
    # the new clip only: its whole fingerprint and each piece, per model
    assert len(fake) == third["embedded"] == 2 * (1 + len(new[0]["pieces"]))
    assert third["ms"]["fit"] < 1000
    # a clip that leaves every bank leaves the cache too
    anchors.store().forget(household["Mateo"])
    vc.build(ON)
    assert len(vc._cache) == third["clips"] - len(HOUSEHOLD["Mateo"])


def test_a_clip_that_fails_to_embed_is_retried(household, monkeypatch):
    works = fake_embed_factory()

    def flaky(model, pcm, sample_rate, cfg):
        x = np.frombuffer(pcm[:len(pcm) // 2 * 2], "<i2").astype(float)
        if model == vc.ERES and abs(_peak(x, 80, 400) - PITCH["Mateo"]) < 5:
            return None                             # a model hiccup
        return works(model, pcm, sample_rate, cfg)
    monkeypatch.setattr(vc, "embed", flaky)
    monkeypatch.setattr(vc, "models_state", lambda cfg: "ready")
    vc._build_once(ON, threading.Event())
    first = vc.current()
    assert first["missed"] == len(HOUSEHOLD["Mateo"])
    assert household["Mateo"] not in first["readiness"]
    monkeypatch.setattr(vc, "embed", works)
    vc._build_once(ON, threading.Event())           # same bank, rebuilt
    again = vc.current()
    assert again is not first and again["missed"] == 0
    assert household["Mateo"] in again["readiness"]
    vc._build_once(ON, threading.Event())           # now nothing to do
    assert vc.current() is again


def test_builds_run_on_the_worker_thread_only(household, monkeypatch):
    threads, calls = set(), []
    monkeypatch.setattr(vc, "embed", fake_embed_factory(calls,
                                                        threads=threads))
    monkeypatch.setattr(vc, "models_state", lambda cfg: "ready")
    monkeypatch.setattr(vc, "DEBOUNCE_S", 0.01)
    assert vc.start(ON) is True                     # the startup build
    assert _wait(lambda: vc.current() is not None
                 and vc.status(ON)["state"] == "ready")
    first, made = vc.current(), len(calls)
    # reading embeds and fits nothing
    assert vc.readiness(ON)[household["Alex"]]["ready"] is True
    assert vc.status(ON)["people"] == 4
    assert len(calls) == made
    # a bank change asks the worker for a build
    anchors.store().add_clip(household["Sam"], voice("Sam", 6, 2, take=8),
                             SR, "accumulated", added_at=D0 + 2 * 86400)
    assert _wait(lambda: vc.current() is not first
                 and vc.status(ON)["state"] == "ready")
    assert vc.current()["clips"] == first["clips"] + 1
    assert len(calls) > made
    assert threads == {"voice-calibration"}
    vc.stop()
    assert _calibration_threads() == []


def test_the_worker_waits_for_the_models_and_says_so(household, monkeypatch):
    state = {"now": "waiting"}
    monkeypatch.setattr(vc, "models_state", lambda cfg: state["now"])
    monkeypatch.setattr(vc, "embed", fake_embed_factory())
    monkeypatch.setattr(vc, "DEBOUNCE_S", 0.01)
    monkeypatch.setattr(vc, "MODEL_POLL_S", 0.01)
    vc.start(ON)
    assert _wait(lambda: vc.status(ON)["state"] == "waiting")
    assert vc.current() is None and vc.readiness(ON) == {}
    state["now"] = "ready"
    assert _wait(lambda: vc.status(ON)["state"] == "ready")
    vc.stop()
    vc._reset_for_tests()
    state["now"] = "unavailable"
    vc.start(ON)
    assert _wait(lambda: vc.status(ON)["state"] == "unavailable")
    assert vc.current() is None


def test_a_live_model_embedding_waits_for_the_live_check(monkeypatch):
    """Each TitaNet-Small embedding first waits, boundedly, while a live
    identity pass is in flight, without holding the live lock, and then
    holds the lock for that one embedding only."""
    seen = []

    class Extractor:
        def create_stream(self):
            class S:
                def accept_waveform(self, sample_rate, waveform):
                    pass

                def input_finished(self):
                    pass
            return S()

        def compute(self, stream):
            seen.append(("compute", voiceid._embed_lock.locked()))
            return [1.0, 0.0, 0.0]

    monkeypatch.setattr(voiceid, "_get_extractor", lambda cfg: Extractor())
    monkeypatch.setattr(vc, "QUIET_WAIT_MAX_S", 0.2)

    def sleep(s):
        seen.append(("wait", voiceid._embed_lock.locked()))
        time.sleep(s)
    monkeypatch.setattr(vc, "_sleep", sleep)
    diarize._TASKS.add("a live pass")
    t0 = time.monotonic()
    vec = vc.embed(vc.SMALL, voice("Alex", 1, 0), SR, ON)
    assert time.monotonic() - t0 >= 0.2
    assert vec == [1.0, 0.0, 0.0]
    waits = [locked for kind, locked in seen if kind == "wait"]
    assert waits and not any(waits)                 # the lock stayed free
    assert [s for s in seen if s[0] == "compute"] == [("compute", True)]
    assert not voiceid._embed_lock.locked()         # and was let go
    diarize._TASKS.clear()
    seen.clear()
    vc.embed(vc.SMALL, voice("Alex", 1, 0), SR, ON)
    assert seen == [("compute", True)]              # no live pass, no wait


def test_a_bank_change_listener_never_reaches_the_store(store):
    def broken():
        raise RuntimeError("listener failure")
    kicked = []
    anchors.add_change_listener(broken)
    anchors.add_change_listener(lambda: kicked.append(1))
    try:
        pid = store.ensure_person("Sam")
        assert store.add_clip(pid, voice("Sam", 3, 0), SR, "accumulated")
    finally:
        anchors._change_listeners.clear()
    assert len(kicked) >= 2


# ── 7. live naming unchanged ────────────────────────────────────────────────

def _live_embed(_ex, audio, sr):
    """The live matcher's fake: reads the pitch, one axis per person."""
    x = np.asarray(audio, dtype=float) * 32768
    who = min(PITCH, key=lambda n: abs(PITCH[n] - _peak(x, 80, 400)))
    vec = [0.0] * len(PITCH)
    vec[list(PITCH).index(who)] = 1.0
    return vec


def _armed_chat(client, roster):
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
    async def drive():
        await diarize.run_pass(chat_id, pcm, SR, time.time(),
                               diarize.RoomSession(enabled=True), cfg,
                               turn_id=turn_id)
    asyncio.run(drive())


def test_the_live_label_is_the_same_with_the_scorer_on(tmp_path, monkeypatch):
    """The real live matcher (its extractor faked) names the same turns
    with the scorer stopped and with it running, mid-build, beside it."""
    monkeypatch.setattr(voiceid, "_get_extractor", lambda cfg: object())
    monkeypatch.setattr(voiceid, "_embed", _live_embed)
    monkeypatch.setattr("backend.voice.transcribe_diarized",
                        lambda *a, **k: pytest.fail("no EL call expected"))
    monkeypatch.setattr("backend.mismatch.schedule_check",
                        lambda *a, **k: None)
    monkeypatch.setattr(vc, "embed", fake_embed_factory())
    monkeypatch.setattr(vc, "models_state", lambda cfg: "ready")
    monkeypatch.setattr(vc, "DEBOUNCE_S", 0.01)
    settings = Settings(data_dir=str(tmp_path / "data"),
                        memory_url="http://127.0.0.1:1", user_name="Alex")
    with TestClient(create_app(settings), base_url="http://127.0.0.1") as c:
        store = anchors.store()
        roster = [(n, bank(store, n, HOUSEHOLD[n], source="introduction"))
                  for n in ("Alex", "Sam")]
        chat = _armed_chat(c, roster)
        cands = diarize.remembered_candidates()
        turns = {"Alex": voice("Alex", 3, 2, take=21),
                 "Sam": voice("Sam", 3, 2, take=22)}
        labels, verdicts = {}, {}
        for mode, cfg in (("off", OFF), ("on", ON)):
            if mode == "on":
                assert vc.start(ON)
                assert _wait(lambda: vc.current() is not None)
            else:
                assert _calibration_threads() == []
            for name, pcm in turns.items():
                tid = f"{mode}-{name}"
                msg = _insert_user_message(chat["id"], voice_turn_id=tid)
                if mode == "on":
                    vc._worker["kick"].set()        # a build in flight too
                _turn(chat["id"], pcm, tid, cfg)
                labels[tid] = _labels(msg["id"])
                verdicts[tid] = voiceid.identify_utterance(pcm, SR, cands,
                                                           cfg)
        for name in turns:
            assert json.loads(labels[f"off-{name}"])["labels"] == [name]
            assert labels[f"on-{name}"] == labels[f"off-{name}"]
            assert verdicts[f"on-{name}"] == verdicts[f"off-{name}"]
        assert vc.readiness(ON)
    assert _calibration_threads() == []             # stopped with the app


def test_a_build_never_writes_to_the_anchor_store(household, fake,
                                                   monkeypatch):
    store = anchors.store()
    for name in ("_save", "_write_clip", "_delete_file", "add_clip",
                 "ensure_person", "set_hygiene", "delete_clip", "forget",
                 "move_clip", "merge_people", "retract_utterance_clips",
                 "_record_refusal"):
        monkeypatch.setattr(store, name, lambda *a, **k: pytest.fail(
            "the scorer wrote to the anchor store"))
    snap = vc.build(ON)
    assert snap["people"] == 4


# ── 8. the route ────────────────────────────────────────────────────────────

def test_the_people_route_carries_readiness(tmp_path, monkeypatch):
    threads = set()
    monkeypatch.setattr(vc, "embed", fake_embed_factory(threads=threads))
    monkeypatch.setattr(vc, "models_state", lambda cfg: "ready")
    monkeypatch.setattr(vc, "DEBOUNCE_S", 0.01)
    settings = Settings(data_dir=str(tmp_path / "data"),
                        memory_url="http://127.0.0.1:1",
                        voice_calibrated_scorer=True)
    with TestClient(create_app(settings), base_url="http://127.0.0.1") as c:
        store = anchors.store()
        pids = {n: bank(store, n, clips) for n, clips in HOUSEHOLD.items()}
        vc._worker["kick"].set()
        assert _wait(lambda: vc.current() is not None
                     and vc.current()["people"] == 4)
        body = c.get("/api/voice/people").json()
    assert threads == {"voice-calibration"}         # the route built nothing
    by = {p["person_id"]: p for p in body["people"]}
    alex, dave = by[pids["Alex"]], by[pids["Dave"]]
    assert alex["readiness"]["ready"] is True
    assert alex["readiness"]["pieces"] >= 20
    assert alex["readiness"]["share_right"] == 1.0
    assert alex["readiness"]["named_as_other"] == 0
    assert alex["readiness"]["speech_seconds"] > 0
    assert dave["readiness"]["reason"] == "one_day"
    assert alex["sufficient"] is True               # the old bar, unchanged
    summary = body["readiness"]
    assert summary["state"] == "ready"
    assert summary["min_pieces"] == 20 and summary["share"] == 0.95
    assert summary["bar"] == 0.9 and summary["piece_seconds"] == 2
    assert summary["unknown"]["named"] == 0
    # content-free: no name reaches the readiness half of the answer
    raw = json.dumps(summary) + json.dumps([p["readiness"]
                                            for p in body["people"]])
    for name in PITCH:
        assert name not in raw


# ── the real model (skips without the file) ────────────────────────────────

@pytest.mark.skipif(voiceid.sherpa_onnx is None,
                    reason="sherpa-onnx wheel not installed")
def test_integration_real_eres2net_embeds():
    path = os.environ.get("CROSSBAND_TEST_ERES2NET_PATH")
    if not path or not os.path.exists(path):
        pytest.skip("ERes2Net not present (fetched at runtime, absent in CI)")
    assert voiceid._sha256_file(Path(path)) == vc.ERES2NET["sha256"]
    so = voiceid.sherpa_onnx
    ex = so.SpeakerEmbeddingExtractor(so.SpeakerEmbeddingExtractorConfig(
        model=path, num_threads=1, provider="cpu"))
    old = dict(vc._eres)
    vc._eres.update(state="ready", ex=ex)
    try:
        emb = vc.embed(vc.ERES, voice("Alex", 2, 0), SR, ON)
    finally:
        vc._eres.update(old)
    assert emb is not None and len(emb) == 192
    assert abs(sum(x * x for x in emb) - 1.0) < 1e-4
