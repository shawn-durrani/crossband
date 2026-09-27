"""Recording a voice on purpose (#504, item B of the #482 plan).

The Voices page records someone reading a passage aloud, about 30
seconds, and POSTs it as a 16 kHz mono WAV. The contract under test:

- The speech check trims and judges it, and it's cut at its pauses into
  clips of at most MAX_CLIP_SECONDS, each banked as an introduction:
  vouched, and protected from rotation.
- Non-speech, silence, too little speech, too much audio and the wrong
  format are refused with a plain reason, and nothing is stored.
- The person must exist, an AI participant's name is refused at this
  door too, and the owner can record themselves.
- Rotation and the protected rule apply as usual: recorded clips push
  out automated ones, never a clip a human stood behind, and later
  automated clips never push them out.
- Forget, the correction ledger and the membro sync treat the clips like
  any other.
- The answer carries the person's readiness, "checking" until the
  background build covers the new clips, and /readiness says when it has.
- Session-gated, and nothing logged names anyone.
"""

import logging
import struct

import pytest
from fastapi.testclient import TestClient

from backend import anchors, person_sync, voice_calibration as vc, \
    voice_recording
from backend.app import create_app
from backend.config import Settings
from backend.diarize import pcm16_wav
from tests.conftest import speech_pcm
from tests.test_person_sync import FakeMembro
from tests.test_room_anchors import noise_pcm, quiet_pcm
from tests.test_voice_calibration import (HOUSEHOLD, SR, _wait, bank,
                                          fake_embed_factory, voice)

SILENCE = b"\x00\x00"


@pytest.fixture
def app(tmp_path):
    return create_app(Settings(data_dir=str(tmp_path / "data"),
                               memory_url="http://127.0.0.1:1",
                               user_name="Sam"))


def reading(seconds, phrase=2.6, gap=0.6, rate=16000):
    """A synthetic reading: speech-shaped phrases with silent gaps between
    them, the way someone reads a passage aloud."""
    out = bytearray()
    while len(out) / 2 / rate < seconds:
        out += speech_pcm(phrase, rate)
        out += SILENCE * int(gap * rate)
    return bytes(out[:int(seconds * rate) * 2])


def post(c, pid, body):
    return c.post(f"/api/voice/people/{pid}/record", content=body,
                  headers={"Content-Type": "audio/wav"})


def wav(pcm, rate=16000):
    return pcm16_wav(pcm, rate)


# ── 1. the save ─────────────────────────────────────────────────────────────

def test_a_recording_is_banked_as_introduced_clips(app):
    with TestClient(app, base_url="http://127.0.0.1") as c:
        pid = c.post("/api/voice/people", json={"name": "Alex"}
                     ).json()["person_id"]
        r = post(c, pid, wav(reading(30)))
        assert r.status_code == 200, r.text
        body = r.json()
        store = anchors.store()
        clips = store.clips_of(pid)
        assert body["ok"] is True
        assert body["saved"] == len(clips) >= 3
        assert {c_["source"] for c_ in clips} == {"introduction"}
        assert all(c_["seconds"] <= anchors.MAX_CLIP_SECONDS for c_ in clips)
        assert body["seconds"] == pytest.approx(
            sum(c_["seconds"] for c_ in clips), abs=0.3)
        assert body["speech_seconds"] >= 20
        person = next(p for p in store.people() if p["person_id"] == pid)
        assert person["vouched"] is True and person["trust"] == "human"
        assert person["sufficient"] is True
        for c_ in clips:
            assert store.clip_path(pid, c_["file"]).is_file()
        # the scorer ships off, and the answer says so
        assert body["state"] == "off" and body["readiness"] is None
        assert body["summary"]["state"] == "off"


def test_clips_are_cut_in_the_pauses():
    pcm = reading(30)
    cuts = voice_recording.cut_points(pcm, 16000)
    assert len(cuts) >= 3
    cap = anchors.MAX_CLIP_SECONDS * 16000
    for (s0, e0), (s1, _) in zip(cuts, cuts[1:]):
        assert e0 <= s1                                  # never overlapping
    for start, end in cuts:
        assert end - start <= cap
        # every cut falls in a pause: the samples either side are silent
        for at in (start, end - 1):
            if 0 < at < len(pcm) // 2 - 1:
                assert pcm[at * 2:at * 2 + 2] == SILENCE
    # nothing that was speech is left out
    covered = sum(e - s for s, e in cuts)
    speech = sum(hi - lo for lo, hi in anchors_speech(pcm))
    assert covered >= speech


