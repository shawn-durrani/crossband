"""Offline local speaker identification (#28 part 2): the FAST identity path.

The problem this solves, measured on the issue (night test 4): the ElevenLabs
diarize batch call costs commit + 1.0-1.9s, but a round's first responder reads
the transcript at commit + 0.3-0.9s - so the label loses the race and the seat
reads the turn before its name exists. This module names a confident single
speaker LOCALLY in roughly commit + 100-300ms (a 33-59ms embedding plus, only
when two people could be present, a few short-window embeddings), so the label
usually lands BEFORE the first responder and the pending-identity head (#28,
night test 4) resolves to a real name on the current turn. It also ends room
mode's doubled transcription cost: the batch call runs only when the local
matcher sees more than one voice (crosstalk) or cannot decide.

THE CORE LAW, absolute: this adds ZERO latency to the live voice path. It runs
ONLY inside diarize.py's already-never-awaited fire-and-forget pass; nothing a
round dispatches on ever awaits it. Every embedding runs on a worker thread via
the caller's asyncio.to_thread, exactly like the batch call it may replace.

PURELY ADDITIVE, degrades to today's behaviour on any doubt. If sherpa-onnx is
not installed, or the model has not been fetched yet, or the utterance is not a
confident single known speaker, identify_utterance returns a DEFER verdict and
the pass falls back to the unchanged ElevenLabs diarize path. The feature can
never break voice: the worst case is the behaviour crossband already shipped.

Privacy: embeddings are derived locally from the anchor clips crossband already
stores (backend/anchors.py); nothing about a voice is sent anywhere. The model
runs fully offline after a one-time fetch of the pinned public model file.

Design map:
  * Pure math seam (no third-party imports, no ONNX) - normalise/average/cosine
    and the whole identify+open-set+multi-voice DECISION (classify_utterance).
    This is what the keyless unit tests exercise with synthetic vectors, no
    model present.
  * Impure edges (guarded) - the sherpa-onnx extractor wrapper, the pinned
    model fetch-and-verify, and the enrolment cache. All degrade to "matcher
    unavailable" rather than raising into the pass.
"""

import hashlib
import logging
import math
import os
import threading
from pathlib import Path

from . import anchors, db

log = logging.getLogger("crossband.voiceid")

# sherpa-onnx is small and pure (it bundles its own runtime, no torch) but it is
# an optional wheel: a missing one must degrade the matcher, never crash import.
try:  # pragma: no cover - import-guard, exercised by absence in CI
    import sherpa_onnx  # type: ignore
except Exception:  # ImportError, or a broken native load
    sherpa_onnx = None  # type: ignore

# ---- the pinned model (#28 part 2) --------------------------------------
#
# nemo_en_titanet_small (NVIDIA NeMo TitaNet-Small, CC-BY-4.0), the cleanest
# same-vs-impostor separation of the four models benchmarked on the issue and
# the smallest strong one. Delivered from the official sherpa-onnx speaker
# recognition release; NEVER committed (38MB), fetched once to the data dir and
# SHA-256-verified before use. The URL and hash both pin - override BOTH via
# config together (a URL override checked against the old hash simply fails
# verification and the matcher stays unavailable, which is the safe direction).
MODEL_FILENAME = "nemo_en_titanet_small.onnx"
MODEL_URL = ("https://github.com/k2-fsa/sherpa-onnx/releases/download/"
             "speaker-recongition-models/nemo_en_titanet_small.onnx")
MODEL_SHA256 = "ad4a1802485d8b34c722d2a9d04249662f2ece5d28a7a039063ca22f515a789e"
MODEL_BYTES = 40257283  # the release asset's exact size; a cheap early reject
MODELS_DIR_NAME = "voice_models"
NUM_THREADS = 2  # embedding threads; off the live path, so tuned for a shared box

