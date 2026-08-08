"""Durable voice anchors (#28 phase 2): the store that makes voices REMEMBERED.

Per named person, this module accumulates several seconds of clean
single-speaker speech across utterances - deliberately not just the first two
seconds ever heard - and keeps the best few clips. Those clips are what get
prepended to every diarization request (the anchor prefix), which is the only
thing that makes per-request cluster labels comparable across utterances and
across sessions: a known person is re-identified the moment they speak in a
later session, with no introduction needed.

Privacy posture, matching the rest of the data directory:

- Everything lives under <data_dir>/voice_anchors/, directory mode 0o700,
  every file 0o600 (owner-only) - the same posture as .env and the browser
  gate's secrets.
- Anchors are DELETABLE: forget() removes the person's clip files from disk
  and drops the index entry. The roster UI's "forget" button lands here.
- Nothing here is ever sent anywhere except inside a diarization request the
  operator's own room mode already makes.

Sufficiency is a REQUIREMENT, not an accident (owner decision on the issue):
below SUFFICIENT_SECONDS of accepted clip audio a person's anchor is not used
for identification at all - their turns stay uncertain, and downstream
attribution treats them accordingly. The quality gate keeps junk out (too
short, too quiet), and the keep-best-N policy refreshes anchors as better
speech arrives. A tap-to-correct feeds its utterance in as ground truth
(source='correction'), which both fixes the label and improves the anchor.

Everything blocking runs on worker threads via the callers (the diarization
pass, the introduction scan) - nothing here is awaited by any live path.
"""

import array
import json
import logging
import os
import re
import threading
import time
import uuid
from collections import OrderedDict
from pathlib import Path

from . import db

log = logging.getLogger("crossband.anchors")

DIR_NAME = "voice_anchors"
INDEX_NAME = "index.json"

# ---- clip acceptance / sufficiency (the tuning knobs, in one place) ----
MIN_CLIP_SECONDS = 1.0     # shorter than this carries too little voice to help
MAX_CLIP_SECONDS = 10.0    # longer clips are trimmed to their first 10s
MIN_CLIP_RMS = 120         # int16 RMS floor - near-silence is not an anchor
KEEP_CLIPS = 5             # best N clips per person, by quality score
SUFFICIENT_SECONDS = 6.0   # accepted seconds needed before identification is trusted
PREFIX_PERSON_SECONDS = 2.5  # roughly how much of each person rides the prefix
MAX_PREFERRED_CHARS = 40   # display-name bound, matching the roster's
PREFIX_CACHE_MAX = 8       # built-prefix snapshots kept per store (#28)

# Recent-utterance audio cache for tap-to-correct: message_id -> utterance
# audio, IN MEMORY ONLY, bounded. A correction made while the session is warm
# feeds real audio to the corrected person's anchors; after a restart the
# correction still fixes the label, it just has no audio left to learn from
# (stated in the UI copy rather than pretended away).
RECENT_MAX_ENTRIES = 24
RECENT_MAX_SECONDS = 30.0  # per-entry tail cap
_recent_audio: OrderedDict = OrderedDict()  # message_id -> (pcm, sr, n_clusters)
_recent_lock = threading.Lock()


# ---------- pure rules (unit-tested directly, no I/O) ----------

def pcm_rms(pcm: bytes) -> int:
    """int16 RMS of a PCM buffer (audioop left the stdlib in 3.13; this is
    the two lines of it we used). Decimated 4:1 - a level estimate does not
    need every sample, and this runs on worker threads over clips up to 10s."""
    usable = len(pcm) - (len(pcm) % 2)
    if usable <= 0:
        return 0
    samples = array.array("h")
    samples.frombytes(pcm[:usable])
    picked = samples[::4] or samples
    return int((sum(x * x for x in picked) / len(picked)) ** 0.5)


def clip_quality(pcm: bytes, sample_rate: int) -> dict:
    """Content-free quality measures for one candidate clip: duration, int16
    RMS, and a comparable score (capped seconds, discounted while quiet)."""
    sr = sample_rate or 16000
    seconds = len(pcm) / 2 / sr
    rms = pcm_rms(pcm)
    score = min(seconds, MAX_CLIP_SECONDS) * min(1.0, rms / 1000.0)
    return {"seconds": round(seconds, 2), "rms": rms, "score": round(score, 3)}


def accepts_clip(quality: dict) -> bool:
    """The gate: long enough to carry a voice, loud enough to be one."""
    return (quality["seconds"] >= MIN_CLIP_SECONDS
            and quality["rms"] >= MIN_CLIP_RMS)


