"""Local speaker identification (#28): THE identity path.

Since PR-B (the eighth field test's owner decision) the matcher is not an
accelerator in front of a cloud fallback - identity is local or honestly
uncertain, and the only ElevenLabs trigger left is the matcher's own "multi"
verdict (crosstalk splitting). These pins, all of which run WITHOUT the 38MB
model and WITHOUT sherpa-onnx present:

1. The pure decision seam (normalise/average/cosine + classify_utterance) -
   identify, open-set "none of the enrolled", the ambiguity margin (widened
   for flagged close pairs), and the two-voice split - proved with synthetic
   vectors, no ONNX.
2. Enrolment averages a person's stored anchor clips and caches the embedding
   keyed by the clip set, so identification re-embeds anchors only when they
   change (exercised with a MOCKED extractor - no model).
3. The pinned-model fetch verifies SHA-256 before use and refuses a mismatch,
   with no network (httpx mocked).
4. run_pass wiring (#28 PR-B): a confident match fast-labels with NO batch
   call; a solo utterance and EVERY deferred verdict fire NO batch call
   either; ONLY the "multi" verdict runs the ElevenLabs crosstalk split; and
   with the matcher disabled nothing automatic happens at all.
5. The pairwise hygiene rules (#28 PR-B): contaminated clips quarantine,
   close centroid pairs are flagged, and the audit persists both through the
   anchor store.
6. The EL sniff is RETIRED (#28 PR-B): pinned structurally - the functions
   are gone, so no code path can fire it.
7. An integration test that builds the real extractor when the model is
   present and SKIPS cleanly when it is not (the CI case).
"""

import time
import asyncio
import hashlib
import os

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
    v = voiceid.classify_utterance(voiceid.l2_normalize([0.98, 0.05, 0.0]), [], enr)
    assert v["status"] == "match" and v["name"] == "Alex" and v["person_id"] == "p1"


def test_classify_open_set_stranger_defers():
    """A voice matching nobody (below the threshold) is the open-set None."""
    enr = _enr(p1=("Alex", [1, 0, 0]), p2=("Sam", [0, 1, 0]))
    v = voiceid.classify_utterance(voiceid.l2_normalize([0.3, 0.3, 0.9]), [], enr)
    assert v["status"] == "defer" and v["reason"] == "below_threshold"


def test_classify_ambiguous_two_close_voices_defers():
    # Query sits between two enrolled people: over threshold to the best, but
    # the runner-up is too close to claim it (a blend, or two similar voices).
    enr = _enr(p1=("Alex", [1, 0, 0]), p2=("Sam", [0.95, 0.31, 0]))
    q = voiceid.l2_normalize([1.0, 0.15, 0.0])
    v = voiceid.classify_utterance(q, [], enr)
    assert v["status"] == "defer" and v["reason"] == "ambiguous"


def test_classify_multi_voice_is_the_crosstalk_verdict():
    enr = _enr(p1=("Alex", [1, 0, 0]), p2=("Sam", [0, 1, 0]))
    whole = voiceid.l2_normalize([0.98, 0.1, 0.0])          # Alex dominates
    a = voiceid.l2_normalize([1, 0, 0])
    s = voiceid.l2_normalize([0, 1, 0])
    windows = [a, a, s, s]                                  # two clear winners
    v = voiceid.classify_utterance(whole, windows, enr)
    assert v["status"] == "defer" and v["reason"] == "multi"
    assert voiceid.is_multi(v) is True   # the one batch-pass trigger (PR-B)
    assert voiceid.is_multi(voiceid.classify_utterance(whole, [], enr)) is False


def test_classify_multi_outranks_a_blended_whole_verdict():
    """#28 PR-B: the window evidence is checked FIRST. A two-voice blend
    whose whole-utterance embedding resembles nobody (below threshold) must
    still classify as "multi" - it is now the only door to the crosstalk
    split, and the blend landing exactly there was the reason the old
    confident-only pre-check had to go."""
    enr = _enr(p1=("Alex", [1, 0, 0]), p2=("Sam", [0, 1, 0]))
    blend = voiceid.l2_normalize([0.5, 0.5, 0.9])   # resembles nobody enrolled
    a = voiceid.l2_normalize([1, 0, 0])
    s = voiceid.l2_normalize([0, 1, 0])
    v = voiceid.classify_utterance(blend, [a, a, s, s], enr)
    assert v["reason"] == "multi"
    # without window evidence the same blend stays an open-set defer
    v2 = voiceid.classify_utterance(blend, [], enr)
    assert v2["reason"] == "below_threshold"


def test_classify_no_candidates_defers():
    v = voiceid.classify_utterance([1, 0, 0], [], {})
    assert v["status"] == "defer" and v["reason"] == "no_candidates"


def test_window_multi_voice_rules():
    enr = _enr(p1=("Alex", [1, 0, 0]), p2=("Sam", [0, 1, 0]))
    a = voiceid.l2_normalize([1, 0, 0])
    s = voiceid.l2_normalize([0, 1, 0])
    assert voiceid.window_multi_voice([a, a, a], enr, 0.5) is False   # one winner
    assert voiceid.window_multi_voice([a, a, s, s], enr, 0.5) is True  # two winners
    assert voiceid.window_multi_voice([a, s], enr, 0.5) is False       # one each < min
    # A single enrolled candidate can never be "multi".
    assert voiceid.window_multi_voice([a, a], {"p1": enr["p1"]}, 0.5) is False