# ---- decision constants (calibrated locally; see the sim numbers below) ---
#
# Cosine threshold for "this is an enrolled voice". The published TitaNet-Small
# verification EER is 1.15%; on THIS machine, over the sherpa-onnx sample clips
# (scratchpad/spkbench/sim.py + calib.py), same-speaker cosine measured
# 0.63-0.73 against averaged enrolment while the best impostor stayed 0.12-0.31
# - a wide gap. 0.5 sits squarely in it. Overridable via CROSSBAND_VOICE_ID_
# THRESHOLD.
DEFAULT_THRESHOLD = 0.5
# The best enrolled match must beat the runner-up by this cosine margin to be
# claimed. True single-speaker margins measured 0.32-0.55; a two-voice blend or
# a stranger sits near the threshold with a small margin, so this rejects the
# ambiguous cases to the batch path without ever rejecting a clean match.
MATCH_MARGIN = 0.12
# Short-window multi-voice detection. A single speaker's 1.5s windows can be as
# little as 0.19 cosine apart (calib.py), so raw window cohesion is NOT a usable
# split signal; instead we count DISTINCT enrolled people that each WIN
# >= MIN_STRONG_WINDOWS windows above the threshold. A two-speaker utterance
# splits cleanly into two winners (calib.py concat mixes); a single speaker only
# ever has one winner, its impostors staying well under the threshold.
WINDOW_SECONDS = 1.5
WINDOW_HOP_SECONDS = 0.75
MIN_STRONG_WINDOWS = 2
MIN_IDENTIFY_SECONDS = 0.8       # shorter utterances carry too little voice; defer
MIN_WINDOW_UTTERANCE_SECONDS = 2.25  # need >= 2 windows before a split is meaningful

ENROLL_CACHE_MAX = 64  # per-person averaged embeddings kept, keyed by clip set

# A verdict's status. "match" means skip the batch call and name this turn;
# "defer" means run today's ElevenLabs diarize path unchanged.
MATCH = "match"
DEFER = "defer"


# ================= pure math seam (no numpy, no ONNX) =====================
# Everything below operates on plain lists of floats, so the keyless suite can
# pin the identify / threshold / averaging / open-set / multi-voice logic with
# synthetic vectors and no model.

def l2_normalize(vec):
    """Unit-length copy of a vector; a zero vector comes back unchanged."""
    norm = math.sqrt(sum(x * x for x in vec))
    if norm == 0.0:
        return [float(x) for x in vec]
    return [x / norm for x in vec]


def average_embeddings(vecs):
    """The enrolment embedding: mean of the per-clip vectors, each L2-normalised
    first so a loud clip does not outvote a quiet one, then the mean itself
    re-normalised. Returns None for an empty list."""
    vecs = [v for v in vecs if v]
    if not vecs:
        return None
    dim = len(vecs[0])
    acc = [0.0] * dim
    for v in vecs:
        nv = l2_normalize(v)
        for i in range(dim):
            acc[i] += nv[i]
    acc = [a / len(vecs) for a in acc]
    return l2_normalize(acc)


def cosine(a, b):
    """Cosine similarity, robust to un-normalised input (0.0 if either is a
    zero vector or the lengths differ)."""
    if not a or not b or len(a) != len(b):
        return 0.0
    dot = sum(x * y for x, y in zip(a, b))
    na = math.sqrt(sum(x * x for x in a))
    nb = math.sqrt(sum(x * x for x in b))
    if na == 0.0 or nb == 0.0:
        return 0.0
    return dot / (na * nb)


def best_two(query, enrolled):
    """Best and runner-up enrolled matches for a query embedding.
    `enrolled`: {person_id: {"name", "emb"}}. Returns
    (best_pid, best_score, second_score); second_score is -1.0 with one
    candidate, and best_pid is None with none."""
    scored = sorted(((cosine(query, e["emb"]), pid)
                     for pid, e in enrolled.items()), reverse=True)
    if not scored:
        return None, -1.0, -1.0
    best_score, best_pid = scored[0]
    second = scored[1][0] if len(scored) > 1 else -1.0
    return best_pid, best_score, second


def _defer(reason, score=0.0):
    return {"status": DEFER, "person_id": None, "name": None,
            "score": round(score, 4), "reason": reason}


def window_multi_voice(window_embs, enrolled, threshold,
                       min_strong=MIN_STRONG_WINDOWS):
    """Do the utterance's short windows show TWO OR MORE distinct enrolled
    voices? Each window votes for its best enrolled match if that match clears
    the threshold; two different people each winning >= min_strong windows is a
    genuine multi-speaker turn (crosstalk), which belongs on the batch path so
    its word-splitting and ordinals are preserved. Pure and unit-tested."""
    if len(enrolled) < 2:
        return False
    wins = {}
    for w in window_embs:
        pid, score, _ = best_two(w, enrolled)
        if pid is not None and score >= threshold:
            wins[pid] = wins.get(pid, 0) + 1
    strong = [pid for pid, n in wins.items() if n >= min_strong]
    return len(strong) >= 2


