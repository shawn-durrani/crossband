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
from tests.conftest import speech_pcm


def loud_pcm(seconds, sample_rate=16000):
    """Speech-shaped PCM-16 at a strong level - passes every gate (#218)."""
    return speech_pcm(seconds, sample_rate)


def quiet_pcm(seconds, sample_rate=16000):
    """Near-silence (sample value 1) - fails the RMS gate."""
    return b"\x01\x00" * int(seconds * sample_rate)


def noise_pcm(seconds, sample_rate=16000, seed=11):
    """Deterministic white noise: loud, long - and not a voice (#218)."""
    import random
    import struct
    rng = random.Random(seed)
    return b"".join(struct.pack("<h", rng.randint(-12000, 12000))
                    for _ in range(int(seconds * sample_rate)))


@pytest.fixture
def store(tmp_path):
    return anchors.AnchorStore(tmp_path / "voice_anchors")


# ── pure rules ──────────────────────────────────────────────────────────────

def test_clip_quality_measures_duration_and_level():
    q = anchors.clip_quality(loud_pcm(2.0), 16000)
    assert q["seconds"] == pytest.approx(2.0)
    assert q["rms"] >= 5000  # a strong level
    assert q["score"] > 0
    silent = anchors.clip_quality(b"\x00\x00" * 16000, 16000)
    assert silent["rms"] == 0 and silent["score"] == 0


def test_quality_gate_rejects_short_and_quiet():
    assert anchors.accepts_clip(anchors.clip_quality(loud_pcm(1.0), 16000))
    assert not anchors.accepts_clip(anchors.clip_quality(loud_pcm(0.5), 16000))
    assert not anchors.accepts_clip(anchors.clip_quality(quiet_pcm(3.0), 16000))


def test_quality_gate_rejects_non_speech(store):
    """#218: loud, long static cleared the old gate and its
    seconds-times-loudness score could EVICT genuine speech under
    keep-best-N. The gate now asks the speech question, so no source can
    bank noise - however any caller reached add_clip."""
    noisy = anchors.clip_quality(noise_pcm(3.0), 16000)
    assert noisy["seconds"] >= 1.0 and noisy["rms"] >= 120  # old gate passed
    assert not anchors.accepts_clip(noisy)
    pid = store.ensure_person("Alex")
    for source in ("introduction", "accumulated", "harvested-short",
                   "correction", "cold-start"):
        assert not store.add_clip(pid, noise_pcm(3.0), 16000, source=source)
    assert store.clips_of(pid) == []
    # speech through the same call sites is unaffected
    assert store.add_clip(pid, loud_pcm(2.0), 16000, source="accumulated")


def test_keep_policy_keeps_the_best_n_per_length_class():
    """Deliberately updated for #28 PR-B: keep-best-N applies PER LENGTH
    CLASS. Under the old single pool the short clips (scores scale with
    seconds) were always evicted first, which starved exactly the clips the
    two-part sufficiency bar needs - so shorts now keep their own best-N."""
    clips = [{"file": f"c{i}", "seconds": s, "score": s, "added_at": i}
             for i, s in enumerate([1.0, 5.0, 2.0, 4.0, 3.0, 6.0, 0.5,
                                    1.5, 1.2])]
    kept = anchors.select_keep(clips)
    # longs (> SHORT_CLIP_MAX_SECONDS): 5.0, 4.0, 3.0, 6.0 - all four kept
    # shorts (<= 2.0): best KEEP_SHORT_CLIPS of 1.0/2.0/0.5/1.5/1.2
    assert {c["file"] for c in kept if not anchors.is_short(c)} \
        == {"c1", "c3", "c4", "c5"}
    assert {c["file"] for c in kept if anchors.is_short(c)} \
        == {"c2", "c7", "c8"}  # 2.0s, 1.5s, 1.2s - the best three shorts
    # a long-heavy pool still caps at KEEP_CLIPS longs
    longs = [{"file": f"l{i}", "seconds": 3.0 + i, "score": 3.0 + i,
              "added_at": i} for i in range(anchors.KEEP_CLIPS + 2)]
    assert len(anchors.select_keep(longs)) == anchors.KEEP_CLIPS


