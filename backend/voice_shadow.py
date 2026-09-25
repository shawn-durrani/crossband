"""The room-mode shadow test (#465 stage 1): measure, never act.

Room mode names a voice turn by comparing the WHOLE turn with every
remembered voice, using TitaNet-Small (backend/voiceid.py). Two changes
might name people better, and this module measures both on real turns,
next to what the app did, without changing anything:

  * SPLIT, THEN NAME. The turn's audio goes to a local diariser on
    loopback (workbench's diarserve), which answers "who spoke when" as
    segments with anonymous speaker slots. Each segment is then named with
    the same speaker model and the same open-set rule as the live path.
  * TWO MODELS. A second embedding model, TitaNet-Large, scores the same
    audio against its own anchors. Each turn and each segment then records
    both models' best name and score, whether they agree, a fused score,
    and what a strict-agreement rule would have named.

Both parts are optional and default off (`diarize_shadow_url`,
`voice_shadow_model`); either runs without the other.

THE RULES, all pinned in tests/test_voice_shadow.py:

  * Never alters anything. The shadow reads the anchor store and writes
    one JSON line to <data_dir>/voice_shadow.jsonl. It never labels a
    message, seats anyone, arms or disarms a room, banks a clip, touches
    memory or reaches the UI. The second model's anchors are built in
    memory from the stored enrolment clips and never written anywhere.
  * Never delays the live path. run_pass hands the turn over in its
    finally, after today's label is written, as a fire-and-forget task.
    Its work runs on this module's own single worker thread, the diariser
    call has a short timeout, at most MAX_IN_FLIGHT turns queue (the rest
    are counted and dropped), and every Small embedding (the one model the
    live matcher shares) first waits, briefly and boundedly, for any live
    identity pass in flight.
  * Loopback only. The diariser URL must name this machine, or that part
    stays off. Redirects are not followed and proxy environment variables
    are ignored, so the audio can't be sent anywhere else.
  * Content-free rows. A row holds ids, names, scores, counts and
    timings. No words, and no audio: the turn's PCM lives in memory for
    the length of the pass.
  * Failures are logged once, not per turn, and logged again only after
    they recover and recur.

THE FUSED SCORE. Cosines from two models sit on different scales, so each
model's score is normalised by that model's own impostor statistics on
THIS household: every enrolled person's clips scored against every OTHER
person's centroid. z = (cosine - impostor mean) / impostor spread, and the
fused score for a person is the mean of the two models' z. The bars follow
from the live ones: TitaNet-Large's threshold is the cosine at the same z
as Small's live threshold, so both models are equally strict on this
household's impostors, and the fused threshold is that same z. Margins and
the pending bump scale by the spread ratio. With fewer than
MIN_IMPOSTOR_SCORES impostor scores (one enrolled person, or too few
clips) there is nothing to normalise by: Large falls back to
LARGE_FALLBACK_BAR, which nobody has calibrated, and no fused score is
recorded. Every row carries the raw best and runner-up cosines and the
statistics, so any threshold can be re-applied later.

The words in a row: `agree` is whether both models' BEST match is the same
person, named or not; `consensus` names that person only when both models
also clear their own bars; `fused` applies the same open-set rule to the
fused scores. `today` is what the live pass wrote, and today.ms is the end
of speech (the commit) to the label written.
"""

import asyncio
import collections
import concurrent.futures
import json
import logging
import math
import os
import threading
import time
from pathlib import Path
from urllib.parse import urlsplit

from . import db, voiceid

log = logging.getLogger("crossband.voice_shadow")

# ---- the second model -----------------------------------------------------
# NVIDIA NeMo TitaNet-Large (CC-BY-4.0), from the same sherpa-onnx speaker
# recognition release as the primary; the hash matches that release's own
# checksum.txt. Fetched by voiceid.fetch_verified into the same models dir,
# only when the setting names it, and never used by the live path.
SECOND_MODELS = {
    "titanet_large": {
        "file": "nemo_en_titanet_large.onnx",
        "url": ("https://github.com/k2-fsa/sherpa-onnx/releases/download/"
                "speaker-recongition-models/nemo_en_titanet_large.onnx"),
        "sha256": ("d51abcf31717ef28162f26acb9d44dd4127c3d44c9b8624f"
                   "699f3425daca8e77"),
    },
}
SECOND_MODEL_ALIASES = {"nemo_en_titanet_large": "titanet_large",
                        "titanet-large": "titanet_large"}