def classify_utterance(whole_emb, window_embs, enrolled, threshold=DEFAULT_THRESHOLD,
                       margin=MATCH_MARGIN, min_strong=MIN_STRONG_WINDOWS):
    """THE DECISION, pure and fully testable with synthetic vectors.

    - no enrolled candidates -> defer ("no_candidates")
    - best match below the threshold -> defer ("below_threshold": a stranger,
      an insufficiently-anchored person, or a two-voice blend that resembles
      nobody). This is the open-set "none of the enrolled" verdict.
    - best match too close to the runner-up -> defer ("ambiguous")
    - two enrolled voices across the windows -> defer ("multi")
    - otherwise -> a confident single match, named.
    Every defer routes the pass to the unchanged ElevenLabs diarize path."""
    if not enrolled:
        return _defer("no_candidates")
    best_pid, best_score, second = best_two(whole_emb, enrolled)
    if best_score < threshold:
        return _defer("below_threshold", best_score)
    if (best_score - second) < margin:
        return _defer("ambiguous", best_score)
    if window_multi_voice(window_embs, enrolled, threshold, min_strong):
        return _defer("multi", best_score)
    return {"status": MATCH, "person_id": best_pid,
            "name": enrolled[best_pid]["name"], "score": round(best_score, 4),
            "reason": "match"}


def matched(verdict) -> bool:
    return bool(verdict) and verdict.get("status") == MATCH


# ================= config helpers =========================================

def enabled(cfg) -> bool:
    """Feature flag (CROSSBAND_VOICE_ID_ENABLED, default true). When false the
    pass is byte-for-byte today's ElevenLabs-only path."""
    val = (cfg or {}).get("voice_id_enabled", True)
    return bool(val)


def _threshold(cfg) -> float:
    try:
        t = float((cfg or {}).get("voice_id_threshold") or DEFAULT_THRESHOLD)
    except (TypeError, ValueError):
        return DEFAULT_THRESHOLD
    return t if 0.0 < t < 1.0 else DEFAULT_THRESHOLD


def _model_url(cfg) -> str:
    return (cfg or {}).get("voice_id_model_url") or MODEL_URL


def _model_sha(cfg) -> str:
    return ((cfg or {}).get("voice_id_model_sha256") or MODEL_SHA256).strip().lower()


# ================= the model file (fetch + verify) ========================

def _models_dir() -> Path:
    return Path(db.DATA_DIR) / MODELS_DIR_NAME


def model_path() -> Path:
    return _models_dir() / MODEL_FILENAME


def _sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def file_valid(path: Path, sha: str) -> bool:
    """Is `path` the pinned model? Present and its SHA-256 matches. This is the
    verification gate every use passes through - a corrupt or wrong file is
    treated as absent, never loaded."""
    try:
        return path.exists() and _sha256_file(path) == sha
    except OSError:
        return False


def ensure_model(cfg) -> Path | None:
    """Return the verified model path, fetching it once if needed. Owner-only
    posture (dir 0o700, file 0o600), atomic install (temp + os.replace), SHA-256
    verified before the file is put in place AND on every reuse. Returns None on
    any failure - the matcher then reports unavailable and the pass falls back.
    Blocking (hashes/downloads 38MB); ALWAYS called on a worker thread."""
    sha = _model_sha(cfg)
    path = model_path()
    if file_valid(path, sha):
        return path
    try:
        os.makedirs(_models_dir(), exist_ok=True)
        os.chmod(_models_dir(), 0o700)
    except OSError:
        log.warning("voiceid: cannot create the model dir; matcher unavailable",
                    exc_info=True)
        return None
    tmp = path.with_suffix(f".onnx.{os.getpid()}.tmp")
    url = _model_url(cfg)
    try:
        import httpx
        h = hashlib.sha256()
        size = 0
        with open(tmp, "wb") as out:
            with httpx.stream("GET", url, follow_redirects=True,
                              timeout=300.0) as r:
                r.raise_for_status()
                for chunk in r.iter_bytes(1 << 20):
                    out.write(chunk)
                    h.update(chunk)
                    size += len(chunk)
        digest = h.hexdigest()
        if digest != sha:
            log.warning("voiceid: fetched model failed SHA-256 pin "
                        "(got %s, %d bytes); refusing it", digest, size)
            _quiet_unlink(tmp)
            return None
        os.chmod(tmp, 0o600)
        os.replace(tmp, path)
        log.info("voiceid: model fetched and verified (%d bytes)", size)
        return path
    except Exception:
        log.warning("voiceid: model fetch failed; matcher stays on the EL path",
                    exc_info=True)
        _quiet_unlink(tmp)
        return None


