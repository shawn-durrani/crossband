"""The durable voice-anchor store (#28 phase 2), pinned to its two promises.

PRIVACY: everything lives under the data directory in owner-only files
(0o700 directory, 0o600 files, the .env posture), and forget() actually
deletes the person's audio from disk - not just the index entry.

SUFFICIENCY: identification is gated on several seconds of accepted clean
speech per person. The quality gate keeps junk clips out (too short, too
quiet), keep-best-N refreshes anchors as better speech arrives, and the
prefix builder simply excludes anyone below the bar - below it,
identification stays uncertain by construction.

Pure rules (quality, keep policy, sufficiency) are tested directly;
store behaviour through a tmp-path-rooted AnchorStore. No network anywhere.
"""

import os

import pytest

from backend import anchors


def loud_pcm(seconds, sample_rate=16000, amp=b"\x00\x40"):
    """PCM-16 with a strong level (0x4000 samples) - passes the RMS gate."""
    return amp * int(seconds * sample_rate)


def quiet_pcm(seconds, sample_rate=16000):
    """Near-silence (sample value 1) - fails the RMS gate."""
    return b"\x01\x00" * int(seconds * sample_rate)


@pytest.fixture
def store(tmp_path):
    return anchors.AnchorStore(tmp_path / "voice_anchors")


# ── pure rules ──────────────────────────────────────────────────────────────

def test_clip_quality_measures_duration_and_level():
    q = anchors.clip_quality(loud_pcm(2.0), 16000)
    assert q["seconds"] == pytest.approx(2.0)
    assert q["rms"] >= 16000  # 0x4000 samples
    assert q["score"] > 0
    silent = anchors.clip_quality(b"\x00\x00" * 16000, 16000)
    assert silent["rms"] == 0 and silent["score"] == 0


def test_quality_gate_rejects_short_and_quiet():
    assert anchors.accepts_clip(anchors.clip_quality(loud_pcm(1.0), 16000))
    assert not anchors.accepts_clip(anchors.clip_quality(loud_pcm(0.5), 16000))
    assert not anchors.accepts_clip(anchors.clip_quality(quiet_pcm(3.0), 16000))


def test_keep_policy_keeps_the_best_n_by_score():
    clips = [{"file": f"c{i}", "seconds": s, "score": s, "added_at": i}
             for i, s in enumerate([1.0, 5.0, 2.0, 4.0, 3.0, 6.0, 0.5])]
    kept = anchors.select_keep(clips)
    assert len(kept) == anchors.KEEP_CLIPS
    assert {c["file"] for c in kept} == {"c1", "c3", "c4", "c5", "c2"}


def test_sufficiency_is_a_seconds_threshold():
    two = [{"seconds": 2.0}, {"seconds": 2.0}]
    assert not anchors.is_sufficient(two)
    assert anchors.is_sufficient(two + [{"seconds": 2.0}])


def test_trim_caps_a_clip_at_max_seconds():
    pcm = anchors.trim_clip(loud_pcm(30.0), 16000)
    assert len(pcm) == int(anchors.MAX_CLIP_SECONDS * 16000) * 2


# ── the store: privacy posture ──────────────────────────────────────────────

def test_store_files_are_owner_only(store):
    pid = store.ensure_person("Alex")
    assert store.add_clip(pid, loud_pcm(2.0), 16000, source="introduction")
    assert (os.stat(store.root).st_mode & 0o777) == 0o700
    files = os.listdir(store.root)
    assert anchors.INDEX_NAME in files
    assert any(f.endswith(".wav") for f in files)
    for f in files:
        assert (os.stat(store.root / f).st_mode & 0o777) == 0o600, f


def test_forget_deletes_the_audio_from_disk(store):
    pid = store.ensure_person("Alex")
    for _ in range(3):
        assert store.add_clip(pid, loud_pcm(2.0), 16000, source="accumulated")
    wavs = [f for f in os.listdir(store.root) if f.endswith(".wav")]
    assert len(wavs) == 3
    assert store.forget(pid)
    left = [f for f in os.listdir(store.root) if f.endswith(".wav")]
    assert left == []                       # the audio is GONE, not orphaned
    assert store.people() == []             # and so is the person
    assert not store.forget(pid)            # idempotent-ish: second time is a no


def test_rejected_clips_write_nothing(store):
    pid = store.ensure_person("Alex")
    assert not store.add_clip(pid, quiet_pcm(3.0), 16000, source="accumulated")
    assert not store.add_clip(pid, loud_pcm(0.3), 16000, source="accumulated")
    assert [f for f in os.listdir(store.root) if f.endswith(".wav")] == []


# ── the store: sufficiency + refresh ────────────────────────────────────────