def select_keep(clips: list) -> list:
    """Keep the best KEEP_CLIPS by score (ties: newest wins) - the refresh
    policy that lets better speech displace an early mediocre clip."""
    ranked = sorted(clips, key=lambda c: (c["score"], c.get("added_at", 0)),
                    reverse=True)
    return ranked[:KEEP_CLIPS]


def is_sufficient(clips: list) -> bool:
    """The hard requirement: enough accepted audio to trust identification."""
    return sum(c["seconds"] for c in clips) >= SUFFICIENT_SECONDS


def trim_clip(pcm: bytes, sample_rate: int) -> bytes:
    """Cap a clip at MAX_CLIP_SECONDS (keep the head - utterance starts are
    where the cleanest single-speaker audio usually is)."""
    cap = int(MAX_CLIP_SECONDS * (sample_rate or 16000)) * 2
    return pcm[:cap]


def person_id_for(name: str) -> str:
    """Stable-ish id from a name plus a short random suffix, so two people
    who share a first name never collide in the store."""
    slug = re.sub(r"[^a-z0-9]+", "-", (name or "").lower()).strip("-") or "person"
    return f"{slug}-{uuid.uuid4().hex[:6]}"


# ---------- the store ----------

class AnchorStore:
    """File-backed, owner-only anchor store. Index writes are atomic (temp +
    os.replace) and serialized behind a lock - the diarization pass, the
    introduction scan and the correction endpoint all write from worker
    threads."""

    def __init__(self, root: Path):
        self.root = Path(root)
        self._lock = threading.Lock()
        # Built-prefix cache (#28, night test 4): (person_ids, sample_rate)
        # -> (index fingerprint at build time, (pcm, segments)). Guarded by
        # _lock; a stale fingerprint simply misses, so correctness never
        # depends on eviction.
        self._prefix_cache: OrderedDict = OrderedDict()
        # In-process mutation counter, bumped by every _save: the half of
        # the fingerprint that never depends on filesystem timestamp
        # granularity (the on-disk mtime/size half covers other processes).
        self._gen = 0

    # -- filesystem plumbing --

    def _ensure_dir(self):
        os.makedirs(self.root, exist_ok=True)
        os.chmod(self.root, 0o700)

    def _index_path(self) -> Path:
        return self.root / INDEX_NAME

    def _load(self) -> dict:
        try:
            with open(self._index_path()) as f:
                data = json.load(f)
            return data if isinstance(data, dict) else {"people": {}}
        except FileNotFoundError:
            return {"people": {}}
        except Exception:
            # A corrupt index must never brick voice sessions; the audio files
            # still exist but are unreachable until re-learned. Loud log.
            log.error("voice-anchor index unreadable - starting empty",
                      exc_info=True)
            return {"people": {}}

    def _save(self, data: dict):
        self._ensure_dir()
        tmp = self._index_path().with_suffix(".json.tmp")
        with open(tmp, "w") as f:
            json.dump(data, f, indent=1, sort_keys=True)
        os.chmod(tmp, 0o600)
        os.replace(tmp, self._index_path())
        self._gen += 1  # every mutation invalidates the built-prefix cache

    def _index_fingerprint(self):
        """Cheap change detector for build_prefix's cache: the in-process
        generation counter (bumped by every _save) plus the index file's
        mtime_ns and size (which every atomic _save rewrites - so a mutation
        by ANOTHER process misses too). One stat call, no file read."""
        try:
            st = os.stat(self._index_path())
            disk = (st.st_mtime_ns, st.st_size)
        except OSError:
            disk = None
        return (self._gen, disk)

    def _write_clip(self, pcm: bytes, sample_rate: int, person_id: str) -> str:
        from .diarize import pcm16_wav
        self._ensure_dir()
        fname = f"{person_id}-{uuid.uuid4().hex[:8]}.wav"
        path = self.root / fname
        with open(path, "wb") as f:
            f.write(pcm16_wav(pcm, sample_rate))
        os.chmod(path, 0o600)
        return fname

    def _read_clip_pcm(self, fname: str) -> bytes:
        """Raw PCM back out of one of our own minimal WAV files (fixed 44-byte
        header - we wrote it, so no general WAV parsing is needed)."""
        with open(self.root / fname, "rb") as f:
            return f.read()[44:]

    # -- people --

    def people(self) -> list:
        """Every remembered person, with the sufficiency verdict the UI and
        the identification path both key on. No audio leaves here - names,
        counts and seconds only."""
        with self._lock:
            data = self._load()
        out = []
        for pid, p in sorted(data["people"].items(),
                             key=lambda kv: kv[1].get("created_at", 0)):
            clips = p.get("clips", [])
            name = p.get("name", pid)
            out.append({
                "person_id": pid,
                "name": name,
                # The correctable display name (#28 phase 3): what the roster
                # chip, the memory ingest and the STT keyterms call this
                # person. Defaults to the name they were introduced under.
                "preferred_name": p.get("preferred_name") or name,
                "created_at": p.get("created_at", 0),
                "clip_count": len(clips),
                "seconds": round(sum(c["seconds"] for c in clips), 1),
                "sufficient": is_sufficient(clips),
            })
        return out

    def find_by_name(self, name: str):
        """Case-insensitive name lookup: the re-identification hook - an
        introduction (or correction) for a name we already know reuses that
        person's anchors instead of starting over."""
        want = (name or "").strip().lower()
        if not want:
            return None
        for p in self.people():
            if p["name"].strip().lower() == want:
                return p
        return None

    def ensure_person(self, name: str) -> str:
        """Return the person_id for `name`, creating the entry if new."""
        existing = self.find_by_name(name)
        if existing:
            return existing["person_id"]
        with self._lock:
            data = self._load()
            pid = person_id_for(name)
            data["people"][pid] = {"name": name.strip(),
                                   "created_at": time.time(), "clips": []}
            self._save(data)
        return pid

    def set_preferred_name(self, person_id: str, preferred: str) -> bool:
        """Set the person's correctable display name (#28 phase 3). The
        introduced name stays as the identity key (voice labels and roster
        rows keep matching); this only changes what the display, ingest and
        keyterm surfaces call them. Returns False for an unknown person or
        an empty name after trimming."""
        preferred = (preferred or "").strip()[:MAX_PREFERRED_CHARS].strip()
        if not preferred or not re.search(r"[A-Za-z]", preferred):
            return False
        with self._lock:
            data = self._load()
            person = data["people"].get(person_id)
            if person is None:
                return False
            person["preferred_name"] = preferred
            self._save(data)
        return True

    def add_clip(self, person_id: str, pcm: bytes, sample_rate: int,
                 source: str) -> bool:
        """Offer one utterance's audio as an anchor clip. Applies the quality
        gate, the trim cap and the keep-best-N refresh; evicted clips have
        their files deleted. Returns True if the clip was accepted.
        `source`: 'introduction' | 'accumulated' | 'correction' - recorded so
        the store stays explainable."""
        pcm = trim_clip(pcm or b"", sample_rate)
        q = clip_quality(pcm, sample_rate)
        if not accepts_clip(q):
            return False
        with self._lock:
            data = self._load()
            person = data["people"].get(person_id)
            if person is None:
                return False
            fname = self._write_clip(pcm, sample_rate, person_id)
            clips = person.get("clips", [])
            clips.append({"file": fname, "seconds": q["seconds"],
                          "rms": q["rms"], "score": q["score"],
                          "sample_rate": sample_rate, "source": source,
                          "added_at": time.time()})
            kept = select_keep(clips)
            person["clips"] = kept
            self._save(data)
            kept_files = {c["file"] for c in kept}
            for c in clips:
                if c["file"] not in kept_files:
                    self._delete_file(c["file"])
        return True

    def forget(self, person_id: str) -> bool:
        """Delete a remembered person: every clip file AND the index entry.
        This is the privacy contract behind the roster UI's forget button -
        after it returns, no audio of that person remains on disk."""
        with self._lock:
            data = self._load()
            person = data["people"].pop(person_id, None)
            if person is None:
                return False
            self._save(data)
            for c in person.get("clips", []):
                self._delete_file(c["file"])
        return True

    def _delete_file(self, fname: str):
        try:
            os.remove(self.root / fname)
        except OSError:
            log.warning("anchor clip already gone: %s", fname)

    # -- the prefix --

    def build_prefix(self, person_ids: list, sample_rate: int):
        """Concatenated anchor audio for the given people, plus the segment
        map [(person_id, name, start_s, end_s), ...] the identification pass
        uses to read the diarizer's prefix clusters back into names.

        Only SUFFICIENT people contribute (below the bar, identification must
        stay uncertain rather than guess off thin evidence), and only clips
        recorded at the requested sample rate (in practice everything is 16k;
        a mismatched clip is skipped rather than resampled badly). Per person,
        best clips first up to ~PREFIX_PERSON_SECONDS.

        Cached per roster snapshot (#28, night test 4): every diarization
        pass used to re-read the same clip files from disk. The built prefix
        is now kept keyed on (person ids, sample rate) and validated against
        the index fingerprint - a hit reads NO clip files, and any anchor
        mutation (add, refresh, forget, rename - anything that rewrites the
        index) or roster change misses and rebuilds. Bounded to
        PREFIX_CACHE_MAX snapshots; segments are copied on the way in and
        out, so callers can never corrupt a cached entry."""
        key = (tuple(person_ids), sample_rate)
        with self._lock:
            fingerprint = self._index_fingerprint()
            hit = self._prefix_cache.get(key)
            if hit and hit[0] == fingerprint:
                self._prefix_cache.move_to_end(key)
                pcm, segments = hit[1]
                return pcm, [dict(s) for s in segments]
            data = self._load()
        pcm_parts = []
        segments = []
        cursor = 0.0
        for pid in person_ids:
            person = data["people"].get(pid)
            if not person:
                continue
            clips = [c for c in person.get("clips", [])
                     if c.get("sample_rate") == sample_rate]
            if not is_sufficient(clips):
                continue
            take = []
            got = 0.0
            for c in sorted(clips, key=lambda c: c["score"], reverse=True):
                if got >= PREFIX_PERSON_SECONDS:
                    break
                take.append(c)
                got += c["seconds"]
            if not take:
                continue
            part = b""
            for c in take:
                try:
                    part += self._read_clip_pcm(c["file"])
                except OSError:
                    log.warning("anchor clip unreadable: %s", c["file"])
            if not part:
                continue
            cap = int(PREFIX_PERSON_SECONDS * sample_rate) * 2
            part = part[:cap]
            seconds = len(part) / 2 / sample_rate
            pcm_parts.append(part)
            segments.append({"person_id": pid, "name": person.get("name", pid),
                            "start": round(cursor, 3),
                            "end": round(cursor + seconds, 3)})
            cursor += seconds
        prefix = b"".join(pcm_parts)
        with self._lock:
            # Stored under the PRE-BUILD fingerprint: a mutation that landed
            # while we were reading clips changed the fingerprint, so this
            # entry can never satisfy the next lookup - it just misses.
            self._prefix_cache[key] = (fingerprint,
                                       (prefix, [dict(s) for s in segments]))
            self._prefix_cache.move_to_end(key)
            while len(self._prefix_cache) > PREFIX_CACHE_MAX:
                self._prefix_cache.popitem(last=False)
        return prefix, segments