def _quiet_unlink(path: Path):
    try:
        os.remove(path)
    except OSError:
        pass


# ================= the extractor (warm once, in the background) ===========
#
# State machine, process-global: cold -> fetching -> (ready | unavailable).
# The fetch+build runs on ONE background daemon thread so no pass ever blocks on
# the 38MB download; until it is ready, identify_utterance defers to the EL path.
# Sticky terminal states mean we never hammer the network: a restart re-attempts.

_lock = threading.Lock()
_embed_lock = threading.Lock()  # serialise native inference across sessions
_extractor = None
_state = "cold"
_enroll_cache = {}  # (person_id, clip-file tuple) -> averaged, normalised embedding


def _spawn_fetch(cfg):
    """Start the one-time background warm. Isolated so tests can neutralise the
    network by stubbing exactly this function (see tests/conftest.py)."""
    threading.Thread(target=_warm, args=(dict(cfg or {}),),
                     name="voiceid-warm", daemon=True).start()


def _warm(cfg):
    global _extractor, _state
    try:
        path = ensure_model(cfg)
        if path is None:
            _set_state("unavailable")
            return
        ex = sherpa_onnx.SpeakerEmbeddingExtractor(
            sherpa_onnx.SpeakerEmbeddingExtractorConfig(
                model=str(path), num_threads=NUM_THREADS, provider="cpu"))
        with _lock:
            _extractor = ex
            _state = "ready"
        log.info("voiceid: matcher ready (local identification active)")
    except Exception:
        log.warning("voiceid: extractor build failed; matcher unavailable",
                    exc_info=True)
        _set_state("unavailable")


def _set_state(state):
    global _state
    with _lock:
        _state = state


def _get_extractor(cfg):
    """The ready extractor, or None while cold/fetching/unavailable. On the very
    first cold call it kicks off the background warm and returns None - the pass
    defers to the EL path until the matcher is ready. Never blocks, never raises."""
    global _state
    if sherpa_onnx is None:
        return None
    with _lock:
        state = _state
        if state == "ready":
            return _extractor
        if state in ("fetching", "unavailable"):
            return None
        _state = "fetching"  # claim the warm exactly once
    _spawn_fetch(cfg)
    return None


# ================= embedding + enrolment ==================================

def _pcm_to_float(pcm: bytes):
    """PCM-16 mono bytes -> the float32 sequence sherpa-onnx wants. Uses numpy
    when present (the proven path; sherpa's ecosystem ships it), else the
    stdlib array module - so the conversion never hard-depends on numpy."""
    usable = len(pcm) - (len(pcm) % 2)
    if usable <= 0:
        return None
    try:
        import numpy as np
        return (np.frombuffer(pcm[:usable], dtype="<i2").astype(np.float32)
                / 32768.0)
    except Exception:
        import array
        a = array.array("h")
        a.frombytes(pcm[:usable])
        return array.array("f", (x / 32768.0 for x in a))


def _embed(ex, audio_float, sample_rate):
    """One L2-normalised embedding for a float waveform. Serialised across
    sessions - native inference is cheap (tens of ms) but need not be assumed
    reentrant. Returns None on any failure."""
    if audio_float is None or len(audio_float) == 0:
        return None
    try:
        with _embed_lock:
            stream = ex.create_stream()
            stream.accept_waveform(sample_rate=sample_rate, waveform=audio_float)
            stream.input_finished()
            vec = list(ex.compute(stream))
        return l2_normalize(vec) if vec else None
    except Exception:
        log.debug("voiceid: embedding failed", exc_info=True)
        return None


