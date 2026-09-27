"""Local speaker identification (#28): the speaker model and the matcher.

Identity is local or honestly uncertain: no cloud pass ever names a voice.
Voice turns are named by the session naming (tests/test_voice_sessions.py);
the whole-turn matcher here answers the two narrow questions still asked of
it, tap-to-correct's owner guard and the introduction's voice check. These
pins all run WITHOUT the 38MB model and WITHOUT sherpa-onnx present:

1. The pure decision seam (normalise/average/cosine + classify_utterance) -
   identify, open-set "none of the enrolled", and the ambiguity margin
   (widened for flagged close pairs) - proved with synthetic vectors, no
   ONNX.
2. Enrolment averages a person's stored anchor clips and caches the embedding
   keyed by the clip set, so identification re-embeds anchors only when they
   change (exercised with a MOCKED extractor - no model).
3. The pinned-model fetch verifies SHA-256 before use and refuses a mismatch,
   with no network (httpx mocked).
4. The speech gate: non-speech never reaches the matcher, and is never
   named by elimination.
5. The pairwise hygiene rules (#28 PR-B): contaminated clips quarantine,
   close centroid pairs are flagged, and the audit persists both through the
   anchor store.
6. The EL sniff is RETIRED (#28 PR-B): pinned structurally - the functions
   are gone, so no code path can fire it.
7. The banking bar the fallback scorer saves clips at.
8. An integration test that builds the real extractor when the model is
   present and SKIPS cleanly when it is not (the CI case).
"""

import time
import hashlib
import math
import os
import struct

import pytest

from backend import anchors, db, diarize, voiceid
from backend.config import Settings, _env_overrides


def _cfg(**over):
    c = Settings().as_cfg()
    c.update(over)
    return c


# ============================ 1. pure decision seam =======================

def test_l2_normalize_unit_and_zero():
    v = voiceid.l2_normalize([3.0, 4.0])
    assert abs((v[0] ** 2 + v[1] ** 2) ** 0.5 - 1.0) < 1e-9
    assert voiceid.l2_normalize([0.0, 0.0]) == [0.0, 0.0]  # zero stays zero


def test_average_embeddings_normalises_and_handles_empty():
    # Two vectors either side of the x axis average onto it, unit length.
    avg = voiceid.average_embeddings([[1.0, 1.0, 0.0], [1.0, -1.0, 0.0]])
    assert avg[0] > 0.99 and abs(avg[1]) < 1e-9
    assert abs(sum(x * x for x in avg) - 1.0) < 1e-9
    assert voiceid.average_embeddings([]) is None
    # A loud clip does not outvote a quiet one: per-clip L2 first.
    avg2 = voiceid.average_embeddings([[100.0, 0.0], [0.0, 1.0]])
    assert abs(avg2[0] - avg2[1]) < 1e-9


def test_cosine_edges():
    assert voiceid.cosine([1, 0, 0], [1, 0, 0]) == 1.0
    assert voiceid.cosine([1, 0, 0], [0, 1, 0]) == 0.0
    assert voiceid.cosine([0, 0, 0], [1, 0, 0]) == 0.0    # zero vector
    assert voiceid.cosine([1, 0], [1, 0, 0]) == 0.0       # length mismatch


def _enr(**people):
    return {pid: {"name": name, "emb": voiceid.l2_normalize(vec)}
            for pid, (name, vec) in people.items()}


def test_classify_confident_single_match():
    enr = _enr(p1=("Alex", [1, 0, 0]), p2=("Sam", [0, 1, 0]))
    v = voiceid.classify_utterance(voiceid.l2_normalize([0.98, 0.05, 0.0]), enr)
    assert v["status"] == "match" and v["name"] == "Alex" and v["person_id"] == "p1"


def test_classify_open_set_stranger_defers():
    """A voice matching nobody (below the threshold) is the open-set None."""
    enr = _enr(p1=("Alex", [1, 0, 0]), p2=("Sam", [0, 1, 0]))
    v = voiceid.classify_utterance(voiceid.l2_normalize([0.3, 0.3, 0.9]), enr)
    assert v["status"] == "defer" and v["reason"] == "below_threshold"


def test_classify_ambiguous_two_close_voices_defers():
    # Query sits between two enrolled people: over threshold to the best, but
    # the runner-up is too close to claim it (a blend, or two similar voices).
    enr = _enr(p1=("Alex", [1, 0, 0]), p2=("Sam", [0.95, 0.31, 0]))
    q = voiceid.l2_normalize([1.0, 0.15, 0.0])
    v = voiceid.classify_utterance(q, enr)
    assert v["status"] == "defer" and v["reason"] == "ambiguous"


def test_classify_no_candidates_defers():
    v = voiceid.classify_utterance([1, 0, 0], {})
    assert v["status"] == "defer" and v["reason"] == "no_candidates"


