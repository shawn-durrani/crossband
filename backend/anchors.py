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
import hashlib
import json
import logging
import os
import re
import statistics
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
# Clip LENGTH CLASSES (#28 PR-B, eighth field test): the banks were built
# from long utterances only, so a second-long interjection had nothing like
# itself to match against and stayed perpetually below threshold. Clips at or
# under SHORT_CLIP_MAX_SECONDS are the "short" class; keep-best-N applies PER
# CLASS (a short clip's quality score is inherently lower - seconds times
# loudness - so a single shared pool would always evict every short clip),
# and sufficiency requires BOTH the seconds bar AND a minimum number of short
# clips, so short utterances become identifiable rather than unmatchable.
SHORT_CLIP_MAX_SECONDS = 2.0
KEEP_CLIPS = 5             # best N LONG clips per person, by quality score
KEEP_SHORT_CLIPS = 3       # best N SHORT clips per person, kept separately
MIN_SHORT_CLIPS = 2        # short clips needed for sufficiency (configurable)
SUFFICIENT_SECONDS = 6.0   # accepted seconds needed before identification is trusted
PREFIX_PERSON_SECONDS = 2.5  # roughly how much of each person rides the prefix
ENROLL_CLIPS = 3           # best N clips averaged into a local voice-id embedding (#28 part 2)
MAX_PREFERRED_CHARS = 40   # display-name bound, matching the roster's
MERGED_NAMES_MAX = 8       # spellings one person answers to, bounded (#28)
PREFIX_CACHE_MAX = 8       # built-prefix snapshots kept per store (#28)
# Quarantined clips (#28 PR-B, the hygiene guard): clips the pairwise audit
# set aside are KEPT ON DISK but excluded from matching. Bounded per person;
# past the cap the oldest set-aside clip is deleted for real.
QUARANTINE_MAX = 8
# Refusal visibility (#312): a clip the acceptance gate turns away is
# recorded on the person - a bounded list of recent refusal times and the
# last reason - so a still-learning bank that never grows can say why on
# screen instead of needing a shell on the box.
REFUSAL_KEEP = 50          # refusal timestamps kept per person
REFUSAL_WINDOW_S = 7 * 86400  # the "recently refused" window the UI reads