def _windows(audio_float, sample_rate):
    """Overlapping fixed windows over the utterance for multi-voice detection."""
    win = int(WINDOW_SECONDS * sample_rate)
    hop = int(WINDOW_HOP_SECONDS * sample_rate)
    if win <= 0 or hop <= 0 or len(audio_float) < win:
        return []
    out = []
    i = 0
    while i + win <= len(audio_float):
        out.append(audio_float[i:i + win])
        i += hop
    return out


def _enrolled_embeddings(candidates, sample_rate, ex):
    """{person_id: {"name", "emb"}} for the candidate people, from their stored
    anchor clips. Averaged per person and CACHED keyed by the person's kept clip
    set, so identification re-embeds a person only when their anchors actually
    change - not on every utterance (mirrors the phase-1 prefix cache intent).
    Candidates whose clips are unreadable or insufficient are simply omitted."""
    ids = [c["person_id"] for c in candidates if c.get("person_id")]
    if not ids:
        return {}
    clips = anchors.store().enrollment_clips(ids, sample_rate)
    names = {c["person_id"]: c.get("name") for c in candidates}
    out = {}
    for pid, info in clips.items():
        key = (pid, info["fingerprint"])
        emb = _enroll_cache.get(key)
        if emb is None:
            per_clip = []
            for pcm in info["pcms"]:
                v = _embed(ex, _pcm_to_float(pcm), sample_rate)
                if v:
                    per_clip.append(v)
            emb = average_embeddings(per_clip)
            if emb is None:
                continue
            _enroll_cache[key] = emb
            while len(_enroll_cache) > ENROLL_CACHE_MAX:
                _enroll_cache.pop(next(iter(_enroll_cache)))
        out[pid] = {"name": names.get(pid) or info["name"], "emb": emb}
    return out


# ================= the public entry point =================================

def identify_utterance(pcm, sample_rate, candidates, cfg):
    """Name a confident single enrolled speaker for one utterance, or defer.

    `candidates`: [{"person_id", "name"}, ...] - the SUFFICIENT present people
    the pass already computed (diarize._room_plan's prefix segments). Returns a
    verdict dict {status, person_id, name, score, reason}; matched(verdict) is
    the pass's branch. NEVER raises and NEVER blocks the live path: it runs on
    the pass's worker thread, and any doubt or failure is a DEFER to today's
    ElevenLabs path. The first call in a process kicks off the one-time model
    fetch in the background and defers until it is ready."""
    if not enabled(cfg):
        return _defer("disabled")
    if not candidates:
        return _defer("no_candidates")
    ex = _get_extractor(cfg)
    if ex is None:
        return _defer("unavailable")
    sr = sample_rate or 16000
    seconds = len(pcm) / 2 / sr
    if seconds < MIN_IDENTIFY_SECONDS:
        return _defer("too_short")
    try:
        enrolled = _enrolled_embeddings(candidates, sr, ex)
    except Exception:
        log.debug("voiceid: enrolment failed", exc_info=True)
        return _defer("unavailable")
    if not enrolled:
        return _defer("no_enrolled")
    audio = _pcm_to_float(pcm)
    whole = _embed(ex, audio, sr)
    if whole is None:
        return _defer("unavailable")
    threshold = _threshold(cfg)
    # Cheap pre-check before spending window embeddings: only a confident,
    # unambiguous whole-utterance match is worth the multi-voice split test.
    _, best_score, second = best_two(whole, enrolled)
    if best_score < threshold or (best_score - second) < MATCH_MARGIN:
        return classify_utterance(whole, [], enrolled, threshold)
    windows = []
    if len(enrolled) >= 2 and seconds >= MIN_WINDOW_UTTERANCE_SECONDS:
        windows = [e for e in (_embed(ex, w, sr) for w in _windows(audio, sr))
                   if e is not None]
    return classify_utterance(whole, windows, enrolled, threshold)


# ---- test seam ----------------------------------------------------------

def _reset_for_tests():
    """Return the module to cold and clear caches. Used by the test harness so
    the process-global matcher never leaks a ready extractor between tests."""
    global _extractor, _state
    with _lock:
        _extractor = None
        _state = "cold"
    _enroll_cache.clear()