def test_close_pair_widens_the_required_margin():
    """#28 PR-B, the hygiene guard's teeth: a margin that would win between
    unrelated voices is NOT enough when the best and runner-up are a flagged
    close pair - similar-sounding households get stricter matching, never a
    confident mistake."""
    enr = _enr(p1=("Alex", [1.0, 0.0, 0.0]), p2=("Sam", [0.9, 0.4359, 0.0]))
    q = voiceid.l2_normalize([1.0, 0.05, 0.0])
    plain = voiceid.classify_utterance(q, enr, margin=0.05)
    assert plain["status"] == "match" and plain["name"] == "Alex"
    strict = voiceid.classify_utterance(q, enr, margin=0.05,
                                        close_pairs=[("p1", "p2")])
    assert strict["status"] == "defer" and strict["reason"] == "ambiguous"
    # pair order is irrelevant, and an unrelated pair changes nothing
    strict2 = voiceid.classify_utterance(q, enr, margin=0.05,
                                         close_pairs=[("p2", "p1")])
    assert strict2["reason"] == "ambiguous"
    other = voiceid.classify_utterance(q, enr, margin=0.05,
                                       close_pairs=[("p1", "p9")])
    assert other["status"] == "match"


# ── the pairwise hygiene rules (#28 PR-B; pure, synthetic vectors) ─────────

def _bank(**people):
    """{pid: [(clip_name, normalised emb), ...]}"""
    return {pid: [(n, voiceid.l2_normalize(v)) for n, v in clips]
            for pid, clips in people.items()}


def test_quarantine_flags_the_contaminated_clip_only():
    """The sixth field test's defect in miniature: Sam's bank holds one clip
    that is actually Alex's voice. The audit sets exactly that clip aside -
    Alex's own clips and Sam's clean ones are untouched."""
    bank = _bank(
        alex=[("a1", [1, 0, 0]), ("a2", [0.99, 0.05, 0])],
        sam=[("s1", [0, 1, 0]), ("s2", [0.05, 0.99, 0]),
             ("bad", [0.98, 0.1, 0])],   # Alex's voice in Sam's bank
    )
    q = voiceid.quarantine_verdicts(bank)
    assert q == {"sam": ["bad"]}


def test_quarantine_never_judges_a_single_clip_bank():
    bank = _bank(alex=[("a1", [1, 0, 0])], sam=[("s1", [0.9, 0.3, 0])])
    assert voiceid.quarantine_verdicts(bank) == {}


def test_clean_banks_quarantine_nothing():
    bank = _bank(alex=[("a1", [1, 0, 0]), ("a2", [0.98, 0.1, 0])],
                 sam=[("s1", [0, 1, 0]), ("s2", [0.1, 0.98, 0])])
    assert voiceid.quarantine_verdicts(bank) == {}


def test_close_centroid_pairs_flags_alike_voices_once():
    cents = {
        "alex": voiceid.l2_normalize([1, 0, 0]),
        "twin": voiceid.l2_normalize([0.95, 0.31, 0]),   # cosine ~0.95
        "sam": voiceid.l2_normalize([0, 1, 0]),
    }
    pairs = voiceid.close_centroid_pairs(cents)
    assert [(a, b) for a, b, _ in pairs] == [("alex", "twin")]
    assert pairs[0][2] >= voiceid.CLOSE_PAIR_COSINE
    assert voiceid.close_centroid_pairs({}) == []


def test_audit_banks_persists_quarantine_and_close_pairs(store, monkeypatch):
    """The impure half wired end to end with a mocked extractor: a
    contaminated clip lands quarantined in the store - kept on disk,
    excluded from matching."""
    monkeypatch.setattr(voiceid, "_get_extractor", lambda cfg: object())
    monkeypatch.setattr(voiceid, "_embed", _fake_embed)
    s = store["store"]
    # Poison Sam's bank with a clip that is really Alex's voice.
    assert s.add_clip(store["sam"], _tone(_ALEX_VAL, 2), 16000, "accumulated")
    before = {p["person_id"]: p for p in s.people()}
    assert before[store["sam"]]["quarantined_count"] == 0
    assert voiceid.audit_banks(_cfg()) is True
    after = {p["person_id"]: p for p in s.people()}
    assert after[store["sam"]]["quarantined_count"] == 1
    # the file is still on disk - set aside, not deleted
    import os
    wavs = [f for f in os.listdir(s.root) if f.endswith(".wav")]
    assert len(wavs) == after[store["sam"]]["clip_count"] \
        + after[store["alex"]]["clip_count"] + 1
    # distinct synthetic voices: no close pair flagged
    assert s.close_pairs() == []


