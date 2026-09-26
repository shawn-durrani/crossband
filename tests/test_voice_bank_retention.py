"""Stronger, more varied voice banks (#477), keyless and synthetic.

A bank is the few stored clips room mode recognises a person by. These
pins cover what changed about them, with synthetic audio and a stubbed
embedding model, no ONNX:

1. Speech-only fingerprints. A clip's long pauses are left out of what
   the model hears (voiceid.speech_only), the stored file never changes,
   a clip with too little speech is fingerprinted whole, and the
   enrolment cache key carries the rule's version, so no embedding of
   the untrimmed audio is reused. With a stub whose silence frames pull
   the embedding away from the voice, as pooling models do, the trim
   raises a same-speaker score on a silence-padded bank.
2. Retention. Ten long and five short clips per person; a new clip
   competes with its own session's clips first, so one evening can't
   replace every other day; clips a human stood behind are never rotated
   out for an automated clip; restored clips keep their capture time.
3. The upgrade deletes nothing: an old-shaped bank keeps every clip, and
   an audit that sets a whole bank aside deletes no file.
4. The pairwise hygiene rule with bigger banks. Two recording setups of
   one person disagree with each other and stay; a clip that sits closer
   to another person's voice than its own is still set aside.
"""

import math
import os
import struct
import time

import pytest

from backend import anchors, db, voiceid
from backend.config import Settings

SR = 16000
_ALEX, _SAM = 1200, 6000          # identity codes, read back by the stubs
_VOICE = {_ALEX: [1.0, 0.0, 0.0, 0.0], _SAM: [0.0, 1.0, 0.0, 0.0]}
_QUIET = [0.0, 0.0, 0.0, 1.0]


def _cfg(**over):
    c = Settings().as_cfg()
    c.update(over)
    return c


def _tone(value, seconds, sr=SR):
    """Speech-shaped PCM-16 riding a constant offset: the offset is the
    identity code the stubs read back, and the low-band harmonics carry it
    past the speech gate."""
    out = bytearray()
    for i in range(int(seconds * sr)):
        t = i / sr
        ac = sum(a * math.sin(2 * math.pi * f * t) for f, a in
                 ((140, 2500), (280, 1600), (420, 1000), (560, 600)))
        out += struct.pack("<h", int(max(-32000, min(32000, value + ac))))
    return bytes(out)


def _silence(seconds, sr=SR):
    return b"\x00\x00" * int(seconds * sr)


def _padded(value, speech=1.5, gap=2.5):
    """A clip that is mostly pause: speech, a long silence, speech."""
    return _tone(value, speech) + _silence(gap) + _tone(value, speech)


def _diluting_embed(_ex, audio_float, _sr):
    """A stand-in model that pools frames the way speaker models do: each
    32ms frame votes for the voice its level and offset say, and a quiet
    frame votes for a 'silence' axis. A pause-heavy clip therefore embeds
    part way to silence, which is the effect the trim removes."""
    n = 512
    acc = [0.0] * 4
    for s in range(0, len(audio_float) - n + 1, n):
        frame = audio_float[s:s + n]
        rms = math.sqrt(sum(x * x for x in frame) / n) * 32768.0
        if rms < 300:
            vec = _QUIET
        else:
            mean = sum(frame) / n * 32768.0
            vec = _VOICE[min(_VOICE, key=lambda k: abs(k - mean))]
        acc = [a + v for a, v in zip(acc, vec)]
    return voiceid.l2_normalize(acc)


@pytest.fixture
def store(tmp_path, monkeypatch):
    monkeypatch.setattr(db, "DATA_DIR", str(tmp_path))
    anchors._store = None
    return anchors.store()


def _files(store):
    return sorted(f for f in os.listdir(store.root) if f.endswith(".wav"))


# ── 1. speech-only fingerprints ────────────────────────────────────────────