def test_accumulation_reaches_sufficiency_across_utterances(store):
    pid = store.ensure_person("Alex")
    store.add_clip(pid, loud_pcm(2.0), 16000, source="introduction")
    assert store.people()[0]["sufficient"] is False   # 2s < the bar
    store.add_clip(pid, loud_pcm(2.0), 16000, source="accumulated")
    assert store.people()[0]["sufficient"] is False   # 4s - still short
    store.add_clip(pid, loud_pcm(2.0), 16000, source="accumulated")
    person = store.people()[0]
    assert person["sufficient"] is True               # 6s - the bar
    assert person["seconds"] == pytest.approx(6.0)


def test_keep_best_n_evicts_worst_clip_files(store):
    pid = store.ensure_person("Alex")
    # KEEP_CLIPS strong clips, then a weak-but-acceptable one and another
    # strong one: the weak one is evicted and its FILE deleted.
    for _ in range(anchors.KEEP_CLIPS):
        assert store.add_clip(pid, loud_pcm(3.0), 16000, source="accumulated")
    assert store.add_clip(pid, loud_pcm(1.0), 16000, source="accumulated")
    assert store.add_clip(pid, loud_pcm(3.0), 16000, source="accumulated")
    person = store.people()[0]
    assert person["clip_count"] == anchors.KEEP_CLIPS
    wavs = [f for f in os.listdir(store.root) if f.endswith(".wav")]
    assert len(wavs) == anchors.KEEP_CLIPS  # evictions deleted their files
    assert person["seconds"] == pytest.approx(3.0 * anchors.KEEP_CLIPS)


def test_find_by_name_is_case_insensitive_reidentification(store):
    pid = store.ensure_person("Alex")
    assert store.find_by_name("alex")["person_id"] == pid
    assert store.find_by_name("ALEX")["person_id"] == pid
    assert store.find_by_name("Sam") is None
    # ensure_person on a known name reuses the entry - no duplicate person
    assert store.ensure_person("aLeX") == pid


# ── the prefix ──────────────────────────────────────────────────────────────

def test_prefix_includes_only_sufficient_people(store):
    strong = store.ensure_person("Shawn")
    for _ in range(3):
        store.add_clip(strong, loud_pcm(2.0), 16000, source="accumulated")
    weak = store.ensure_person("Alex")
    store.add_clip(weak, loud_pcm(2.0), 16000, source="introduction")
    pcm, segments = store.build_prefix([strong, weak], 16000)
    assert [s["name"] for s in segments] == ["Shawn"]  # Alex is below the bar
    seg = segments[0]
    assert seg["start"] == 0.0
    assert seg["end"] == pytest.approx(anchors.PREFIX_PERSON_SECONDS)
    assert len(pcm) == int(seg["end"] * 16000) * 2


def test_prefix_segments_tile_for_multiple_people(store):
    a = store.ensure_person("Shawn")
    b = store.ensure_person("Bea")
    for pid in (a, b):
        for _ in range(3):
            store.add_clip(pid, loud_pcm(2.0), 16000, source="accumulated")
    pcm, segments = store.build_prefix([a, b], 16000)
    assert [s["name"] for s in segments] == ["Shawn", "Bea"]
    assert segments[0]["end"] == pytest.approx(segments[1]["start"])
    assert len(pcm) == int(segments[1]["end"] * 16000) * 2


def test_prefix_skips_clips_recorded_at_another_sample_rate(store):
    pid = store.ensure_person("Shawn")
    for _ in range(3):
        store.add_clip(pid, loud_pcm(2.0, sample_rate=48000), 48000,
                       source="accumulated")
    pcm, segments = store.build_prefix([pid], 16000)
    assert pcm == b"" and segments == []


# ── recent-utterance cache (tap-to-correct's audio source) ──────────────────

def test_recent_audio_round_trip_and_bounds():
    anchors.clear_recent_audio()
    anchors.remember_audio(7, loud_pcm(1.0), 16000, 1)
    pcm, sr, n = anchors.take_audio(7)
    assert sr == 16000 and n == 1 and len(pcm) == 32000
    assert anchors.take_audio(7) is None  # claimed once
    # entry cap: oldest falls off
    for i in range(anchors.RECENT_MAX_ENTRIES + 5):
        anchors.remember_audio(100 + i, loud_pcm(0.1), 16000, 1)
    assert anchors.take_audio(100) is None
    assert anchors.take_audio(100 + anchors.RECENT_MAX_ENTRIES + 4) is not None
    anchors.clear_recent_audio()


def test_recent_audio_tail_caps_long_utterances():
    anchors.clear_recent_audio()
    long_pcm = loud_pcm(anchors.RECENT_MAX_SECONDS + 20)
    anchors.remember_audio(9, long_pcm, 16000, 2)
    pcm, _, n = anchors.take_audio(9)
    assert len(pcm) == int(anchors.RECENT_MAX_SECONDS * 16000) * 2
    assert n == 2
    anchors.clear_recent_audio()