def test_audit_sets_noise_clips_aside_with_their_own_reason(store, monkeypatch):
    """#219: the pairwise rules cannot see a noise clip - it is near
    nobody's centroid - so the audit's speech test catches it, under its
    own reason. The installed base self-cleans on its next audit: this
    clip entered the store the way an older build would have banked it,
    with no acceptance gate."""
    monkeypatch.setattr(voiceid, "_get_extractor", lambda cfg: object())
    monkeypatch.setattr(voiceid, "_embed", _fake_embed)
    s = store["store"]
    noise = _noise(3.0)
    fname = s._write_clip(noise, 16000, store["alex"])
    with s._lock:
        data = s._load()
        data["people"][store["alex"]]["clips"].append(
            {"file": fname, "seconds": 3.0, "rms": 7000, "score": 3.0,
             "sample_rate": 16000, "source": "accumulated",
             "added_at": time.time()})
        s._save(data)
    before = {p["person_id"]: p for p in s.people()}
    seconds_before = before[store["alex"]]["seconds"]
    assert voiceid.audit_banks(_cfg()) is True
    after = {p["person_id"]: p for p in s.people()}
    assert after[store["alex"]]["quarantined_count"] == 1
    assert after[store["alex"]]["noise_count"] == 1
    # sufficiency and seconds recompute without the noise clip
    assert after[store["alex"]]["seconds"] < seconds_before
    aside = [c for c in s.clips_of(store["alex"]) if c["quarantined"]]
    assert [c["quarantine_reason"] for c in aside] == ["not_speech"]
    # and enrolment never sees it
    enr = s.enrollment_clips([store["alex"]], 16000)
    assert fname not in enr[store["alex"]]["fingerprint"]


def test_audit_reinstates_speech_wrongly_set_aside(store, monkeypatch):
    """#309: real speech was set aside as "not a voice" because its dead
    air voted against it. The audit's output IS the quarantine state, so
    the corrected check brings a surviving clip back with no hand-holding
    - the installed base self-cleans on its next audit."""
    monkeypatch.setattr(voiceid, "_get_extractor", lambda cfg: object())
    monkeypatch.setattr(voiceid, "_embed", _fake_embed)
    s = store["store"]
    clip = _captured(_ALEX_VAL, 2.4, 2.0)
    fname = s._write_clip(clip, 16000, store["alex"])
    with s._lock:
        data = s._load()
        data["people"][store["alex"]]["clips"].append(
            {"file": fname, "seconds": round(len(clip) / 2 / 16000, 2),
             "rms": 2000, "score": 3.0, "sample_rate": 16000,
             "source": "accumulated", "added_at": time.time()})
        s._save(data)
    s.set_hygiene({store["alex"]: {fname: "not_speech"}}, [])
    row = next(c for c in s.clips_of(store["alex"]) if c["file"] == fname)
    assert row["quarantined"] is True
    assert voiceid.audit_banks(_cfg()) is True
    row = next(c for c in s.clips_of(store["alex"]) if c["file"] == fname)
    assert row["quarantined"] is False
    assert row["quarantine_reason"] == ""


def test_set_hygiene_accepts_both_reason_shapes(store):
    s = store["store"]
    first = s.clips_of(store["alex"])[0]["file"]
    s.set_hygiene({store["alex"]: [first]}, [])            # legacy iterable
    row = next(c for c in s.clips_of(store["alex"]) if c["file"] == first)
    assert row["quarantined"] and row["quarantine_reason"] == "contaminated"
    s.set_hygiene({store["alex"]: {first: "not_speech"}}, [])
    row = next(c for c in s.clips_of(store["alex"]) if c["file"] == first)
    assert row["quarantine_reason"] == "not_speech"
    s.set_hygiene({}, [])                                  # reinstated whole
    row = next(c for c in s.clips_of(store["alex"]) if c["file"] == first)
    assert not row["quarantined"] and row["quarantine_reason"] == ""


def test_audit_banks_is_a_noop_without_the_extractor(store):
    # conftest keeps the matcher offline: the audit must decline honestly.
    assert voiceid.audit_banks(_cfg()) is False
    assert store["store"].close_pairs() == []


def test_audit_banks_if_changed_runs_once_per_bank_shape(store, monkeypatch):
    monkeypatch.setattr(voiceid, "_get_extractor", lambda cfg: object())
    monkeypatch.setattr(voiceid, "_embed", _fake_embed)
    calls = {"n": 0}
    real = voiceid.audit_banks

    def counting(cfg, sample_rate=16000):
        calls["n"] += 1
        return real(cfg, sample_rate)

    monkeypatch.setattr(voiceid, "audit_banks", counting)
    assert voiceid.audit_banks_if_changed(_cfg()) is True
    assert voiceid.audit_banks_if_changed(_cfg()) is False  # unchanged bank
    assert calls["n"] == 1
    # a new clip changes the fingerprint - but inside the #133 cool-down
    # window the audit DEFERS (a still-learning bank changes on nearly
    # every utterance, and re-auditing each one starved the request path)
    store["store"].add_clip(store["alex"], _tone(_ALEX_VAL, 2), 16000,
                            "accumulated")
    assert voiceid.audit_banks_if_changed(_cfg()) is False
    assert calls["n"] == 1
    # once the window passes, the deferred change runs - never dropped
    monkeypatch.setattr(voiceid, "_audit_last_ran",
                        time.monotonic() - voiceid.AUDIT_MIN_INTERVAL_S - 1)
    assert voiceid.audit_banks_if_changed(_cfg()) is True
    assert calls["n"] == 2