def test_long_pauses_are_left_out_and_word_gaps_stay():
    clip = _tone(_ALEX, 1.0) + _silence(2.0) + _tone(_ALEX, 1.0)
    spans = voiceid.speech_spans(clip, SR)
    assert len(spans) == 2                       # the long pause splits it
    trimmed = voiceid.speech_only(clip, SR)
    seconds = len(trimmed) / 2 / SR
    # both stretches of speech, each with its pad, and most of the pause gone
    assert 2.0 < seconds < 2.0 + 4 * voiceid.SPEECH_ONLY_PAD_SECONDS
    # a gap between words is bridged by the pads and nothing is spliced
    words = _tone(_ALEX, 1.0) + _silence(0.4) + _tone(_ALEX, 1.0)
    assert voiceid.speech_only(words, SR) == words


def test_too_little_speech_fingerprints_the_whole_clip():
    thin = _tone(_ALEX, 0.4) + _silence(3.0)
    assert voiceid.speech_only(thin, SR) == thin     # the measure failed
    assert voiceid.speech_only(_silence(2.0), SR) == _silence(2.0)
    assert voiceid.speech_spans(_silence(2.0), SR) == []
    clean = _tone(_ALEX, 2.0)
    assert voiceid.speech_only(clean, SR) == clean   # nothing to cut


def test_the_spans_are_the_same_without_numpy(monkeypatch):
    """numpy is optional at runtime, as everywhere in the speech gate: the
    stdlib path finds the same speech."""
    import array
    clip = _tone(_ALEX, 1.0) + _silence(1.5) + _tone(_ALEX, 1.2)
    fast = voiceid.speech_spans(clip, SR)

    def stdlib(pcm, usable):
        samples = array.array("h")
        samples.frombytes(pcm[:usable * 2])
        return None, samples

    monkeypatch.setattr(voiceid, "_load_samples", stdlib)
    assert voiceid.speech_spans(clip, SR) == fast


def test_enrolment_reads_speech_only_and_never_changes_the_file(store):
    pid = store.ensure_person("Alex")
    for _ in range(2):
        assert store.add_clip(pid, _padded(_ALEX), SR, "introduction")
    before = {f: (store.root / f).read_bytes() for f in _files(store)}
    enr = store.enrollment_clips([pid], SR)[pid]
    for pcm, fname in zip(enr["pcms"], enr["fingerprint"][1:]):
        stored = before[fname][44:]
        assert len(pcm) < len(stored)                 # the pause left out
        assert pcm == voiceid.speech_only(stored, SR)
    # the rule's version leads the fingerprint, then the kept files
    assert enr["fingerprint"][0] == voiceid.SPEECH_ONLY_VERSION
    assert set(enr["fingerprint"][1:]) == set(before)
    assert {f: (store.root / f).read_bytes() for f in _files(store)} == before


def test_trimming_raises_a_same_speaker_score_on_a_padded_bank(
        store, monkeypatch):
    """The mechanism, with the stub above: the same clean remark scores
    higher against a bank fingerprinted from its speech than against the
    same bank fingerprinted whole, and the live check names it with that
    higher score."""
    monkeypatch.setattr(voiceid, "_get_extractor", lambda cfg: object())
    monkeypatch.setattr(voiceid, "_embed", _diluting_embed)
    alex = store.ensure_person("Alex")
    sam = store.ensure_person("Sam")
    for _ in range(3):
        assert store.add_clip(alex, _padded(_ALEX), SR, "introduction")
        assert store.add_clip(sam, _tone(_SAM, 3.0), SR, "introduction")
    query = _diluting_embed(None, voiceid._pcm_to_float(_tone(_ALEX, 2.0)),
                            SR)
    whole = voiceid.average_embeddings([
        _diluting_embed(None, voiceid._pcm_to_float(
            store._read_clip_pcm(c["file"])), SR)
        for c in store.clips_of(alex)])
    cands = [{"person_id": alex, "name": "Alex"},
             {"person_id": sam, "name": "Sam"}]
    speech = voiceid._enrolled_embeddings(cands, SR, object())[alex]["emb"]
    assert voiceid.cosine(query, whole) < 0.8        # diluted by the pauses
    assert voiceid.cosine(query, speech) > voiceid.cosine(query, whole) + 0.1
    verdict = voiceid.identify_utterance(_tone(_ALEX, 2.0), SR, cands, _cfg())
    assert verdict["name"] == "Alex"
    assert verdict["score"] == pytest.approx(voiceid.cosine(query, speech),
                                             abs=1e-4)