# ---- knobs ----------------------------------------------------------------
DIARISE_TIMEOUT_S = 3.0
DIARISE_SAMPLE_RATE = 16000     # diarserve takes 16 kHz only
MAX_SEGMENTS = 8                # segments embedded per turn; the rest skipped
MAX_IN_FLIGHT = 2               # shadow turns queued at once; more are dropped
QUIET_WAIT_MAX_S = 0.5          # longest a Small embedding waits for live work
QUIET_POLL_S = 0.02
MIN_IMPOSTOR_SCORES = 4
SPREAD_FLOOR = 0.02             # a near-zero spread would blow z up
LARGE_FALLBACK_BAR = {"threshold": 0.45, "margin": 0.12}
ANCHOR_CACHE_MAX = 64
ROWS_FILE = "voice_shadow.jsonl"
ROWS_MAX = 5000                 # past this the file is cut back to ROWS_KEEP
ROWS_KEEP = 4000
ROW_VERSION = 1

# ---- process state (reset by _reset_for_tests) ----------------------------
_TASKS: set = set()
_EXECUTOR = concurrent.futures.ThreadPoolExecutor(
    max_workers=1, thread_name_prefix="voice-shadow")
_lock = threading.Lock()            # guards _large, _anchor_cache, _stats
_rows_lock = threading.Lock()
_large_embed_lock = threading.Lock()
_large = {"key": None, "state": "cold", "ex": None}
_anchor_cache: dict = {}            # (model, pid, fingerprint) -> anchors
_warned: set = set()
_stats = {"rows": 0, "dropped": 0, "diariser": "", "diariser_model": ""}


# ================= settings ================================================

def _is_loopback_host(host) -> bool:
    import ipaddress
    if host == "localhost":
        return True
    try:
        ip = ipaddress.ip_address(host)
    except ValueError:
        return False
    mapped = getattr(ip, "ipv4_mapped", None)
    return (mapped or ip).is_loopback


def loopback_base_url(raw):
    """The diariser's base URL when `raw` is an http(s) URL naming this
    machine, else None. A trailing /diarize is dropped. User info, a query
    or a fragment refuses the URL: none belongs in a loopback base."""
    raw = (raw or "").strip()
    if not raw:
        return None
    try:
        parts = urlsplit(raw)
        port = parts.port
    except ValueError:
        return None
    if parts.scheme not in ("http", "https") or parts.username \
            or parts.password or parts.query or parts.fragment:
        return None
    host = (parts.hostname or "").lower()
    if not _is_loopback_host(host):
        return None
    netloc = f"[{host}]" if ":" in host else host
    if port:
        netloc += f":{port}"
    path = parts.path.rstrip("/")
    if path.endswith("/diarize"):
        path = path[:-len("/diarize")]
    return f"{parts.scheme}://{netloc}{path}"


def diariser_url(cfg):
    """The configured diariser base URL, or None: unset, or refused as not
    loopback (logged once)."""
    raw = ((cfg or {}).get("diarize_shadow_url") or "").strip()
    if not raw:
        return None
    url = loopback_base_url(raw)
    if url is None:
        _warn_once("url", "diarize_shadow_url refused: it must be an http "
                          "URL on this machine (127.0.0.1, ::1 or "
                          "localhost); the split part of the shadow stays off")
    return url


def second_model(cfg):
    """The configured second model's key, or None: unset, or unknown
    (logged once)."""
    raw = ((cfg or {}).get("voice_shadow_model") or "").strip().lower()
    if not raw:
        return None
    key = SECOND_MODEL_ALIASES.get(raw, raw)
    if key not in SECOND_MODELS:
        _warn_once("model", "voice_shadow_model %r is not a known model "
                            "(known: %s); the second model stays off",
                   raw, ", ".join(sorted(SECOND_MODELS)))
        return None
    return key


def active(cfg) -> bool:
    """Is any part of the shadow switched on? Needs the live matcher on too:
    the shadow shares its model and its anchors."""
    return bool(voiceid.enabled(cfg)
                and (diariser_url(cfg) or second_model(cfg)))


def status(cfg) -> dict:
    """Content-free state for the read route."""
    key = second_model(cfg)
    with _lock:
        large_state = _large["state"] if _large["key"] == key else "cold"
    return {
        "active": active(cfg),
        "diariser": {"on": bool(diariser_url(cfg)),
                     "refused": bool((cfg or {}).get("diarize_shadow_url"))
                     and not diariser_url(cfg),
                     "last": _stats["diariser"],
                     "model": _stats["diariser_model"]},
        "second_model": {"on": bool(key), "name": key or "",
                         "state": large_state if key else "off"},
        "rows_written": _stats["rows"],
        "dropped_busy": _stats["dropped"],
    }