def test_a_cold_matcher_never_spends_the_audit_attempt(store, monkeypatch):
    """#28, tenth field test: the audit memoised the bank shape even when the
    extractor was still warming, so the FIRST anchor change of a process -
    which almost always lands during warm-up - permanently consumed the one
    attempt for that shape. The live store proved the cost: a phantom
    owner-double sat there with byte-identical clips and no quarantine
    verdict ever written. A cold matcher must leave the fingerprint alone."""
    monkeypatch.setattr(voiceid, "_get_extractor", lambda cfg: None)
    assert voiceid.audit_banks_if_changed(_cfg()) is False   # cold: no audit
    assert voiceid.audit_banks_if_changed(_cfg()) is False   # still cold
    # the matcher warms up; the SAME unchanged bank shape must now audit
    monkeypatch.setattr(voiceid, "_get_extractor", lambda cfg: object())
    monkeypatch.setattr(voiceid, "_embed", _fake_embed)
    assert voiceid.audit_banks_if_changed(_cfg()) is True
    # and only once it has actually run does the shape memoise
    assert voiceid.audit_banks_if_changed(_cfg()) is False


# ============================ 2. config surface ===========================

def test_enabled_default_and_off():
    assert voiceid.enabled(_cfg()) is True
    assert voiceid.enabled(_cfg(voice_id_enabled=False)) is False
    assert voiceid.enabled({}) is True  # absent key -> default on


def test_threshold_override_and_bounds():
    assert voiceid._threshold(_cfg()) == 0.5
    assert voiceid._threshold(_cfg(voice_id_threshold=0.6)) == 0.6
    assert voiceid._threshold(_cfg(voice_id_threshold=1.5)) == 0.5   # out of range
    assert voiceid._threshold(_cfg(voice_id_threshold="x")) == 0.5   # unparseable


def test_env_overrides_map_the_new_settings():
    out = _env_overrides({"CROSSBAND_VOICE_ID_ENABLED": "false",
                          "CROSSBAND_VOICE_ID_THRESHOLD": "0.62",
                          "CROSSBAND_VOICE_ID_MARGIN": "0.2",
                          "CROSSBAND_VOICE_ID_SUFFICIENT_SECONDS": "8",
                          "CROSSBAND_VOICE_ID_MIN_SHORT_CLIPS": "3",
                          "CROSSBAND_VOICE_ID_MODEL_URL": "https://example/x.onnx"})
    assert out["voice_id_enabled"] is False
    assert out["voice_id_threshold"] == 0.62
    assert out["voice_id_margin"] == 0.2
    assert out["voice_id_sufficient_seconds"] == 8.0
    assert out["voice_id_min_short_clips"] == 3
    assert out["voice_id_model_url"] == "https://example/x.onnx"


def test_margin_override_and_bounds():
    assert voiceid._margin(_cfg()) == 0.12
    assert voiceid._margin(_cfg(voice_id_margin=0.2)) == 0.2
    assert voiceid._margin(_cfg(voice_id_margin=1.5)) == 0.12   # out of range
    assert voiceid._margin(_cfg(voice_id_margin="x")) == 0.12   # unparseable


# ============================ 2b. extractor state machine =================
# Reproduces the CI condition - sherpa-onnx installed but the model not yet
# fetched - which must resolve to "unavailable" WITHOUT raising into the pass.

def test_get_extractor_cold_with_sherpa_present_never_raises(monkeypatch):
    monkeypatch.setattr(voiceid, "sherpa_onnx", object())  # pretend installed
    spawned = {"n": 0}
    monkeypatch.setattr(voiceid, "_spawn_fetch",
                        lambda cfg: spawned.__setitem__("n", spawned["n"] + 1))
    assert voiceid._get_extractor(_cfg()) is None    # cold -> claims the warm
    assert voiceid._state == "fetching"
    assert spawned["n"] == 1
    assert voiceid._get_extractor(_cfg()) is None    # already fetching: no raise
    assert spawned["n"] == 1                          # warm claimed exactly once


def test_identify_with_sherpa_present_but_not_ready_defers(store, monkeypatch):
    # The exact CI scenario: sherpa importable, model absent, matcher cold.
    monkeypatch.setattr(voiceid, "sherpa_onnx", object())
    monkeypatch.setattr(voiceid, "_spawn_fetch", lambda cfg: None)
    v = voiceid.identify_utterance(_tone(_ALEX_VAL, 1.5), 16000,
                                   _candidates(store), _cfg())
    assert v["status"] == "defer" and v["reason"] == "unavailable"