def configure_sufficiency(sufficient_seconds=None, min_short_clips=None):
    """Apply the operator's sufficiency knobs (#28 PR-B; settings
    voice_id_sufficient_seconds / voice_id_min_short_clips). Called once at
    app creation; None or invalid values keep the current defaults."""
    global SUFFICIENT_SECONDS, MIN_SHORT_CLIPS
    try:
        if sufficient_seconds is not None and float(sufficient_seconds) > 0:
            SUFFICIENT_SECONDS = float(sufficient_seconds)
    except (TypeError, ValueError):
        pass
    try:
        if min_short_clips is not None and int(min_short_clips) >= 0:
            MIN_SHORT_CLIPS = int(min_short_clips)
    except (TypeError, ValueError):
        pass

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
    RMS, the voiced fraction (#218), and a comparable score (capped seconds,
    discounted while quiet)."""
    from . import voiceid
    sr = sample_rate or 16000
    seconds = len(pcm) / 2 / sr
    rms = pcm_rms(pcm)
    score = min(seconds, MAX_CLIP_SECONDS) * min(1.0, rms / 1000.0)
    return {"seconds": round(seconds, 2), "rms": rms,
            "voiced": round(voiceid.voiced_fraction(pcm, sr), 3),
            "score": round(score, 3)}


def accepts_clip(quality: dict) -> bool:
    """The gate: long enough to carry a voice, loud enough to be one - and
    actually speech (#218). Loud static used to clear the first two bars,
    bank as an anchor, and its seconds-times-loudness score could then
    EVICT genuine quiet speech under keep-best-N; the voiced fraction is
    what says a voice is in there at all. Every add_clip call site passes
    through here, so no source can bank non-speech, whatever the caller."""
    from . import voiceid
    return (quality["seconds"] >= MIN_CLIP_SECONDS
            and quality["rms"] >= MIN_CLIP_RMS
            and quality.get("voiced", 1.0)
            >= voiceid.SPEECH_MIN_VOICED_FRACTION)


def is_short(clip: dict) -> bool:
    """Does a clip fall in the SHORT length class (#28 PR-B)?"""
    return clip["seconds"] <= SHORT_CLIP_MAX_SECONDS


def active_clips(clips: list) -> list:
    """The clips that actually take part in matching: everything the hygiene
    guard (#28 PR-B) has not set aside. Every gate - sufficiency, the prefix,
    enrolment, the UI's seconds - counts these and only these."""
    return [c for c in clips or [] if not c.get("quarantined")]


def select_keep(clips: list) -> list:
    """Keep the best clips by score (ties: newest wins) - the refresh policy
    that lets better speech displace an early mediocre clip. Applied PER
    LENGTH CLASS (#28 PR-B): the best KEEP_CLIPS long clips AND the best
    KEEP_SHORT_CLIPS short ones, because scores scale with seconds and a
    single shared pool starved the short class that interjection matching
    needs. Callers pass ACTIVE clips; quarantined clips are not ranked here
    (they are set aside, not competing)."""
    ranked = sorted(clips, key=lambda c: (c["score"], c.get("added_at", 0)),
                    reverse=True)
    longs = [c for c in ranked if not is_short(c)][:KEEP_CLIPS]
    shorts = [c for c in ranked if is_short(c)][:KEEP_SHORT_CLIPS]
    return [c for c in ranked if c in longs or c in shorts]


def is_sufficient(clips: list) -> bool:
    """The hard requirement: enough accepted audio to trust identification.

    The seconds bar ALONE (#28, tenth field test). PR-B briefly made this
    two-part (seconds AND short clips), which instantly reclassified every
    existing bank as insufficient and deadlocked the system: no candidates,
    so no matches, so no accumulation, so never sufficient again. The
    short-clip requirement lives on as `short_ready` (a progress indicator
    and a short-utterance precision aid), never as a gate on matching -
    and confident long matches now HARVEST short slices from their own
    audio (harvest_short_slice), so the short class fills itself.
    Quarantined clips count for nothing."""
    live = active_clips(clips)
    return sum(c["seconds"] for c in live) >= SUFFICIENT_SECONDS


def is_short_ready(clips: list) -> bool:
    """The short-utterance readiness indicator (#28 PR-B's second bar,
    demoted from gate to signal): enough short clips that second-long
    interjections have something like themselves to match."""
    live = active_clips(clips)
    return sum(1 for c in live if is_short(c)) >= MIN_SHORT_CLIPS


VOUCH_SOURCES = ("introduction", "correction")

# #221: when rotation has replaced every clip a human stood behind, the
# save-time match scores of the survivors decide the bank's standing. The
# bar sits inside the measured same-speaker range (0.63-0.73 on the pinned
# model) and above the borderline-steal shape that hovers near the naming
# threshold: a genuine bank's accumulations clear it comfortably, a
# taken-over bank's early thefts do not.
TRUST_SCORE_BAR = 0.65


def bank_vouched(person: dict) -> bool:
    """A human has stood behind this bank (#83): someone voice-introduced
    into it, the owner corrected a turn into it, the owner auditioned it -
    or a clip of a vouching source is still present (legacy banks predate
    the person-level stamps)."""
    if person.get("vouched_at") or person.get("audition_confirmed_at"):
        return True
    return any(c.get("source") in VOUCH_SOURCES
               for c in person.get("clips", []))


def surviving_human_clip(clips: list) -> bool:
    """Is a clip a human stood behind still IN the bank (#221)? Vouching
    stamps are person-level and permanent; this is the clip-level question
    rotation can change."""
    return any(c.get("source") in VOUCH_SOURCES or c.get("moved_at")
               for c in active_clips(clips))


def bank_trust(person: dict) -> str:
    """The bank's standing (#221), one of:

    - 'human': a human-backed clip survives, or the owner's audition is
      newer than the newest clip - their ear has heard today's content;
    - 'self': never human-backed at all. The #83 floor owns this shape:
      it always carries the flag and the audition ask, whatever the
      scores say, because save-time confidence is graded against the bank
      itself - a polluted bank grades its own donor highly;
    - 'high' / 'low': vouched once, but accumulation has replaced every
      human-backed clip. The save-time scores of the survivors decide:
      a median at TRUST_SCORE_BAR or above keeps working (flagged), below
      it identification pauses for the owner's ear. Clips banked before
      scores were recorded carry none and are left out; with no scored
      clip at all the verdict is 'high', because pausing the installed
      base on upgrade would be a regression, not a safeguard (#83's own
      lesson)."""
    clips = person.get("clips", [])
    if surviving_human_clip(clips):
        return "human"
    if not (person.get("vouched_at") or person.get("audition_confirmed_at")):
        return "self"
    confirmed = person.get("audition_confirmed_at") or 0
    newest = max((c.get("added_at", 0) for c in active_clips(clips)),
                 default=0)
    if confirmed and confirmed >= newest:
        return "human"
    scored = [c["match_score"] for c in active_clips(clips)
              if c.get("match_score") is not None]
    if not scored:
        return "high"
    return "high" if statistics.median(scored) >= TRUST_SCORE_BAR else "low"


def needs_audition(person: dict) -> bool:
    """Ask for the owner's ear (#83/#221): the bank is sufficient, and
    either nobody human ever stood behind it (the phantom shape - #65
    banks passed every automated check), or its human backing has rotated
    away and the surviving save-time scores read low."""
    if not is_sufficient(person.get("clips", [])):
        return False
    if not bank_vouched(person):
        return True
    return bank_trust(person) == "low"


def identification_paused(person: dict) -> bool:
    """Excluded from the anchor prefix and matcher enrolment until the
    owner auditions (#83/#221). For a never-vouched bank this applies only
    when the sufficiency CROSSING was observed (the stamp exists only
    post-#83): a pre-existing sufficient bank keeps working while it
    awaits the owner's ear, because pausing the whole installed base on
    upgrade would be a regression, not a safeguard. A LOW-TRUST bank
    (#221) needs no stamp: low trust can only arise from match scores
    recorded after this shipped, so it cannot shock the installed base."""
    if not needs_audition(person):
        return False
    if bank_vouched(person):
        return True
    return bool(person.get("sufficiency_crossed_at"))


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
        close = {}
        for pair in data.get("close_pairs") or []:
            if isinstance(pair, (list, tuple)) and len(pair) >= 2:
                close.setdefault(pair[0], []).append(pair[1])
                close.setdefault(pair[1], []).append(pair[0])
        out = []
        now_ts = time.time()
        for pid, p in sorted(data["people"].items(),
                             key=lambda kv: kv[1].get("created_at", 0)):
            all_clips = p.get("clips", [])
            clips = active_clips(all_clips)
            name = p.get("name", pid)
            out.append({
                "person_id": pid,
                "name": name,
                # The correctable display name (#28 phase 3): what the roster
                # chip, the memory ingest and the STT keyterms call this
                # person. Defaults to the name they were introduced under.
                "preferred_name": p.get("preferred_name") or name,
                # Owner-set names are LAW (#28): once the owner has set the
                # display name - by the rename UI or a spoken correction - no
                # automated path may change it again.
                "owner_set": bool(p.get("preferred_owner_set")),
                # Identity names this person has ALSO been known under
                # (merge_people folds them in), so old voice labels and
                # re-introductions under a merged-away name still resolve to
                # this person instead of minting a twin.
                "merged_names": list(p.get("merged_names") or []),
                "created_at": p.get("created_at", 0),
                "clip_count": len(clips),
                "seconds": round(sum(c["seconds"] for c in clips), 1),
                # Learning visibility (#28, thirteenth field test): when this
                # person last banked a clip, and whether BOTH length classes
                # are full - the difference between "still growing" and
                # "refreshing in place", which was previously knowable only by
                # reading the store file by hand.
                "last_clip_at": max((c.get("added_at", 0) for c in clips),
                                    default=0),
                "at_capacity": (
                    sum(1 for c in clips if not is_short(c)) >= KEEP_CLIPS
                    and sum(1 for c in clips if is_short(c)) >= KEEP_SHORT_CLIPS),
                # Short-clip progress (#28 PR-B, demoted to a signal by the
                # tenth field test): readiness for second-long interjections,
                # shown in the UI, never a gate on matching.
                "short_clips": sum(1 for c in clips if is_short(c)),
                "short_ready": is_short_ready(all_clips),
                # Hygiene guard surfacing (#28 PR-B): clips the pairwise
                # audit set aside (kept on disk, excluded from matching),
                # and which other people this person's voice sits close to.
                "quarantined_count": len(all_clips) - len(clips),
                # #219: how many of those were set aside as not a voice at
                # all, so the panel can say which problem this bank has.
                "noise_count": sum(
                    1 for c in all_clips
                    if c.get("quarantined")
                    and c.get("quarantine_reason") == "not_speech"),
                # #312: how many clips the gate refused in the last week,
                # and why the last one was refused - the difference between
                # "nobody has spoken near it" and "it hears them and says
                # no", which used to be indistinguishable on screen.
                "refused_last_week": sum(
                    1 for ts in (p.get("clip_refusals") or {})
                    .get("recent", [])
                    if now_ts - ts <= REFUSAL_WINDOW_S),
                "refusal_reason": (p.get("clip_refusals") or {})
                .get("last_reason", ""),
                "close_to": list(close.get(pid, [])),
                "sufficient": is_sufficient(all_clips),
                # #83: has a human ever stood behind this bank, does it
                # need the owner's ear, and is identification paused until
                # then (only for crossings observed post-#83).
                "membro_slug": p.get("membro_slug"),
                "vouched": bank_vouched(p),
                "needs_audition": needs_audition(p),
                "id_paused": identification_paused(p),
                # #221: the bank's standing once vouching is outlived.
                "trust": bank_trust(p),
            })
        return out

    def find_by_name(self, name: str):
        """Case-insensitive name lookup: the re-identification hook - an
        introduction (or correction) for a name we already know reuses that
        person's anchors instead of starting over. Merged-away identity names
        match too: after a merge, the old name still means this person."""
        want = (name or "").strip().lower()
        if not want:
            return None
        people = self.people()
        for p in people:
            if p["name"].strip().lower() == want:
                return p
        for p in people:
            if any(m.strip().lower() == want for m in p["merged_names"]):
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

    # -- the correction ledger (#33 slice 3): a move, delete or merge is
    # the owner's judgement, and it must reach the durable home too - or
    # the correction resurrects through a rebuild. Each mutator records
    # its decision here; person_sync replays them against membro and
    # removes what landed. Records carry ids and hashes only, never audio.

    def _record_correction(self, data: dict, entry: dict):
        entry["cid"] = uuid.uuid4().hex[:12]
        entry["at"] = time.time()
        data.setdefault("pending_corrections", []).append(entry)

    def _clip_sha(self, fname: str):
        try:
            return hashlib.sha256((self.root / fname).read_bytes()).hexdigest()
        except OSError:
            return None

    def pending_corrections(self) -> list:
        with self._lock:
            data = self._load()
        return list(data.get("pending_corrections") or [])

    def remove_corrections(self, cids) -> None:
        cids = set(cids)
        if not cids:
            return
        with self._lock:
            data = self._load()
            data["pending_corrections"] = [
                c for c in (data.get("pending_corrections") or [])
                if c.get("cid") not in cids]
            self._save(data)

    # -- membro sync bookkeeping (#33 slice 2): which durable record each
    # local person maps to, and how far the last pull got. Stored in the
    # index so it rides the same atomic writes and backups as everything
    # else here.

    def membro_slugs(self) -> dict:
        with self._lock:
            data = self._load()
        return {pid: p["membro_slug"] for pid, p in data["people"].items()
                if p.get("membro_slug")}

    def set_membro_slug(self, person_id: str, slug: str) -> bool:
        with self._lock:
            data = self._load()
            person = data["people"].get(person_id)
            if person is None:
                return False
            person["membro_slug"] = slug
            self._save(data)
        return True

    def get_sync_watermark(self) -> float:
        with self._lock:
            data = self._load()
        return float(data.get("persons_synced_at") or 0)

    def set_sync_watermark(self, ts: float) -> None:
        with self._lock:
            data = self._load()
            data["persons_synced_at"] = float(ts)
            self._save(data)

    def confirm_audition(self, person_id: str) -> bool:
        """The owner listened and confirmed the bank is who it claims
        (#83). Restores the identification rights an unvouched sufficiency
        crossing withholds. The negative outcome needs no method - a wrong
        bank gets the existing tools (reassign clips, merge, forget)."""
        with self._lock:
            data = self._load()
            person = data["people"].get(person_id)
            if person is None:
                return False
            person["audition_confirmed_at"] = time.time()
            self._save(data)
        return True

    def set_preferred_name(self, person_id: str, preferred: str,
                           owner_set: bool = True) -> bool:
        """Set the person's correctable display name (#28 phase 3). The
        introduced name stays as the identity key (voice labels and roster
        rows keep matching); this only changes what the display, ingest and
        keyterm surfaces call them. Returns False for an unknown person or
        an empty name after trimming.

        Naming is law (#28): `owner_set=True` (the rename UI, a spoken
        correction) marks the name OWNER-SET, and from then on every
        automated path - alias capture, introductions, anything calling with
        `owner_set=False` - is refused. The owner's word is final until the
        owner speaks again."""
        preferred = (preferred or "").strip()[:MAX_PREFERRED_CHARS].strip()
        if not preferred or not re.search(r"[A-Za-z]", preferred):
            return False
        with self._lock:
            data = self._load()
            person = data["people"].get(person_id)
            if person is None:
                return False
            if not owner_set and person.get("preferred_owner_set"):
                return False  # locked: no automated path may rename them
            person["preferred_name"] = preferred
            if owner_set:
                person["preferred_owner_set"] = True
            self._save(data)
        return True

    def add_merged_name(self, person_id: str, name: str) -> bool:
        """Record another identity name this person answers to (#28: names
        collapse by voice). The door the fourteenth field test demanded: an
        introduction spelt a remembered person's name in a way no spelling
        rule could bridge (four edits apart), but the introducer's VOICE
        confidently matched - so the introduced spelling lands here, and
        find_by_name, the variant rule, keyterm biasing and future
        introductions all resolve it to this person instead of minting a
        twin. Also the alias-declaration door ("Matteo is the spelling but
        it's pronounced Mateo" puts both forms on one person).

        Conservative and idempotent: a name already covering this person
        (identity, preferred or merged, case-insensitively) is refused, and
        the list is bounded at MERGED_NAMES_MAX - a runaway transcriber
        cannot grow it forever. Returns True when the name was recorded."""
        name = (name or "").strip()[:MAX_PREFERRED_CHARS].strip()
        if not name or not re.search(r"[A-Za-z]", name):
            return False
        with self._lock:
            data = self._load()
            person = data["people"].get(person_id)
            if person is None:
                return False
            merged = list(person.get("merged_names") or [])
            covered = [person.get("name", ""),
                       person.get("preferred_name", "")] + merged
            if any(n and n.strip().casefold() == name.casefold()
                   for n in covered):
                return False
            if len(merged) >= MERGED_NAMES_MAX:
                return False
            person["merged_names"] = merged + [name]
            self._save(data)
        return True

    def merge_people(self, person_id_a: str, person_id_b: str) -> str | None:
        """Fold two remembered people into ONE (#28: variants merge instead
        of duplicating). The OLDEST person_id survives; the other's clips
        join the survivor's pool and the keep-best-N rule applies across the
        union (evicted clip files are deleted, so the merged bank obeys the
        same bounds as any other). The non-survivor's identity name (and any
        names already merged into it) land in the survivor's merged_names, so
        old voice labels and re-introductions under that name keep resolving.
        The survivor's own preferred name and owner-set flag are untouched -
        the caller applies the owner's chosen display name after the merge.
        Returns the surviving person_id, or None (unknown id, or a == b)."""
        if not person_id_a or not person_id_b or person_id_a == person_id_b:
            return None
        with self._lock:
            data = self._load()
            a = data["people"].get(person_id_a)
            b = data["people"].get(person_id_b)
            if a is None or b is None:
                return None
            survivor_id, gone_id = person_id_a, person_id_b
            if b.get("created_at", 0) < a.get("created_at", 0):
                survivor_id, gone_id = person_id_b, person_id_a
            survivor = data["people"][survivor_id]
            gone = data["people"].pop(gone_id)
            merged = list(survivor.get("clips", [])) + list(gone.get("clips", []))
            # Quarantined clips ride along set-aside (#28 PR-B): the merged
            # bank is new evidence, so the next hygiene audit re-judges them.
            quarantined = [c for c in merged
                           if c.get("quarantined")][:QUARANTINE_MAX]
            kept = select_keep(active_clips(merged)) + quarantined
            survivor["clips"] = kept
            # #33 slice 3: a merged-away person with a durable record must
            # merge there too, or its stale clips rebuild one day
            if gone.get("membro_slug"):
                self._record_correction(data, {
                    "kind": "merge", "loser_slug": gone["membro_slug"],
                    "winner": survivor_id})
            # #83: vouching survives a merge - either side's human stamp
            # carries, and the earliest of each stamp stands.
            for key in ("vouched_at", "audition_confirmed_at",
                        "sufficiency_crossed_at"):
                if gone.get(key) and (not survivor.get(key)
                                      or gone[key] < survivor[key]):
                    survivor[key] = gone[key]
            if gone.get("vouched_by") and not survivor.get("vouched_by"):
                survivor["vouched_by"] = gone["vouched_by"]
            # close-pair records naming the forgotten id are stale
            data["close_pairs"] = [
                p for p in (data.get("close_pairs") or [])
                if gone_id not in p[:2]]
            names = list(survivor.get("merged_names") or [])
            for extra in [gone.get("name", gone_id)] + list(
                    gone.get("merged_names") or []):
                if extra and extra.lower() not in (
                        [n.lower() for n in names]
                        + [survivor.get("name", "").lower()]):
                    names.append(extra)
            survivor["merged_names"] = names
            self._save(data)
            kept_files = {c["file"] for c in kept}
            for c in merged:
                if c["file"] not in kept_files:
                    self._delete_file(c["file"])
        log.info("voices merged: survivor=%s forgotten=%s clips=%d",
                 survivor_id, gone_id, len(kept))
        return survivor_id

    def add_clip(self, person_id: str, pcm: bytes, sample_rate: int,
                 source: str, score=None, membro_sha=None) -> bool:
        """Offer one utterance's audio as an anchor clip. Trims the dead
        air off both ends (#310), applies the quality gate, the 10s cap
        and the keep-best-N refresh; evicted clips have their files
        deleted. Returns True if the clip was accepted.
        `source`: 'introduction' | 'accumulated' | 'harvested-short' |
        'correction' | 'cold-start' - recorded so the store stays
        explainable. 'cold-start' (#28) is the by-elimination clip banked
        for the only person in an armed room whose bank cannot identify
        them yet. `score` (#221) is the MATCH score this clip banked at,
        recorded at write time like every provenance stamp: it is what a
        bank that outlives its human backing is later judged by.
        `membro_sha` (#310): for a clip pulled from membro, the durable
        home's content address for it. The local bytes are trimmed here,
        so they stop hashing to that address - corrections and the push
        diff must speak to membro by this stamp, never by re-hashing the
        local file."""
        from . import voiceid
        pcm = trim_clip(voiceid.trim_dead_air(pcm or b"", sample_rate),
                        sample_rate)
        q = clip_quality(pcm, sample_rate)
        if not accepts_clip(q):
            self._record_refusal(person_id, source, q)
            return False
        with self._lock:
            data = self._load()
            person = data["people"].get(person_id)
            if person is None:
                return False
            fname = self._write_clip(pcm, sample_rate, person_id)
            clips = person.get("clips", [])
            was_sufficient = is_sufficient(clips)
            entry = {"file": fname, "seconds": q["seconds"],
                     "rms": q["rms"], "score": q["score"],
                     "sample_rate": sample_rate, "source": source,
                     "added_at": time.time()}
            if membro_sha:
                entry["membro_sha"] = str(membro_sha)
            if score is not None:
                try:
                    entry["match_score"] = round(float(score), 4)
                except (TypeError, ValueError):
                    pass
            clips.append(entry)
            # Keep-best-N ranks ACTIVE clips only (#28 PR-B): quarantined
            # clips are set aside, not competing, and the keep policy may
            # neither evict them nor be crowded out by them.
            quarantined = [c for c in clips if c.get("quarantined")]
            kept = select_keep(active_clips(clips))
            person["clips"] = kept + quarantined
            # #83: person-level provenance, rotation-proof. A bank is
            # VOUCHED the moment a human stands behind a clip in it -
            # introduction or owner correction - even if that clip is later
            # rotated out. The moment a bank crosses sufficiency is
            # recorded too: crossing with nobody having vouched is exactly
            # the phantom shape (#65), and such a bank must earn the
            # owner's ear before remembered-first may re-seat it.
            if source in VOUCH_SOURCES and not person.get("vouched_at"):
                person["vouched_at"] = time.time()
                person["vouched_by"] = source
            if (not was_sufficient and is_sufficient(person["clips"])
                    and not person.get("sufficiency_crossed_at")):
                person["sufficiency_crossed_at"] = time.time()
            self._save(data)
            kept_files = {c["file"] for c in kept} | {c["file"]
                                                      for c in quarantined}
            for c in clips:
                if c["file"] not in kept_files:
                    self._delete_file(c["file"])
        return True

    def _record_refusal(self, person_id, source, q):
        """A refused clip is not silent (#312). The failing measure lands
        in the log, and the refusal lands on the person, so the panel can
        say "still learning, and here is why nothing is arriving". Nothing
        is stored but times, a reason and counts - never audio."""
        reason = ("too short" if q["seconds"] < MIN_CLIP_SECONDS
                  else "too quiet" if q["rms"] < MIN_CLIP_RMS
                  else "not speech")
        log.info("anchor clip refused: person=%s source=%s reason=%s "
                 "seconds=%.2f rms=%d voiced=%.2f",
                 person_id, source, reason, q["seconds"], q["rms"],
                 q.get("voiced", 0.0))
        with self._lock:
            data = self._load()
            person = data["people"].get(person_id)
            if person is None:
                return
            rec = person.setdefault(
                "clip_refusals", {"recent": [], "last_reason": "",
                                  "total": 0})
            rec["recent"] = ((rec.get("recent") or [])[-(REFUSAL_KEEP - 1):]
                             + [time.time()])
            rec["last_reason"] = reason
            rec["total"] = int(rec.get("total") or 0) + 1
            self._save(data)

    def clips_of(self, person_id: str) -> list | None:
        """One person's clip METADATA for the audition panel (#68): file
        token, source, timestamps, seconds, quality score, quarantine state.
        None for an unknown person. No audio leaves here - the panel fetches
        each clip's bytes separately, and only after this list has proven
        the file token belongs to this person."""
        with self._lock:
            data = self._load()
        person = data["people"].get(person_id)
        if person is None:
            return None
        return [{
            "file": c["file"],
            "source": c.get("source", "?"),
            "added_at": c.get("added_at", 0),
            "seconds": round(float(c.get("seconds", 0)), 1),
            "score": round(float(c.get("score", 0)), 1),
            "quarantined": bool(c.get("quarantined")),
            # #219: WHY it was set aside - "contaminated" (sounded like
            # another person) or "not_speech" (not a voice at all). Empty
            # for clips quarantined before reasons existed.
            "quarantine_reason": c.get("quarantine_reason", "")
            if c.get("quarantined") else "",
            # #90: owner-reassigned clips say so - the strongest provenance.
            "moved": bool(c.get("moved_at")),
        } for c in sorted(person.get("clips", []),
                          key=lambda c: c.get("added_at", 0), reverse=True)]

    def membro_stamps(self, person_id: str) -> dict:
        """{clip filename: membro sha} for clips pulled from membro (#310):
        the durable home's content address, which the trimmed local bytes
        no longer hash to. The sync pushes and corrects by these; the
        clip panel never sees them (clips_of stays the pinned surface)."""
        with self._lock:
            data = self._load()
        person = data["people"].get(person_id)
        return {c["file"]: c["membro_sha"]
                for c in (person or {}).get("clips", [])
                if c.get("membro_sha")}

    def clip_path(self, person_id: str, fname: str) -> Path | None:
        """Resolve a clip file token to its on-disk path, ONLY when the index
        says that clip belongs to that person - the client's file token is
        never trusted as a path (#68). None otherwise."""
        with self._lock:
            data = self._load()
        person = data["people"].get(person_id)
        if person is None:
            return None
        if not any(c.get("file") == fname for c in person.get("clips", [])):
            return None
        path = self.root / fname
        return path if path.is_file() else None

    def move_clip(self, person_id: str, fname: str, to_person_id: str) -> bool:
        """Refile ONE clip under the person it actually belongs to (#90):
        the owner heard it and it is someone else's voice. The audio stays
        on disk untouched; only the index changes hands. Both banks
        re-derive from what remains on their next read.

        A quarantine flag is cleared by the move: the hygiene audit judged
        the clip against the WRONG owner, and the next audit re-evaluates
        it against the right one. The move is stamped (moved_at/moved_from)
        because owner-reassigned is the strongest provenance a clip can
        carry - stronger than any automated capture path."""
        if person_id == to_person_id:
            return False
        with self._lock:
            data = self._load()
            src = data["people"].get(person_id)
            dst = data["people"].get(to_person_id)
            if src is None or dst is None:
                return False
            clip = next((c for c in src.get("clips", [])
                         if c.get("file") == fname), None)
            if clip is None:
                return False
            src["clips"] = [c for c in src["clips"] if c.get("file") != fname]
            clip.pop("quarantined", None)
            clip["moved_from"] = person_id
            clip["moved_at"] = time.time()
            dst.setdefault("clips", []).append(clip)
            # #33 slice 3: the correction must reach the durable home too.
            # A pulled clip's local bytes are trimmed (#310), so its stamp,
            # not a re-hash, is what membro knows it by.
            sha = clip.get("membro_sha") or self._clip_sha(fname)
            if sha:
                self._record_correction(data, {
                    "kind": "move", "from": person_id,
                    "to": to_person_id, "sha": sha})
            self._save(data)
        return True

    def delete_clip(self, person_id: str, fname: str) -> bool:
        """Delete ONE clip from a person's bank (#68): the owner heard it
        and it is wrong. The file goes from disk, the index entry goes, and
        everything derived - sufficiency, capacity, the anchor prefix -
        recomputes from what remains on its next read. Deleting the last
        clip leaves the person known but unlearnt (anchor pending), exactly
        the state a fresh introduction produces."""
        with self._lock:
            data = self._load()
            person = data["people"].get(person_id)
            if person is None:
                return False
            clips = person.get("clips", [])
            entry = next((c for c in clips if c.get("file") == fname), None)
            if entry is None:
                return False
            person["clips"] = [c for c in clips if c.get("file") != fname]
            # #33 slice 3: record before the bytes go, so the durable
            # home's copy is deleted too instead of resurrecting later.
            # The stamp, when present, is the address membro knows (#310).
            sha = entry.get("membro_sha") or self._clip_sha(fname)
            if sha:
                self._record_correction(data, {
                    "kind": "delete", "from": person_id, "sha": sha})
            self._save(data)
            self._delete_file(fname)
        return True

    def forget(self, person_id: str, record: bool = True) -> bool:
        """Delete a remembered person: every clip file AND the index entry.
        This is the privacy contract behind the roster UI's forget button -
        after it returns, no audio of that person remains on disk.

        The durable home must forget them too, or the next sync pass
        rebuilds the person from membro's copy. A person membro knows is
        recorded in the correction ledger by slug (the local id is gone
        with the entry), and person_sync replays it as membro's own
        forget: audio deleted there, their approved facts back to review.
        `record=False` is for the sync pass itself, when the forget
        ORIGINATED in membro and there is nothing to send back."""
        with self._lock:
            data = self._load()
            person = data["people"].pop(person_id, None)
            if person is None:
                return False
            data["close_pairs"] = [p for p in (data.get("close_pairs") or [])
                                   if person_id not in p[:2]]
            slug = person.get("membro_slug")
            if record and slug:
                self._record_correction(data, {"kind": "forget",
                                               "slug": slug})
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
            # Quarantined clips never ride the prefix (#28 PR-B): a clip the
            # hygiene audit judged closer to someone else's voice would seed
            # exactly the cross-matching it was set aside to prevent.
            clips = [c for c in active_clips(person.get("clips", []))
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

    def retract_utterance_clips(self, person_id: str, pcm: bytes) -> int:
        """Remove the clips ONE utterance banked to this person (#220): an
        introduction turn's own words named someone else, so the audio the
        voice match banked here is contested. The accumulated clip is the
        (trimmed) utterance and the harvested short slice a middle cut of
        it, so a clip is this utterance's exactly when its stored bytes are
        a contiguous slice of the utterance. Only automated match captures
        are eligible - a clip a human stood behind is never retracted by an
        automated path. Deletions are recorded for the durable home like an
        owner correction, so the clip cannot resurrect through a rebuild.
        Returns how many clips were removed."""
        if not pcm:
            return 0
        with self._lock:
            data = self._load()
            person = data["people"].get(person_id)
            if person is None:
                return 0
            keep, gone, shas = [], [], {}
            for c in person.get("clips", []):
                contested = False
                if c.get("source") in ("accumulated", "harvested-short"):
                    try:
                        clip_pcm = self._read_clip_pcm(c["file"])
                        contested = bool(clip_pcm) and clip_pcm in pcm
                    except OSError:
                        log.warning("anchor clip unreadable: %s", c["file"])
                if contested:
                    gone.append(c["file"])
                    if c.get("membro_sha"):
                        shas[c["file"]] = c["membro_sha"]
                else:
                    keep.append(c)
            if not gone:
                return 0
            person["clips"] = keep
            for fname in gone:
                sha = (shas.get(fname)
                       or self._clip_sha(fname))
                if sha:
                    self._record_correction(data, {
                        "kind": "delete", "from": person_id, "sha": sha})
            self._save(data)
            for fname in gone:
                self._delete_file(fname)
        log.info("contested clips retracted: person=1 clips=%d", len(gone))
        return len(gone)

    def enrollment_clips(self, person_ids: list, sample_rate: int,
                         max_clips: int = ENROLL_CLIPS) -> dict:
        """Per-person clip PCM for the local voice-id matcher (#28 part 2):
        the raw store data the offline matcher averages into one embedding per
        person. Mirrors build_prefix's gates - only SUFFICIENT people, only
        clips recorded at the requested sample rate, best clips first - but
        keeps the clips SEPARATE (per-clip embeddings are L2-normalised and
        averaged; a hard-cut concatenation would embed the seams, not the
        voice) and reads at most `max_clips` per person.

        Returns {person_id: {"name", "fingerprint", "pcms"}}. `fingerprint` is
        the tuple of kept clip filenames: the matcher keys its embedding cache
        on it, so identification re-embeds a person ONLY when their kept clip
        set actually changes (anchor accumulation that displaces a clip), not
        on every store save the way the coarse index fingerprint would - the
        same 'invalidate only on real change' intent as build_prefix's cache,
        one level finer. No audio leaves the process; this is read only."""
        with self._lock:
            data = self._load()
        out = {}
        for pid in person_ids:
            person = data["people"].get(pid)
            if not person:
                continue
            # Quarantined clips are excluded from enrolment too (#28 PR-B) -
            # they are the contamination the hygiene audit found.
            clips = [c for c in active_clips(person.get("clips", []))
                     if c.get("sample_rate") == sample_rate]
            if not is_sufficient(clips):
                continue
            take = sorted(clips, key=lambda c: c["score"],
                          reverse=True)[:max_clips]
            # A short clip joins the enrolment mix when one exists (#28
            # PR-B): the averaged embedding then carries what a one-second
            # interjection actually sounds like, not only long-form speech.
            if not any(is_short(c) for c in take):
                shorts = sorted((c for c in clips if is_short(c)),
                                key=lambda c: c["score"], reverse=True)
                if shorts:
                    take = take[:max_clips - 1] + [shorts[0]]
            pcms, files = [], []
            for c in take:
                try:
                    pcms.append(self._read_clip_pcm(c["file"]))
                    files.append(c["file"])
                except OSError:
                    log.warning("anchor clip unreadable: %s", c["file"])
            if pcms:
                out[pid] = {"name": person.get("name", pid),
                            "fingerprint": tuple(files), "pcms": pcms}
        return out

    # -- the pairwise hygiene guard's storage (#28 PR-B) --
    #
    # The AUDIT itself lives in voiceid.py (it needs the embedding model);
    # this store only holds its verdicts: a per-clip `quarantined` flag
    # (set-aside clips stay on disk but take part in nothing) and the
    # store-level `close_pairs` list of enrolled voices whose centroids sit
    # suspiciously close (the matcher widens its margin for those pairs).

    def bank_clips(self, sample_rate: int) -> dict:
        """EVERY clip per person for the hygiene audit - quarantined ones
        included, so a clip set aside under an old bank shape can be
        reinstated when the evidence changes. {pid: {"name",
        "clips": [(fname, pcm), ...]}}; unreadable files are skipped."""
        with self._lock:
            data = self._load()
        out = {}
        for pid, person in data["people"].items():
            clips = []
            for c in person.get("clips", []):
                if c.get("sample_rate") != sample_rate:
                    continue
                try:
                    clips.append((c["file"], self._read_clip_pcm(c["file"])))
                except OSError:
                    log.warning("anchor clip unreadable: %s", c["file"])
            if clips:
                out[pid] = {"name": person.get("name", pid), "clips": clips}
        return out

    def clip_fingerprint(self):
        """Change detector for the audit: the full clip-file sets, per
        person, sorted. Quarantine flags do NOT enter it - the audit WRITES
        those, and a fingerprint that moved on every audit would re-audit
        forever."""
        with self._lock:
            data = self._load()
        return tuple(sorted(
            (pid, tuple(sorted(c["file"] for c in p.get("clips", []))))
            for pid, p in data["people"].items()))

    def set_hygiene(self, quarantine: dict, close_pairs: list):
        """Persist one audit's verdicts. `quarantine`: {pid: {clip filename:
        reason}} - or an iterable of filenames, read as the pairwise
        audit's "contaminated" (#219 added the second reason, "not_speech").
        Every clip not named is reinstated, so the audit's output IS the
        quarantine state. `close_pairs`: [(pid_a, pid_b, cosine), ...].
        Set-aside clips past QUARANTINE_MAX per person are deleted for real
        (oldest first) - set aside is not a licence to hoard audio."""
        norm = {}
        for pid, files in (quarantine or {}).items():
            if isinstance(files, dict):
                norm[pid] = {f: str(r or "contaminated")
                             for f, r in files.items()}
            else:
                norm[pid] = {f: "contaminated" for f in files or ()}
        with self._lock:
            data = self._load()
            for pid, person in data["people"].items():
                bad = norm.get(pid, {})
                kept, evict = [], []
                for c in person.get("clips", []):
                    c["quarantined"] = c["file"] in bad
                    if c["quarantined"]:
                        c["quarantine_reason"] = bad[c["file"]]
                    else:
                        c.pop("quarantine_reason", None)
                    kept.append(c)
                q = [c for c in kept if c["quarantined"]]
                if len(q) > QUARANTINE_MAX:
                    q.sort(key=lambda c: c.get("added_at", 0))
                    evict = q[:len(q) - QUARANTINE_MAX]
                    gone = {c["file"] for c in evict}
                    kept = [c for c in kept if c["file"] not in gone]
                person["clips"] = kept
                for c in evict:
                    self._delete_file(c["file"])
            data["close_pairs"] = [
                [a, b, round(float(cos), 3)] for a, b, cos in close_pairs or []
                if a in data["people"] and b in data["people"]]
            self._save(data)

    def close_pairs(self) -> list:
        """The stored close-pair verdicts, as [(pid_a, pid_b, cosine)...]."""
        with self._lock:
            data = self._load()
        return [tuple(p) for p in data.get("close_pairs") or []
                if isinstance(p, (list, tuple)) and len(p) >= 2]


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


def peek_audio(message_id: int):
    """Read a stashed utterance WITHOUT claiming it (#28, tenth field test).
    The correction path checks the audio against the owner's bank before
    deciding whose record to feed, and the feed itself still needs the same
    entry afterwards, so this read must not consume it."""
    with _recent_lock:
        return _recent_audio.get(message_id)


def clear_recent_audio():
    with _recent_lock:
        _recent_audio.clear()