# ================= log once =================================================

def _warn_once(key, msg, *args):
    if key in _warned:
        return
    _warned.add(key)
    log.warning("voice shadow: " + msg, *args)


def _recovered(key, msg):
    if key in _warned:
        _warned.discard(key)
        log.info("voice shadow: %s", msg)


# ================= the second model's extractor =============================
# cold -> fetching -> ready | unavailable, like voiceid's, on its own daemon
# thread so no shadow pass waits on a 100 MB download. Until it is ready the
# rows say so and carry Small alone.

def _spawn_large_fetch(cfg, key):
    """Start the one-time background fetch and build. Isolated so tests can
    stub exactly this (tests/conftest.py)."""
    threading.Thread(target=_warm_large, args=(key,), name="voice-shadow-warm",
                     daemon=True).start()


def _warm_large(key):
    spec = SECOND_MODELS[key]
    try:
        path = voiceid.fetch_verified(
            spec["url"], spec["sha256"],
            Path(db.DATA_DIR) / voiceid.MODELS_DIR_NAME / spec["file"])
        if path is None or voiceid.sherpa_onnx is None:
            raise RuntimeError("model file or sherpa-onnx unavailable")
        so = voiceid.sherpa_onnx
        ex = so.SpeakerEmbeddingExtractor(so.SpeakerEmbeddingExtractorConfig(
            model=str(path), num_threads=voiceid.NUM_THREADS, provider="cpu"))
        with _lock:
            _large.update(ex=ex, state="ready")
        log.info("voice shadow: %s ready", key)
    except Exception:
        with _lock:
            _large.update(ex=None, state="unavailable")
        _warn_once("large", "second model %s unavailable; rows carry "
                            "TitaNet-Small alone until a restart", key)
        log.debug("second model failure detail", exc_info=True)


def _large_extractor(cfg):
    key = second_model(cfg)
    if key is None or voiceid.sherpa_onnx is None:
        return None
    with _lock:
        if _large["key"] != key:
            _large.update(key=key, state="cold", ex=None)
        if _large["state"] == "ready":
            return _large["ex"]
        if _large["state"] != "cold":
            return None
        _large["state"] = "fetching"
    _spawn_large_fetch(cfg, key)
    return None


# ================= embedding (the test seam) ================================

def _wait_for_quiet():
    """Let a live identity pass go first. Small is the live matcher's own
    extractor behind its own lock, so a shadow embedding in flight could
    hold up the next turn's check by one embedding. Bounded: the shadow
    never waits more than QUIET_WAIT_MAX_S for one embedding."""
    from . import diarize
    deadline = time.monotonic() + QUIET_WAIT_MAX_S
    while diarize._TASKS and time.monotonic() < deadline:
        time.sleep(QUIET_POLL_S)


def embed(model, pcm, sample_rate, cfg):
    """One L2-normalised embedding of PCM-16 bytes with "small" (the live
    matcher's TitaNet-Small) or "large" (the second model). None when that
    model isn't ready. Tests replace this one function."""
    audio = voiceid._pcm_to_float(pcm)
    if audio is None or len(audio) == 0:
        return None
    if model == "small":
        ex = voiceid._get_extractor(cfg)
        if ex is None:
            return None
        _wait_for_quiet()
        return voiceid._embed(ex, audio, sample_rate)
    ex = _large_extractor(cfg)
    if ex is None:
        return None
    try:
        with _large_embed_lock:
            stream = ex.create_stream()
            stream.accept_waveform(sample_rate=sample_rate, waveform=audio)
            stream.input_finished()
            vec = list(ex.compute(stream))
        return voiceid.l2_normalize(vec) if vec else None
    except Exception:
        log.debug("second-model embedding failed", exc_info=True)
        return None


# ================= pure maths (unit-tested with synthetic vectors) ==========

def impostor_stats(anchors):
    """Mean and spread of this model's cross-speaker scores: each person's
    clip embeddings against every OTHER person's centroid. None when there
    are fewer than MIN_IMPOSTOR_SCORES of them."""
    scores = [voiceid.cosine(clip, other["emb"])
              for pid, a in (anchors or {}).items() for clip in a["clips"]
              for opid, other in anchors.items() if opid != pid]
    if len(scores) < MIN_IMPOSTOR_SCORES:
        return None
    mean = sum(scores) / len(scores)
    spread = math.sqrt(sum((s - mean) ** 2 for s in scores) / len(scores))
    return {"mean": round(mean, 4), "std": round(max(spread, SPREAD_FLOOR), 4),
            "n": len(scores)}


