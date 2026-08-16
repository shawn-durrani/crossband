"""Offline local speaker identification (#28): THE identity path.

Since PR-B (#28, eighth field test) this module is not an accelerator in
front of a cloud fallback - it is the only way a voice ever gets a name.
The owner decision on the issue: identity is local or honestly uncertain,
full stop. The matcher names a confident single enrolled speaker in roughly
commit + 100-300ms (a 33-59ms embedding plus a few short-window embeddings);
anything it cannot decide leaves the turn UNRESOLVED - the seats' honest
"cannot determine" state - and NO ElevenLabs pass ever fires because the
matcher deferred. The batch diarize call survives with exactly one job:
per-word crosstalk splitting when this module's window analysis returns the
"multi" verdict (genuinely overlapping speech), which is also room mode's
only remaining cloud spend.

THE CORE LAW, absolute: this adds ZERO latency to the live voice path. It runs
ONLY inside diarize.py's already-never-awaited fire-and-forget passes; nothing
a round dispatches on ever awaits it. Every embedding runs on a worker thread
via the caller's asyncio.to_thread.

Degradation is HONEST, never wrong. If sherpa-onnx is not installed, or the
model has not been fetched yet, identify_utterance defers and the turn simply
stays unnamed - and automatic voice arming does not happen at all
(introductions, spoken commands and the toggle still arm, so degraded means
manual). A wrong name asserted by a cloud pass is structurally impossible;
the worst case is uncertainty, stated as such.

Privacy: embeddings are derived locally from the anchor clips crossband already
stores (backend/anchors.py); nothing about a voice is sent anywhere. The model
runs fully offline after a one-time fetch of the pinned public model file.

Design map:
  * Pure math seam (no third-party imports, no ONNX) - normalise/average/cosine,
    the identify+open-set+multi-voice DECISION (classify_utterance), and the
    pairwise hygiene rules (#28 PR-B: quarantine_verdicts /
    close_centroid_pairs). This is what the keyless unit tests exercise with
    synthetic vectors, no model present.
  * Impure edges (guarded) - the sherpa-onnx extractor wrapper, the pinned
    model fetch-and-verify, the enrolment cache, and the bank audit
    (audit_banks). All degrade to "matcher unavailable" / no-op rather than
    raising into the pass.
"""

import hashlib
import logging
import math
import os
import threading
import time
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
# a stranger sits near the threshold with a small margin, so this leaves the
# ambiguous cases honestly unresolved without ever rejecting a clean match.
# Overridable via CROSSBAND_VOICE_ID_MARGIN (#28 PR-B).
MATCH_MARGIN = 0.12
# Short-window multi-voice detection. A single speaker's 1.5s windows can be as
# little as 0.19 cosine apart (calib.py), so raw window cohesion is NOT a usable
# split signal; instead we count DISTINCT enrolled people that each WIN
# >= MIN_STRONG_WINDOWS windows above the threshold. A two-speaker utterance
# splits cleanly into two winners (calib.py concat mixes); a single speaker only
# ever has one winner, its impostors staying well under the threshold.
# The pending-present bump (#81). When someone on the roster is still
# anchor-pending, the open-set risk changes shape: the very person most
# likely to be speaking has NO bank to score against, so a borderline
# cosine to a remembered person is exactly how a new guest's turns get
# confidently mislabelled as someone else - and her own seat never banks a
# first clip. Raising the naming bar by this much while a pending seat
# exists keeps genuine matches (measured 0.63-0.73 on this machine) passing
# and pushes the borderline impostor case to an honest defer, which is what
# lets elimination bank the pending person. Overridable via
# voice_id_pending_extra; 0 disables.
PENDING_EXTRA_THRESHOLD = 0.08
WINDOW_SECONDS = 1.5
WINDOW_HOP_SECONDS = 0.75
MIN_STRONG_WINDOWS = 2
# Short-utterance floor (#28 PR-B): was 0.8s flat. Second-long interjections
# are exactly what the two-part bank bar exists to identify, so utterances
# down to 0.5s are now embedded WHERE QUALITY ALLOWS - below
# SHORT_IDENTIFY_SECONDS the utterance must also clear an RMS floor, because
# a faint half-second blip genuinely carries too little voice to judge.
MIN_IDENTIFY_SECONDS = 0.5
SHORT_IDENTIFY_SECONDS = 0.8
MIN_SHORT_IDENTIFY_RMS = 500
MIN_WINDOW_UTTERANCE_SECONDS = 2.25  # need >= 2 windows before a split is meaningful

