"""The calibrated voice scorer and the readiness test (#482 stage 2).

Groundwork for the voice identity redesign (docs/VOICE_ID_REDESIGN.md:
"Fingerprint the clean speech", "Name each voice", "When a voice is
ready"). NOTHING HERE NAMES A TURN. Live naming, labels and banking still
run through voiceid.identify_utterance and the old sufficiency bar. This
module builds, in the background, the scorer the next stage will name
voices with, and a readiness verdict per person for the Voices page.

THE SCORER, the configuration the stage 1 naming spike recommended:

  1. Fingerprints. Every kept clip is fingerprinted whole, from its speech
     only (voiceid.speech_only), by two models: TitaNet-Small (the live
     matcher's own extractor) and ERes2Net (pinned below, fetched only
     while `voice_calibrated_scorer` is on).
  2. Score. A voice's score against a person, per model, is the mean of
     its TOP_CLIPS best cosines against that person's kept clips. The two
     models' scores are averaged. No cohort or stranger normalisation:
     in the spike, public recordings made on other microphones made it
     worse.
  3. Probability. A logistic calibration on the fused score s and the log
     of the seconds of speech behind it (features 1, s, log t, s log t)
     turns the score into the chance that the voice is that person, with
     every known person and "someone new" equally likely to start with:
     a prior of 1 in N+1 per person, N being the people with a bank.

THE FIT. Every kept clip's speech is cut into pieces of 1, 2, 4 and 8
seconds, one starting every second, and each piece is scored against
every bank.

  * Against its own person's bank, with the piece's leave-out group
    removed. A person with clips from two capture days or more leaves the
    whole day out. A person whose clips all come from one day leaves the
    clip out instead, since there's no other day to keep. The capture day
    is the local calendar date of the clip's `added_at`.
  * A harvested short clip (source 'harvested-short') is a slice cut from
    the same turn as an accumulated clip, so it leaves with that clip: it
    takes the clip's day and group. It's found the way
    anchors.retract_utterance_clips finds it, by its bytes being a slice
    of the other clip's, and failing that by an accumulated clip of the
    same person banked within HARVEST_PAIR_S before it.
  * Against every other person's bank, whole. To that bank the piece is a
    voice it doesn't know, which is what a person looks like with their
    own bank hidden: these rows are how the calibration learns what an
    unknown voice looks like. The same pieces with each person's bank
    hidden in turn are also the build's unknown-voice check.

Same-person and different-person rows count half the weight each, and the
logistic regression is fitted by Newton's method with a small ridge, on
numpy alone.

READINESS. A person is ready when their own 2 s pieces are named as them,
at probability NAME_BAR or more, READY_SHARE of the time or better, never
as anyone else, over READY_MIN_PIECES pieces or more. Each piece is judged
with its whole capture day left out of their bank, and with a calibration
fitted without that day's pieces. A person whose clips all come from one
day can't pass: with the day left out there's nothing left to name them
by. Seconds count speech only, never pauses. Quarantined clips count for
nothing, anywhere.

THE RULES, pinned in tests/test_voice_calibration.py:

  * Off by default (`voice_calibrated_scorer`). Off means no thread, no
    download and no work, and the route reports state "off".
  * Background only. One daemon thread builds at startup and after any
    bank change (an anchors change listener, debounced). Request threads
    only read the last finished snapshot.
  * The live path goes first. Every embedding waits, briefly and
    boundedly, for a live identity pass in flight, the way the shadow
    test does, and holds the live matcher's lock for one embedding at a
    time, through voiceid._embed.
  * Cached. Fingerprints are kept in memory per clip file and content
    hash, so a refit after a bank change embeds only the new clips, and
    the fit itself takes a fraction of a second. Nothing is written to
    disk and nothing leaves the process.
  * Content-free. The snapshot and the log hold person ids, counts,
    shares and milliseconds, never names, words or audio.
"""

import datetime
import hashlib
import logging
import math
import threading
import time
from pathlib import Path

from . import anchors, db, voiceid

try:  # numpy is in requirements.txt; its absence switches this module off
    import numpy as np
except Exception:  # pragma: no cover - import guard
    np = None  # type: ignore

log = logging.getLogger("crossband.voice_calibration")