def model_bars(cfg, small_stats, large_stats, pending):
    """The naming bar for each view: Small's is the live one; Large's and the
    fused one follow from it through the impostor statistics (see the module
    docstring). Each bar: threshold, margin, pending bump, close-pair extra."""
    t = voiceid._threshold(cfg)
    m = voiceid._margin(cfg)
    p = voiceid._pending_extra(cfg) if pending else 0.0
    c = voiceid.CLOSE_PAIR_EXTRA_MARGIN
    bars = {"small": {"threshold": t, "margin": m, "pending": p,
                      "close_extra": c, "stats": small_stats}}
    if small_stats and large_stats:
        ratio = large_stats["std"] / small_stats["std"]
        z = (t - small_stats["mean"]) / small_stats["std"]
        bars["large"] = {"threshold": round(large_stats["mean"]
                                            + z * large_stats["std"], 4),
                         "margin": round(m * ratio, 4),
                         "pending": round(p * ratio, 4),
                         "close_extra": round(c * ratio, 4),
                         "stats": large_stats, "source": "matched"}
        bars["fused"] = {"threshold": round(z, 4),
                         "margin": round(m / small_stats["std"], 4),
                         "pending": round(p / small_stats["std"], 4),
                         "close_extra": round(c / small_stats["std"], 4),
                         "z": True}
    else:
        bars["large"] = dict(LARGE_FALLBACK_BAR, pending=p, close_extra=c,
                             stats=large_stats, source="fallback")
        bars["fused"] = None
    return bars


def decide(scores, bar, close_pairs=()):
    """voiceid.classify_utterance's open-set rule over a {person_id: score}
    map, so one rule serves Small, Large and the fused score alike: below
    the threshold, under the pending bump, or too close to the runner-up
    (wider for a flagged close pair) names nobody. Returns (person_id or
    None, reason)."""
    if not scores:
        return None, "no_candidates"
    ranked = sorted(((s, pid) for pid, s in scores.items()), reverse=True)
    best, pid = ranked[0]
    if best < bar["threshold"]:
        return None, "below_threshold"
    if bar.get("pending") and best < bar["threshold"] + bar["pending"]:
        return None, "pending_present"
    if len(ranked) > 1:
        second, second_pid = ranked[1]
        required = bar["margin"]
        # The live rule widens only against a real (non-negative) runner-up
        # cosine; a z-score has no such floor.
        if (bar.get("z") or second >= 0.0) and voiceid._is_close_pair(
                pid, second_pid, close_pairs):
            required += bar["close_extra"]
        if best - second < required:
            return None, "ambiguous"
    return pid, "match"


def fused_scores(small_scores, large_scores, bars):
    """{person_id: mean of the two models' z} over the people both scored."""
    s, l = bars["small"]["stats"], bars["large"]["stats"]
    return {pid: ((small_scores[pid] - s["mean"]) / s["std"]
                  + (large_scores[pid] - l["mean"]) / l["std"]) / 2
            for pid in small_scores if pid in large_scores}


def _view(scores, names, bar, close_pairs):
    ranked = sorted(((s, pid) for pid, s in scores.items()), reverse=True)
    best, pid = ranked[0] if ranked else (None, None)
    named, reason = decide(scores, bar, close_pairs)
    return {"best": names.get(pid) if pid else None, "pid": pid,
            "score": None if best is None else round(best, 4),
            "second": round(ranked[1][0], 4) if len(ranked) > 1 else None,
            "named": names.get(named) if named else None, "reason": reason}