def test_sufficiency_is_the_seconds_bar_and_short_readiness_is_a_signal():
    """#28, tenth field test: PR-B's two-part gate deadlocked existing banks
    (no candidates -> no matches -> no accumulation -> never sufficient).
    Sufficiency is the SECONDS bar alone again; short-clip readiness is the
    separate is_short_ready signal, and confident long matches harvest their
    own short slices so it fills without a ceremony."""
    two = [{"seconds": 2.0}, {"seconds": 2.0}]
    assert not anchors.is_sufficient(two)                      # seconds short
    assert anchors.is_sufficient(two + [{"seconds": 2.0}])     # 6s
    # seconds met by LONG clips only: SUFFICIENT (the unbrick), but not
    # short-ready until MIN_SHORT_CLIPS short clips exist
    longs = [{"seconds": 4.0}, {"seconds": 4.0}]
    assert anchors.is_sufficient(longs)
    assert not anchors.is_short_ready(longs)
    assert not anchors.is_short_ready(longs + [{"seconds": 1.5}])  # 1 short
    assert anchors.is_short_ready(longs + [{"seconds": 1.5},
                                           {"seconds": 1.2}])      # 2 shorts
    # quarantined clips count for neither measure
    assert not anchors.is_sufficient(
        [{"seconds": 4.0}, {"seconds": 4.0, "quarantined": True}])
    assert not anchors.is_short_ready(
        longs + [{"seconds": 1.5, "quarantined": True},
                 {"seconds": 1.2, "quarantined": True}])


def test_configure_sufficiency_applies_and_guards_the_knobs():
    orig_secs, orig_short = anchors.SUFFICIENT_SECONDS, anchors.MIN_SHORT_CLIPS
    try:
        anchors.configure_sufficiency(8.0, 3)
        assert anchors.SUFFICIENT_SECONDS == 8.0
        assert anchors.MIN_SHORT_CLIPS == 3
        # junk keeps the current values rather than crashing or zeroing
        anchors.configure_sufficiency("x", -1)
        assert anchors.SUFFICIENT_SECONDS == 8.0
        assert anchors.MIN_SHORT_CLIPS == 3
        anchors.configure_sufficiency(None, None)
        assert anchors.SUFFICIENT_SECONDS == 8.0
    finally:
        anchors.configure_sufficiency(orig_secs, orig_short)


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


# ── the store: preferred display names (#28 phase 3) ────────────────────────

def test_preferred_name_defaults_to_the_introduced_name(store):
    store.ensure_person("Lex")
    person = store.people()[0]
    assert person["preferred_name"] == "Lex"


def test_set_preferred_name_persists_and_keeps_identity(store):
    pid = store.ensure_person("Lex")
    assert store.set_preferred_name(pid, "  Alex  ")
    person = store.people()[0]
    assert person["preferred_name"] == "Alex"   # trimmed, persisted
    assert person["name"] == "Lex"              # identity key untouched
    # survives a fresh store over the same directory (it's on disk)
    fresh = anchors.AnchorStore(store.root)
    assert fresh.people()[0]["preferred_name"] == "Alex"
    # find_by_name still keys on the identity name
    assert fresh.find_by_name("Lex")["person_id"] == pid


def test_set_preferred_name_guards_its_inputs(store):
    pid = store.ensure_person("Lex")
    assert not store.set_preferred_name(pid, "")
    assert not store.set_preferred_name(pid, "   ")
    assert not store.set_preferred_name(pid, "!!!")   # no letters, no name
    assert not store.set_preferred_name("nope", "Alex")
    assert store.people()[0]["preferred_name"] == "Lex"  # nothing changed
    long = "A" * 100
    assert store.set_preferred_name(pid, long)
    assert len(store.people()[0]["preferred_name"]) <= anchors.MAX_PREFERRED_CHARS


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
    """Deliberately updated for #28 PR-B: eviction is per length class, so
    the weak-but-short 1.0s clip now SURVIVES a long-clip refresh (it is the
    short class's best) while the sixth LONG clip evicts a long one - and
    the evicted long's file is really deleted."""
    pid = store.ensure_person("Alex")
    for _ in range(anchors.KEEP_CLIPS):
        assert store.add_clip(pid, loud_pcm(3.0), 16000, source="accumulated")
    assert store.add_clip(pid, loud_pcm(1.0), 16000, source="accumulated")
    assert store.add_clip(pid, loud_pcm(3.0), 16000, source="accumulated")
    person = store.people()[0]
    assert person["clip_count"] == anchors.KEEP_CLIPS + 1  # 5 longs + 1 short
    assert person["short_clips"] == 1
    wavs = [f for f in os.listdir(store.root) if f.endswith(".wav")]
    assert len(wavs) == anchors.KEEP_CLIPS + 1  # the evicted long is deleted
    assert person["seconds"] == pytest.approx(3.0 * anchors.KEEP_CLIPS + 1.0)


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
        # #83: vouch the bank (remembered = introduced), or the prefix
        # rightly refuses it.
        store.add_clip(pid, loud_pcm(2.0), 16000, source="introduction")
        for _ in range(2):
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