# ---- the second fingerprint model ------------------------------------------
# ERes2Net (3D-Speaker, Apache-2.0), trained on VoxCeleb, from the same
# sherpa-onnx speaker recognition release as TitaNet-Small. The hash is the
# release's own checksum.txt entry. Fetched by voiceid.fetch_verified into
# the same models dir, only while the setting is on.
ERES2NET = {
    "file": "3dspeaker_speech_eres2net_sv_en_voxceleb_16k.onnx",
    "url": ("https://github.com/k2-fsa/sherpa-onnx/releases/download/"
            "speaker-recongition-models/"
            "3dspeaker_speech_eres2net_sv_en_voxceleb_16k.onnx"),
    "sha256": ("c59158379255ad66e161679cca6af8d52d51e389e3224ab7d7a7baae"
               "295c2db5"),
}
SMALL, ERES = "titanet_small", "eres2net"
MODELS = (SMALL, ERES)

# ---- knobs (the spike's values) --------------------------------------------
SAMPLE_RATE = 16000
FIT_PIECE_SECONDS = (1, 2, 4, 8)
READY_PIECE_SECONDS = 2
PIECE_HOP_SECONDS = 1
# A piece is kept when this share of its 10 ms frames is voiced, a frame
# being voiced over max(PIECE_FRAME_FLOOR, PIECE_FRAME_RATIO x the piece's
# 95th percentile level). A sanity check only: pieces are already cut from
# speech-only audio.
PIECE_ACTIVE_MIN = 0.3
PIECE_FRAME_FLOOR = 0.004
PIECE_FRAME_RATIO = 0.12
TOP_CLIPS = 3
NAME_BAR = 0.9
NEW_BAR = 0.1
READY_SHARE = 0.95
READY_MIN_PIECES = 20
RIDGE = 1e-3
NEWTON_ITERS = 50
HARVEST_PAIR_S = 2.0
# Rides every cache key, so a change to how pieces are cut re-embeds.
PIECES_VERSION = "pieces-v1"
DEBOUNCE_S = 3.0            # a burst of bank changes builds once
MODEL_POLL_S = 0.5          # how often the worker checks the models load
QUIET_WAIT_MAX_S = 0.5      # longest one embedding waits for live work
QUIET_POLL_S = 0.02
STOP_JOIN_S = 5.0

READY_REASONS = ("ready", "too_few_pieces", "one_day", "no_calibration",
                 "named_as_other", "share_below")

# ---- process state (reset by _reset_for_tests) -----------------------------
_lock = threading.Lock()            # guards _status, _snapshot, _worker
_cache_lock = threading.Lock()      # guards _cache
_eres_lock = threading.Lock()       # guards _eres
_eres_embed_lock = threading.Lock()
_eres = {"state": "cold", "ex": None}
_cache: dict = {}                   # (file, sha, versions) -> clip entry
_status = {"state": "off", "error": ""}
_snapshot = None
_worker = {"thread": None, "stop": threading.Event(),
           "kick": threading.Event()}
_warned: set = set()
_sleep = time.sleep                 # the quiet wait's sleep; a test seam


# ================= settings ===================================================

def enabled(cfg) -> bool:
    """Is the calibrated scorer switched on? It rides the live matcher's
    switch too: with voice identification off there's nothing to calibrate.
    numpy missing switches it off."""
    return bool(np is not None and (cfg or {}).get("voice_calibrated_scorer")
                and voiceid.enabled(cfg))


# ================= pure maths (numpy, no model) ==============================

def active_fraction(x) -> float:
    """Share of 10 ms frames of a float waveform in [-1, 1] that are voiced:
    over max(PIECE_FRAME_FLOOR, PIECE_FRAME_RATIO x the 95th percentile
    frame level). 0.0 for audio shorter than one frame."""
    n = len(x) // 160
    if n == 0:
        return 0.0
    frames = np.asarray(x[:n * 160], dtype=np.float64).reshape(n, 160)
    rms = np.sqrt((frames ** 2).mean(1))
    floor = max(PIECE_FRAME_FLOOR, PIECE_FRAME_RATIO * np.percentile(rms, 95))
    return float((rms > floor).mean())


def piece_starts(x, sample_rate, lengths=FIT_PIECE_SECONDS):
    """[(seconds, start sample)] for the pieces of a speech-only float
    waveform: each length in `lengths`, one starting every
    PIECE_HOP_SECONDS, kept when active_fraction clears PIECE_ACTIVE_MIN."""
    sr = sample_rate or SAMPLE_RATE
    hop = int(PIECE_HOP_SECONDS * sr)
    out = []
    for length in lengths:
        w = int(length * sr)
        for st in range(0, len(x) - w + 1, hop):
            if active_fraction(x[st:st + w]) >= PIECE_ACTIVE_MIN:
                out.append((length, st))
    return out