def score_unit(pcm, sample_rate, per_model, bars, close_pairs, cfg,
               models=("small", "large")):
    """Everything the shadow records for one stretch of audio, a whole turn
    or one segment: each model's view, whether they agree, the fused view
    and the strict-agreement verdict. `per_model` maps a model to its
    anchors ({pid: {"name", "emb", "clips"}}); a model with no anchors is
    reported unavailable. Pure apart from `embed`."""
    out, scores, ms = {}, {}, {}
    for model in models:
        anchors = per_model.get(model)
        if not anchors:
            out[model] = {"reason": "unavailable"}
            continue
        t0 = time.perf_counter()
        emb = embed(model, pcm, sample_rate, cfg)
        ms[model] = round((time.perf_counter() - t0) * 1000, 1)
        if emb is None:
            out[model] = {"reason": "unavailable"}
            continue
        scores[model] = {pid: voiceid.cosine(emb, a["emb"])
                         for pid, a in anchors.items()}
        names = {pid: a["name"] for pid, a in anchors.items()}
        out[model] = _view(scores[model], names, bars[model], close_pairs)
        out[model]["ms"] = ms[model]
    small, large = out.get("small", {}), out.get("large", {})
    both = "pid" in small and "pid" in large
    out["agree"] = (small["pid"] == large["pid"]) if both else None
    out["fused"] = None
    if both and bars.get("fused"):
        names = {pid: a["name"] for pid, a in per_model["small"].items()}
        out["fused"] = _view(fused_scores(scores["small"], scores["large"],
                                          bars), names, bars["fused"],
                             close_pairs)
    if both:
        if small["named"] and small["named"] == large["named"]:
            out["consensus"] = {"named": small["named"], "reason": "agree"}
        elif small["named"] and large["named"]:
            out["consensus"] = {"named": None, "reason": "disagree"}
        else:
            out["consensus"] = {"named": None, "reason": "unsure"}
    else:
        out["consensus"] = None
    return out


def gate(pcm, sample_rate):
    """identify_utterance's audio gates, in its order: None when the audio
    may be judged, else the defer reason it would give."""
    from . import anchors
    sr = sample_rate or 16000
    seconds = len(pcm) / 2 / sr
    if seconds < voiceid.MIN_IDENTIFY_SECONDS:
        return "too_short"
    if seconds < voiceid.SHORT_IDENTIFY_SECONDS \
            and anchors.pcm_rms(pcm) < voiceid.MIN_SHORT_IDENTIFY_RMS:
        return "too_short"
    if not voiceid.is_speech(pcm, sr):
        return voiceid.NOT_SPEECH
    return None


def exclusive_spans(segment, segments):
    """The parts of `segment` no segment with ANOTHER slot overlaps, as
    (start, end) seconds. Overlapped speech is two voices at once, which
    names neither."""
    spans = [(segment["start"], segment["end"])]
    for other in segments:
        if other["speaker_slot"] == segment["speaker_slot"]:
            continue
        a, b = other["start"], other["end"]
        cut = []
        for s, e in spans:
            if b <= s or a >= e:
                cut.append((s, e))
                continue
            if a > s:
                cut.append((s, a))
            if b < e:
                cut.append((b, e))
        spans = cut
    return spans


def span_pcm(pcm, sample_rate, spans):
    """The PCM-16 bytes of the given (start, end) second spans, joined."""
    out = bytearray()
    total = len(pcm) // 2
    for s, e in spans:
        a = max(0, min(total, int(round(s * sample_rate))))
        b = max(0, min(total, int(round(e * sample_rate))))
        out += pcm[a * 2:b * 2]
    return bytes(out)


def clean_segments(payload, seconds):
    """The diariser's answer as a checked segment list, or None when it isn't
    the documented shape."""
    if not isinstance(payload, dict) or not isinstance(
            payload.get("segments"), list):
        return None
    out = []
    for seg in payload["segments"]:
        try:
            start = max(0.0, min(float(seg["start"]), seconds))
            end = max(0.0, min(float(seg["end"]), seconds))
            slot = int(seg["speaker_slot"])
        except (TypeError, ValueError, KeyError):
            return None
        if end > start:
            out.append({"start": round(start, 3), "end": round(end, 3),
                        "speaker_slot": slot})
    return sorted(out, key=lambda s: (s["start"], s["end"]))


# ================= the diariser call ========================================

def _post_diarise(base_url, pcm):
    """POST the turn to the loopback diariser. The audio goes nowhere else:
    no redirects, no proxies from the environment, a short timeout."""
    import httpx
    with httpx.Client(trust_env=False, follow_redirects=False,
                      timeout=DIARISE_TIMEOUT_S) as client:
        resp = client.post(base_url + "/diarize", content=pcm,
                           headers={"Content-Type": "application/octet-stream"})
    resp.raise_for_status()
    return resp.json()