def test_close_pair_widens_the_required_margin():
    """#28 PR-B, the hygiene guard's teeth: a margin that would win between
    unrelated voices is NOT enough when the best and runner-up are a flagged
    close pair - similar-sounding households get stricter matching, never a
    confident mistake."""
    enr = _enr(p1=("Alex", [1.0, 0.0, 0.0]), p2=("Sam", [0.9, 0.4359, 0.0]))
    q = voiceid.l2_normalize([1.0, 0.05, 0.0])
    plain = voiceid.classify_utterance(q, [], enr, margin=0.05)
    assert plain["status"] == "match" and plain["name"] == "Alex"
    strict = voiceid.classify_utterance(q, [], enr, margin=0.05,
                                        close_pairs=[("p1", "p2")])
    assert strict["status"] == "defer" and strict["reason"] == "ambiguous"
    # pair order is irrelevant, and an unrelated pair changes nothing
    strict2 = voiceid.classify_utterance(q, [], enr, margin=0.05,
                                         close_pairs=[("p2", "p1")])
    assert strict2["reason"] == "ambiguous"
    other = voiceid.classify_utterance(q, [], enr, margin=0.05,
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
    """Constant-value PCM-16 - loud and long enough to pass the anchor gate,
    and its mean value is the identity code the fake extractor reads back."""
    import struct
    return struct.pack("<h", value) * int(seconds * sr)


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


def test_identify_two_voices_defers_multi(store, monkeypatch):
    monkeypatch.setattr(voiceid, "_get_extractor", lambda cfg: object())
    monkeypatch.setattr(voiceid, "_embed", _fake_embed)
    # 5s of Alex then 3s of Sam: Alex dominates the whole embedding but the
    # windows split, so the turn defers to the batch path.
    pcm = _tone(_ALEX_VAL, 5) + _tone(_SAM_VAL, 3)
    v = voiceid.identify_utterance(pcm, 16000, _candidates(store), _cfg())
    assert v["status"] == "defer" and v["reason"] == "multi"


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


# ============================ 4b. run_pass wiring =========================

def _run(coro):
    return asyncio.get_event_loop().run_until_complete(coro) \
        if False else asyncio.run(coro)


def _plan(solo_pending=None):
    # (prefix_pcm, segments, pending, num_speakers, solo_pending,
    # remembered) as _room_plan returns. The fifth item arrived with
    # cold-start enrolment (#28): the ONE present person whose bank cannot
    # identify them yet, or None when the roster is not that shape.
    # Defaulting it to None keeps every pin below asserting exactly what it
    # always did - with no by-elimination candidate, cold start cannot
    # fire. The sixth is the remembered-first candidate list (#28,
    # fourteenth field test) - the armed pass's local candidates became
    # every sufficient remembered person; mirroring the one rostered
    # person here keeps these wiring pins byte-identical in behaviour.
    return (b"\x00\x40" * 16000,
            [{"person_id": "p1", "name": "Alex", "start": 0.0, "end": 1.0}],
            [], 2, solo_pending,
            [{"person_id": "p1", "name": "Alex"}])


def test_run_pass_fast_match_skips_batch(monkeypatch):
    monkeypatch.setattr(diarize, "_room_plan", lambda *a, **k: _plan())
    monkeypatch.setattr(diarize.voiceid, "identify_utterance",
                        lambda *a, **k: {"status": "match", "person_id": "p1",
                                         "name": "Alex", "score": 0.9,
                                         "reason": "match"})

    def no_batch(*a, **k):
        raise AssertionError("batch STT must NOT run on a confident match")
    monkeypatch.setattr(diarize.voice, "transcribe_diarized", no_batch)

    seen = {}

    async def fake_attach(chat_id, commit_ts, payload, session, turn_id=None):
        seen["payload"] = payload
        return 42
    monkeypatch.setattr(diarize, "_attach_until_deadline", fake_attach)
    monkeypatch.setattr(diarize, "_accumulate_fast_anchor", lambda *a: None)
    monkeypatch.setattr(diarize, "_meter",
                        lambda *a: pytest.fail("no metering without a batch call"))

    session = diarize.RoomSession(enabled=True)
    _run(diarize.run_pass(1, b"\x00\x40" * 32000, 16000, 1000.0, session,
                          _cfg(), turn_id="t1"))
    assert seen["payload"]["labels"] == ["Alex"]
    assert seen["payload"]["uncertain"] == []


def test_run_pass_defer_fires_no_batch_call(monkeypatch):
    """THE RETIREMENT PIN (#28 PR-B; deliberately inverts the pre-PR-B
    test_run_pass_defer_runs_batch): a deferred verdict leaves the turn
    unresolved - NO ElevenLabs call, NO label, NO metering. Every defer
    reason takes the same silent exit."""
    for reason in ("below_threshold", "ambiguous", "too_short",
                   "unavailable", "no_candidates", "no_enrolled"):
        monkeypatch.setattr(diarize, "_room_plan", lambda *a, **k: _plan())
        monkeypatch.setattr(diarize.voiceid, "identify_utterance",
                            lambda *a, _r=reason, **k: {
                                "status": "defer", "person_id": None,
                                "name": None, "score": 0.1, "reason": _r})
        monkeypatch.setattr(
            diarize.voice, "transcribe_diarized",
            lambda *a, **k: pytest.fail("no EL call on a deferred verdict"))
        monkeypatch.setattr(
            diarize, "_attach_until_deadline",
            lambda *a, **k: pytest.fail("no label write on a defer"))
        monkeypatch.setattr(
            diarize, "_meter",
            lambda *a: pytest.fail("no metering without a batch call"))
        session = diarize.RoomSession(enabled=True)
        _run(diarize.run_pass(1, b"\x00\x40" * 32000, 16000, 1000.0, session,
                              _cfg(), turn_id="t1"))


def test_run_pass_multi_verdict_runs_the_crosstalk_split(monkeypatch):
    """The ONE surviving ElevenLabs trigger (#28 PR-B): the matcher's window
    analysis heard overlapping speech, so the batch diarize call runs (with
    the anchor prefix) for per-word crosstalk splitting, and is metered."""
    monkeypatch.setattr(diarize, "_room_plan", lambda *a, **k: _plan())
    monkeypatch.setattr(diarize.voiceid, "identify_utterance",
                        lambda *a, **k: {"status": "defer", "person_id": None,
                                         "name": None, "score": 0.4,
                                         "reason": "multi"})
    ran = {"batch": False, "metered": False, "labelled": False}

    def fake_batch(wav, mime, cfg, num_speakers=None):
        ran["batch"] = True
        assert num_speakers == 2          # the roster+1 hint rides along
        return {"words": []}
    monkeypatch.setattr(diarize.voice, "transcribe_diarized", fake_batch)

    async def fake_room_label(*a, **k):
        ran["labelled"] = True
    monkeypatch.setattr(diarize, "_room_label_pass", fake_room_label)
    monkeypatch.setattr(diarize, "_meter",
                        lambda *a: ran.__setitem__("metered", True))

    session = diarize.RoomSession(enabled=True)
    _run(diarize.run_pass(1, b"\x00\x40" * 32000, 16000, 1000.0, session,
                          _cfg(), turn_id="t1"))
    assert ran == {"batch": True, "metered": True, "labelled": True}


def test_run_pass_disabled_does_nothing_automatic(monkeypatch):
    """#28 PR-B (deliberately replaces the pre-PR-B pin that disabling the
    matcher ran the EL path): with the matcher off there is NO identity at
    all - no matcher call, no ElevenLabs call, no labels. Degraded means
    manual, never wrong."""
    monkeypatch.setattr(diarize, "_room_plan", lambda *a, **k: _plan())
    monkeypatch.setattr(diarize.voiceid, "identify_utterance",
                        lambda *a, **k: pytest.fail("matcher must be untouched"))
    monkeypatch.setattr(diarize.voice, "transcribe_diarized",
                        lambda *a, **k: pytest.fail("no EL call with the matcher off"))
    monkeypatch.setattr(diarize, "_meter",
                        lambda *a: pytest.fail("no metering either"))

    session = diarize.RoomSession(enabled=True)
    _run(diarize.run_pass(1, b"\x00\x40" * 32000, 16000, 1000.0, session,
                          _cfg(voice_id_enabled=False), turn_id="t1"))


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


# ── the pending-present bump (#81) ───────────────────────────────────────────

def test_pending_extra_defers_the_borderline_match():
    """Over the ordinary bar, under the pending-present one: exactly the
    shape a new guest mislabelled as a remembered person takes - defers,
    named distinctly, so elimination can bank the pending person."""
    enr = _enr(p1=("Alex", [1, 0, 0]))
    q = voiceid.l2_normalize([0.66, 0.75, 0.0])   # cosine ~0.66 to Alex... 
    v = voiceid.classify_utterance(q, [], enr, threshold=0.6,
                                   pending_extra=0.08)
    assert v["status"] == "defer" and v["reason"] == "pending_present"
    # the same score names confidently once nobody is pending
    v2 = voiceid.classify_utterance(q, [], enr, threshold=0.6)
    assert v2["status"] == "match" and v2["name"] == "Alex"


def test_pending_extra_never_blocks_a_strong_match():
    enr = _enr(p1=("Alex", [1, 0, 0]))
    q = voiceid.l2_normalize([0.98, 0.05, 0.0])
    v = voiceid.classify_utterance(q, [], enr, threshold=0.6,
                                   pending_extra=0.08)
    assert v["status"] == "match" and v["name"] == "Alex"


def test_pending_extra_zero_is_the_old_behaviour():
    enr = _enr(p1=("Alex", [1, 0, 0]))
    q = voiceid.l2_normalize([0.66, 0.75, 0.0])
    v = voiceid.classify_utterance(q, [], enr, threshold=0.6, pending_extra=0.0)
    assert v["status"] == "match"