def features(scores, seconds):
    """The calibration's inputs: 1, the fused score, the log of the seconds
    of speech, and their product. Arrays broadcast together."""
    s = np.asarray(scores, dtype=np.float64)
    t = np.log(np.maximum(np.broadcast_to(np.asarray(seconds, np.float64),
                                          s.shape), 0.1))
    return np.stack([np.ones_like(s), s, t, s * t], axis=-1)


def fit_logistic(X, y, w, ridge=RIDGE, iters=NEWTON_ITERS):
    """Weighted logistic regression by Newton's method with a ridge on every
    coefficient. Returns the coefficient vector."""
    b = np.zeros(X.shape[1])
    eye = np.eye(X.shape[1])
    for _ in range(iters):
        z = np.clip(X @ b, -30, 30)
        p = 1.0 / (1.0 + np.exp(-z))
        g = X.T @ (w * (p - y)) + ridge * b
        H = (X * (w * p * (1 - p))[:, None]).T @ X + ridge * eye
        try:
            step = np.linalg.solve(H, g)
        except np.linalg.LinAlgError:
            step = np.linalg.lstsq(H, g, rcond=None)[0]
        b = b - step
        if np.abs(step).max() < 1e-8:
            break
    return b


def fit_calibration(scores, seconds, same):
    """The calibration from rows of (fused score, seconds of speech, same
    person or not). Same-person and different-person rows carry half the
    total weight each, so the fitted log odds read as evidence alone and
    the prior is added after. None without both kinds of row."""
    s = np.asarray(scores, dtype=np.float64)
    t = np.asarray(seconds, dtype=np.float64)
    y = np.asarray(same, dtype=np.float64)
    keep = np.isfinite(s)
    s, t, y = s[keep], t[keep], y[keep]
    n_same, n_diff = y.sum(), (1 - y).sum()
    if n_same == 0 or n_diff == 0:
        return None
    w = np.where(y == 1, 0.5 / n_same, 0.5 / n_diff) * len(y)
    return fit_logistic(features(s, t), y, w)


def prior_logit(n_known) -> float:
    """Log odds of one known person before any evidence: every known person
    and "someone new" equally likely, so 1 in N+1 against N in N+1."""
    return math.log(1.0 / max(1, int(n_known)))


def apply_calibration(b, scores, seconds, n_known):
    """Probability that each score's voice is that person: the calibration
    plus the prior. NaN scores stay NaN."""
    s = np.asarray(scores, dtype=np.float64)
    z = features(np.nan_to_num(s), seconds) @ np.asarray(b) \
        + prior_logit(n_known)
    p = 1.0 / (1.0 + np.exp(-np.clip(z, -30, 30)))
    return np.where(np.isfinite(s), p, np.nan)


def top_mean(sims, k=TOP_CLIPS):
    """Row-wise mean of the k highest finite values of a 2-d array (-inf
    marks a clip left out); NaN for a row with none."""
    sims = np.asarray(sims, dtype=np.float64)
    if sims.shape[1] == 0:
        return np.full(sims.shape[0], np.nan)
    top = -np.sort(-sims, axis=1)[:, :k]
    fin = np.isfinite(top)
    count = fin.sum(1)
    total = np.where(fin, top, 0.0).sum(1)
    out = total / np.maximum(count, 1)
    out[count == 0] = np.nan
    return out


def bank_scores(queries, clips, clip_person, n_people, piece_code=None,
                clip_code=None):
    """(n queries, n people) scores for one model: each person's top-k
    cosine mean. `queries` and `clips` are unit rows. When codes are given,
    a clip whose code equals the query's is left out of its bank (codes
    are per person, so only the query's own person is touched)."""
    S = np.asarray(queries) @ np.asarray(clips).T
    out = np.full((S.shape[0], n_people), np.nan)
    clip_person = np.asarray(clip_person)
    for p in range(n_people):
        cols = np.flatnonzero(clip_person == p)
        sub = S[:, cols]
        if piece_code is not None and len(cols):
            left_out = (np.asarray(clip_code)[cols][None, :]
                        == np.asarray(piece_code)[:, None])
            sub = np.where(left_out, -np.inf, sub)
        out[:, p] = top_mean(sub)
    return out