def diarise(base_url, pcm, sample_rate):
    """(segments, model id, ms) or ({"error": reason}, "", ms). Failures are
    logged once until the diariser answers again."""
    t0 = time.perf_counter()
    if sample_rate != DIARISE_SAMPLE_RATE:
        return {"error": "sample_rate"}, "", 0.0
    try:
        import httpx
        try:
            payload = _post_diarise(base_url, pcm)
        except httpx.TimeoutException:
            raise _DiariseError("timeout")
        except httpx.HTTPStatusError as exc:
            raise _DiariseError(f"http_{exc.response.status_code}")
        except httpx.HTTPError:
            raise _DiariseError("unreachable")
        except ValueError:
            raise _DiariseError("bad_response")
        except Exception:
            raise _DiariseError("error")
        segments = clean_segments(payload, len(pcm) / 2 / sample_rate)
        if segments is None:
            raise _DiariseError("bad_response")
    except _DiariseError as err:
        ms = round((time.perf_counter() - t0) * 1000, 1)
        _stats["diariser"] = err.reason
        _warn_once("diariser", "the diariser at %s failed (%s); the split "
                               "part records the error until it answers",
                   base_url, err.reason)
        return {"error": err.reason}, "", ms
    ms = round((time.perf_counter() - t0) * 1000, 1)
    model = str(payload.get("model") or "")[:120]
    _stats["diariser"], _stats["diariser_model"] = "ok", model
    _recovered("diariser", "the diariser answers again")
    return segments, model, ms


class _DiariseError(Exception):
    def __init__(self, reason):
        super().__init__(reason)
        self.reason = reason


# ================= anchors per model ========================================

def build_anchors(model, candidates, sample_rate, cfg):
    """{pid: {"name", "emb", "clips"}} for one model, from the SAME stored
    enrolment clips the live matcher averages (anchors.enrollment_clips),
    embedded with that model. Cached in memory per (model, person, kept clip
    set), so a bank change re-embeds that person and nothing is ever written
    over the primary's anchors or the store."""
    from . import anchors as anchor_store
    ids = [c["person_id"] for c in candidates or () if c.get("person_id")]
    if not ids:
        return {}
    names = {c["person_id"]: c.get("name") for c in candidates}
    clips = anchor_store.store().enrollment_clips(ids, sample_rate)
    out = {}
    for pid, info in clips.items():
        key = (model, pid, tuple(info["fingerprint"]))
        with _lock:
            entry = _anchor_cache.get(key)
        if entry is None:
            embs = [e for e in (embed(model, pcm, sample_rate, cfg)
                                for pcm in info["pcms"]) if e]
            centroid = voiceid.average_embeddings(embs)
            if centroid is None:
                continue
            entry = {"emb": centroid, "clips": embs}
            with _lock:
                _anchor_cache[key] = entry
                while len(_anchor_cache) > ANCHOR_CACHE_MAX:
                    _anchor_cache.pop(next(iter(_anchor_cache)))
        out[pid] = {"name": names.get(pid) or info["name"], **entry}
    return out


# ================= one turn =================================================

def _message_id(chat_id, turn_id):
    """The user message this turn became, found by its voice turn id, so an
    owner correction can be joined to the row later. None when there is no
    id or the row isn't there yet."""
    if not turn_id:
        return None
    con = db.connect()
    try:
        row = db.get_message_by_voice_turn(con, chat_id, turn_id)
        return row["id"] if row else None
    finally:
        con.close()


def _today(today):
    """What the live pass did, content-free."""
    return {"path": today.get("path", ""),
            "labels": [n for n in today.get("labels") or ()
                       if n not in set(today.get("uncertain") or ())],
            "uncertain": list(today.get("uncertain") or ()),
            "reason": today.get("reason", ""),
            "score": today.get("score"),
            "ms": today.get("ms")}