def test_get_extractor_ready_returns_extractor(monkeypatch):
    sentinel = object()
    monkeypatch.setattr(voiceid, "sherpa_onnx", object())
    monkeypatch.setattr(voiceid, "_extractor", sentinel)
    monkeypatch.setattr(voiceid, "_state", "ready")
    assert voiceid._get_extractor(_cfg()) is sentinel


# ============================ 3. pinned-model fetch =======================

class _FakeStream:
    def __init__(self, payload):
        self.payload = payload

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def raise_for_status(self):
        return None

    def iter_bytes(self, n):
        for i in range(0, len(self.payload), n):
            yield self.payload[i:i + n]


def test_file_valid_gate(tmp_path, monkeypatch):
    monkeypatch.setattr(db, "DATA_DIR", str(tmp_path))
    p = voiceid.model_path()
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_bytes(b"hello-model")
    good = hashlib.sha256(b"hello-model").hexdigest()
    assert voiceid.file_valid(p, good) is True
    assert voiceid.file_valid(p, "0" * 64) is False      # wrong hash
    assert voiceid.file_valid(p.with_name("nope.onnx"), good) is False  # absent


def test_ensure_model_present_and_valid_no_network(tmp_path, monkeypatch):
    monkeypatch.setattr(db, "DATA_DIR", str(tmp_path))
    p = voiceid.model_path()
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_bytes(b"the-model")
    sha = hashlib.sha256(b"the-model").hexdigest()

    import httpx
    monkeypatch.setattr(httpx, "stream", lambda *a, **k:
                        (_ for _ in ()).throw(AssertionError("must not fetch")))
    assert voiceid.ensure_model(_cfg(voice_id_model_sha256=sha)) == p


def test_ensure_model_fetches_and_verifies(tmp_path, monkeypatch):
    monkeypatch.setattr(db, "DATA_DIR", str(tmp_path))
    payload = b"downloaded-model-bytes" * 100
    sha = hashlib.sha256(payload).hexdigest()
    import httpx
    monkeypatch.setattr(httpx, "stream", lambda *a, **k: _FakeStream(payload))
    path = voiceid.ensure_model(_cfg(voice_id_model_sha256=sha))
    assert path is not None and path.read_bytes() == payload
    assert oct(os.stat(path).st_mode)[-3:] == "600"     # owner-only


def test_ensure_model_refuses_hash_mismatch(tmp_path, monkeypatch):
    monkeypatch.setattr(db, "DATA_DIR", str(tmp_path))
    import httpx
    monkeypatch.setattr(httpx, "stream", lambda *a, **k: _FakeStream(b"tampered"))
    # cfg pins a DIFFERENT hash than the bytes fetched.
    path = voiceid.ensure_model(_cfg(voice_id_model_sha256="a" * 64))
    assert path is None
    assert not voiceid.model_path().exists()            # nothing installed


# ============================ 4. enrolment + flow (mocked extractor) ======

# identity codes carried in the constant PCM value of a clip/utterance
_ALEX_VAL, _SAM_VAL, _STRANGER_VAL = 1200, 6000, 12000
_VEC = {_ALEX_VAL: [1.0, 0.0, 0.0], _SAM_VAL: [0.0, 1.0, 0.0],
        _STRANGER_VAL: [0.0, 0.0, 1.0]}


def _tone(value, seconds, sr=16000):
    """Speech-shaped PCM-16 riding a constant offset: the offset is the
    identity code the fake extractor reads back (the MEAN sample value), and
    the low-band harmonics carry it past the speech gate (#217) - the old
    bare constant was DC, which the gate rightly rejects."""
    out = bytearray()
    for i in range(int(seconds * sr)):
        t = i / sr
        ac = sum(a * math.sin(2 * math.pi * f * t) for f, a in
                 ((140, 2500), (280, 1600), (420, 1000), (560, 600)))
        out += struct.pack("<h", int(max(-32000, min(32000, value + ac))))
    return bytes(out)


def _fake_embed(_ex, audio_float, _sr):
    """Map a waveform to a synthetic unit embedding by its mean sample value,
    bucketed to the nearest identity code. Stands in for the ONNX extractor."""
    n = len(audio_float)
    mean = (sum(audio_float) / n) * 32768.0 if n else 0.0
    code = min(_VEC, key=lambda k: abs(k - mean))
    return voiceid.l2_normalize(_VEC[code])


@pytest.fixture
def store(tmp_path, monkeypatch):
    monkeypatch.setattr(db, "DATA_DIR", str(tmp_path))
    anchors._store = None  # rebind to this data dir
    s = anchors.store()
    # Alex and Sam each get sufficient anchor audio. The bar is TWO-PART
    # since #28 PR-B (seconds AND short clips), so the fixture stores a mix
    # of lengths: 2 x 3s + 2 x 1.5s = 9s with two short clips each.
    alex = s.ensure_person("Alex")
    sam = s.ensure_person("Sam")
    for seconds in (3, 3, 1.5, 1.5):
        s.add_clip(alex, _tone(_ALEX_VAL, seconds), 16000, "introduction")
        s.add_clip(sam, _tone(_SAM_VAL, seconds), 16000, "introduction")
    return {"store": s, "alex": alex, "sam": sam}