# ── the prefix cache (#28, night test 4) ────────────────────────────────────
#
# Every diarization pass used to re-read the same clip files from disk. The
# built prefix is cached per roster snapshot, with two hard pins: a cache hit
# performs ZERO clip-file reads, and ANY anchor mutation invalidates - the
# index fingerprint changes on every _save, whichever store instance (or
# process) wrote it.


def _count_clip_reads(store):
    counter = {"n": 0}
    real = store._read_clip_pcm

    def counting(fname):
        counter["n"] += 1
        return real(fname)

    store._read_clip_pcm = counting
    return counter


def _sufficient_person(store, name):
    pid = store.ensure_person(name)
    # #83: the first clip is the introduction that vouched the bank - an
    # accumulation-only sufficient bank is deliberately paused out of the
    # prefix now (test_audition_gate.py owns that behaviour).
    assert store.add_clip(pid, loud_pcm(2.0), 16000, source="introduction")
    for _ in range(2):
        assert store.add_clip(pid, loud_pcm(2.0), 16000, source="accumulated")
    return pid


def test_prefix_cache_hit_reads_zero_clip_files(store):
    a = _sufficient_person(store, "Shawn")
    b = _sufficient_person(store, "Bea")
    reads = _count_clip_reads(store)
    pcm1, segs1 = store.build_prefix([a, b], 16000)
    assert reads["n"] > 0                      # the first build hits disk
    reads["n"] = 0
    pcm2, segs2 = store.build_prefix([a, b], 16000)
    assert reads["n"] == 0                     # THE pin: a hit reads nothing
    assert pcm2 == pcm1 and segs2 == segs1     # and is byte-identical
    # a caller mutating what it got back can never corrupt the cache
    segs2[0]["name"] = "Mallory"
    assert store.build_prefix([a, b], 16000)[1] == segs1


def test_any_anchor_mutation_invalidates_the_prefix_cache(store):
    a = _sufficient_person(store, "Shawn")
    reads = _count_clip_reads(store)
    store.build_prefix([a], 16000)
    reads["n"] = 0
    # a new accepted clip rewrites the index: the next build re-reads
    assert store.add_clip(a, loud_pcm(3.0), 16000, source="accumulated")
    store.build_prefix([a], 16000)
    assert reads["n"] > 0
    reads["n"] = 0
    # even a pure metadata write (preferred name) invalidates - "any
    # mutation" means any, so the rule never needs a per-field exception
    assert store.set_preferred_name(a, "Shawnie")
    store.build_prefix([a], 16000)
    assert reads["n"] > 0
    # forgetting a person empties their prefix, cache notwithstanding
    assert store.forget(a)
    pcm, segments = store.build_prefix([a], 16000)
    assert pcm == b"" and segments == []


def test_prefix_cache_keys_on_the_roster_snapshot(store):
    a = _sufficient_person(store, "Shawn")
    b = _sufficient_person(store, "Bea")
    reads = _count_clip_reads(store)
    solo = store.build_prefix([a], 16000)
    reads["n"] = 0
    pair = store.build_prefix([a, b], 16000)   # different roster: a miss
    assert reads["n"] > 0
    assert pair[0] != solo[0]
    reads["n"] = 0
    assert store.build_prefix([a], 16000) == solo   # the solo entry survived
    assert reads["n"] == 0


def test_prefix_cache_sees_another_stores_writes(store):
    """The on-disk half of the fingerprint: a mutation through a DIFFERENT
    store instance over the same directory (another process, in real life)
    still invalidates - the index rewrite changes mtime/size, and this
    instance's cached prefix may not outlive it."""
    a = _sufficient_person(store, "Shawn")
    reads = _count_clip_reads(store)
    store.build_prefix([a], 16000)
    reads["n"] = 0
    other = anchors.AnchorStore(store.root)
    assert other.add_clip(a, loud_pcm(3.0), 16000, source="correction")
    store.build_prefix([a], 16000)
    assert reads["n"] > 0


# ── the hygiene guard's storage (#28 PR-B) ──────────────────────────────────
#
# The AUDIT lives in voiceid.py (tests/test_voice_id.py pins its rules);
# this section pins the store half: set-aside clips stay on disk but take
# part in NOTHING (sufficiency, prefix, enrolment, the UI counts), close
# pairs persist and clean up with their people, and quarantine is bounded.


def _clip_files(store, pid):
    with store._lock:
        data = store._load()
    return [c["file"] for c in data["people"][pid]["clips"]]