# Pairwise hygiene (#28 PR-B, sixth/eighth field tests). Two enrolled
# centroids whose cosine reaches CLOSE_PAIR_COSINE sound alike enough that
# the audit flags the pair (locally the best impostor measured 0.12-0.31, so
# 0.6 is far outside honest-stranger territory) and the matcher demands
# CLOSE_PAIR_EXTRA_MARGIN more separation before naming either of them.
CLOSE_PAIR_COSINE = 0.6
CLOSE_PAIR_EXTRA_MARGIN = 0.10

ENROLL_CACHE_MAX = 64  # per-person averaged embeddings kept, keyed by clip set
CLIP_EMB_CACHE_MAX = 256  # per-clip audit embeddings (clip files never change)

# A verdict's status. "match" means name this turn locally; "defer" means the
# turn stays unresolved - honestly uncertain - EXCEPT the "multi" reason,
# which routes the one remaining ElevenLabs job (crosstalk word-splitting).
MATCH = "match"
DEFER = "defer"
MULTI = "multi"  # the defer reason that is the batch pass's only trigger


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
                       margin=MATCH_MARGIN, min_strong=MIN_STRONG_WINDOWS,
                       close_pairs=(), pending_extra=0.0):
    """THE DECISION, pure and fully testable with synthetic vectors.

    - no enrolled candidates -> defer ("no_candidates")
    - two enrolled voices across the windows -> defer ("multi"). Checked
      FIRST since #28 PR-B: a two-voice blend's whole-utterance embedding
      often resembles nobody (below threshold) or everybody (ambiguous), and
      "multi" is now the ONLY door to the ElevenLabs crosstalk split - so
      window evidence of two voices must outrank the blended whole verdict.
    - best match below the threshold -> defer ("below_threshold": a stranger,
      an insufficiently-anchored person, or a two-voice blend that resembles
      nobody). This is the open-set "none of the enrolled" verdict.
    - best match too close to the runner-up -> defer ("ambiguous"). When the
      best and runner-up are a flagged CLOSE PAIR (#28 PR-B: the hygiene
      audit found their centroids suspiciously alike), the required margin
      WIDENS by CLOSE_PAIR_EXTRA_MARGIN - similar-sounding households get
      stricter matching, not confident mistakes.
    - otherwise -> a confident single match, named.
    Every defer leaves the turn honestly unresolved; "multi" alone routes
    the batch crosstalk pass. `close_pairs` is an iterable of person-id
    pairs, order-insensitive."""
    if not enrolled:
        return _defer("no_candidates")
    best_pid, best_score, second = best_two(whole_emb, enrolled)
    if window_multi_voice(window_embs, enrolled, threshold, min_strong):
        return _defer(MULTI, best_score)
    if best_score < threshold:
        return _defer("below_threshold", best_score)
    if pending_extra and best_score < threshold + pending_extra:
        # #81: over the ordinary bar, under the pending-present one. Named
        # distinctly so the trace says WHY the room deferred: someone
        # unlearnt is in the roster, and this score is exactly the shape a
        # new guest mislabelled as a remembered person takes.
        return _defer("pending_present", best_score)
    required = margin
    if second >= 0.0:
        second_pid, _, _ = best_two(whole_emb,
                                    {p: e for p, e in enrolled.items()
                                     if p != best_pid})
        if _is_close_pair(best_pid, second_pid, close_pairs):
            required = margin + CLOSE_PAIR_EXTRA_MARGIN
    if (best_score - second) < required:
        return _defer("ambiguous", best_score)
    return {"status": MATCH, "person_id": best_pid,
            "name": enrolled[best_pid]["name"], "score": round(best_score, 4),
            "reason": "match"}