def _candidates(store):
    return [{"person_id": store["alex"], "name": "Alex"},
            {"person_id": store["sam"], "name": "Sam"}]


def test_identify_confident_match(store, monkeypatch):
    monkeypatch.setattr(voiceid, "_get_extractor", lambda cfg: object())
    monkeypatch.setattr(voiceid, "_embed", _fake_embed)
    v = voiceid.identify_utterance(_tone(_ALEX_VAL, 1.5), 16000,
                                   _candidates(store), _cfg())
    assert v["status"] == "match" and v["name"] == "Alex"


def test_identify_open_set_unknown_defers(store, monkeypatch):
    monkeypatch.setattr(voiceid, "_get_extractor", lambda cfg: object())
    monkeypatch.setattr(voiceid, "_embed", _fake_embed)
    v = voiceid.identify_utterance(_tone(_STRANGER_VAL, 1.5), 16000,
                                   _candidates(store), _cfg())
    assert v["status"] == "defer" and v["reason"] == "below_threshold"


def test_identify_disabled_defers_without_embedding(store, monkeypatch):
    monkeypatch.setattr(voiceid, "_get_extractor",
                        lambda cfg: pytest.fail("must not touch the extractor"))
    v = voiceid.identify_utterance(_tone(_ALEX_VAL, 1.5), 16000,
                                   _candidates(store), _cfg(voice_id_enabled=False))
    assert v["status"] == "defer" and v["reason"] == "disabled"


def test_enrolment_embedding_is_cached_by_clip_set(store, monkeypatch):
    calls = {"n": 0}

    def counting_embed(ex, audio, sr):
        calls["n"] += 1
        return _fake_embed(ex, audio, sr)

    monkeypatch.setattr(voiceid, "_get_extractor", lambda cfg: object())
    monkeypatch.setattr(voiceid, "_embed", counting_embed)
    cands = _candidates(store)
    voiceid.identify_utterance(_tone(_ALEX_VAL, 1.5), 16000, cands, _cfg())
    first = calls["n"]
    assert first >= 7          # 3 Alex + 3 Sam enrolment clips + 1 utterance
    calls["n"] = 0
    voiceid.identify_utterance(_tone(_ALEX_VAL, 1.5), 16000, cands, _cfg())
    assert calls["n"] == 1     # enrolment served from cache; only the utterance


# ============================ 4a. the speech gate (#217) ==================
#
# Field failure 2026-08-24: a static burst embedded, matched a remembered
# bank at threshold, seated an absent person and banked itself as her voice
# sample. The gate asks "is this a voice at all" BEFORE the matcher may ask
# "whose voice" - and its defer can neither cold-start-bank nor arm.

def _noise(seconds, sr=16000, seed=7):
    """Deterministic white noise: the broadband shape of a static burst."""
    import random
    rng = random.Random(seed)
    return b"".join(struct.pack("<h", rng.randint(-12000, 12000))
                    for _ in range(int(seconds * sr)))


def _hiss(seconds, sr=16000):
    """Band-limited high-frequency noise: static without the low end."""
    out = bytearray()
    for i in range(int(seconds * sr)):
        t = i / sr
        ac = sum(3000 * math.sin(2 * math.pi * f * t + ph)
                 for f, ph in ((4100, 0.3), (4900, 1.7), (5600, 2.9),
                               (6400, 0.9), (7300, 2.1)))
        out += struct.pack("<h", int(max(-32000, min(32000, ac))))
    return bytes(out)


def test_voiced_fraction_separates_speech_from_noise():
    assert voiceid.voiced_fraction(_tone(0, 1.5), 16000) >= 0.9
    assert voiceid.voiced_fraction(_noise(1.5), 16000) <= 0.1
    assert voiceid.voiced_fraction(_hiss(1.5), 16000) <= 0.1
    assert voiceid.voiced_fraction(b"\x00\x00" * 24000, 16000) == 0.0  # silence
    assert voiceid.is_speech(_tone(0, 1.5), 16000) is True
    assert voiceid.is_speech(_noise(1.5), 16000) is False


def _captured(value, talk_seconds, tail_seconds, sr=16000):
    """Speech the way capture actually banks it (#309): bursts of voice
    separated by word gaps, then the endpoint pause the client waits out
    before committing. Under an all-frames denominator this shape - the
    ordinary shape of every stored clip - read as "not a voice"."""
    gap = b"\x00\x00" * int(0.18 * sr)
    out, made = [], 0.0
    while made < talk_seconds:
        out.append(_tone(value, 0.3, sr))
        out.append(gap)
        made += 0.48
    out.append(b"\x00\x00" * int(tail_seconds * sr))
    return b"".join(out)