def harvest_parents(clips) -> dict:
    """{child index: parent index} for harvested short clips. `clips`:
    [{"pid", "file", "added_at", "source", "pcm"}]. A harvested clip's
    parent is another clip of the same person whose bytes hold its bytes
    as a slice (the rule anchors.retract_utterance_clips uses), or failing
    that the accumulated clip of the same person banked closest before it,
    within HARVEST_PAIR_S (the two are banked from one turn, back to
    back)."""
    out = {}
    for i, c in enumerate(clips):
        if c.get("source") != "harvested-short":
            continue
        own = [j for j, o in enumerate(clips)
               if j != i and o["pid"] == c["pid"]]
        pcm = c.get("pcm") or b""
        parent = next((j for j in own
                       if pcm and len(clips[j].get("pcm") or b"") > len(pcm)
                       and pcm in clips[j]["pcm"]), None)
        if parent is None:
            near = [(c["added_at"] - clips[j]["added_at"], j) for j in own
                    if clips[j].get("source") == "accumulated"
                    and 0 <= c["added_at"] - clips[j]["added_at"]
                    <= HARVEST_PAIR_S]
            if near:
                parent = min(near)[1]
        if parent is not None:
            out[i] = parent
    return out


def capture_day(added_at) -> int:
    """The local calendar date a clip was banked on, as an ordinal. A clip
    with no timestamp lands on the epoch's day, so legacy clips share one
    day and leave together."""
    try:
        return datetime.date.fromtimestamp(float(added_at or 0)).toordinal()
    except (OverflowError, OSError, ValueError):
        return datetime.date.fromtimestamp(0).toordinal()


def leave_out_units(clips) -> list:
    """Each clip's (day, family, fit group, readiness unit), in order.

    The family is the clip a harvested short clip was cut from (or the clip
    itself), and a harvested clip takes that clip's day. The fit group is
    the whole day for a person with clips from two days or more, and the
    family for a person with one day. The readiness unit is always the
    person's day."""
    parents = harvest_parents(clips)

    def root(i):
        seen = set()
        while i in parents and i not in seen:
            seen.add(i)
            i = parents[i]
        return i

    roots = [root(i) for i in range(len(clips))]
    days = [capture_day(clips[r]["added_at"]) for r in roots]
    days_of: dict = {}
    for c, d in zip(clips, days):
        days_of.setdefault(c["pid"], set()).add(d)
    out = []
    for i, c in enumerate(clips):
        pid, day, fam = c["pid"], days[i], clips[roots[i]]["file"]
        group = (pid, "day", day) if len(days_of[pid]) >= 2 \
            else (pid, "clip", fam)
        out.append({"day": day, "family": fam, "group": group,
                    "unit": (pid, day)})
    return out


def decide(probs, bar=NAME_BAR):
    """The person index a row of probabilities names at `bar`, or None."""
    row = np.where(np.isfinite(probs), probs, -1.0)
    if not len(row):
        return None
    k = int(np.argmax(row))
    return k if row[k] >= bar else None


def verdict(pieces, named_right, named_as_other, days, calibrated):
    """(ready, reason) for one person's readiness counts. The reasons, in
    the order they're checked: too_few_pieces, one_day, no_calibration,
    named_as_other, share_below."""
    if pieces < READY_MIN_PIECES:
        return False, "too_few_pieces"
    if days < 2:
        return False, "one_day"
    if not calibrated:
        return False, "no_calibration"
    if named_as_other:
        return False, "named_as_other"
    if named_right < READY_SHARE * pieces:
        return False, "share_below"
    return True, "ready"


# ================= fingerprints (the embedding seam) ==========================

def _wait_for_quiet():
    """Let a live identity pass go first, as the shadow test does: a
    background embedding in flight could hold up the next turn's check by
    one embedding. Bounded by QUIET_WAIT_MAX_S."""
    from . import diarize
    deadline = time.monotonic() + QUIET_WAIT_MAX_S
    while diarize._TASKS and time.monotonic() < deadline:
        _sleep(QUIET_POLL_S)


def embed(model, pcm, sample_rate, cfg):
    """One L2-normalised fingerprint of PCM-16 bytes, as a list, from
    TitaNet-Small (the live matcher's extractor, behind its own lock for
    this one embedding) or ERES2NET. None when that model isn't ready.
    Tests replace this one function."""
    audio = voiceid._pcm_to_float(pcm)
    if audio is None or len(audio) == 0:
        return None
    if model == SMALL:
        ex = voiceid._get_extractor(cfg)
        if ex is None:
            return None
        _wait_for_quiet()
        return voiceid._embed(ex, audio, sample_rate)
    ex = _eres2net_extractor(cfg)
    if ex is None:
        return None
    _wait_for_quiet()
    try:
        with _eres_embed_lock:
            stream = ex.create_stream()
            stream.accept_waveform(sample_rate=sample_rate, waveform=audio)
            stream.input_finished()
            vec = list(ex.compute(stream))
        return voiceid.l2_normalize(vec) if vec else None
    except Exception:
        log.debug("ERes2Net embedding failed", exc_info=True)
        return None