def _is_close_pair(a, b, close_pairs) -> bool:
    for pair in close_pairs or ():
        if {pair[0], pair[1]} == {a, b}:
            return True
    return False


def matched(verdict) -> bool:
    return bool(verdict) and verdict.get("status") == MATCH


def is_multi(verdict) -> bool:
    """The one verdict that may still spend ElevenLabs money (#28 PR-B):
    the window analysis heard genuinely overlapping speech."""
    return bool(verdict) and verdict.get("status") == DEFER \
        and verdict.get("reason") == MULTI


# ---- pairwise bank hygiene, the pure half (#28 PR-B) --------------------
#
# The sixth field test's serious defect: a guest's bank partly accumulated
# from the owner's audio, so the two banks scored too close together and a
# turn was confidently MIS-named. These rules audit the banks themselves:
# a clip that sits closer to another person's centroid than to its own
# people's is contamination and gets set aside; two people whose centroids
# sit too close get flagged so the matcher demands a wider margin between
# them. Pure functions over {person_id: [(clip_name, embedding), ...]} so
# the keyless suite pins them with synthetic vectors.

def bank_centroids(per_person) -> dict:
    """{pid: centroid} - each person's clips averaged (normalised first)."""
    out = {}
    for pid, clips in (per_person or {}).items():
        emb = average_embeddings([e for _, e in clips])
        if emb is not None:
            out[pid] = emb
    return out


def quarantine_verdicts(per_person) -> dict:
    """Which clips are contaminated? A clip is quarantined when it sits
    closer to ANOTHER person's centroid than to its own - own centroid
    computed leave-one-out (the clip must not defend itself), or the clip
    alone when it is the person's only one (then it cannot be judged and is
    never quarantined). Returns {pid: [clip_name, ...]} with entries only
    for people who have contaminated clips."""
    cents = bank_centroids(per_person)
    out = {}
    for pid, clips in (per_person or {}).items():
        if len(clips) < 2:
            continue
        bad = []
        for name, emb in clips:
            rest = average_embeddings([e for n, e in clips if n != name])
            if rest is None:
                continue
            own = cosine(emb, rest)
            for other_pid, cent in cents.items():
                if other_pid != pid and cosine(emb, cent) > own:
                    bad.append(name)
                    break
        if bad:
            out[pid] = bad
    return out


def close_centroid_pairs(centroids, close=CLOSE_PAIR_COSINE) -> list:
    """Enrolled pairs whose centroids sit suspiciously close:
    [(pid_a, pid_b, cosine), ...], each pair once, order stable."""
    out = []
    pids = list((centroids or {}).keys())
    for i, a in enumerate(pids):
        for b in pids[i + 1:]:
            cos = cosine(centroids[a], centroids[b])
            if cos >= close:
                out.append((a, b, round(cos, 4)))
    return out


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


def _margin(cfg) -> float:
    """The match margin knob (#28 PR-B; voice_id_margin). Same guard shape
    as the threshold: out-of-range or unparseable keeps the default."""
    try:
        m = float((cfg or {}).get("voice_id_margin") or MATCH_MARGIN)
    except (TypeError, ValueError):
        return MATCH_MARGIN
    return m if 0.0 < m < 1.0 else MATCH_MARGIN