def anchors_speech(pcm):
    from backend import voiceid
    return voiceid.speech_spans(pcm, 16000, pad_seconds=0.0)


def test_a_stretch_with_no_pause_is_cut_evenly():
    pcm = speech_pcm(25)
    cuts = voice_recording.cut_points(pcm, 16000)
    assert len(cuts) == 3
    lengths = [(e - s) / 16000 for s, e in cuts]
    assert all(7.5 <= n <= anchors.MAX_CLIP_SECONDS for n in lengths)
    assert sum(lengths) == pytest.approx(25, abs=0.05)


def test_a_last_word_too_short_for_a_clip_is_left_out():
    pcm = speech_pcm(9) + SILENCE * 16000 * 2 + speech_pcm(0.4)
    cuts = voice_recording.cut_points(pcm, 16000)
    assert len(cuts) == 1
    assert (cuts[0][1] - cuts[0][0]) / 16000 < 10


# ── 2. refusals ─────────────────────────────────────────────────────────────

@pytest.mark.parametrize("pcm, words", [
    (noise_pcm(30), "didn't sound like someone speaking"),
    (quiet_pcm(30), "didn't hear anyone"),
    (SILENCE * 16000 * 30, "didn't hear anyone"),
    (reading(6), "seconds of speech. Read the whole passage"),
])
def test_what_isnt_a_reading_is_refused_and_nothing_is_stored(app, pcm,
                                                              words):
    with TestClient(app, base_url="http://127.0.0.1") as c:
        pid = anchors.store().ensure_person("Alex")
        r = post(c, pid, wav(pcm))
        assert r.status_code == 422
        assert words in r.json()["detail"]
        assert anchors.store().clips_of(pid) == []
        person = anchors.store().people()[0]
        # refused before the gate: not counted as the gate refusing clips
        assert person["refused_last_week"] == 0 and not person["vouched"]


def test_the_wrong_format_is_refused(app):
    with TestClient(app, base_url="http://127.0.0.1") as c:
        pid = anchors.store().ensure_person("Alex")
        eight_k = wav(reading(30, rate=8000), rate=8000)
        stereo = bytearray(wav(reading(15) * 2))
        struct.pack_into("<H", stereo, 22, 2)          # two channels
        for body in (eight_k, bytes(stereo), b"not a wav at all"):
            r = post(c, pid, body)
            assert r.status_code == 422
            assert "16 kHz mono" in r.json()["detail"]
        r = post(c, pid, b"")
        assert r.status_code == 422 and "Nothing was recorded" in \
            r.json()["detail"]
        assert anchors.store().clips_of(pid) == []


def test_over_a_minute_is_refused(app):
    with TestClient(app, base_url="http://127.0.0.1") as c:
        pid = anchors.store().ensure_person("Alex")
        r = post(c, pid, wav(reading(65)))
        assert r.status_code == 413
        assert "over a minute" in r.json()["detail"]
        assert anchors.store().clips_of(pid) == []
    with pytest.raises(voice_recording.Refused) as refused:
        voice_recording.check(reading(61))
    assert refused.value.reason == "too_long"


def test_an_unknown_person_is_404(app):
    with TestClient(app, base_url="http://127.0.0.1") as c:
        assert post(c, "nobody-000000", wav(reading(30))).status_code == 404
        assert c.get("/api/voice/people/nobody-000000/readiness"
                     ).status_code == 404


@pytest.mark.parametrize("name", ["Claude", "Clyde"])
def test_an_ai_participants_name_is_never_recorded(app, name):
    """The #77 boundary holds at this door too, spelt-by-ear variants
    included: a guard entry under an AI's name is never a person."""
    with TestClient(app, base_url="http://127.0.0.1") as c:
        pid = anchors.store().ensure_person(name)
        r = post(c, pid, wav(reading(30)))
        assert r.status_code == 400
        assert "AI participant" in r.json()["detail"]
        assert anchors.store().clips_of(pid) == []


def test_a_person_renamed_to_an_ais_name_is_refused_too(app):
    with TestClient(app, base_url="http://127.0.0.1") as c:
        store = anchors.store()
        pid = store.ensure_person("Dave")
        assert store.set_preferred_name(pid, "Claude")
        assert post(c, pid, wav(reading(30))).status_code == 400
        assert store.clips_of(pid) == []