def _unit(vec):
    v = np.asarray(vec, dtype=np.float32)
    n = float(np.linalg.norm(v))
    return v / n if n > 0 else v


def clip_layout(pcm, sample_rate=SAMPLE_RATE):
    """(speech-only PCM, seconds of speech, pieces) for one clip. Seconds
    count the speech spans (voiceid.speech_spans), so pauses never count;
    the pieces are cut from the speech-only audio the fingerprint hears."""
    sr = sample_rate or SAMPLE_RATE
    spans = voiceid.speech_spans(pcm, sr)
    speech = voiceid.speech_only(pcm, sr, spans=spans)
    seconds = sum(hi - lo for lo, hi in spans) / sr
    x = np.frombuffer(speech[:len(speech) - len(speech) % 2],
                      dtype="<i2").astype(np.float32) / 32768.0
    return speech, seconds, piece_starts(x, sr)


class _Stopped(Exception):
    pass


def _entry_for(fname, pcm, cfg, stop=None):
    """The cached fingerprints of one clip, embedding whatever is missing.
    Returns (entry, embeddings made), or (None, n) when a model couldn't
    embed it (it's retried next build, never cached half done)."""
    sha = hashlib.sha256(pcm).hexdigest()
    key = (fname, sha, voiceid.SPEECH_ONLY_VERSION, PIECES_VERSION)
    with _cache_lock:
        entry = _cache.get(key)
    made = 0
    speech = None
    if entry is None:
        speech, seconds, pieces = clip_layout(pcm, SAMPLE_RATE)
        entry = {"key": key, "seconds": seconds, "pieces": pieces,
                 "prints": {}}
    for model in MODELS:
        if model in entry["prints"]:
            continue
        if speech is None:
            speech = clip_layout(pcm, SAMPLE_RATE)[0]
        vecs = []
        for length, st in [(None, None)] + list(entry["pieces"]):
            if stop is not None and stop.is_set():
                raise _Stopped()
            part = speech if length is None else \
                speech[st * 2:(st + int(length * SAMPLE_RATE)) * 2]
            vec = embed(model, part, SAMPLE_RATE, cfg)
            made += 1
            if vec is None:
                return None, made
            vecs.append(_unit(vec))
        entry["prints"][model] = {
            "whole": vecs[0],
            "pieces": (np.stack(vecs[1:]) if len(vecs) > 1
                       else np.zeros((0, len(vecs[0])), np.float32))}
    with _cache_lock:
        _cache[key] = entry
    return entry, made


# ================= the build ==================================================

def bank_fingerprint(index) -> tuple:
    """What a build depends on: each person's active clip files."""
    return tuple(sorted((pid, tuple(sorted(c["file"] for c in info["clips"])))
                        for pid, info in (index or {}).items()))


def build(cfg, stop=None):
    """Build a fresh snapshot from the anchor store: fingerprints (cached),
    the calibration, and readiness per person. Blocking; the worker thread
    calls it, and tests call it directly. Returns the snapshot."""
    t0 = time.perf_counter()
    store = anchors.store()
    index = store.calibration_clips(SAMPLE_RATE, audio=True)
    fingerprint = bank_fingerprint(index)
    clips, made, missed = [], 0, 0
    for pid in sorted(index):
        for c in index[pid]["clips"]:
            entry, n = _entry_for(c["file"], c["pcm"], cfg, stop)
            made += n
            if entry is None:
                missed += 1
                continue
            clips.append(dict(c, pid=pid, entry=entry))
    if missed:
        # A clip a model couldn't embed is left out of this snapshot, and
        # the fingerprint won't match, so the next kick tries it again.
        fingerprint = ("incomplete", missed) + fingerprint
    embed_ms = (time.perf_counter() - t0) * 1000
    t1 = time.perf_counter()
    snap = fit_snapshot(clips)
    for c in clips:
        c.pop("pcm", None)
    fit_ms = (time.perf_counter() - t1) * 1000
    snap.update(fingerprint=fingerprint, built_at=round(time.time(), 3),
                embedded=made, missed=missed,
                ms={"embed": round(embed_ms, 1), "fit": round(fit_ms, 1),
                    "total": round((time.perf_counter() - t0) * 1000, 1)})
    _evict(clips)
    return snap


def _evict(clips):
    """Drop cache entries for clips no longer in any bank."""
    live = {c["entry"]["key"] for c in clips}
    with _cache_lock:
        for key in [k for k in _cache if k not in live]:
            _cache.pop(key, None)