def test_dead_air_does_not_vote_against_speech():
    """#309: a short remark plus the 2s endpoint pause must read as speech.
    The old whole-clip majority made this shape unpassable - at most 0.9s
    of a 3.4s clip could ever be voiced."""
    remark = _captured(0, 0.9, 2.0)
    assert voiceid.voiced_fraction(remark, 16000) >= 0.9
    assert voiceid.is_speech(remark, 16000) is True
    # the acceptance gate banks it, so a still-learning person can grow
    assert anchors.accepts_clip(anchors.clip_quality(remark, 16000))


def test_excluding_silence_does_not_launder_noise():
    """#309 regression guard on #217/#218: the audible frames of static
    are still noise-shaped, and a lone click leaves too few audible
    frames to judge at all."""
    assert voiceid.voiced_fraction(_noise(3.0) + b"\x00\x00" * 32000,
                                   16000) <= 0.1
    click = b"\x00\x00" * 24000 + _noise(0.06) + b"\x00\x00" * 24000
    assert voiceid.voiced_fraction(click, 16000) == 0.0
    faint = b"\x14\x00" * 48000   # room tone alone: nothing to judge
    assert voiceid.voiced_fraction(faint, 16000) == 0.0


def test_trim_dead_air_cuts_the_ends_and_keeps_the_margin():
    """#310: pre-roll and the endpoint pause leave the clip; the voiced
    span and a small margin stay."""
    lead, talk, tail = 0.5, 1.5, 2.0
    clip = (b"\x00\x00" * int(lead * 16000) + _tone(0, talk)
            + b"\x00\x00" * int(tail * 16000))
    out = voiceid.trim_dead_air(clip, 16000)
    kept = len(out) / 2 / 16000
    margin = voiceid.SPEECH_TRIM_MARGIN_SECONDS
    assert talk <= kept <= talk + 2 * margin + 0.1
    assert voiceid.voiced_fraction(out, 16000) >= 0.9


def test_trim_dead_air_keeps_gaps_inside_the_span():
    """Only the ends are cut - a two-burst remark keeps the word gap
    between its bursts, so stored audio is never spliced."""
    out = voiceid.trim_dead_air(_captured(0, 0.9, 2.0), 16000)
    kept = len(out) / 2 / 16000
    assert 0.85 <= kept <= 1.1   # both bursts, the gap, the margins


def test_trim_dead_air_reads_a_noisy_tail_as_dead():
    """#310: the endpoint pause in a fan-filled room is loud but not
    speech-shaped; it trims like a silent one."""
    import random
    rng = random.Random(4)
    fan = b"".join(struct.pack("<h", rng.randint(-1400, 1400))
                   for _ in range(2 * 16000))
    clip = _tone(0, 1.5) + fan
    out = voiceid.trim_dead_air(clip, 16000)
    kept = len(out) / 2 / 16000
    assert kept <= 1.5 + 2 * voiceid.SPEECH_TRIM_MARGIN_SECONDS + 0.1
    assert voiceid.is_speech(out, 16000) is True


def test_trim_dead_air_leaves_edge_shapes_alone():
    tone = _tone(0, 1.5)
    assert voiceid.trim_dead_air(tone, 16000) == tone       # gapless
    quiet = b"\x01\x00" * 48000
    assert voiceid.trim_dead_air(quiet, 16000) == quiet     # nothing voiced
    noise = _noise(2.0)
    assert voiceid.trim_dead_air(noise, 16000) == noise     # junk is the gate's job
    assert voiceid.trim_dead_air(b"", 16000) == b""         # too short to frame


def test_trim_dead_air_stdlib_fallback_agrees(monkeypatch):
    import sys
    clip = _captured(0, 0.9, 2.0)
    with_np = voiceid.trim_dead_air(clip, 16000)
    monkeypatch.setitem(sys.modules, "numpy", None)   # import now fails
    assert voiceid.trim_dead_air(clip, 16000) == with_np


def test_voiced_fraction_stdlib_fallback_agrees(monkeypatch):
    """numpy is optional (the _pcm_to_float contract): the Goertzel fallback
    must reach the same verdicts."""
    import sys
    speech, noise = _tone(0, 1.0), _noise(1.0)
    banked = _captured(0, 0.9, 2.0)   # the #309 shape: speech plus dead air
    with_np = (voiceid.voiced_fraction(speech, 16000),
               voiceid.voiced_fraction(noise, 16000),
               voiceid.voiced_fraction(banked, 16000))
    monkeypatch.setitem(sys.modules, "numpy", None)   # import now fails
    without = (voiceid.voiced_fraction(speech, 16000),
               voiceid.voiced_fraction(noise, 16000),
               voiceid.voiced_fraction(banked, 16000))
    assert without[0] >= 0.9 and without[1] <= 0.1 and without[2] >= 0.9
    assert with_np == pytest.approx(without, abs=0.1)