def test_the_owner_can_record_themselves(app):
    with TestClient(app, base_url="http://127.0.0.1") as c:
        pid = anchors.store().ensure_person("Sam")     # the owner
        r = post(c, pid, wav(reading(30)))
        assert r.status_code == 200 and r.json()["saved"] >= 3
        assert len(anchors.store().people()) == 1      # no second owner


# ── 3. rotation and the protected rule ──────────────────────────────────────

def test_recorded_clips_push_out_automated_ones_never_a_human_one(app):
    with TestClient(app, base_url="http://127.0.0.1") as c:
        store = anchors.store()
        pid = store.ensure_person("Mateo")
        assert store.add_clip(pid, speech_pcm(4), 16000, source="correction")
        for k in range(anchors.KEEP_CLIPS):
            assert store.add_clip(pid, speech_pcm(3 + k * 0.1, amp=9000 + k),
                                  16000, source="accumulated")
        correction = [c_["file"] for c_ in store.clips_of(pid)
                      if c_["source"] == "correction"]
        long_before = [c_ for c_ in store.clips_of(pid)
                       if c_["seconds"] > anchors.SHORT_CLIP_MAX_SECONDS]
        assert len(long_before) == anchors.KEEP_CLIPS

        r = post(c, pid, wav(reading(30)))
        assert r.status_code == 200
        saved = r.json()["saved"]
        clips = store.clips_of(pid)
        long_after = [c_ for c_ in clips
                      if c_["seconds"] > anchors.SHORT_CLIP_MAX_SECONDS]
        assert len(long_after) == anchors.KEEP_CLIPS    # the bank's bound
        intro = [c_["file"] for c_ in clips if c_["source"] == "introduction"]
        assert len(intro) == saved >= 3
        assert correction[0] in {c_["file"] for c_ in clips}
        # later automated clips, louder and longer, never push them out
        for k in range(anchors.KEEP_CLIPS):
            store.add_clip(pid, speech_pcm(9.5, amp=12000 + k), 16000,
                           source="accumulated")
        kept = {c_["file"] for c_ in store.clips_of(pid)}
        assert set(intro) <= kept and correction[0] in kept


# ── 4. forget, the ledger and the sync ──────────────────────────────────────

def test_forget_and_delete_treat_recorded_clips_like_any_other(app):
    with TestClient(app, base_url="http://127.0.0.1") as c:
        store = anchors.store()
        pid = store.ensure_person("Dave")
        assert post(c, pid, wav(reading(30))).status_code == 200
        files = [c_["file"] for c_ in store.clips_of(pid)]
        paths = [store.clip_path(pid, f) for f in files]

        assert c.delete(f"/api/voice/people/{pid}/clips/{files[0]}"
                        ).status_code == 200
        assert [r["kind"] for r in store.pending_corrections()] == ["delete"]
        assert not paths[0].exists()

        assert c.delete(f"/api/voice/people/{pid}").status_code == 200
        assert store.people() == []
        assert not any(p.exists() for p in paths)


def test_the_sync_pushes_recorded_clips_like_any_other(app, monkeypatch):
    monkeypatch.setenv("MEMORY_AUTH_TOKEN", "test-token")
    person_sync._state.update({"last": 0.0, "warned": False})
    membro = FakeMembro()
    try:
        with TestClient(app, base_url="http://127.0.0.1") as c:
            pid = anchors.store().ensure_person("Alex")
            saved = post(c, pid, wav(reading(30))).json()["saved"]
            out = person_sync.sync_once(membro.url, force=True)
        assert out["pushed_people"] == 1 and out["pushed_clips"] == saved
        assert {a["source"] for a in membro.anchors[pid]} == {"introduction"}
    finally:
        membro.stop()


# ── 5. readiness straight after ─────────────────────────────────────────────