def fit_snapshot(clips) -> dict:
    """Everything the scorer and the Voices page need, from fingerprinted
    clips: [{"pid", "file", "added_at", "source", "pcm", "entry"}]. Pure
    apart from reading `entry`, and quick: no embedding happens here."""
    people = sorted({c["pid"] for c in clips})
    pidx = {p: i for i, p in enumerate(people)}
    n_people = len(people)
    units = leave_out_units(clips)
    clip_person = np.array([pidx[c["pid"]] for c in clips], dtype=int)
    group_code = _codes([u["group"] for u in units])
    unit_code = _codes([u["unit"] for u in units])
    days_of = {p: len({u["day"] for c, u in zip(clips, units) if c["pid"] == p})
               for p in people}
    seconds_of = {p: sum(c["entry"]["seconds"] for c in clips if c["pid"] == p)
                  for p in people}
    # piece rows
    p_clip, p_len = [], []
    for ci, c in enumerate(clips):
        for length, _ in c["entry"]["pieces"]:
            p_clip.append(ci)
            p_len.append(length)
    p_clip = np.array(p_clip, dtype=int)
    p_len = np.array(p_len, dtype=np.float64)
    p_person = clip_person[p_clip] if len(p_clip) else np.zeros(0, int)
    p_group = group_code[p_clip] if len(p_clip) else np.zeros(0, int)
    p_unit = unit_code[p_clip] if len(p_clip) else np.zeros(0, int)
    fit_scores, unit_scores, banks = [], [], {}
    for model in MODELS:
        C = (np.stack([c["entry"]["prints"][model]["whole"] for c in clips])
             if clips else np.zeros((0, 1), np.float32))
        Q = (np.concatenate([c["entry"]["prints"][model]["pieces"]
                             for c in clips])
             if clips else np.zeros((0, 1), np.float32))
        fit_scores.append(bank_scores(Q, C, clip_person, n_people,
                                      p_group, group_code))
        unit_scores.append(bank_scores(Q, C, clip_person, n_people,
                                       p_unit, unit_code))
        for p in people:
            banks.setdefault(p, {})[model] = C[clip_person == pidx[p]]
    F_fit = np.mean(fit_scores, axis=0) if len(p_clip) else \
        np.zeros((0, n_people))
    F_unit = np.mean(unit_scores, axis=0) if len(p_clip) else \
        np.zeros((0, n_people))
    # calibration rows: every piece against every bank it has a score for
    same = (np.arange(n_people)[None, :] == p_person[:, None])
    row_group = np.repeat(p_group[:, None], n_people, axis=1)
    row_len = np.repeat(p_len[:, None], n_people, axis=1)
    fits: dict = {}

    def fit_without(groups):
        key = frozenset(groups)
        if key not in fits:
            keep = ~np.isin(row_group, list(key)) if key else \
                np.ones_like(row_group, dtype=bool)
            fits[key] = fit_calibration(F_fit[keep], row_len[keep],
                                        same[keep])
        return fits[key]

    b_all = fit_without(())
    readiness, unknown = {}, {"pieces": 0, "named": 0, "new": 0}
    for p in people:
        i = pidx[p]
        right = other = total = 0
        for u in sorted(set(p_unit[(p_person == i)
                                   & (p_len == READY_PIECE_SECONDS)])):
            rows = np.flatnonzero((p_unit == u)
                                  & (p_len == READY_PIECE_SECONDS))
            groups = set(p_group[p_unit == u])
            b = fit_without(groups)
            total += len(rows)
            if b is None:
                continue
            probs = apply_calibration(b, F_unit[rows], READY_PIECE_SECONDS,
                                      n_people)
            for row in probs:
                named = decide(row)
                if named == i:
                    right += 1
                elif named is not None:
                    other += 1
                hidden = np.delete(row, i)
                unknown["pieces"] += 1
                if decide(hidden) is not None:
                    unknown["named"] += 1
                elif np.all(np.nan_to_num(hidden, nan=0.0) < NEW_BAR):
                    unknown["new"] += 1
        ready, reason = verdict(total, right, other, days_of[p],
                                b_all is not None)
        readiness[p] = {
            "ready": ready, "reason": reason, "pieces": total,
            "named_right": right,
            "share_right": round(right / total, 3) if total else None,
            "named_as_other": other, "days": days_of[p],
            "speech_seconds": round(seconds_of[p], 1)}
    return {"people": n_people, "clips": len(clips), "pieces": int(len(p_clip)),
            "calibrated": b_all is not None,
            "calibration": None if b_all is None else [float(x) for x in b_all],
            "banks": banks, "readiness": readiness, "unknown": unknown}