def score_turn(chat_id, turn_id, pcm, sample_rate, cfg, today, split,
               queued_ms=0.0):
    """Build one shadow row (worker thread). `split` is what diarise()
    returned, or None when that part is off."""
    from . import anchors as anchor_store
    t0 = time.perf_counter()
    sr = sample_rate or 16000
    candidates = today.get("candidates") or []
    models = ("small", "large") if second_model(cfg) else ("small",)
    per_model = {}
    anchor_ms = {}
    for model in models:
        a0 = time.perf_counter()
        per_model[model] = build_anchors(model, candidates, sr, cfg)
        anchor_ms[model] = round((time.perf_counter() - a0) * 1000, 1)
    stats = {m: impostor_stats(per_model.get(m)) for m in models}
    bars = model_bars(cfg, stats.get("small"), stats.get("large"),
                      bool(today.get("pending")))
    try:
        close = anchor_store.store().close_pairs()
    except Exception:
        close = []
    ms = {"queued": round(queued_ms, 1), "anchors": anchor_ms}

    reason = gate(pcm, sr)
    whole = {"reason": reason} if reason else score_unit(
        pcm, sr, per_model, bars, close, cfg, models)
    ms["whole"] = {m: (whole.get(m) or {}).get("ms") for m in models} \
        if not reason else {}

    split_out = None
    if split is not None:
        segments, model_id, diarise_ms = split
        ms["diarise"] = diarise_ms
        if isinstance(segments, dict):
            split_out = {"error": segments["error"]}
        else:
            rows, split_ms = [], {m: 0.0 for m in models}
            for i, seg in enumerate(segments):
                spans = exclusive_spans(seg, segments)
                seg_pcm = span_pcm(pcm, sr, spans)
                entry = {"start": seg["start"], "end": seg["end"],
                         "slot": seg["speaker_slot"],
                         "used_s": round(len(seg_pcm) / 2 / sr, 3)}
                why = "skipped" if i >= MAX_SEGMENTS else gate(seg_pcm, sr)
                if why:
                    entry["reason"] = why
                else:
                    entry.update(score_unit(seg_pcm, sr, per_model, bars,
                                            close, cfg, models))
                    for m in models:
                        split_ms[m] += (entry.get(m) or {}).get("ms") or 0.0
                rows.append(entry)
            split_out = {"model": model_id, "count": len(rows),
                         "slots": len({s["slot"] for s in rows}),
                         "segments": rows}
            ms["split"] = {m: round(v, 1) for m, v in split_ms.items()}

    ms["total"] = round((time.perf_counter() - t0) * 1000 + queued_ms
                        + (ms.get("diarise") or 0.0), 1)
    large_key = second_model(cfg)
    return {
        "v": ROW_VERSION,
        "at": round(time.time(), 3),
        "chat_id": chat_id,
        "turn_id": str(turn_id or "")[:64],
        "message_id": _message_id(chat_id, turn_id),
        "seconds": round(len(pcm) / 2 / sr, 3),
        "candidates": len(candidates),
        "pending": bool(today.get("pending")),
        "today": _today(today),
        "models": {"small": voiceid.MODEL_FILENAME,
                   "large": SECOND_MODELS[large_key]["file"]
                   if large_key else None},
        "bars": bars,
        "whole": whole,
        "split": split_out,
        "ms": ms,
    }


# ================= the rows file ============================================

def rows_path() -> Path:
    return Path(db.DATA_DIR) / ROWS_FILE


def write_row(row):
    """Append one row, owner-only, and cut the file back to ROWS_KEEP rows
    once it passes ROWS_MAX."""
    path = rows_path()
    line = json.dumps(row, separators=(",", ":"), sort_keys=True) + "\n"
    with _rows_lock:
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
        with os.fdopen(fd, "a") as f:
            f.write(line)
        _stats["rows"] += 1
        if _stats["rows"] % 200 == 0:
            with open(path) as f:
                lines = f.readlines()
            if len(lines) > ROWS_MAX:
                tmp = path.with_name(path.name + ".tmp")
                fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC,
                             0o600)
                with os.fdopen(fd, "w") as f:
                    f.writelines(lines[-ROWS_KEEP:])
                os.replace(tmp, path)


def read_rows(limit=100, chat_id=None) -> list:
    """The newest rows first, at most `limit`, optionally for one chat. A
    row whose message wasn't there when it was written gets its message id
    filled in from the turn id now."""
    path = rows_path()
    keep = collections.deque(maxlen=max(1, int(limit)))
    try:
        with _rows_lock, open(path) as f:
            for line in f:
                try:
                    row = json.loads(line)
                except ValueError:
                    continue
                if chat_id is None or row.get("chat_id") == chat_id:
                    keep.append(row)
    except FileNotFoundError:
        return []
    rows = list(reversed(keep))
    for row in rows:
        if row.get("message_id") is None and row.get("turn_id"):
            try:
                row["message_id"] = _message_id(row["chat_id"],
                                                row["turn_id"])
            except Exception:
                pass
    return rows


# ================= reading the comparison ===================================

METHODS = ("small", "small_split", "large", "large_split", "consensus",
           "consensus_split", "fused", "fused_split")


def _names_for(row, method):
    """The names one method put on one turn, in order, no repeats."""
    base, split = method.split("_")[0], method.endswith("_split")
    units = []
    if split:
        units = [s for s in ((row.get("split") or {}).get("segments") or ())
                 if "reason" not in s]
    else:
        units = [row.get("whole") or {}]
    names = []
    for unit in units:
        view = unit.get(base) or {}
        name = view.get("named") if isinstance(view, dict) else None
        if name and name not in names:
            names.append(name)
    return names