def _pending_extra(cfg) -> float:
    """The pending-present bump knob (#81; voice_id_pending_extra). Same
    guard shape as the others; 0.0 is a valid value (feature off)."""
    try:
        raw = (cfg or {}).get("voice_id_pending_extra")
        e = PENDING_EXTRA_THRESHOLD if raw is None else float(raw)
    except (TypeError, ValueError):
        return PENDING_EXTRA_THRESHOLD
    return e if 0.0 <= e < 0.5 else PENDING_EXTRA_THRESHOLD


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


def matcher_status(cfg) -> str:
    """The matcher's state for the voice health strip (#28): one of
    'disabled' (feature flag off), 'unavailable' (no sherpa-onnx wheel, or
    the fetch/build failed - identification runs on the cloud fallback),
    'cold' (nothing has needed the matcher yet; the first voice check warms
    it), 'fetching' (the one-time model download is in flight) or 'ready'.
    Read-only and content-free: it never triggers the warm itself."""
    if not enabled(cfg):
        return "disabled"
    if sherpa_onnx is None:
        return "unavailable"
    with _lock:
        return _state


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

def identify_utterance(pcm, sample_rate, candidates, cfg,
                       pending_present=False):
    """Name a confident single enrolled speaker for one utterance, or defer.

    `candidates`: [{"person_id", "name"}, ...] - the SUFFICIENT people the
    caller is prepared to name. Returns a verdict dict {status, person_id,
    name, score, reason}; matched(verdict) is the naming branch, is_multi()
    the crosstalk one, and every other defer leaves the turn honestly
    unresolved (#28 PR-B: there is no cloud identity fallback to route to).
    NEVER raises and NEVER blocks the live path: it runs on the pass's worker
    thread. The first call in a process kicks off the one-time model fetch in
    the background and defers until it is ready."""
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
    if seconds < SHORT_IDENTIFY_SECONDS:
        # Sub-0.8s utterances are embedded only where quality allows (#28
        # PR-B): a clearly-voiced interjection identifies; a faint blip is
        # honestly too short to judge.
        if anchors.pcm_rms(pcm) < MIN_SHORT_IDENTIFY_RMS:
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
    # Window embeddings run whenever a split is even possible (#28 PR-B).
    # The old confident-match pre-check skipped them for below-threshold and
    # ambiguous utterances - but a two-voice blend lands EXACTLY there, and
    # the "multi" verdict is now the only door to the ElevenLabs crosstalk
    # split, so the window analysis must see those utterances too. A few
    # short embeddings, tens of milliseconds, on a worker thread.
    windows = []
    if len(enrolled) >= 2 and seconds >= MIN_WINDOW_UTTERANCE_SECONDS:
        windows = [e for e in (_embed(ex, w, sr) for w in _windows(audio, sr))
                   if e is not None]
    close = []
    try:
        close = anchors.store().close_pairs()
    except Exception:
        log.debug("voiceid: close-pair read failed", exc_info=True)
    return classify_utterance(whole, windows, enrolled, _threshold(cfg),
                              margin=_margin(cfg), close_pairs=close,
                              pending_extra=_pending_extra(cfg)
                              if pending_present else 0.0)


# ================= the bank audit (#28 PR-B, impure half) =================
#
# Runs the pure hygiene rules over real embeddings and persists the verdicts
# in the anchor store. Called after every anchor-bank change, always from
# worker threads inside never-awaited passes or owner endpoints - never from
# the live voice path. With the extractor unavailable it is a silent no-op:
# no quarantine is better than a guessed one, and the matcher is equally
# unavailable so nothing can mis-match meanwhile.

_audit_lock = threading.Lock()
_audit_fingerprint = None
# #133: a still-learning bank changes on nearly every utterance, and the
# audit re-embeds and recomputes pairwise closeness across every stored
# person each time - back-to-back utterances queued behind it and starved
# the request path. The audit now runs at most once per window; a change
# arriving inside the window is not lost (the fingerprint still differs on
# the next call after the window, so the audit runs then).
AUDIT_MIN_INTERVAL_S = 20.0
_audit_last_ran = 0.0
_clip_emb_cache: dict = {}  # clip filename -> embedding (files are immutable)