def _codes(keys):
    table: dict = {}
    return np.array([table.setdefault(k, len(table)) for k in keys],
                    dtype=int)


# ================= using a snapshot ===========================================

def fused_scores(fingerprints, banks) -> dict:
    """{person_id: fused score} for one voice: per model, the mean of its
    TOP_CLIPS best cosines against the person's kept clips, averaged over
    the models. `fingerprints`: {model: vector}. A person missing a model's
    bank is left out."""
    out = {}
    for pid, bank in (banks or {}).items():
        per = []
        for model in MODELS:
            vec, clips = (fingerprints or {}).get(model), bank.get(model)
            if vec is None or clips is None or not len(clips):
                break
            sims = np.asarray(clips) @ _unit(vec)
            per.append(float(top_mean(sims[None, :])[0]))
        else:
            out[pid] = float(np.mean(per))
    return out


def probability(fingerprints, seconds, snapshot=None):
    """{person_id: probability} that a voice with these fingerprints and
    this many seconds of speech is each known person, with the prior of 1
    in N+1. None until a calibrated snapshot exists."""
    snap = snapshot if snapshot is not None else current()
    if not snap or not snap.get("calibrated"):
        return None
    scores = fused_scores(fingerprints, snap["banks"])
    if not scores:
        return {}
    pids = sorted(scores)
    probs = apply_calibration(snap["calibration"],
                              [scores[p] for p in pids], seconds,
                              snap["people"])
    return {p: float(v) for p, v in zip(pids, probs)}


def current():
    """The last finished snapshot, or None."""
    with _lock:
        return _snapshot


def readiness(cfg) -> dict:
    """{person_id: readiness} from the last snapshot, or {} while off or
    not built yet. A copy, content-free."""
    if not enabled(cfg):
        return {}
    snap = current()
    return {pid: dict(r) for pid, r in (snap or {}).get("readiness",
                                                       {}).items()}


def status(cfg) -> dict:
    """The scorer's state and the readiness rules, for the Voices page:
    "off", "waiting" (models loading), "building", "ready", "unavailable"
    or "failed". Content-free."""
    out = {"state": "off", "min_pieces": READY_MIN_PIECES,
           "share": READY_SHARE, "bar": NAME_BAR,
           "piece_seconds": READY_PIECE_SECONDS}
    if not enabled(cfg):
        return out
    snap = current()
    with _lock:
        out["state"] = _status["state"]
    if snap:
        out.update(built_at=snap["built_at"], calibrated=snap["calibrated"],
                   people=snap["people"], clips=snap["clips"],
                   pieces=snap["pieces"], unknown=dict(snap["unknown"]),
                   ms=dict(snap["ms"]))
    return out


# ================= ERes2Net's extractor =======================================
# cold -> fetching -> ready | unavailable, on its own daemon thread, like the
# shadow test's second model. Sticky: a restart tries again.

def eres2net_path() -> Path:
    return Path(db.DATA_DIR) / voiceid.MODELS_DIR_NAME / ERES2NET["file"]


def _spawn_eres2net_fetch(cfg):
    """Start the one-time fetch and build. Isolated so tests stub exactly
    this (tests/conftest.py)."""
    threading.Thread(target=_warm_eres2net, name="voice-calibration-warm",
                     daemon=True).start()


def _warm_eres2net():
    try:
        path = voiceid.fetch_verified(ERES2NET["url"], ERES2NET["sha256"],
                                      eres2net_path())
        if path is None or voiceid.sherpa_onnx is None:
            raise RuntimeError("model file or sherpa-onnx unavailable")
        so = voiceid.sherpa_onnx
        ex = so.SpeakerEmbeddingExtractor(so.SpeakerEmbeddingExtractorConfig(
            model=str(path), num_threads=voiceid.NUM_THREADS, provider="cpu"))
        with _eres_lock:
            _eres.update(ex=ex, state="ready")
        log.info("voice calibration: ERes2Net ready")
    except Exception:
        with _eres_lock:
            _eres.update(ex=None, state="unavailable")
        _warn_once("eres", "ERes2Net unavailable; readiness waits for a "
                           "restart")
        log.debug("ERes2Net failure detail", exc_info=True)


def _eres2net_extractor(cfg):
    """The ready extractor, or None. The first call with the setting on
    claims the one fetch."""
    if not enabled(cfg) or voiceid.sherpa_onnx is None:
        return None
    with _eres_lock:
        if _eres["state"] == "ready":
            return _eres["ex"]
        if _eres["state"] != "cold":
            return None
        _eres["state"] = "fetching"
    _spawn_eres2net_fetch(cfg)
    return None