def compare(rows) -> dict:
    """One line per turn, today's label beside every method, plus a tally.
    A method's answer is "+"-joined names, "" when it named nobody, and None
    when it didn't run on that turn."""
    lines, tally = [], {m: collections.Counter() for m in METHODS}
    for row in rows:
        today = row.get("today") or {}
        today_names = today.get("labels") or []
        line = {"turn_id": row.get("turn_id"),
                "message_id": row.get("message_id"),
                "at": row.get("at"), "seconds": row.get("seconds"),
                "today": "+".join(today_names),
                "today_uncertain": "+".join(today.get("uncertain") or []),
                "today_path": today.get("path"),
                "today_reason": today.get("reason") or "",
                "agree": (row.get("whole") or {}).get("agree")}
        for method in METHODS:
            ran = _ran(row, method)
            if not ran:
                line[method] = None
                continue
            names = _names_for(row, method)
            line[method] = "+".join(names)
            t = tally[method]
            t["turns"] += 1
            t["named" if names else "unnamed"] += 1
            if len(names) > 1:
                t["two_or_more"] += 1
            if names and today_names:
                t["same_as_today" if names == today_names
                  else "differs_from_today"] += 1
            elif names:
                t["named_where_today_did_not"] += 1
            elif today_names:
                t["unnamed_where_today_named"] += 1
        lines.append(line)
    return {"lines": lines,
            "tally": {m: dict(c) for m, c in tally.items() if c}}


def _ran(row, method):
    base, split = method.split("_")[0], method.endswith("_split")
    if split:
        seg = row.get("split") or {}
        if "segments" not in seg:
            return False
        units = [s for s in seg["segments"] if "reason" not in s]
    else:
        units = [row.get("whole") or {}]
    for unit in units:
        view = unit.get(base)
        if base in ("fused", "consensus"):
            if view:
                return True
        elif isinstance(view, dict) and "pid" in view:
            return True
    return False


# ================= scheduling ===============================================

def schedule(chat_id, pcm, sample_rate, cfg, turn_id, today):
    """Hand one armed-room turn to the shadow and return at once. Called
    from diarize.run_pass's finally, after today's label is written. Never
    raises; returns the task, or None when the shadow is off, the turn had
    no identity check, or MAX_IN_FLIGHT turns are already queued."""
    try:
        if not today or not today.get("path") or not pcm:
            return None
        if not active(cfg):
            return None
        if len(_TASKS) >= MAX_IN_FLIGHT:
            _stats["dropped"] += 1
            _warn_once("busy", "turns are arriving faster than the shadow "
                               "scores them; the extra ones are counted "
                               "and skipped")
            return None
        task = asyncio.get_running_loop().create_task(
            _run(chat_id, bytes(pcm), sample_rate, dict(cfg), turn_id,
                 dict(today), time.perf_counter()))
        _TASKS.add(task)
        task.add_done_callback(_TASKS.discard)
        return task
    except Exception:
        log.debug("voice shadow scheduling failed", exc_info=True)
        return None


async def _run(chat_id, pcm, sample_rate, cfg, turn_id, today, t_sched):
    loop = asyncio.get_running_loop()
    try:
        split = None
        url = diariser_url(cfg)
        if url:
            split = await loop.run_in_executor(_EXECUTOR, diarise, url, pcm,
                                               sample_rate)
        queued = (time.perf_counter() - t_sched) * 1000
        if split is not None:
            queued -= split[2]
        row = await loop.run_in_executor(
            _EXECUTOR, score_turn, chat_id, turn_id, pcm, sample_rate, cfg,
            today, split, max(0.0, queued))
        await loop.run_in_executor(_EXECUTOR, write_row, row)
        _recovered("row", "shadow rows are being written again")
    except asyncio.CancelledError:
        raise
    except Exception:
        _warn_once("row", "a shadow row could not be built or written; "
                          "the live path is unaffected")
        log.debug("voice shadow failure detail", exc_info=True)


# ---- test seam ------------------------------------------------------------

def _reset_for_tests():
    _TASKS.clear()
    with _lock:
        _large.update(key=None, state="cold", ex=None)
        _anchor_cache.clear()
    _warned.clear()
    _stats.update(rows=0, dropped=0, diariser="", diariser_model="")