def test_an_embedding_of_the_untrimmed_audio_is_never_reused(
        store, monkeypatch):
    """The enrolment cache used to be keyed on the clip files alone. A
    stale entry under that key must miss, so every bank is re-embedded
    from its speech the first time it is matched."""
    monkeypatch.setattr(voiceid, "_embed", _diluting_embed)
    pid = store.ensure_person("Alex")
    for _ in range(3):
        assert store.add_clip(pid, _padded(_ALEX), SR, "introduction")
    files = tuple(store.enrollment_clips([pid], SR)[pid]["fingerprint"][1:])
    stale = [0.0, 0.0, 1.0, 0.0]
    voiceid._enroll_cache[(pid, files)] = stale
    got = voiceid._enrolled_embeddings(
        [{"person_id": pid, "name": "Alex"}], SR, object())[pid]["emb"]
    assert got != stale
    assert voiceid.cosine(got, _VOICE[_ALEX]) > 0.9


def test_the_audit_embeds_the_same_speech_only_audio(store, monkeypatch):
    seen = []

    def recording(_ex, audio_float, sr):
        seen.append(len(audio_float))
        return _diluting_embed(_ex, audio_float, sr)

    monkeypatch.setattr(voiceid, "_get_extractor", lambda cfg: object())
    monkeypatch.setattr(voiceid, "_embed", recording)
    pid = store.ensure_person("Alex")
    assert store.add_clip(pid, _padded(_ALEX), SR, "introduction")
    raw = store._read_clip_pcm(_files(store)[0])
    assert voiceid.audit_banks(_cfg()) is True
    assert seen == [len(voiceid.speech_only(raw, SR)) // 2]
    assert seen[0] < len(raw) // 2


# ── 2. retention ───────────────────────────────────────────────────────────

DAY = 86400
T0 = 1_700_000_000


def _clip(name, at, score=3.0, seconds=3.0, source="accumulated", **extra):
    return {"file": name, "seconds": seconds, "score": score,
            "added_at": at, "source": source, **extra}


def test_bank_size_caps_per_length_class():
    assert (anchors.KEEP_CLIPS, anchors.KEEP_SHORT_CLIPS) == (10, 5)
    longs = [_clip(f"l{i}", T0 + i * DAY) for i in range(14)]
    shorts = [_clip(f"s{i}", T0 + i * DAY, score=1.5, seconds=1.5)
              for i in range(8)]
    kept = anchors.select_keep(longs + shorts)
    assert sum(1 for c in kept if not anchors.is_short(c)) == 10
    assert sum(1 for c in kept if anchors.is_short(c)) == 5


def test_sessions_are_runs_of_clips_split_by_a_long_gap():
    hour = 3600
    clips = [_clip("a", T0), _clip("b", T0 + 1 * hour),
             _clip("c", T0 + 2 * hour),                    # chained: one run
             _clip("d", T0 + 2 * hour + anchors.SESSION_GAP_S + 1),
             _clip("legacy1", 0), _clip("legacy2", None)]
    s = anchors.clip_sessions(clips)
    assert s[0] == s[1] == s[2]
    assert s[3] != s[2]
    # clips with no timestamp are never lumped together
    assert len({s[4], s[5], s[0], s[3]}) == 4


def test_one_evening_cannot_replace_every_other_day():
    """A full bank of ten days, then one evening of six better clips. The
    evening's best takes one slot from the weakest day, and its other five
    compete with each other. The old best-by-score rule kept all six."""
    days = [_clip(f"day{i}", T0 + i * DAY, score=3.0) for i in range(10)]
    evening = [_clip(f"eve{i}", T0 + 20 * DAY + i * 600, score=5.0 + i / 10)
               for i in range(6)]
    kept = {c["file"] for c in anchors.select_keep(days + evening)}
    assert len(kept) == anchors.KEEP_CLIPS
    assert sum(1 for f in kept if f.startswith("day")) == 9
    assert kept & {f"eve{i}" for i in range(6)} == {"eve5"}   # its best


def test_a_new_clip_competes_with_its_own_session_first():
    """Day A holds five clips and five other days one each. A better clip
    from day A's session evicts one of day A's own, and every other day
    keeps its only clip, weaker though each one is."""
    a = [_clip(f"a{i}", T0 + i * 60, score=4.0) for i in range(5)]
    others = [_clip(f"d{i}", T0 + (i + 1) * DAY, score=2.0) for i in range(5)]
    new = _clip("a-new", T0 + 400, score=5.0)
    kept = {c["file"] for c in anchors.select_keep(a + others + [new])}
    assert {f"d{i}" for i in range(5)} <= kept
    assert "a-new" in kept
    assert "a0" not in kept          # the oldest of the tied session clips


def test_a_clip_a_human_stood_behind_is_never_rotated_out():
    intro = _clip("intro", T0, score=0.5, source="introduction")
    fixed = _clip("fixed", T0 + DAY, score=0.5, source="correction")
    moved = _clip("moved", T0 + 2 * DAY, score=0.5, moved_at=T0 + 3 * DAY)
    flood = [_clip(f"acc{i}", T0 + 5 * DAY + i * 60, score=9.0)
             for i in range(15)]
    kept = {c["file"] for c in anchors.select_keep([intro, fixed, moved]
                                                   + flood)}
    assert {"intro", "fixed", "moved"} <= kept
    assert len(kept) == anchors.KEEP_CLIPS
    # past the cap, human-backed clips compete among themselves: still
    # bounded, and still ranked for variety
    many = [_clip(f"i{i}", T0 + i * DAY, source="introduction")
            for i in range(anchors.KEEP_CLIPS + 3)]
    assert len(anchors.select_keep(many)) == anchors.KEEP_CLIPS


def test_protected_clips_count_toward_their_own_session_share():
    """Day A's introduction already speaks for day A, so day A's automated
    clips queue behind every other day's first clip."""
    intro = _clip("a-intro", T0, source="introduction")
    a_auto = [_clip(f"a{i}", T0 + (i + 1) * 60, score=9.0) for i in range(9)]
    others = [_clip(f"d{i}", T0 + (i + 1) * DAY, score=1.0) for i in range(9)]
    kept = {c["file"] for c in anchors.select_keep([intro] + a_auto + others)}
    assert "a-intro" in kept
    assert {f"d{i}" for i in range(9)} <= kept
    assert len(kept & {f"a{i}" for i in range(9)}) == 0


def test_the_store_protects_an_introduction_through_a_flood(store):
    pid = store.ensure_person("Alex")
    assert store.add_clip(pid, _tone(_ALEX, 2.5), SR, "introduction")
    intro = _files(store)
    for _ in range(anchors.KEEP_CLIPS + 2):
        assert store.add_clip(pid, _tone(_ALEX, 6.0), SR, "accumulated")
    clips = store.clips_of(pid)
    assert len(clips) == anchors.KEEP_CLIPS
    assert intro[0] in {c["file"] for c in clips}
    assert set(_files(store)) == {c["file"] for c in clips}   # evicted: gone


def test_a_restored_clip_keeps_the_time_it_was_captured(store):
    pid = store.ensure_person("Alex")
    old = time.time() - 30 * DAY
    assert store.add_clip(pid, _tone(_ALEX, 2.5), SR, "accumulated",
                          added_at=old)
    assert store.add_clip(pid, _tone(_ALEX, 2.6), SR, "accumulated",
                          added_at=time.time() + DAY)       # future: now
    assert store.add_clip(pid, _tone(_ALEX, 2.7), SR, "accumulated",
                          added_at="soon")                  # junk: now
    stamps = sorted(c["added_at"] for c in store.clips_of(pid))
    assert stamps[0] == pytest.approx(old)
    assert all(abs(t - time.time()) < 60 for t in stamps[1:])


# ── 3. the upgrade deletes nothing ─────────────────────────────────────────

def test_an_old_shaped_bank_keeps_every_clip_on_upgrade(store):
    """Five long and three short clips filled a bank before #477. Growing
    under the new caps removes none of them, and an audit that sets the
    whole bank aside deletes no file."""
    pid = store.ensure_person("Alex")
    for secs in (3.0, 3.2, 3.4, 3.6, 3.8):
        assert store.add_clip(pid, _tone(_ALEX, secs), SR, "accumulated")
    for secs in (1.2, 1.4, 1.6):
        assert store.add_clip(pid, _tone(_ALEX, secs), SR, "accumulated")
    old = set(_files(store))
    assert len(old) == 8
    assert store.add_clip(pid, _tone(_ALEX, 2.5), SR, "accumulated")
    assert store.add_clip(pid, _tone(_ALEX, 1.1), SR, "accumulated")
    assert old < set(_files(store))
    assert len(store.clips_of(pid)) == 10
    store.set_hygiene({pid: sorted(_files(store))}, [])
    assert len(_files(store)) == 10          # set aside, every file kept
    assert store.people()[0]["quarantined_count"] == 10


# ── 4. the hygiene rule with bigger banks ──────────────────────────────────

def _emb(speaker, setup, w_setup=0.6):
    """Speaker direction plus a recording-setup direction."""
    return voiceid.l2_normalize([s + w_setup * c for s, c in
                                 zip(speaker, setup)])


_SPK_ALEX = [1, 0, 0, 0, 0, 0]
_SPK_SAM = [0, 1, 0, 0, 0, 0]
_PHONE = [0, 0, 1, 0, 0, 0]
_LAPTOP = [0, 0, 0, 1, 0, 0]


def test_two_recording_setups_of_one_person_both_stay():
    """Alex's bank holds twelve phone clips and three laptop clips, and
    the laptop clips sit well away from the phone majority. Sam has used
    both. Disagreeing with one's own bank is not contamination: each
    laptop clip is still closer to Alex than to Sam, so nothing is set
    aside."""
    alex = [(f"phone{i}", _emb(_SPK_ALEX, _PHONE)) for i in range(12)]
    alex += [(f"laptop{i}", _emb(_SPK_ALEX, _LAPTOP)) for i in range(3)]
    sam = [(f"sam-phone{i}", _emb(_SPK_SAM, _PHONE)) for i in range(4)]
    sam += [(f"sam-laptop{i}", _emb(_SPK_SAM, _LAPTOP)) for i in range(4)]
    laptop = alex[-1][1]
    to_own = voiceid.cosine(
        laptop, voiceid.average_embeddings([e for _, e in alex[:-1]]))
    to_sam = voiceid.cosine(
        laptop, voiceid.average_embeddings([e for _, e in sam]))
    assert to_own < 0.85                 # the setups do disagree
    assert 0.0 < to_sam < to_own         # and Sam's voice is further off
    assert voiceid.quarantine_verdicts({"alex": alex, "sam": sam}) == {}


def test_real_contamination_is_still_set_aside_in_a_big_bank():
    alex = [(f"phone{i}", _emb(_SPK_ALEX, _PHONE)) for i in range(12)]
    alex += [(f"laptop{i}", _emb(_SPK_ALEX, _LAPTOP)) for i in range(2)]
    alex += [("stolen", _emb(_SPK_SAM, _PHONE))]    # Sam's voice, Alex's bank
    sam = [(f"sam{i}", _emb(_SPK_SAM, _PHONE)) for i in range(8)]
    assert voiceid.quarantine_verdicts({"alex": alex, "sam": sam}) \
        == {"alex": ["stolen"]}


def test_a_setup_clip_closer_to_someone_else_is_judged_as_before():
    """The rule itself is unchanged: when a recording setup outweighs the
    voice so far that Alex's laptop clip sits closer to Sam, who only ever
    used the laptop, than to Alex's phone-heavy bank, it is set aside, as
    it always would have been."""
    alex = [(f"phone{i}", _emb(_SPK_ALEX, _PHONE, 2.0)) for i in range(12)]
    alex += [("laptop", _emb(_SPK_ALEX, _LAPTOP, 2.0))]
    sam = [(f"sam{i}", _emb(_SPK_SAM, _LAPTOP, 2.0)) for i in range(5)]
    assert voiceid.quarantine_verdicts({"alex": alex, "sam": sam}) \
        == {"alex": ["laptop"]}