def models_state(cfg) -> str:
    """"ready" when both models can embed, "unavailable" when either never
    will (until a restart), else "waiting". Claims each model's one warm."""
    if voiceid.sherpa_onnx is None:
        return "unavailable"
    small = "ready" if voiceid._get_extractor(cfg) is not None \
        else voiceid.matcher_status(cfg)
    eres = "ready" if _eres2net_extractor(cfg) is not None else \
        _eres["state"]
    if "unavailable" in (small, eres):
        return "unavailable"
    return "ready" if small == eres == "ready" else "waiting"


# ================= the worker =================================================

def start(cfg) -> bool:
    """Start the one background worker, at app startup, when the setting
    is on, and ask it for a first build. Never blocks and never raises.
    True when the worker runs."""
    try:
        if not enabled(cfg):
            return False
        with _lock:
            thread = _worker["thread"]
            if thread is not None and thread.is_alive():
                _worker["kick"].set()
                return True
            _worker["stop"] = threading.Event()
            _worker["kick"] = threading.Event()
            _worker["kick"].set()
            thread = threading.Thread(
                target=_run, args=(dict(cfg), _worker["stop"],
                                   _worker["kick"]),
                name="voice-calibration", daemon=True)
            _worker["thread"] = thread
            _status.update(state="waiting", error="")
        anchors.add_change_listener(_on_bank_change)
        thread.start()
        return True
    except Exception:
        log.warning("voice calibration could not start", exc_info=True)
        return False


def stop():
    """Stop the worker (app shutdown). A build in flight stops at its next
    embedding."""
    anchors.remove_change_listener(_on_bank_change)
    with _lock:
        thread = _worker["thread"]
        _worker["thread"] = None
        _worker["stop"].set()
        _worker["kick"].set()
    if thread is not None and thread is not threading.current_thread():
        thread.join(STOP_JOIN_S)


def _on_bank_change():
    """The anchors change listener: ask the worker for a build. Only sets
    a flag, since it runs inside the store's lock."""
    _worker["kick"].set()


def _run(cfg, stop_event, kick):
    while not stop_event.is_set():
        kick.wait()
        if stop_event.is_set():
            return
        kick.clear()
        while not stop_event.is_set() and kick.wait(DEBOUNCE_S):
            kick.clear()
        if stop_event.is_set():
            return
        try:
            _build_once(cfg, stop_event)
        except _Stopped:
            return
        except Exception:
            with _lock:
                _status.update(state="failed", error="build failed")
            _warn_once("build", "a readiness build failed; live naming is "
                                "unaffected")
            log.debug("voice calibration build failure detail",
                      exc_info=True)


def _build_once(cfg, stop_event):
    """One pass of the worker: wait for both models, skip when nothing a
    build depends on has changed, otherwise build and publish."""
    global _snapshot
    while True:
        state = models_state(cfg)
        if state == "ready":
            break
        with _lock:
            _status.update(state=state)
        if state == "unavailable":
            return
        if stop_event.wait(MODEL_POLL_S):
            raise _Stopped()
    index = anchors.store().calibration_clips(SAMPLE_RATE, audio=False)
    snap = current()
    if snap is not None and snap["fingerprint"] == bank_fingerprint(index):
        with _lock:
            _status.update(state="ready")
        return
    with _lock:
        _status.update(state="building")
    snap = build(cfg, stop_event)
    with _lock:
        _snapshot = snap
        _status.update(state="ready", error="")
    _recovered("build", "readiness builds again")
    log.info("voice calibration built: people=%d clips=%d pieces=%d "
             "embedded=%d ready=%d calibrated=%s unknown_named=%d/%d "
             "embed_ms=%.0f fit_ms=%.0f",
             snap["people"], snap["clips"], snap["pieces"], snap["embedded"],
             sum(1 for r in snap["readiness"].values() if r["ready"]),
             snap["calibrated"], snap["unknown"]["named"],
             snap["unknown"]["pieces"], snap["ms"]["embed"],
             snap["ms"]["fit"])


# ================= log once ===================================================

def _warn_once(key, msg):
    if key in _warned:
        return
    _warned.add(key)
    log.warning("voice calibration: %s", msg)


def _recovered(key, msg):
    if key in _warned:
        _warned.discard(key)
        log.info("voice calibration: %s", msg)


# ---- test seam ----------------------------------------------------------------

def _reset_for_tests():
    global _snapshot
    stop()
    with _lock:
        _snapshot = None
        _status.update(state="off", error="")
    with _eres_lock:
        _eres.update(state="cold", ex=None)
    with _cache_lock:
        _cache.clear()
    _warned.clear()