def test_quarantined_clips_are_set_aside_not_deleted(store):
    pid = store.ensure_person("Alex")
    for secs in (2.5, 2.5, 1.5, 1.5):
        assert store.add_clip(pid, loud_pcm(secs), 16000, source="accumulated")
    assert store.people()[0]["sufficient"] is True
    bad = _clip_files(store, pid)[0]
    store.set_hygiene({pid: [bad]}, [])
    person = store.people()[0]
    assert person["quarantined_count"] == 1
    assert person["clip_count"] == 3            # active clips only
    # the file is STILL on disk - set aside, not forgotten
    assert (store.root / bad).exists()
    # the prefix and enrolment exclude it
    pcm, segments = store.build_prefix([pid], 16000)
    enrol = store.enrollment_clips([pid], 16000)
    if pid in enrol:
        assert bad not in enrol[pid]["fingerprint"]
    # a later audit that clears the verdict reinstates the clip
    store.set_hygiene({}, [])
    person = store.people()[0]
    assert person["quarantined_count"] == 0
    assert person["clip_count"] == 4


def test_quarantine_can_cost_sufficiency(store):
    """Setting aside a load-bearing clip honestly drops the person below
    the bar - their turns go back to uncertain rather than resting on
    contaminated evidence."""
    pid = store.ensure_person("Alex")
    for secs in (4.0, 1.5, 1.2):
        assert store.add_clip(pid, loud_pcm(secs), 16000, source="accumulated")
    assert store.people()[0]["sufficient"] is True
    short = next(f for f, s in zip(_clip_files(store, pid), (4.0, 1.5, 1.2))
                 if s == 1.5)
    store.set_hygiene({pid: [short]}, [])
    assert store.people()[0]["sufficient"] is False  # one short left < 2


def test_close_pairs_persist_and_surface_per_person(store):
    a = store.ensure_person("Alex")
    b = store.ensure_person("Sam")
    c = store.ensure_person("Dave")
    store.set_hygiene({}, [(a, b, 0.71)])
    assert store.close_pairs() == [(a, b, 0.71)]
    people = {p["person_id"]: p for p in store.people()}
    assert people[a]["close_to"] == [b]
    assert people[b]["close_to"] == [a]
    assert people[c]["close_to"] == []
    # forgetting either person clears the stale pair
    store.forget(b)
    assert store.close_pairs() == []


def test_quarantine_is_bounded_per_person(store):
    """Set aside is not a licence to hoard audio: past QUARANTINE_MAX the
    oldest set-aside clips are deleted for real. Overflow needs two rounds -
    a full bank quarantined, refilled, and quarantined again."""
    pid = store.ensure_person("Alex")
    for _ in range(anchors.KEEP_CLIPS):
        assert store.add_clip(pid, loud_pcm(3.0), 16000, source="accumulated")
    for _ in range(anchors.KEEP_SHORT_CLIPS):
        assert store.add_clip(pid, loud_pcm(1.5), 16000, source="accumulated")
    store.set_hygiene({pid: _clip_files(store, pid)}, [])
    assert store.people()[0]["quarantined_count"] \
        == anchors.KEEP_CLIPS + anchors.KEEP_SHORT_CLIPS
    # the bank refills with fresh clips; the next audit condemns those too
    assert store.add_clip(pid, loud_pcm(3.0), 16000, source="accumulated")
    assert store.add_clip(pid, loud_pcm(1.5), 16000, source="accumulated")
    store.set_hygiene({pid: _clip_files(store, pid)}, [])
    person = store.people()[0]
    assert person["quarantined_count"] == anchors.QUARANTINE_MAX
    assert person["clip_count"] == 0
    wavs = [f for f in os.listdir(store.root) if f.endswith(".wav")]
    assert len(wavs) == anchors.QUARANTINE_MAX  # the overflow was deleted


def test_bank_clips_and_clip_fingerprint(store):
    pid = store.ensure_person("Alex")
    assert store.add_clip(pid, loud_pcm(2.0), 16000, source="accumulated")
    fp1 = store.clip_fingerprint()
    bank = store.bank_clips(16000)
    assert list(bank) == [pid] and bank[pid]["name"] == "Alex"
    assert len(bank[pid]["clips"]) == 1
    # quarantined clips STAY in the audit's view (they can be reinstated),
    # and flipping the flag does not move the fingerprint - only real clip
    # changes do, or the audit would chase its own writes forever
    store.set_hygiene({pid: [bank[pid]["clips"][0][0]]}, [])
    assert store.clip_fingerprint() == fp1
    assert len(store.bank_clips(16000)[pid]["clips"]) == 1
    assert store.add_clip(pid, loud_pcm(2.2), 16000, source="accumulated")
    assert store.clip_fingerprint() != fp1


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