def test_the_answer_says_checking_until_the_build_covers_the_new_clips(
        tmp_path, monkeypatch):
    monkeypatch.setattr(vc, "embed", fake_embed_factory())
    monkeypatch.setattr(vc, "models_state", lambda cfg: "ready")
    monkeypatch.setattr(vc, "DEBOUNCE_S", 0.01)
    settings = Settings(data_dir=str(tmp_path / "data"),
                        memory_url="http://127.0.0.1:1",
                        voice_calibrated_scorer=True)
    with TestClient(create_app(settings), base_url="http://127.0.0.1") as c:
        store = anchors.store()
        pids = {n: bank(store, n, clips) for n, clips in HOUSEHOLD.items()}
        dave = pids["Dave"]
        vc._worker["kick"].set()
        assert _wait(lambda: c.get(f"/api/voice/people/{dave}/readiness")
                     .json()["state"] == "current")
        before = c.get(f"/api/voice/people/{dave}/readiness").json()
        assert before["readiness"]["reason"] == "one_day"
        assert before["summary"]["state"] == "ready"

        # Dave reads the passage today, in another room
        r = post(c, dave, wav(voice("Dave", 30, day=3)))
        assert r.status_code == 200, r.text
        body = r.json()
        assert body["state"] in ("checking", "current")
        assert body["summary"]["min_pieces"] == 20

        def landed():
            now = c.get(f"/api/voice/people/{dave}/readiness").json()
            return now["state"] == "current" and now["readiness"]["days"] == 2
        assert _wait(landed)
        after = c.get(f"/api/voice/people/{dave}/readiness").json()
        assert after["readiness"]["pieces"] > before["readiness"]["pieces"]
        # a second day was the gap, and this recording filled it
        assert after["readiness"]["ready"] is True


def test_person_readiness_states(tmp_path, monkeypatch):
    monkeypatch.setattr(vc, "embed", fake_embed_factory())
    monkeypatch.setattr(vc, "models_state", lambda cfg: "ready")
    monkeypatch.setattr(vc, "DEBOUNCE_S", 0.01)
    cfg = Settings(voice_calibrated_scorer=True).as_cfg()
    monkeypatch.setattr("backend.db.DATA_DIR", str(tmp_path))
    anchors._store = None
    store = anchors.store()
    assert vc.person_readiness({}, "anyone")["state"] == "off"
    assert vc.person_readiness(cfg, "anyone")["state"] == "checking"
    pids = {n: bank(store, n, HOUSEHOLD[n]) for n in ("Alex", "Sam", "Dave")}
    assert vc.start(cfg)
    assert _wait(lambda: vc.person_readiness(cfg, pids["Alex"])["state"]
                 == "current")
    ready = vc.person_readiness(cfg, pids["Alex"])
    assert ready["readiness"]["ready"] is True
    # a person with no clip the test can read is current, with no result
    empty = store.ensure_person("Mateo")
    assert _wait(lambda: vc.person_readiness(cfg, empty)["state"]
                 == "current")
    assert vc.person_readiness(cfg, empty)["readiness"] is None
    # a new clip makes it checking until the next build lands
    vc.stop()
    assert store.add_clip(pids["Alex"], voice("Alex", 6, day=4), SR,
                          source="introduction")
    assert vc.person_readiness(cfg, pids["Alex"])["state"] == "checking"
    with vc._lock:
        vc._status["state"] = "unavailable"
    assert vc.person_readiness(cfg, pids["Alex"])["state"] == "unavailable"


# ── 6. the gate and the logs ────────────────────────────────────────────────

def test_the_route_is_session_gated(tmp_path):
    app = create_app(Settings(data_dir=str(tmp_path / "data"),
                              memory_url="http://127.0.0.1:1"))
    with TestClient(app, base_url="http://127.0.0.1") as c:
        pid = anchors.store().ensure_person("Alex")
        assert c.post("/api/auth/setup", json={
            "recovery_secret": app.state.recovery_secret,
            "password": "a-durable-owner-passphrase"}).status_code == 200
        c.cookies.clear()
        assert post(c, pid, wav(reading(30))).status_code == 401
        assert c.get(f"/api/voice/people/{pid}/readiness").status_code == 401
        assert anchors.store().clips_of(pid) == []


def test_the_logs_carry_sizes_seconds_and_outcome_only(app, caplog):
    caplog.set_level(logging.INFO)
    with TestClient(app, base_url="http://127.0.0.1") as c:
        pid = anchors.store().ensure_person("Mateo")
        assert post(c, pid, wav(reading(30))).status_code == 200
        assert post(c, pid, wav(noise_pcm(20))).status_code == 422
    ours = [r.getMessage() for r in caplog.records
            if r.name.startswith("crossband")]
    assert any(line.startswith("voice recorded: bytes=") for line in ours)
    assert any("reason=not_speech" in line for line in ours)
    # the test client logs its own request URLs; the app says no name
    assert not any("mateo" in line.lower() for line in ours)