def test_identify_defers_non_speech_before_embedding(store, monkeypatch):
    monkeypatch.setattr(voiceid, "_get_extractor", lambda cfg: object())
    monkeypatch.setattr(voiceid, "_embed",
                        lambda *a: pytest.fail("non-speech must never embed"))
    for pcm in (_noise(1.5), _hiss(1.5)):
        v = voiceid.identify_utterance(pcm, 16000, _candidates(store), _cfg())
        assert v["status"] == "defer" and v["reason"] == "not_speech"


def test_non_speech_is_never_named_by_elimination(monkeypatch):
    """#217 acceptance: static is nobody's by elimination. The session
    naming's speech gate turns it away before any fingerprint, and a turn
    with nothing named is left listening, never the one unlearnt person."""
    from backend import voice_pass, voice_sessions
    monkeypatch.setattr(voice_sessions, "embed_live", lambda *a: pytest.fail(
        "non-speech must never be fingerprinted"))
    for pcm in (_noise(1.5), _hiss(1.5)):
        assert voice_sessions.gate(pcm, 16000) == "not_speech"
        assert voice_sessions.name_single_turn(1, pcm, 16000, _cfg()) is None
    plan = {"room_on": True, "unlearnt": ["Mateo"], "owner_known": True}
    d = voice_pass.decide(None, plan)
    assert d["labels"] == [] and not d["learning"]


def test_not_speech_reason_reaches_the_pulse():
    diarize.record_decision(991, diarize.DECISION_UNRESOLVED, 12.0,
                            "not_speech")
    assert diarize.last_decision(991)["reason"] == "not_speech"


# ============================ 4c. the sniff is retired ====================

def test_the_el_sniff_is_structurally_gone():
    """#28 PR-B: the bounded session-start EL sniff retired with the cloud
    identity path. Pinned structurally - the functions and the session
    budget no longer exist, so no code path can fire one."""
    for name in ("run_sniff", "schedule_sniff", "_sniff_plan",
                 "sniff_eligible", "SNIFF_UTTERANCES"):
        assert not hasattr(diarize, name), name
    assert not hasattr(diarize.RoomSession(), "sniff_remaining")


# ============================ 5. integration (skips without model) ========

def _real_model_path():
    """A usable local model for the integration test, or None to skip: the
    default cache path, or CROSSBAND_TEST_MODEL_PATH for a local checkout."""
    env = os.environ.get("CROSSBAND_TEST_MODEL_PATH")
    if env and os.path.exists(env):
        return env
    p = voiceid.model_path()
    return str(p) if p.exists() else None


@pytest.mark.skipif(voiceid.sherpa_onnx is None,
                    reason="sherpa-onnx wheel not installed")
def test_integration_real_extractor_embeds(tmp_path, monkeypatch):
    model = _real_model_path()
    if model is None:
        pytest.skip("speaker model not present (fetched at runtime, absent in CI)")
    ex = voiceid.sherpa_onnx.SpeakerEmbeddingExtractor(
        voiceid.sherpa_onnx.SpeakerEmbeddingExtractorConfig(
            model=model, num_threads=1, provider="cpu"))
    import struct
    pcm = b"".join(struct.pack("<h", int(8000 * (i % 7 - 3)))
                   for i in range(16000))     # 1s of a rough tone
    emb = voiceid._embed(ex, voiceid._pcm_to_float(pcm), 16000)
    assert emb is not None and len(emb) > 0
    assert abs(sum(x * x for x in emb) - 1.0) < 1e-4   # L2-normalised


# ── the banking bar (#222) ───────────────────────────────────────────────────
#
# A verdict clearing the threshold both LABELLED the turn and BANKED its
# audio, so borderline wrong matches fed the very bank that produced them -
# the compounding half of the 2026-08-24 takeover. Naming and banking are
# split bars: labelling keeps the threshold, banking demands
# voice_id_banking_extra on top. The session naming's fallback scorer
# saves clips at this bar (voice_pass.should_bank, pinned in
# test_voice_pass).

def test_score_banks_splits_the_bars():
    cfg = _cfg()   # threshold 0.5, banking extra 0.1
    assert voiceid.score_banks(0.55, cfg) is False   # labels, must not bank
    assert voiceid.score_banks(0.60, cfg) is True    # at the banking bar
    assert voiceid.score_banks(0.85, cfg) is True    # a strong match banks
    assert voiceid.score_banks(None, cfg) is False   # no score, no banking
    assert voiceid.score_banks(0.55, _cfg(voice_id_banking_extra=0)) is True
    # the knob guards like its siblings: junk keeps the default
    assert voiceid.score_banks(0.55, _cfg(voice_id_banking_extra="wat")) is False