def audit_banks(cfg, sample_rate=16000):
    """One pairwise hygiene audit (#28 PR-B): embed every stored clip
    (cached per file - clip files never change once written), quarantine
    clips sitting closer to another person's centroid than their own, flag
    close centroid pairs, and persist both through the anchor store. Never
    raises; returns True when an audit actually ran."""
    try:
        if not enabled(cfg):
            return False
        ex = _get_extractor(cfg)
        if ex is None:
            return False
        store = anchors.store()
        bank = store.bank_clips(sample_rate)
        per_person = {}
        for pid, info in bank.items():
            clips = []
            for fname, pcm in info["clips"]:
                emb = _clip_emb_cache.get(fname)
                if emb is None:
                    emb = _embed(ex, _pcm_to_float(pcm), sample_rate)
                    if emb is None:
                        continue
                    _clip_emb_cache[fname] = emb
                    while len(_clip_emb_cache) > CLIP_EMB_CACHE_MAX:
                        _clip_emb_cache.pop(next(iter(_clip_emb_cache)))
                clips.append((fname, emb))
            if clips:
                per_person[pid] = clips
        quarantine = quarantine_verdicts(per_person)
        pairs = close_centroid_pairs(bank_centroids(
            {pid: [(n, e) for n, e in clips
                   if n not in set(quarantine.get(pid, ()))]
             for pid, clips in per_person.items()}))
        store.set_hygiene(quarantine, pairs)
        if quarantine or pairs:
            log.info("voiceid audit: quarantined=%d close_pairs=%d",
                     sum(len(v) for v in quarantine.values()), len(pairs))
        return True
    except Exception:
        log.warning("voiceid audit failed; banks left as they were",
                    exc_info=True)
        return False


def audit_banks_if_changed(cfg):
    """Run the audit only when the clip-file sets actually changed since the
    last one - the 'on every anchor-bank change' trigger, made idempotent so
    every add_clip call site can call it unconditionally. Serialised: two
    passes finishing together audit once."""
    global _audit_fingerprint, _audit_last_ran
    try:
        fp = anchors.store().clip_fingerprint()
    except Exception:
        log.debug("voiceid audit fingerprint failed", exc_info=True)
        return False
    with _audit_lock:
        if fp == _audit_fingerprint:
            return False
        # #133: inside the cool-down window, defer - the changed
        # fingerprint keeps the debt on the books for the next call.
        if time.monotonic() - _audit_last_ran < AUDIT_MIN_INTERVAL_S:
            return False
        # Do NOT spend the attempt while the matcher is cold (#28, tenth
        # field test): the first anchor change of a process almost always
        # lands during model warm-up, and memoising THAT shape meant the
        # audit never ran again for it. The live store proved the cost - it
        # carried a phantom owner-double whose clips were byte-identical to
        # the owner's own, with no quarantine verdict ever written and no
        # close-pair recorded. Returning before touching the fingerprint
        # keeps the anti-busy-retry intent (the matcher's own state machine
        # throttles warming, not this function) while guaranteeing the audit
        # happens once the matcher is ready.
        if _get_extractor(cfg) is None:
            return False
        ran = audit_banks(cfg)
        # Remember the shape only when an audit ACTUALLY ran.
        if ran:
            _audit_fingerprint = fp
            _audit_last_ran = time.monotonic()
        return ran


# ---- test seam ----------------------------------------------------------

def _reset_for_tests():
    """Return the module to cold and clear caches. Used by the test harness so
    the process-global matcher never leaks a ready extractor between tests."""
    global _extractor, _state, _audit_fingerprint, _audit_last_ran
    with _lock:
        _extractor = None
        _state = "cold"
    _enroll_cache.clear()
    _clip_emb_cache.clear()
    _audit_fingerprint = None
    _audit_last_ran = 0.0