_store: AnchorStore | None = None


def store() -> AnchorStore:
    """The process-wide store, bound to the CURRENT data directory (tests and
    custom installs move it via db.configure, so the binding is re-checked on
    every access rather than frozen at import)."""
    global _store
    root = Path(db.DATA_DIR) / DIR_NAME
    if _store is None or _store.root != root:
        _store = AnchorStore(root)
    return _store


# ---------- recent-utterance cache (tap-to-correct's audio source) ----------

def remember_audio(message_id: int, pcm: bytes, sample_rate: int,
                   n_clusters: int):
    """Stash one labelled utterance's audio, in memory, so a correction made
    soon after can feed it to the corrected person's anchors. Bounded both
    ways (entry count and per-entry tail seconds); never written to disk
    unless a correction actually promotes it to an anchor clip."""
    if not message_id or not pcm:
        return
    cap = int(RECENT_MAX_SECONDS * (sample_rate or 16000)) * 2
    with _recent_lock:
        _recent_audio[message_id] = (pcm[-cap:], sample_rate, n_clusters)
        _recent_audio.move_to_end(message_id)
        while len(_recent_audio) > RECENT_MAX_ENTRIES:
            _recent_audio.popitem(last=False)


def take_audio(message_id: int):
    """Claim (and remove) a stashed utterance for a correction. Returns
    (pcm, sample_rate, n_clusters) or None. Single-cluster utterances are the
    only ones a caller should feed to anchors - a two-voice utterance is not
    ground truth for either voice - but that decision is the caller's; this
    just hands back what was heard."""
    with _recent_lock:
        return _recent_audio.pop(message_id, None)


def clear_recent_audio():
    with _recent_lock:
        _recent_audio.clear()
