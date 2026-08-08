"""Local speaker identification (#28 part 2): the offline fast identity path.

The matcher is an ACCELERATOR that must never break voice: on any doubt it
defers and the pass runs the unchanged ElevenLabs diarize path. These pins,
all of which run WITHOUT the 38MB model and WITHOUT sherpa-onnx present:

1. The pure decision seam (normalise/average/cosine + classify_utterance) -
   identify, open-set "none of the enrolled", the ambiguity margin, and the
   two-voice split - proved with synthetic vectors, no ONNX.
2. Enrolment averages a person's stored anchor clips and caches the embedding
   keyed by the clip set, so identification re-embeds anchors only when they
   change (exercised with a MOCKED extractor - no model).
3. The pinned-model fetch verifies SHA-256 before use and refuses a mismatch,
   with no network (httpx mocked).
4. run_pass takes the fast path on a confident match (NO batch call) and the
   ElevenLabs path on a defer or when disabled - the additive/fallback contract.
5. An integration test that builds the real extractor when the model is present
   and SKIPS cleanly when it is not (the CI case).
"""

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


def test_classify_multi_voice_defers_to_batch():
    enr = _enr(p1=("Alex", [1, 0, 0]), p2=("Sam", [0, 1, 0]))
    whole = voiceid.l2_normalize([0.98, 0.1, 0.0])          # Alex dominates
    a = voiceid.l2_normalize([1, 0, 0])
    s = voiceid.l2_normalize([0, 1, 0])
    windows = [a, a, s, s]                                  # two clear winners
    v = voiceid.classify_utterance(whole, windows, enr)
    assert v["status"] == "defer" and v["reason"] == "multi"


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
                          "CROSSBAND_VOICE_ID_MODEL_URL": "https://example/x.onnx"})
    assert out["voice_id_enabled"] is False
    assert out["voice_id_threshold"] == 0.62
    assert out["voice_id_model_url"] == "https://example/x.onnx"


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
    # Alex and Sam each get sufficient (>=6s) anchor audio.
    alex = s.ensure_person("Alex")
    sam = s.ensure_person("Sam")
    for _ in range(3):
        s.add_clip(alex, _tone(_ALEX_VAL, 3), 16000, "introduction")
        s.add_clip(sam, _tone(_SAM_VAL, 3), 16000, "introduction")
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


def _plan():
    # (prefix_pcm, segments, pending, num_speakers) as _room_plan returns
    return (b"\x00\x40" * 16000,
            [{"person_id": "p1", "name": "Alex", "start": 0.0, "end": 1.0}],
            [], 2)


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


def test_run_pass_defer_runs_batch(monkeypatch):
    monkeypatch.setattr(diarize, "_room_plan", lambda *a, **k: _plan())
    monkeypatch.setattr(diarize.voiceid, "identify_utterance",
                        lambda *a, **k: {"status": "defer", "person_id": None,
                                         "name": None, "score": 0.1,
                                         "reason": "below_threshold"})
    ran = {"batch": False}

    def fake_batch(*a, **k):
        ran["batch"] = True
        return {"words": []}
    monkeypatch.setattr(diarize.voice, "transcribe_diarized", fake_batch)

    async def fake_room_label(*a, **k):
        return None
    monkeypatch.setattr(diarize, "_room_label_pass", fake_room_label)
    monkeypatch.setattr(diarize, "_meter", lambda *a: None)

    session = diarize.RoomSession(enabled=True)
    _run(diarize.run_pass(1, b"\x00\x40" * 32000, 16000, 1000.0, session,
                          _cfg(), turn_id="t1"))
    assert ran["batch"] is True   # the unchanged ElevenLabs path ran


def test_run_pass_disabled_never_calls_matcher(monkeypatch):
    monkeypatch.setattr(diarize, "_room_plan", lambda *a, **k: _plan())
    monkeypatch.setattr(diarize.voiceid, "identify_utterance",
                        lambda *a, **k: pytest.fail("matcher must be untouched"))
    ran = {"batch": False}
    monkeypatch.setattr(diarize.voice, "transcribe_diarized",
                        lambda *a, **k: ran.__setitem__("batch", True) or {"words": []})

    async def fake_room_label(*a, **k):
        return None
    monkeypatch.setattr(diarize, "_room_label_pass", fake_room_label)
    monkeypatch.setattr(diarize, "_meter", lambda *a: None)

    session = diarize.RoomSession(enabled=True)
    _run(diarize.run_pass(1, b"\x00\x40" * 32000, 16000, 1000.0, session,
                          _cfg(voice_id_enabled=False), turn_id="t1"))
    assert ran["batch"] is True


# ============================ 4c. run_sniff wiring ========================

def _sniff_plan_stub():
    # (prefix_pcm, segments, num_speakers) as _sniff_plan returns
    return (b"\x00\x40" * 16000,
            [{"person_id": "p1", "name": "Sam", "start": 0.0, "end": 1.0}], 2)


def test_run_sniff_fast_nonowner_arms_and_skips_batch(monkeypatch):
    monkeypatch.setattr(diarize, "_sniff_plan", lambda *a, **k: _sniff_plan_stub())
    monkeypatch.setattr(diarize.voiceid, "identify_utterance",
                        lambda *a, **k: {"status": "match", "person_id": "p1",
                                         "name": "Sam", "score": 0.9,
                                         "reason": "match"})
    monkeypatch.setattr(diarize.voice, "transcribe_diarized",
                        lambda *a, **k: pytest.fail("sniff must not batch on a match"))
    armed = {}
    monkeypatch.setattr(diarize, "_arm_from_sniff",
                        lambda chat_id, matched, cfg: armed.update(matched))

    async def fake_attach(*a, **k):
        return 7
    monkeypatch.setattr(diarize, "_attach_until_deadline", fake_attach)
    monkeypatch.setattr(diarize, "_accumulate_fast_anchor", lambda *a: None)

    session = diarize.RoomSession(enabled=False)
    session.sniff_remaining = 1
    _run(diarize.run_sniff(1, b"\x00\x40" * 32000, 16000, 1000.0, session,
                           _cfg(user_name="Alex"), turn_id="t1"))
    assert armed == {"local": "Sam"}         # armed with the matched guest
    assert session.sniff_remaining == 0


def test_run_sniff_fast_owner_match_does_not_arm(monkeypatch):
    monkeypatch.setattr(diarize, "_sniff_plan", lambda *a, **k: _sniff_plan_stub())
    monkeypatch.setattr(diarize.voiceid, "identify_utterance",
                        lambda *a, **k: {"status": "match", "person_id": "p0",
                                         "name": "Alex", "score": 0.9,
                                         "reason": "match"})
    monkeypatch.setattr(diarize.voice, "transcribe_diarized",
                        lambda *a, **k: pytest.fail("owner match must not batch"))
    monkeypatch.setattr(diarize, "_arm_from_sniff",
                        lambda *a, **k: pytest.fail("owner alone must not arm room mode"))

    session = diarize.RoomSession(enabled=False)
    session.sniff_remaining = 1
    _run(diarize.run_sniff(1, b"\x00\x40" * 32000, 16000, 1000.0, session,
                           _cfg(user_name="Alex"), turn_id="t1"))


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
