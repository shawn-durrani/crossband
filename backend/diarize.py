"""Room mode (#28): the never-awaited identity passes.

THE CORE PRINCIPLE - zero added latency on the live voice path. The realtime
STT relay (routers/voice.py) stays byte-for-byte identical upstream whether
room mode is on or off; when it is on, the relay only TEES already-decoded
utterance audio into a per-session buffer and, on each commit boundary, fires
a fire-and-forget task from here. Nothing in the live path ever awaits that
task, and every blocking step inside it (any batch call, every database
touch) runs on a worker thread via asyncio.to_thread, so even the shared
event loop never stalls on it.

Identity is LOCAL or honestly uncertain (#28 PR-B, owner decision after the
eighth field test - the cloud identity fallback is retired). Each committed
utterance runs the on-device matcher (backend/voiceid.py): a confident match
names the turn in ~100-300ms; anything the matcher cannot decide leaves the
turn UNRESOLVED - the projection's pending head simply never resolves and the
turn renders as today's unlabelled/uncertain states. NO ElevenLabs call is
ever fired because the matcher deferred. The batch Scribe v2 diarize call
keeps exactly ONE trigger: the matcher's window analysis returning the
"multi" verdict (genuinely overlapping speech), which routes the per-word
crosstalk split - anchors prefix the request so its clusters read back into
names, and the phase-4 marking/splitting machinery runs unchanged from
there. That split is room mode's only remaining cloud voice spend.

Arming is local too: the ambient check (below) notices remembered voices
with no cloud help, and when the matcher is unavailable automatic arming
simply does not happen - introductions, spoken commands and the toggle still
arm, so degraded means manual, never wrong.

The speculative head start (#28 PR-B): the client notices the silence gap
before the commit frame and sends a content-free hint; the relay fires the
local check on the buffered utterance THEN, so the verdict is usually cached
by the time the commit arrives and the name attaches with the transcript
instead of racing it.

Failure posture: a failed or slow pass leaves the message unlabelled and
everything else untouched. No retry ever feeds back into the live path.
"""

import asyncio
import concurrent.futures
import functools
import logging
import re
import struct
import time

from . import db, voice, voiceid

log = logging.getLogger("crossband.diarize")

# #133: ALL of this module's thread work runs on its own bounded executor,
# never the default asyncio.to_thread pool. The identity pass is
# fire-and-forget by design, but its heavy steps (clip banking, the
# pairwise hygiene audit, crosstalk's synchronous cloud call) were queueing
# on the SAME default executor /send needs to persist the user's message -
# so a still-learning guest's every utterance starved round dispatch and
# the room sat in "listening". Two workers: identity work is sequential
# per-utterance anyway, and a bounded queue here can never crowd out a
# request thread again.
_VOICE_EXECUTOR = concurrent.futures.ThreadPoolExecutor(
    max_workers=2, thread_name_prefix="voiceid")


def _in_voice_thread(fn, /, *args, **kwargs):
    """Awaitable run of `fn` on the DEDICATED voice executor - the drop-in
    replacement for asyncio.to_thread everywhere in this module."""
    loop = asyncio.get_running_loop()
    return loop.run_in_executor(_VOICE_EXECUTOR,
                                functools.partial(fn, *args, **kwargs))

# Utterance buffer cap: ~2 minutes of PCM-16 mono. Past it we keep the TAIL
# (the newest audio) - an utterance that long has long since ceased to be one
# utterance, and an unbounded buffer is a memory leak with a microphone.
MAX_UTTERANCE_SECONDS = 120

# How long the pass keeps looking for the user message to label. The message
# is inserted by the client's own /send a moment after commit, so the match
# normally lands on the first probe; the window only bounds the give-up.
#
# #28 phase 3: when the commit frame carried the client's voice-trace turn id,
# the match is EXACT - the pass labels the one message whose persisted
# voice_turn_id equals it, and nothing else. A dropped short interjection
# (transcript discarded client-side, so no /send, so no row) then labels
# NOTHING instead of smearing onto a neighbouring turn - the field-test
# defect. The time-window rules below survive only as the fallback for a
# commit frame with no turn id (an older client), where they behave exactly
# as phases 1-2:
# No backward slack on the window match: the utterance's message is ALWAYS
# stamped after its commit instant (the client dispatches only once the
# committed transcript returns, and both timestamps come from this process's
# clock), and reaching back before the commit could grab the PREVIOUS turn's
# unlabelled message when utterances arrive quickly.
MATCH_WINDOW_SECS = 8.0
MATCH_PROBE_SECS = 0.5

# Attach immediately on an exact turn id (#28, night test 4). With the id the
# target row is a direct lookup, so the pass attaches the moment the batch
# reply parses instead of riding a probe cadence. The only reason the row can
# be missing is the /send race - the client dispatches once the committed
# transcript returns (~0.3-0.9s after commit) while the batch reply takes
# 1.0-1.9s, so the row is normally already there - and these two constants
# bound a short fast retry for exactly that race. A row that never appears is
# a dropped interjection, which must label nothing; giving up early is
# correct there. The MATCH_PROBE_SECS cadence above survives ONLY for id-less
# commits (older clients), where time-window matching genuinely has to wait.
ID_ATTACH_RETRY_SECS = 0.05
ID_ATTACH_WINDOW_SECS = 2.0

# _attach_labels' "the row is not persisted yet - retry" verdict, distinct
# from None ("nothing to label, stop"): only the /send race retries.
_NO_ROW_YET = object()

# Strong references to in-flight passes: asyncio only holds weak refs to
# tasks, and a garbage-collected fire-and-forget task silently never runs.
_TASKS: set = set()

# ---- server-side room-mode registry (#28 phase 2) ----
#
# The durable truth is chats.room_mode; this dict is the LIVE mirror the STT
# relay consults at each commit boundary - a plain dict lookup, no I/O, so the
# audio path never touches the database. Writers: the relay's own init (seeded
# from the chat row, off the audio path), the introduction scan, and the chat
# PATCH override. A missing entry means "off", which is also the durable
# default - so a process restart is consistent by construction.
_ROOM_ENABLED: dict = {}

# Last finished utterance per chat while room mode was OFF, so a confirmed
# introduction can claim the audio that spoke it as the OWNER's first anchor.
# Bounded three ways (one utterance per chat, tail-capped, at most
# _STASH_MAX_CHATS chats, TTL'd on read) and in-memory only - nothing here
# ever touches disk unless an introduction promotes it to an anchor clip.
_STASHED: dict = {}
_STASH_MAX_CHATS = 8
INTRO_STASH_SECONDS = 30
INTRO_STASH_TTL_S = 180.0

# ---- verdicts waiting for their message row (#28, twelfth field test) ----
#
# THE ORDERING BUG, and it was ordering all along rather than speed: the pass
# can only write a label AFTER /send creates the row to write it on, but
# /send dispatches the round in the same breath and the round renders its
# transcript once, immediately. So the label for the turn a model is ANSWERING
# landed microseconds too late, every single time. The models truthfully read
# "identity pending" on the very turn the browser already showed as confirmed
# - the browser gets a live update, the model's copy is frozen at dispatch.
# No amount of making the pass faster could fix that: the label has to be ON
# the row at INSERT, not written to it afterwards.
#
# So a finished verdict parks HERE, keyed by the client's turn id, and /send
# claims it inside the same insert. Bounded and TTL'd (a verdict nobody claims
# belongs to a dropped interjection); in-memory because this is a handoff
# measured in hundreds of milliseconds, not state worth persisting.
_PENDING_LABELS: dict = {}
_PENDING_MAX = 32
PENDING_LABEL_TTL_S = 30.0


def park_label(turn_id, payload):
    """Park a finished label for a turn whose row may not exist yet."""
    if not turn_id or not payload:
        return
    now = time.monotonic()
    for k, (_, at) in list(_PENDING_LABELS.items()):
        if now - at > PENDING_LABEL_TTL_S:
            _PENDING_LABELS.pop(k, None)
    _PENDING_LABELS[turn_id] = (dict(payload), now)
    while len(_PENDING_LABELS) > _PENDING_MAX:
        _PENDING_LABELS.pop(next(iter(_PENDING_LABELS)))


def claim_label(turn_id):
    """Claim a parked label at insert time (single-use). None when the check
    has not finished yet - the pass then labels the row the old way, exactly
    as before, so this is a fast path and never a dependency."""
    entry = _PENDING_LABELS.pop(turn_id or "", None)
    if not entry:
        return None
    payload, at = entry
    return payload if (time.monotonic() - at) <= PENDING_LABEL_TTL_S else None


MISMATCH_MIN_WORDS = 4  # don't cross-check a grunt
# Fast-path (#28 part 2) mismatch gate: with no batch words to count, use the
# utterance duration as the "enough words to be worth cross-checking" proxy
# (~4 words is roughly 1.5s of speech).
FAST_MISMATCH_MIN_SECONDS = 1.5

# ---- ambient room detection (#28) ----
#
# The arming ceremony (introduction, command, or the retired session-start
# sniff) existed to gate a real cost: identity used to require a second cloud
# transcription per utterance. The local matcher (voiceid.py) made identity
# ~free - one on-device embed, ~33-59ms, offline - so the gate loses its
# reason to exist. With ambient detection on, EVERY committed utterance in a
# room-mode-OFF session gets a quiet LOCAL-ONLY check (never an ElevenLabs
# call): the owner's own voice never arms anything but DOES label the turn
# (#28 PR-C: the owner's identity is shown, not hidden - a confident owner
# match is a fact worth telling the seats), a remembered non-owner arms room
# mode automatically, and a clear voice that matches nobody (only decidable
# when the owner is itself sufficiently enrolled) arms and raises the
# ask-fallback. Anything the matcher cannot decide defers - room mode stays
# off and only the introduction/command/toggle doors remain (#28 PR-B: the
# bounded EL sniff that used to catch defers is retired with the rest of the
# cloud identity path; ambient is now the ONLY automatic arming door).
#
# DISARM IS SACRED: after the owner says "solo mode", chats.ambient_off is set
# and ambient never re-arms that chat until an explicit re-enable (a command,
# an introduction, or the manual toggle) clears it. This live mirror lets the
# relay honour it without a DB read on the audio path, exactly like
# _ROOM_ENABLED.
_AMBIENT_OFF: dict = {}


def set_ambient_off(chat_id, off: bool):
    if chat_id is None:
        return
    if off:
        _AMBIENT_OFF[chat_id] = True
    else:
        _AMBIENT_OFF[chat_id] = False


def ambient_off(chat_id) -> bool:
    return bool(_AMBIENT_OFF.get(chat_id))


def ambient_eligible(people, cfg) -> bool:
    """Should this session run the ambient local check at all? Only when the
    matcher is enabled AND at least one SUFFICIENT remembered voice exists to
    match against. Computed once at session open, off the audio path, into a
    session flag - the per-commit path is then a plain bool. With nobody
    remembered, the common no-household case costs nothing and behaves exactly
    as before ambient existed.

    A COLD owner (#28, cold-start) is deliberately NOT eligible, and the bar
    stays where it is. With no sufficient voice there is nothing to match
    against, so identify_utterance can only answer "no_candidates" and
    ambient_decision can only defer: the check would embed audio on every
    committed utterance to reach a foregone conclusion. It could not arm
    either (with an empty bank it cannot tell the owner from a stranger) and
    it must not bank (same reason), so a cold session would gain nothing it
    could honestly show. Cold-start enrolment therefore lives on the ARMED
    path only - see cold_start_person - where the roster proves who is in
    the room."""
    return bool(voiceid.enabled(cfg)) and any(
        p.get("sufficient") for p in people or [])


def owner_sufficient(people, owner_name) -> bool:
    """Is the owner's OWN voice sufficiently enrolled? Only then can a
    below-threshold match mean 'a clear voice that is not the owner' rather
    than 'the owner, not yet learnt'. Without this, ambient must never arm on
    an unknown - it could be the owner."""
    owner = (owner_name or "").strip().casefold()
    return any(p.get("sufficient")
               and (p.get("name") or "").strip().casefold() == owner
               for p in people or [])


# Ambient decisions, as a pure rule over a matcher verdict (unit-tested with
# no I/O). `owner` is casefolded; `owner_ok` is owner_sufficient() for the
# roster. Outcomes: "noop_owner" (owner spoke - label the turn as the owner,
# voice-confirmed, but never arm; #28 PR-C), "arm_known"
# (a remembered non-owner - arm and name), "arm_unknown" (a clear stranger,
# only when the owner is enrolled so we know it is not them - arm and ask),
# or "defer" (matcher could not decide, or could not rule out the owner).
def ambient_decision(verdict, owner, owner_ok) -> str:
    owner = (owner or "").strip().casefold()
    if voiceid.matched(verdict):
        return "noop_owner" if (verdict.get("name") or "").strip().casefold() == owner \
            else "arm_known"
    if owner_ok and verdict and verdict.get("reason") == "below_threshold":
        return "arm_unknown"
    return "defer"


# ---- cold-start enrolment (#28) -----------------------------------------
#
# THE DEADLOCK this breaks. Every enrolment door needs something a person
# who has just forgotten their voice does not have: a confident match needs
# a bank, the introduction scan needs an introduction-shaped sentence, and
# tap-to-correct needs a label to tap. So an empty bank stayed empty - the
# matcher deferred on every turn ("below_threshold" or "no_candidates" over
# and over), nothing accumulated, and the seats said "identity pending"
# forever.
#
# The way out is elimination, not recognition. In an ARMED room where
# exactly ONE present person's bank cannot yet identify them - and every
# OTHER present person's bank can - an utterance the matcher could not
# place can only be theirs: everyone else in the room would have matched.
# That is enough to bank the audio and to tell the seats a name, honestly
# marked as still being learned. (Generalised by the fourteenth field test:
# the rule used to require exactly one present person TOTAL, which meant a
# genuinely new guest could never start learning while the owner sat
# identified beside them.)
#
# The guards are what keep it conservative:
# - a MULTI verdict never qualifies. Overlapping speech is the one thing
#   elimination cannot survive, and it has its own path.
# - a NOT_SPEECH verdict (#217) never qualifies. Elimination answers WHO
#   spoke; the speech gate just said nobody did, and banking a static burst
#   would poison the pending person's first clips.
# - two or more UNIDENTIFIABLE present people never qualify. The audio
#   could be either of them, and that is exactly what the ask-fallback
#   exists for.
# - a confident MATCH never qualifies (it is not a defer): a named turn
#   already has a better answer, and the normal accumulation path takes it.
# - a matcher that FAILED outright (verdict None) never qualifies: banking
#   is cheap to get wrong and free to skip, so an unknown state skips.
COLD_START_SOURCE = "cold-start"


def cold_start_person(verdict, solo_pending):
    """Who this unplaceable utterance can only belong to, or None (pure,
    unit-tested). `solo_pending` is _room_plan's by-elimination candidate:
    the name of the ONE present person whose bank is insufficient while
    every other present person's is sufficient, and None whenever the
    roster does not hold exactly that. `verdict` is the local matcher's
    answer for this utterance."""
    if not solo_pending:
        return None
    if not verdict or verdict.get("status") != voiceid.DEFER:
        return None
    if verdict.get("reason") in (voiceid.MULTI, voiceid.NOT_SPEECH):
        return None
    return solo_pending


def set_room_enabled(chat_id, on: bool):
    if chat_id is None:
        return
    if on:
        _ROOM_ENABLED[chat_id] = True
    else:
        _ROOM_ENABLED.pop(chat_id, None)


def room_enabled(chat_id) -> bool:
    return bool(_ROOM_ENABLED.get(chat_id))


def stash_utterance(chat_id, pcm: bytes, sample_rate: int):
    """Remember the freshest finished utterance for a chat (room mode off
    path). Tail-capped: the introduction sentence is at the END of the
    utterance's audio if anywhere."""
    if chat_id is None or not pcm:
        return
    cap = INTRO_STASH_SECONDS * (sample_rate or 16000) * 2
    _STASHED.pop(chat_id, None)  # re-insert = newest (dicts keep insert order)
    _STASHED[chat_id] = (pcm[-cap:], sample_rate, time.monotonic())
    while len(_STASHED) > _STASH_MAX_CHATS:
        _STASHED.pop(next(iter(_STASHED)))


def take_stashed_utterance(chat_id):
    """Claim (and clear) the stashed utterance, if it is still fresh enough
    to plausibly be the introduction the scan just confirmed."""
    entry = _STASHED.pop(chat_id, None)
    if not entry:
        return None
    pcm, sample_rate, at = entry
    if time.monotonic() - at > INTRO_STASH_TTL_S:
        return None
    return pcm, sample_rate


def peek_stashed_utterance(chat_id):
    """Read the stashed utterance WITHOUT claiming it (#28: the introduction
    scan's voice-match arm) - the owner-anchor seed path still decides
    whether to consume it. Same freshness rule as take_stashed_utterance."""
    entry = _STASHED.get(chat_id)
    if not entry:
        return None
    pcm, sample_rate, at = entry
    if time.monotonic() - at > INTRO_STASH_TTL_S:
        return None
    return pcm, sample_rate


# ---- the last identification decision (#28: the voice health strip) ----
#
# A tiny in-memory per-chat record of the most recent identification
# decision: which path decided (local matcher or the cloud batch pass) and
# how long it took. Content-free BY CONSTRUCTION - path, milliseconds and a
# monotonic timestamp; never a name, never transcript text - and written
# only from inside the already-never-awaited background passes, so the live
# voice path gains nothing. GET /api/voice/health surfaces it.

_LAST_DECISION: dict = {}
_DECISION_MAX_CHATS = 8

DECISION_LOCAL = "local"
DECISION_CLOUD = "cloud"
# A turn the matcher declined to name (#28, thirteenth field test). The
# matcher ALREADY computes why - "too_short", "below_threshold", "ambiguous"
# - and used to drop it into a log line, so "identity pending" hid two very
# different problems behind one word: audio too poor to judge, versus heard
# clearly but not sure who. Carrying the string costs nothing (it is already
# in hand) and is what lets the strip say which.
DECISION_UNRESOLVED = "unresolved"

# The allowlist for what may leave the process as a reason - content-free by
# construction, like every other value here.
DEFER_REASONS = {"too_short", "below_threshold", "ambiguous", "multi",
                 "not_speech", "no_candidates", "unavailable", "disabled",
                 "error"}


def record_decision(chat_id, path, ms, reason=""):
    """Remember one chat's freshest identification decision. Bounded like
    the stash: at most _DECISION_MAX_CHATS chats, one record each."""
    if chat_id is None or path not in (DECISION_LOCAL, DECISION_CLOUD,
                                       DECISION_UNRESOLVED):
        return
    reason = reason if reason in DEFER_REASONS else ""
    _LAST_DECISION.pop(chat_id, None)  # re-insert = newest (insert order)
    _LAST_DECISION[chat_id] = {"path": path, "ms": round(float(ms), 1),
                               "reason": reason, "at": time.monotonic()}
    while len(_LAST_DECISION) > _DECISION_MAX_CHATS:
        _LAST_DECISION.pop(next(iter(_LAST_DECISION)))


def last_decision(chat_id):
    """The freshest decision for a chat as {"path", "ms", "reason", "age_s"},
    or None. age_s lets the client say how stale the pulse is without sharing
    clocks; reason is "" unless the path is unresolved."""
    entry = _LAST_DECISION.get(chat_id)
    if not entry:
        return None
    return {"path": entry["path"], "ms": entry["ms"],
            "reason": entry.get("reason", ""),
            "age_s": round(time.monotonic() - entry["at"], 1)}


# ---------- pure rules (unit-tested directly, no I/O) ----------

def pcm16_wav(pcm_bytes: bytes, sample_rate: int) -> bytes:
    """Wrap raw PCM-16 mono in a minimal WAV container - what the batch STT
    endpoint expects as a file upload."""
    n = len(pcm_bytes)
    header = struct.pack(
        "<4sI4s4sIHHIIHH4sI",
        b"RIFF", 36 + n, b"WAVE", b"fmt ", 16,
        1,                      # PCM
        1,                      # mono
        sample_rate,
        sample_rate * 2,        # byte rate
        2,                      # block align
        16,                     # bits per sample
        b"data", n,
    )
    return header + pcm_bytes


def utterance_clusters(words) -> list:
    """Ordered, de-duplicated speaker clusters across one utterance's words.
    Only real word entries with a speaker_id count; spacing/audio-event
    entries and unattributed words are ignored."""
    seen = []
    for w in words or []:
        if not isinstance(w, dict):
            continue
        if w.get("type") not in (None, "word"):
            continue
        sid = w.get("speaker_id")
        if sid and sid not in seen:
            seen.append(sid)
    return seen


# ---- crosstalk rules (#28 phase 4; pure, unit-tested) ----
#
# The batch pass's word list carries a per-word speaker map and timestamps.
# Reducing it straight to a cluster list (utterance_clusters) throws away the
# only evidence of people speaking OVER each other - and on a single
# microphone, the quieter voice's overlapped words are often unrecoverable
# from the audio, so the honest move is to SAY SO rather than present the
# transcript as complete. These rules read the word map before it is reduced:
# a turn whose words carry two or more speakers is marked as crosstalk, and
# when the words alternate cleanly (no overlapping intervals) a best-effort
# attributed split is offered as METADATA - message content itself is never
# rewritten. Absence of interleaved words never proves absence of overlap;
# the marker's wording stays honest about that.

_OVERLAP_EPS = 0.02   # seconds; timestamps straddling by less are not overlap
MAX_SEGMENTS = 12     # more alternations than this in one utterance is noise
MAX_SEGMENT_CHARS = 400


def _timed_words(words) -> list:
    """Real word entries with a speaker and usable timestamps, in time order."""
    out = []
    for w in words or []:
        if not isinstance(w, dict) or w.get("type") not in (None, "word"):
            continue
        if not w.get("speaker_id") or w.get("start") is None:
            continue
        out.append(w)
    return sorted(out, key=lambda w: w["start"])


def words_overlap(words) -> bool:
    """Do word intervals from DIFFERENT speakers overlap in time - i.e. was
    there simultaneous speech, as opposed to rapid alternation? One sweep in
    start order, tracking each speaker's furthest end."""
    last_end: dict = {}
    for w in _timed_words(words):
        start = w["start"]
        for sid, end in last_end.items():
            if sid != w["speaker_id"] and end > start + _OVERLAP_EPS:
                return True
        sid = w["speaker_id"]
        end = w.get("end", start)
        if end is None:
            end = start
        last_end[sid] = max(last_end.get(sid, 0.0), end)
    return False


def crosstalk_info(words):
    """{"crosstalk": True, "overlap": bool} when two or more speakers share
    one utterance's words, else None. `overlap` distinguishes simultaneous
    speech (unsalvageable on one microphone) from clean alternation (which
    split_segments may be able to attribute)."""
    if len(utterance_clusters(words)) < 2:
        return None
    return {"crosstalk": True, "overlap": words_overlap(words)}


def split_segments(words, label_of, uncertain_labels=()):
    """Best-effort attributed sub-segments for a cleanly-alternating
    multi-voice utterance: consecutive same-speaker words group into
    [{"label", "text", "uncertain"}, ...] in time order. Returns [] whenever
    the split would be dishonest: any different-speaker overlap (simultaneous
    speech cannot be split from one channel), a cluster with no label, empty
    word text, or more alternations than MAX_SEGMENTS (noise, not dialogue).
    `label_of` maps cluster id -> display label; labels in `uncertain_labels`
    mark their segments uncertain."""
    timed = _timed_words(words)
    if len(utterance_clusters(timed)) < 2 or words_overlap(timed):
        return []
    segments = []
    for w in timed:
        text = (w.get("text") or "").strip()
        if not text:
            continue
        label = label_of.get(w["speaker_id"])
        if not label:
            return []
        if segments and segments[-1]["label"] == label:
            segments[-1]["text"] = (segments[-1]["text"] + " " + text)[:MAX_SEGMENT_CHARS]
        else:
            segments.append({"label": label, "text": text[:MAX_SEGMENT_CHARS],
                             "uncertain": label in uncertain_labels})
        if len(segments) > MAX_SEGMENTS:
            return []
    return segments if len(segments) >= 2 else []


def _norm_text(text) -> str:
    return re.sub(r"\s+", " ",
                  re.sub(r"[^a-z0-9]+", " ", (text or "").casefold())).strip()


def segments_align(segments, content) -> bool:
    """May the split be SHOWN against this message? Only when the batch
    pass's words, joined in order, read as the same text the realtime path
    committed (case, punctuation and spacing aside). The two transcribers
    heard the same audio but are different models; where they disagree on the
    words, an attributed split of the batch text would contradict the message
    it annotates - the marker alone is the honest fallback."""
    if not segments:
        return False
    joined = _norm_text(" ".join(s.get("text") or "" for s in segments))
    return bool(joined) and joined == _norm_text(content)


def should_label(clusters, prev_clusters) -> bool:
    """Label an utterance only when diarization actually says something:
    more than one cluster inside it, or a different cluster mix than the
    previous utterance. A lone speaker talking on (the overwhelmingly common
    case) stays unlabelled - silence is the honest default when the second
    pass found nothing new. Cross-utterance comparison is best-effort by
    nature (clusters are per-request; see the module docstring)."""
    if not clusters:
        return False
    if len(clusters) > 1:
        return True
    return prev_clusters is not None and clusters != prev_clusters


def pick_target(rows, already_labelled) -> dict | None:
    """The user message this utterance's labels belong to: the OLDEST
    still-unlabelled user row created after the commit (rows arrive id-
    ordered). Oldest, not newest - a fast next utterance may already have
    inserted its own message by the time this pass resolves."""
    for r in rows:
        if r["id"] in already_labelled:
            continue
        if r.get("voice_labels"):
            continue
        return r
    return None


# ---- anchored identification rules (#28 phase 2; pure, unit-tested) ----

def split_words_at(words, prefix_seconds: float):
    """Partition a diarized response's word list at the anchor-prefix
    boundary: (prefix_words, utterance_words). Word timestamps are relative
    to the WHOLE request (prefix + utterance); a small epsilon keeps a word
    straddling the seam out of the prefix vote rather than corrupting it."""
    eps = 0.05
    prefix, utterance = [], []
    for w in words or []:
        if not isinstance(w, dict) or w.get("type") not in (None, "word"):
            continue
        if not w.get("speaker_id"):
            continue
        start = w.get("start")
        end = w.get("end", start)
        if start is None:
            continue
        if end is not None and end <= prefix_seconds + eps:
            prefix.append(w)
        elif start >= prefix_seconds - eps:
            utterance.append(w)
    return prefix, utterance


def prefix_cluster_map(prefix_words, segments) -> dict:
    """Read the diarizer's prefix clusters back into NAMES: each prefix word
    falls inside one person's anchor segment (by midpoint); a cluster maps to
    a person only when a clear majority (>= 60%) of its prefix words landed
    in that person's segment - a cluster smeared across two people's anchors
    identifies nobody. Returns {cluster_id: name}."""
    votes: dict = {}  # cluster -> {name: count}
    for w in prefix_words:
        mid = (w["start"] + w.get("end", w["start"])) / 2
        for seg in segments:
            if seg["start"] <= mid < seg["end"]:
                per = votes.setdefault(w["speaker_id"], {})
                per[seg["name"]] = per.get(seg["name"], 0) + 1
                break
    out = {}
    for cluster, per in votes.items():
        total = sum(per.values())
        name, count = max(per.items(), key=lambda kv: kv[1])
        if total and count / total >= 0.6:
            out[cluster] = name
    return out


def resolve_room_labels(clusters, cmap, pending_names, session) -> dict:
    """The naming decision for one utterance in roster mode.

    - a cluster in the prefix map gets that person's NAME;
    - if exactly ONE unmatched cluster meets exactly ONE anchor-pending
      person, elimination names them (uncertain until their anchor is
      sufficient) - this is how a just-introduced person's anchor set gets
      its first audio;
    - any other unmatched cluster keeps a session ordinal, stays uncertain,
      and asks ("someone new is speaking - who?").

    Returns {"labels": [...], "uncertain": [...], "matched": {cluster: name},
    "eliminated": {cluster: name}, "ask": [cluster, ...]}."""
    labels, uncertain = [], []
    matched, eliminated, ask = {}, {}, []
    unmatched = [c for c in clusters if c not in cmap]
    for c in clusters:
        if c in cmap:
            matched[c] = cmap[c]
            labels.append(cmap[c])
            continue
        if len(unmatched) == 1 and len(pending_names) == 1:
            name = pending_names[0]
            eliminated[c] = name
            labels.append(name)
            uncertain.append(name)
        else:
            ordinal = session.assign([c])[0]
            labels.append(ordinal)
            uncertain.append(ordinal)
            ask.append(c)
    return {"labels": labels, "uncertain": uncertain, "matched": matched,
            "eliminated": eliminated, "ask": ask}


class RoomSession:
    """Per-websocket-session room-mode state: the utterance tee buffer and
    the label bookkeeping (cluster -> "Voice N" ordinals in first-seen order,
    the previous utterance's clusters, which messages this session already
    labelled). Session-scoped on purpose: a reconnect starts a fresh session
    and fresh ordinals, which phase 1's best-effort labels are honest about."""

    def __init__(self, enabled=False):
        self.enabled = bool(enabled)
        self.buffer = bytearray()
        self.sample_rate = 16000
        self.ordinals = {}          # cluster id -> "Voice N"
        self.prev_clusters = None   # None = no diarized utterance yet
        self.labelled_ids = set()
        # Ambient local check (#28): when true, every committed utterance runs
        # the local-only matcher while room mode is off. Seeded at session
        # open from ambient_eligible (matcher enabled + a sufficient remembered
        # voice exists); a plain bool so the per-commit path does no I/O.
        self.ambient_on = False
        # Speculative identity (#28 PR-B): the in-flight silence-start check
        # for the CURRENT utterance - {"len": buffered bytes at fire time,
        # "task": the never-awaited local check}. Claimed synchronously at
        # the commit boundary (take_speculative), so a hint can never leak
        # onto the next utterance.
        self.speculative = None

    def set_enabled(self, on: bool):
        """Toggle mid-session. Either edge clears the buffer: audio captured
        while the mode was off was never part of the deal, and a partial
        utterance from before a toggle would diarize misleadingly."""
        on = bool(on)
        if on != self.enabled:
            self.buffer.clear()
        self.enabled = on

    def add_audio(self, pcm_bytes: bytes, sample_rate: int):
        self.sample_rate = sample_rate or self.sample_rate
        self.buffer.extend(pcm_bytes)
        cap = MAX_UTTERANCE_SECONDS * self.sample_rate * 2
        if len(self.buffer) > cap:
            del self.buffer[:len(self.buffer) - cap]  # keep the tail

    def take_utterance(self):
        """Slice on the commit boundary: hand back everything buffered for
        this utterance and start the next one clean - the same boundary the
        realtime path just committed on."""
        pcm = bytes(self.buffer)
        self.buffer.clear()
        return pcm, self.sample_rate

    def peek_utterance(self):
        """The buffered audio so far WITHOUT slicing (#28 PR-B): what the
        speculative silence-start check embeds while the commit is still
        seconds away."""
        return bytes(self.buffer), self.sample_rate

    def take_speculative(self, pcm_len):
        """Claim the speculative entry for the utterance just taken (#28
        PR-B). Called synchronously at the commit boundary (a dict pop, no
        I/O), so an entry can never leak onto the next utterance. Only the
        obviously-wrong case is rejected here - a hint covering MORE audio
        than the utterance holds; the real staleness judgment (did speech
        resume after the hint?) is a tail-RMS check that runs inside the
        pass on a worker thread, never on the live path."""
        entry, self.speculative = self.speculative, None
        if entry and 0 < (entry.get("len") or 0) <= pcm_len:
            return entry
        return None

    def assign(self, clusters) -> list:
        """Per-session ordinal labels, first-seen order: the first cluster
        this session ever sees is "Voice 1", the next new one "Voice 2"."""
        labels = []
        for c in clusters:
            if c not in self.ordinals:
                self.ordinals[c] = f"Voice {len(self.ordinals) + 1}"
            labels.append(self.ordinals[c])
        return labels


# ---------- the fire-and-forget pass ----------

def schedule_pass(chat_id, pcm, sample_rate, commit_ts, session, cfg,
                  turn_id=None, speculative=None):
    """Fire the identity pass for one committed utterance and return
    IMMEDIATELY. The caller (the realtime relay) never awaits the task; a
    strong reference is kept so the loop cannot garbage-collect it mid-run,
    and run_pass itself swallows every failure. `turn_id` is the client's
    voice-trace correlation id from the commit frame - with it the label
    write targets the exact message (#28 phase 3); without it the
    time-window fallback applies. `speculative` is the claimed silence-start
    entry (#28 PR-B), if any."""
    if not pcm:
        return None
    task = asyncio.get_running_loop().create_task(
        run_pass(chat_id, pcm, sample_rate, commit_ts, session, cfg,
                 turn_id=turn_id, speculative=speculative))
    _TASKS.add(task)
    task.add_done_callback(_TASKS.discard)
    return task


async def run_pass(chat_id, pcm, sample_rate, commit_ts, session, cfg,
                   turn_id=None, speculative=None):
    """One utterance's identity pass in an ARMED room (#28 PR-B shape).

    Identity is local or honestly uncertain (owner decision, eighth field
    test - the cloud identity fallback is retired):

    - the local matcher NAMES a confident single speaker -> fast label, no
      ElevenLabs call. REMEMBERED-FIRST (#28, fourteenth field test): the
      matcher's candidates are EVERY sufficient remembered person, not the
      present roster - and a match for someone NOT yet rostered rosters
      them on the spot, through the same durable plumbing an introduction
      uses. The roster-as-candidates rule was a deadlock: a remembered
      guest could not be recognised without being rostered and could not
      be rostered without being recognised, so her turns deferred
      below_threshold while her bank sat sufficient in the store;
    - the matcher's window analysis hears OVERLAPPING speech (the "multi"
      verdict) -> the one surviving ElevenLabs job runs: batch diarize with
      the anchor prefix, for per-word crosstalk splitting (the phase-4
      machinery, unchanged from there). The prefix stays ROSTER-based -
      it is about who is present, not who is remembered;
    - ANY other defer -> COLD START (#28) when the roster leaves exactly one
      possible speaker whose bank cannot identify them yet (everyone else
      present is identifiable): the turn is labelled as that person, marked
      still-learning, and the audio is banked. Otherwise the turn stays
      unresolved. No EL call fires either way because the matcher deferred
      - a solo utterance can never trigger one, and a wrong cloud-asserted
      name is structurally impossible;
    - matcher disabled/unavailable -> same as a defer: unresolved, silent.

    `speculative` is the silence-start head start's claimed entry (#28
    PR-B): when fresh, its cached verdict is used and nothing re-embeds.
    Every blocking step runs on a worker thread; every failure ends here
    (log only - the live path must never notice)."""
    t0 = time.perf_counter()
    plan = None
    try:
        plan = await _in_voice_thread(_room_plan, chat_id, sample_rate)
    except Exception:
        log.debug("room plan failed; utterance stays unresolved",
                  exc_info=True)
    if plan is None:
        # No roster to identify against (a degenerate armed chat): nothing
        # to do. The phase-1 no-roster ordinal pass retired with the rest of
        # the cloud identity path (#28 PR-B).
        return
    prefix_pcm, segments, pending, num_speakers, solo_pending, remembered = plan
    if not voiceid.enabled(cfg):
        # The matcher is switched off: no local identity, and (#28 PR-B) no
        # cloud identity either - turns stay unlabelled, room mode itself
        # still works through its manual doors.
        log.info("diarize pass (matcher off): chat=%s", chat_id)
        return
    # REMEMBERED-FIRST (#28, fourteenth field test): the LOCAL candidate
    # list is every sufficient remembered person (_room_plan's `remembered`,
    # built by remembered_candidates - the same construction the ambient
    # and speculative checks use, pinned so the paths cannot drift apart
    # again). The roster-scoped `segments` survive only as the EL crosstalk
    # prefix, which is about who is PRESENT.
    candidates = remembered
    verdict = await _utterance_verdict(chat_id, pcm, sample_rate, candidates,
                                       cfg, speculative,
                                       pending_present=bool(pending))
    if voiceid.matched(verdict):
        record_decision(chat_id, DECISION_LOCAL,
                        (time.perf_counter() - t0) * 1000)
        log.info("diarize pass (voiceid): chat=%s ms=%.0f score=%.3f",
                 chat_id, (time.perf_counter() - t0) * 1000,
                 verdict["score"])
        try:
            # A confident match for a remembered person NOT on the present
            # roster rosters them first (#28 remembered-first) - the first
            # utterance a remembered guest speaks in an armed room names
            # AND seats them, instead of deferring forever.
            present_names = {s["name"].casefold() for s in segments} \
                | {(n or "").casefold() for n in pending}
            await _fast_label_pass(
                chat_id, pcm, sample_rate, commit_ts, session, cfg, verdict,
                turn_id=turn_id,
                roster_join=verdict["name"].casefold() not in present_names)
        except Exception:
            log.info("voiceid fast-label failed: chat=%s", chat_id)
            log.debug("voiceid fast-label failure detail", exc_info=True)
        return
    if not voiceid.is_multi(verdict):
        cold = cold_start_person(verdict, solo_pending)
        if cold:
            # COLD START (#28, generalised by the fourteenth field test):
            # the matcher could not place this utterance against ANY
            # remembered voice, and exactly one present person's bank
            # cannot identify them while everyone else present would have
            # matched - so it is theirs by elimination. Bank it, and tell
            # the seats a name marked as still being learned. Still NO
            # ElevenLabs call: elimination is free.
            ms = (time.perf_counter() - t0) * 1000
            record_decision(chat_id, DECISION_LOCAL, ms)
            # Content-free, like every log on this path: no name, no words.
            log.info("diarize pass (cold-start): chat=%s ms=%.0f reason=%s",
                     chat_id, ms, (verdict or {}).get("reason", "error"))
            try:
                await _cold_start_pass(chat_id, pcm, sample_rate, commit_ts,
                                       session, cfg, cold, turn_id=turn_id)
            except Exception:
                log.info("cold-start enrolment failed: chat=%s", chat_id)
                log.debug("cold-start failure detail", exc_info=True)
            return
        # THE RETIREMENT PIN (#28 PR-B): a deferred verdict leaves the turn
        # unresolved, full stop. The projection's pending head ages out into
        # today's unlabelled rendering; no ElevenLabs call fires.
        reason = (verdict or {}).get("reason", "error")
        # Carry WHY into the pulse (#28, thirteenth field test): the reason is
        # already computed, so this costs a dict write inside a background
        # task and turns "identity pending" from a dead end into something
        # actionable ("too quiet to judge" vs "not sure who").
        record_decision(chat_id, DECISION_UNRESOLVED,
                        (time.perf_counter() - t0) * 1000, reason)
        log.info("diarize pass (voiceid defer): chat=%s reason=%s", chat_id,
                 reason)
        return
    log.info("diarize pass (crosstalk trigger): chat=%s score=%.3f",
             chat_id, verdict.get("score", 0.0))
    request_pcm = prefix_pcm + pcm
    try:
        result = await _in_voice_thread(
            voice.transcribe_diarized, pcm16_wav(request_pcm, sample_rate),
            "audio/wav", cfg, num_speakers=num_speakers)
    except Exception:
        # Content-free by design, like every log on the voice path.
        log.info("diarize pass failed: chat=%s ms=%.0f", chat_id,
                 (time.perf_counter() - t0) * 1000)
        log.debug("diarize pass failure detail", exc_info=True)
        return
    duration_ms = (time.perf_counter() - t0) * 1000
    # The health strip's live pulse (#28): this decision came from the cloud
    # crosstalk pass. One dict write, content-free, inside the background task.
    record_decision(chat_id, DECISION_CLOUD, duration_ms)
    # Labelling runs FIRST, metering after (#28, night test 4): from the
    # moment the batch reply is parsed, every millisecond spent before the
    # label write is identity latency the seats can observe, so nothing may
    # queue in front of the attach. The metering moves to the finally below -
    # the spend became real when the batch call returned, so a labelling
    # failure still meters it, just no longer ahead of the labels.
    try:
        await _room_label_pass(chat_id, pcm, sample_rate, commit_ts,
                               session, cfg, result, segments, pending,
                               len(prefix_pcm) / 2 / (sample_rate or 16000),
                               duration_ms, turn_id=turn_id)
    except Exception:
        log.info("diarize labelling failed: chat=%s", chat_id)
        log.debug("diarize labelling failure detail", exc_info=True)
    finally:
        try:
            # The crosstalk split is real, metered spend - room mode's only
            # remaining cloud voice cost (#28 PR-B). The anchor prefix is
            # transcribed audio too, so it is metered with it.
            await _in_voice_thread(_meter, chat_id, request_pcm, sample_rate,
                                    cfg)
        except Exception:
            log.info("diarize metering failed: chat=%s", chat_id)
            log.debug("diarize metering failure detail", exc_info=True)


async def _utterance_verdict(chat_id, pcm, sample_rate, candidates, cfg,
                             speculative=None, pending_present=False):
    """The local matcher's verdict for one committed utterance, reusing the
    speculative head start when it is fresh (#28 PR-B). Returns a verdict
    dict, or None when the matcher itself failed (treated as a defer by
    every caller). Never raises.

    Freshness: the hint fired at silence-start, so the buffer has since
    grown by TRAILING SILENCE only - the cached verdict stands when the
    audio past the hinted length is near-silence. If real speech follows
    the hint (a resumed sentence committed by hand), the cached verdict was
    computed on different audio and the check runs fresh. A cached MATCH is
    reused only when the matched person is among this pass's candidates; a
    cached defer is conservative everywhere and reused as-is."""
    if speculative is not None:
        try:
            verdict = await speculative["task"]
        except Exception:
            verdict = None
        if verdict is not None and await _in_voice_thread(
                _speculative_fresh, speculative, pcm):
            if not voiceid.matched(verdict):
                return verdict
            # #81: the speculative check ran without roster context, so its
            # MATCH cleared only the ordinary bar. While someone is
            # anchor-pending the naming bar is higher - recompute strictly
            # rather than let a head start smuggle a borderline match past
            # the pending-present defer.
            if not pending_present and verdict.get("person_id") in {
                    c["person_id"] for c in candidates}:
                return verdict
    try:
        return await _in_voice_thread(
            voiceid.identify_utterance, pcm, sample_rate, candidates, cfg,
            pending_present)
    except Exception:
        log.info("voiceid identify failed: chat=%s", chat_id)
        log.debug("voiceid identify failure detail", exc_info=True)
        return None


# Audio added between the speculative hint and the commit frame must be the
# tail of the silence gap the hint fired on; louder than this int16 RMS means
# speech resumed and the cached verdict is stale.
SPECULATIVE_TAIL_RMS = 300


def _speculative_fresh(entry, pcm) -> bool:
    """Worker-thread staleness check for a claimed speculative entry (#28
    PR-B) - see _utterance_verdict. Pure over its inputs."""
    n = entry.get("len") or 0
    if n <= 0 or n > len(pcm):
        return False
    tail = pcm[n:]
    if not tail:
        return True
    from . import anchors
    return anchors.pcm_rms(tail) < SPECULATIVE_TAIL_RMS


async def _attach_until_deadline(chat_id, commit_ts, payload, session,
                                 turn_id=None):
    """Attach the labels to the utterance's user message. Returns the
    labelled message id, or None.

    With a turn id (#28, night test 4) the target is a direct lookup and the
    attach happens on the FIRST call - no cadence. The only wait left is a
    short fast retry for the /send race (the row not persisted yet); a row
    that exists but may not be labelled ends the attempt immediately, and the
    retry window never outlives the overall match window.

    Without a turn id (an older client) the original probe cadence stands:
    time-window matching has no way to tell "not yet" from "never", so it
    genuinely has to keep looking until the window closes."""
    if turn_id:
        # PARK FIRST (#28, twelfth field test). The row usually does not exist
        # yet, and the moment it does, /send dispatches the round that renders
        # the transcript - so a label written even microseconds after the
        # insert is already too late for the model answering this turn.
        # Parking lets the insert itself carry the label. The retry loop below
        # still runs: it covers the id-less/older-client case and any insert
        # that happened before the verdict was ready, and claiming is
        # single-use so the two paths can never double-write.
        park_label(turn_id, payload)
        deadline = time.monotonic() + min(ID_ATTACH_WINDOW_SECS,
                                          MATCH_WINDOW_SECS)
        while True:
            outcome = await _in_voice_thread(
                _attach_labels, chat_id, commit_ts, payload, session, turn_id)
            if outcome is not _NO_ROW_YET:
                return outcome
            if time.monotonic() >= deadline:
                return None
            await asyncio.sleep(ID_ATTACH_RETRY_SECS)
    deadline = time.monotonic() + MATCH_WINDOW_SECS
    while True:
        target_id = await _in_voice_thread(
            _attach_labels, chat_id, commit_ts, payload, session, turn_id)
        if target_id or time.monotonic() >= deadline:
            return target_id
        await asyncio.sleep(MATCH_PROBE_SECS)


def _room_plan(chat_id, sample_rate):
    """Roster snapshot for one pass (worker thread): the anchor prefix for
    every SUFFICIENT present person, the names still pending an anchor, the
    num_speakers hint (present + 1 - the plus-one is what lets an
    unannounced voice surface as an unmatched cluster), the cold-start
    candidate, and the local matcher's candidates. None when the chat has
    no roster - the phase-1 pass then runs untouched.

    REMEMBERED-FIRST (#28, fourteenth field test): the matcher candidates
    are EVERY sufficient remembered person (remembered_candidates), not the
    present roster. The roster-as-candidates rule was the chicken-and-egg
    failure that night: a remembered, fully-sufficient guest was never
    rostered, so her bank was never even compared, so she could never be
    recognised - and she could not be rostered without being recognised.
    Only the EL crosstalk prefix stays roster-scoped (presence, not
    memory)."""
    con = db.connect()
    try:
        present = db.get_room_roster(con, chat_id, present_only=True)
    finally:
        con.close()
    if not present:
        return None
    from . import anchors
    store = anchors.store()
    people = store.people()
    sufficient = {p["person_id"] for p in people if p["sufficient"]}
    ids, pending = [], []
    for row in present:
        pid = row["person_id"]
        if pid and pid in sufficient:
            ids.append(pid)
        else:
            # No linked anchors, or anchors below the sufficiency bar: this
            # person cannot be identified by voice yet, only by elimination.
            pending.append(row["name"])
    prefix_pcm, segments = store.build_prefix(ids, sample_rate)
    num_speakers = min(32, len(present) + 1)
    # The cold-start candidate (#28), GENERALISED by the fourteenth field
    # test: exactly ONE present person whose bank cannot identify them yet,
    # while every OTHER present person's can. An unplaceable clear
    # utterance can then only be theirs - which is what lets a genuinely
    # new guest start learning while the owner sits identified beside
    # them. Two or more unidentifiable people is ambiguity and offers
    # nobody. (The original rule required exactly one present person
    # TOTAL.) Derived from the roster read the pass already did, so the
    # defer path costs no extra I/O.
    solo_pending = pending[0] if len(pending) == 1 else None
    return (prefix_pcm, segments, pending, num_speakers, solo_pending,
            remembered_candidates(people, {row["person_id"]
                                           for row in present
                                           if row["person_id"]}))


async def _room_label_pass(chat_id, pcm, sample_rate, commit_ts, session, cfg,
                           result, segments, pending, prefix_seconds,
                           duration_ms, turn_id=None):
    """Roster-mode labelling for one utterance: prefix clusters -> names,
    utterance clusters -> labels via resolve_room_labels, anchor
    accumulation, the ask-fallback flag, and the mismatch cross-check."""
    prefix_words, utter_words = split_words_at(result.get("words"),
                                               prefix_seconds)
    cmap = prefix_cluster_map(prefix_words, segments)
    clusters = utterance_clusters(utter_words)
    resolved = resolve_room_labels(clusters, cmap, pending, session)
    if clusters:
        session.prev_clusters = clusters
    ct = crosstalk_info(utter_words)
    log.info("diarize pass (room): chat=%s ms=%.0f clusters=%d matched=%d "
             "ask=%d crosstalk=%d", chat_id, duration_ms, len(clusters),
             len(resolved["matched"]), len(resolved["ask"]), 1 if ct else 0)
    target_id = None
    if resolved["labels"]:
        payload = {"clusters": clusters, "labels": resolved["labels"],
                   "uncertain": resolved["uncertain"]}
        if ct:
            # Crosstalk (#28 phase 4): the marker, plus the best-effort split
            # when the word map alternates cleanly. Segment labels reuse the
            # resolved labels, so an unmatched voice splits as its uncertain
            # ordinal, never as a guessed name.
            payload.update(ct)
            segs = split_segments(utter_words,
                                  dict(zip(clusters, resolved["labels"])),
                                  uncertain_labels=set(resolved["uncertain"]))
            if segs:
                payload["segments"] = segs
        # The attach comes FIRST (#28, night test 4): the label write is the
        # thing the seats are waiting on, so the anchor bookkeeping below -
        # file writes on a worker thread - may not queue in front of it.
        target_id = await _attach_until_deadline(chat_id, commit_ts, payload,
                                                 session, turn_id=turn_id)
    if len(clusters) == 1:
        # A clean single-speaker utterance is anchor food: it refreshes a
        # matched person's clips, and it is the ONLY thing that can build a
        # pending (just-introduced) person's anchors at all. A crosstalk
        # utterance never lands here - two voices are ground truth for
        # neither.
        await _in_voice_thread(_accumulate_anchor, chat_id, pcm, sample_rate,
                                clusters[0], resolved, cfg)
    if not resolved["labels"]:
        return
    if resolved["ask"]:
        # Someone the anchors don't know and elimination can't name: surface
        # the ask-fallback. The turn keeps its uncertain ordinal meanwhile.
        await _in_voice_thread(_raise_unknown_voice, chat_id, target_id)
    if target_id:
        from . import anchors
        # Tap-to-correct's audio source: remembered in memory, bounded.
        anchors.remember_audio(target_id, pcm, sample_rate, len(clusters))
        primary = _primary_named_label(resolved)
        if primary and len(utter_words) >= MISMATCH_MIN_WORDS:
            from . import mismatch
            mismatch.schedule_check(chat_id, target_id, primary, cfg)


def _primary_named_label(resolved) -> str | None:
    """The confidently-NAMED label the mismatch cross-check should test -
    the first anchor-matched name on the turn. Eliminated/ordinal labels are
    already displayed as uncertain, so second-guessing them adds nothing."""
    named = set(resolved["matched"].values())
    for label in resolved["labels"]:
        if label in named:
            return label
    return None


def _accumulate_anchor(chat_id, pcm, sample_rate, cluster, resolved, cfg=None):
    """Feed one single-speaker utterance to the right person's anchor set
    (worker thread). Matched person: a refresh candidate. Eliminated person:
    their first real anchor audio - link the roster row once accepted, so the
    UI's 'anchor pending' honestly ends. Every accepted clip re-runs the
    pairwise hygiene audit (#28 PR-B)."""
    from . import anchors
    store = anchors.store()
    name = resolved["matched"].get(cluster)
    if name:
        person = store.find_by_name(name)
        if person and store.add_clip(person["person_id"], pcm, sample_rate,
                                     source="accumulated"):
            voiceid.audit_banks_if_changed(cfg or {})
        return
    name = resolved["eliminated"].get(cluster)
    if not name:
        return
    pid = store.ensure_person(name)
    if store.add_clip(pid, pcm, sample_rate, source="accumulated"):
        voiceid.audit_banks_if_changed(cfg or {})
        con = db.connect()
        try:
            db.link_room_person(con, chat_id, name, pid)
        finally:
            con.close()


# ---------- the fast local-identity label pass (#28 part 2) ----------

async def _fast_label_pass(chat_id, pcm, sample_rate, commit_ts, session, cfg,
                           verdict, turn_id=None, roster_join=False):
    """Label a turn from a confident LOCAL single-speaker match, with NO
    ElevenLabs batch call. Mirrors the roster path's post-label bookkeeping for
    one matched voice: the name is attached FIRST (it is what the seats are
    waiting on), then the single-speaker utterance refreshes that person's
    anchors, is remembered for tap-to-correct, and gets the LLM mismatch
    cross-check. With the batch pass skipped, that cross-check is now the sole
    detector of an unannounced second voice the matcher took for one (owner
    decision on the issue: the mismatch flag doubles as that detector).

    `roster_join` (#28 remembered-first, fourteenth field test): the match
    named a remembered person who is NOT on the present roster. They are
    seated before the label attaches - the same durable plumbing an
    introduction uses (a linked present row, the room-update bell for the
    seats' refresh, an open who-is-speaking ask answered) - so by the time
    the name is claimable, the room state already agrees with it. The
    write is milliseconds on a worker thread; the attach still parks the
    label the instant it runs."""
    name = verdict["name"]
    if roster_join:
        try:
            await _in_voice_thread(_roster_remembered, chat_id, name,
                                    verdict.get("person_id"), cfg)
        except Exception:
            # The label must still attach: a failed roster write may not
            # cost the turn its name.
            log.info("remembered roster join failed: chat=%s", chat_id)
            log.debug("remembered roster join failure detail", exc_info=True)
    # Payload shape matches the batch path's confident-named case: a single
    # certain label projects as "<name> (in the room)" and chips as a matched
    # voice. 'source' is content-free metadata; every consumer ignores unknown
    # keys. No crosstalk marker - a confident single match is one voice.
    # #33: the matcher's real score rides the label - the ingest identity
    # field turns it into the confidence membro's binding policy reads.
    payload = {"clusters": ["local"], "labels": [name], "uncertain": [],
               "source": "local", "score": round(verdict.get("score") or 0, 3)}
    if name.casefold() == (cfg.get("user_name") or "User").casefold():
        # #28 PR-C (the owner's identity is shown, not hidden): a confident
        # OWNER match is marked so the chips can say "voice confirmed". The
        # projection decides by name, so old payloads without this key still
        # render the new head; consumers ignore unknown keys as ever.
        payload["owner"] = True
    target_id = await _attach_until_deadline(chat_id, commit_ts, payload,
                                             session, turn_id=turn_id)
    # Anchor food: a clean single-speaker utterance refreshes the matched
    # person's clips. The batch room path does the same for a matched single
    # cluster; on the fast path this is the ONLY thing that keeps the person's
    # anchors fresh, since the batch pass no longer runs for them.
    await _in_voice_thread(_accumulate_fast_anchor, verdict["person_id"], pcm,
                            sample_rate, cfg, verdict.get("score"))
    if not target_id:
        return
    from . import anchors
    anchors.remember_audio(target_id, pcm, sample_rate, 1)
    seconds = len(pcm) / 2 / (sample_rate or 16000)
    if seconds >= FAST_MISMATCH_MIN_SECONDS:
        from . import mismatch
        mismatch.schedule_check(chat_id, target_id, name, cfg)


def _roster_remembered(chat_id, name, person_id, cfg):
    """Seat a remembered person a confident LOCAL match just named in an
    ARMED room (worker thread; #28 remembered-first, fourteenth field
    test). The introduction's roster plumbing, minus the introduction: a
    present row linked to their anchors (add_room_person rings the
    room-update bell, so every seat's next refresh sees them), and any
    open "someone new is speaking - who?" ask is answered by the name,
    exactly as a naming introduction answers it. Cap-guarded like every
    roster writer - past the cap the turn is still NAMED (the identity is
    true regardless), the roster simply does not grow. Idempotent: two
    passes racing re-run the same writes harmlessly."""
    con = db.connect()
    try:
        present = db.get_room_roster(con, chat_id, present_only=True)
        if any((p["name"] or "").casefold() == (name or "").casefold()
               for p in present):
            return
        cap = int((cfg or {}).get("room_roster_max") or 6)
        if len(present) >= cap:
            log.info("roster cap reached; matched voice not rostered: "
                     "chat=%s cap=%d", chat_id, cap)
            return
        # #84: the turn's message does not exist yet at seat time (the label
        # parks under turn_id and attaches later), so the trigger is the
        # path, message-less.
        db.add_room_person(con, chat_id, name, person_id=person_id or "",
                           seated_via="voice-match")
        db.resolve_room_flags(con, chat_id, kind="unknown_voice")
        log.info("remembered voice rostered by local match: chat=%s", chat_id)
    finally:
        con.close()


def _accumulate_fast_anchor(person_id, pcm, sample_rate, cfg=None,
                            score=None):
    """Refresh a fast-matched person's anchors (worker thread). Confident-
    match accumulation continues indefinitely (#28 PR-B) - keep-best-N per
    length class bounds the bank - and every accepted clip re-runs the
    pairwise hygiene audit.

    The banking bar (#222): `score` is the verdict's match score, and both
    accumulation and harvesting require it to clear the threshold plus
    voice_id_banking_extra - a borderline match may label its turn but must
    not feed the very bank that produced it. None means the caller has no
    verdict score (a legacy path); banking proceeds as before.

    Short-slice harvesting (#28, tenth field test): a confident LONG match
    also banks a short slice cut from its own audio, so the short class
    fills itself from ordinary speech and the short-utterance deadlock
    (cannot match without short clips, cannot bank short clips without
    matching) can never form."""
    if score is not None and not voiceid.score_banks(score, cfg or {}):
        log.info("anchor accumulation skipped (#222): score=%.3f under the "
                 "banking bar", float(score))
        return
    from . import anchors
    store = anchors.store()
    changed = store.add_clip(person_id, pcm, sample_rate,
                             source="accumulated")
    sr = sample_rate or 16000
    seconds = len(pcm) / 2 / sr
    if seconds >= 2 * anchors.SHORT_CLIP_MAX_SECONDS:
        # The slice comes from the MIDDLE of the utterance: starts carry
        # breaths and ends trail off, mid-speech is the cleanest voice.
        span = int(anchors.SHORT_CLIP_MAX_SECONDS * 0.75 * sr) * 2
        mid = (len(pcm) - span) // 2
        changed = store.add_clip(person_id, pcm[mid:mid + span], sr,
                                 source="harvested-short") or changed
    if changed:
        voiceid.audit_banks_if_changed(cfg or {})


# ---------- the cold-start enrolment pass (#28) ----------

async def _cold_start_pass(chat_id, pcm, sample_rate, commit_ts, session, cfg,
                           name, turn_id=None):
    """Label and bank one by-elimination turn (see cold_start_person).

    The payload is deliberately BOTH things at once: the label names the
    person (so the seats stop being told "identity pending" about someone
    the room can only contain), and the same name rides `uncertain` so every
    consumer that predates this change keeps treating it as a guess. The new
    `learning` marker is what upgrades that guess into an honest state - the
    projection heads the turn "<name> (learning this voice)" and the chip
    says learning - and, being an added key, it changes nothing for payloads
    written before it existed.

    Order matches every other label pass: the attach FIRST (it is what the
    seats are waiting on), then the banking on a worker thread. Tap-to-
    correct's audio is remembered, so a wrong elimination is one tap from
    being fixed."""
    payload = {"clusters": ["local"], "labels": [name], "uncertain": [name],
               "learning": True, "source": COLD_START_SOURCE}
    target_id = await _attach_until_deadline(chat_id, commit_ts, payload,
                                             session, turn_id=turn_id)
    await _in_voice_thread(_bank_cold_start, chat_id, name, pcm, sample_rate,
                            cfg)
    if target_id:
        from . import anchors
        anchors.remember_audio(target_id, pcm, sample_rate, 1)


def _bank_cold_start(chat_id, name, pcm, sample_rate, cfg):
    """Add this utterance to the by-elimination person's bank (worker
    thread), creating their anchor-store entry the first time. The clip
    still has to clear the ordinary quality gate - a cold start is a reason
    to accumulate, never a reason to accept noise - and an accepted clip
    re-runs the pairwise hygiene audit and ends 'anchor pending' on the
    roster row, exactly as the introduction path does. `source` records how
    the clip was earned, so the store stays explainable."""
    from . import anchors
    store = anchors.store()
    pid = store.ensure_person(name)
    if not store.add_clip(pid, pcm, sample_rate, source=COLD_START_SOURCE):
        return
    voiceid.audit_banks_if_changed(cfg or {})
    con = db.connect()
    try:
        db.link_room_person(con, chat_id, name, pid)
    finally:
        con.close()


async def _owner_label_pass(chat_id, pcm, sample_rate, commit_ts, session,
                            cfg, verdict, turn_id=None):
    """Label a room-off turn the ambient check confidently matched to the
    OWNER (#28 PR-C: the owner's identity is shown, not hidden). The same
    turn-id-targeted attach every label write uses, with the owner marker so
    the chips can say "voice confirmed"; then the anchor top-up the noop
    path always did. Nothing else: no arming, no roster, no ElevenLabs
    call, no metering - a solo session stays solo, it just stops pretending
    the app does not know who is speaking."""
    payload = {"clusters": ["local"], "labels": [verdict["name"]],
               "uncertain": [], "source": "local", "owner": True,
               "score": round(verdict.get("score") or 0, 3)}
    await _attach_until_deadline(chat_id, commit_ts, payload, session,
                                 turn_id=turn_id)
    await _in_voice_thread(_accumulate_fast_anchor, verdict["person_id"],
                            pcm, sample_rate, cfg, verdict.get("score"))


async def _arm_known_pass(chat_id, pcm, sample_rate, commit_ts, session, cfg,
                          verdict, t0, turn_id=None):
    """Ambient recognised a remembered NON-OWNER voice (#28; formerly the
    sniff's local arm - the EL sniff itself retired in PR-B). A confident
    OWNER-only match ends here without arming (owner alone is not a
    household). A remembered non-owner arms room mode via the same durable
    plumbing an introduction uses, seeds the roster, labels this turn, and
    refreshes the person's anchors - locally, with no batch call and no
    metering, since no second transcription happened."""
    name = verdict["name"]
    owner = (cfg.get("user_name") or "User").casefold()
    if name.casefold() == owner:
        log.info("diarize pass (ambient voiceid): chat=%s ms=%.0f armed=0",
                 chat_id, (time.perf_counter() - t0) * 1000)
        return
    log.info("diarize pass (ambient voiceid): chat=%s ms=%.0f armed=1",
             chat_id, (time.perf_counter() - t0) * 1000)
    await _in_voice_thread(_arm_known, chat_id, {"local": name}, cfg)
    payload = {"clusters": ["local"], "labels": [name], "uncertain": [],
               "source": "local", "score": round(verdict.get("score") or 0, 3)}
    target_id = await _attach_until_deadline(chat_id, commit_ts, payload,
                                             session, turn_id=turn_id)
    await _in_voice_thread(_accumulate_fast_anchor, verdict["person_id"], pcm,
                            sample_rate, cfg, verdict.get("score"))
    if target_id:
        from . import anchors
        anchors.remember_audio(target_id, pcm, sample_rate, 1)


# ---------- the ambient local check (#28) ----------

def schedule_ambient(chat_id, pcm, sample_rate, commit_ts, session, cfg,
                     turn_id=None, speculative=None):
    """Fire one ambient local check and return IMMEDIATELY - the relay never
    awaits it, exactly the schedule_pass contract. Local-only: this path
    never makes an ElevenLabs call, so it is safe to run on every committed
    utterance while room mode is off."""
    if not pcm:
        return None
    task = asyncio.get_running_loop().create_task(
        run_ambient(chat_id, pcm, sample_rate, commit_ts, session, cfg,
                    turn_id=turn_id, speculative=speculative))
    _TASKS.add(task)
    task.add_done_callback(_TASKS.discard)
    return task


async def run_ambient(chat_id, pcm, sample_rate, commit_ts, session, cfg,
                      turn_id=None, speculative=None):
    """The ambient local check for one committed utterance (room mode off).
    Runs the on-device matcher against every sufficient remembered voice and
    acts on the pure `ambient_decision`: the owner's voice labels the turn
    as the owner, voice-confirmed, without arming anything (#28 PR-C; the
    anchor is topped up as always), a remembered non-owner arms room mode
    and is named, and a
    clear stranger - decidable only when the owner is itself enrolled - arms
    and raises the ask-fallback. Anything undecidable defers: room mode
    stays off and only the introduction/command/toggle doors remain (#28
    PR-B - the bounded EL sniff retired, so ambient is the only automatic
    door and it NEVER makes an ElevenLabs call). Reuses the speculative
    silence-start verdict when fresh. Every failure ends here."""
    t0 = time.perf_counter()
    try:
        plan = await _in_voice_thread(_ambient_plan, chat_id, sample_rate, cfg)
        if plan is None:
            return
        candidates, owner_ok = plan
        verdict = await _utterance_verdict(chat_id, pcm, sample_rate,
                                           candidates, cfg, speculative)
        if verdict is None:
            return
        decision = ambient_decision(verdict, cfg.get("user_name") or "User",
                                    owner_ok)
        if decision != "defer":
            # A local decision was actually made (owner confirmed, known
            # voice named, or a clear stranger detected): the health strip's
            # pulse. A defer records nothing.
            record_decision(chat_id, DECISION_LOCAL,
                            (time.perf_counter() - t0) * 1000)
        log.info("ambient check: chat=%s ms=%.0f decision=%s", chat_id,
                 (time.perf_counter() - t0) * 1000, decision)
        if decision == "noop_owner":
            # #28 PR-C: the owner's identity is shown, not hidden. A
            # confident owner match now LABELS the turn (owner-marked, so
            # the chips can say "voice confirmed") - it still never arms,
            # never rosters, never fires an ElevenLabs call, and still tops
            # up the owner's anchor. Room-off solo chats therefore carry
            # owner labels on confidently-matched turns.
            if voiceid.matched(verdict):
                await _owner_label_pass(chat_id, pcm, sample_rate, commit_ts,
                                        session, cfg, verdict,
                                        turn_id=turn_id)
            return
        if decision == "arm_known":
            await _arm_known_pass(chat_id, pcm, sample_rate, commit_ts,
                                  session, cfg, verdict, t0, turn_id=turn_id)
            return
        if decision == "arm_unknown":
            await _in_voice_thread(_arm_ambient_unknown, chat_id, cfg)
            ordinal = session.assign(["ambient_unknown"])
            payload = {"clusters": ["ambient_unknown"], "labels": ordinal,
                       "uncertain": list(ordinal)}
            target_id = await _attach_until_deadline(chat_id, commit_ts,
                                                     payload, session,
                                                     turn_id=turn_id)
            if target_id:
                await _in_voice_thread(_raise_unknown_voice, chat_id, target_id)
        # decision == "defer": nothing here; the introduction/command/toggle
        # doors still cover what the matcher could not (#28 PR-B).
    except Exception:
        log.info("ambient pass failed: chat=%s", chat_id)
        log.debug("ambient pass failure detail", exc_info=True)


def _ambient_plan(chat_id, sample_rate, cfg):
    """Ambient snapshot for one check (worker thread): None unless the check
    should still run - the chat's durable room mode must be OFF (another path
    may have armed it since the utterance was scheduled), ambient must not be
    disarmed, and at least one sufficient remembered voice must exist to match
    against. Returns (candidates, owner_ok): the sufficient people as matcher
    candidates, and whether the owner is among them (so a below-threshold
    result can mean 'not the owner')."""
    con = db.connect()
    try:
        row = con.execute("SELECT room_mode, ambient_off FROM chats WHERE id=?",
                          (chat_id,)).fetchone()
    finally:
        con.close()
    if not row or row["room_mode"] or row["ambient_off"]:
        return None
    from . import anchors
    people = anchors.store().people()
    candidates = remembered_candidates(people)
    if not candidates:
        return None
    return candidates, owner_sufficient(people, cfg.get("user_name"))


def _arm_ambient_unknown(chat_id, cfg):
    """Arm room mode for a clear stranger the matcher could not name (worker
    thread): the durable flip plus the live mirror, and the owner joins the
    roster so the armed pass runs anchored (the next voice raises the
    ask-fallback rather than a bare ordinal). Idempotent."""
    con = db.connect()
    try:
        if con.execute("SELECT room_mode FROM chats WHERE id=?",
                       (chat_id,)).fetchone()["room_mode"]:
            return
        db.set_chat_room_mode(con, chat_id, True)
        set_room_enabled(chat_id, True)
        log.info("room mode ON via ambient (unknown voice): chat=%s", chat_id)
    finally:
        con.close()
    from . import introductions
    introductions.roster_owner_only(chat_id, cfg)


# ---------- speculative identity at silence-start (#28 PR-B) ----------
#
# The client's VAD knows the utterance's audio is COMPLETE when the silence
# gap begins - roughly two seconds before the commit frame at default
# settings. It sends a content-free hint frame ({"speculative": true}, no
# audio; the relay forwards NOTHING upstream for it), and the relay fires
# the LOCAL-ONLY identity check on the buffered utterance right then. The
# verdict caches on the RoomSession; the commit-time pass claims it and
# attaches the name with no re-embed - so the label lands before or with
# the committed transcript in the common case, closing the label-vs-first-
# responder race structurally. Same never-awaited discipline as every pass;
# a hint that goes stale (speech resumed) is simply discarded.

def schedule_speculative(chat_id, session, cfg):
    """Fire the silence-start local check on the buffer as it stands and
    return IMMEDIATELY. Never an ElevenLabs call, never awaited by the
    relay; the entry parks on the session for the commit boundary to claim."""
    pcm, sample_rate = session.peek_utterance()
    if not pcm:
        return None
    task = asyncio.get_running_loop().create_task(
        run_speculative(chat_id, pcm, sample_rate, cfg))
    _TASKS.add(task)
    task.add_done_callback(_TASKS.discard)
    session.speculative = {"len": len(pcm), "task": task}
    return task


async def run_speculative(chat_id, pcm, sample_rate, cfg):
    """The speculative check itself: the on-device matcher over every
    sufficient remembered voice. Returns the verdict (the task result IS the
    cache); None on any failure. The commit-time consumer re-validates the
    match against its own candidate set, so a broader speculative candidate
    pool can never name someone the consuming pass would not."""
    try:
        candidates = await _in_voice_thread(remembered_candidates)
        if not candidates:
            return None
        verdict = await _in_voice_thread(
            voiceid.identify_utterance, pcm, sample_rate, candidates, cfg)
        log.info("speculative check: chat=%s status=%s", chat_id,
                 (verdict or {}).get("status"))
        return verdict
    except Exception:
        log.info("speculative check failed: chat=%s", chat_id)
        log.debug("speculative check failure detail", exc_info=True)
        return None


def remembered_candidates(people=None, rostered_ids=frozenset()):
    """EVERY sufficient remembered person as matcher candidates - THE
    candidate semantics, shared verbatim by the armed pass (#28
    remembered-first), the ambient room-off check and the speculative
    silence-start check, so the three paths can never drift apart again.
    The fourteenth field test was exactly such a drift: the armed path had
    narrowed its candidates to the present roster, and a remembered guest
    became unrecognisable the moment room mode armed. Reads the store when
    `people` is None - call it on a worker thread then.

    #83: a sufficient bank nobody vouched for (see anchors.needs_audition)
    has no remembered-first rights - it can neither name nor seat anyone
    in a session until the owner auditions it. `rostered_ids` is the one
    exception: a person already SEATED in this chat (the session still
    learning them by elimination, or a seat a human put there) keeps
    being identified, because the pause guards RE-seating, not the seat."""
    if people is None:
        from . import anchors
        people = anchors.store().people()
    return [{"person_id": p["person_id"], "name": p["name"]}
            for p in people
            if p.get("sufficient")
            and (not p.get("id_paused")
                 or p["person_id"] in rostered_ids)]


def _arm_known(chat_id, matched, cfg):
    """The ambient arm itself (worker thread): the durable flip plus the
    live mirror - the same phase-2 control plumbing an introduction uses -
    and one linked roster row per matched remembered person. Idempotent:
    two passes racing re-run the same writes harmlessly. (Formerly
    _arm_from_sniff; the EL sniff retired in #28 PR-B.)"""
    con = db.connect()
    try:
        db.set_chat_room_mode(con, chat_id, True)
        set_room_enabled(chat_id, True)
        log.info("room mode ON via ambient (known voice): chat=%s matched=%d",
                 chat_id, len(matched))
        present = {p["name"].lower()
                   for p in db.get_room_roster(con, chat_id,
                                               present_only=True)}
        cap = int(cfg.get("room_roster_max") or 6)
        from . import anchors
        store = anchors.store()
        for name in dict.fromkeys(matched.values()):
            if name.lower() in present or len(present) >= cap:
                continue
            person = store.find_by_name(name)
            pid = person["person_id"] if person else ""
            db.add_room_person(con, chat_id, name, person_id=pid,
                               seated_via="voice-match")
            present.add(name.lower())
    finally:
        con.close()


def _raise_unknown_voice(chat_id, message_id):
    """One OPEN ask at a time per chat: an unanswered 'who is speaking?' must
    not stack a copy per utterance while the same stranger keeps talking."""
    con = db.connect()
    try:
        open_asks = [f for f in db.get_room_flags(con, chat_id)
                     if f["kind"] == "unknown_voice"]
        if open_asks:
            return
        db.insert_room_flag(con, chat_id, "unknown_voice",
                            message_id=message_id)
    finally:
        con.close()


def _meter(chat_id, pcm, sample_rate, cfg):
    seconds = len(pcm) / 2 / (sample_rate or 16000)
    if seconds <= 0:
        return
    con = db.connect()
    try:
        db.log_voice_usage(con, chat_id, "stt", seconds,
                           voice.voice_cost("stt", seconds, cfg))
        con.commit()
    finally:
        con.close()


def _attach_labels(chat_id, commit_ts, payload, session, turn_id=None):
    """One synchronous attempt (runs on a worker thread): find the
    utterance's user message and persist the labels through the single update
    path, which also rings the live-events bell. Returns the labelled id,
    None for "nothing to label" (id-less callers retry that until their
    window closes), or _NO_ROW_YET when an exact-id target simply is not
    persisted yet - the one case the id path retries.

    With a turn id (#28 phase 3) the lookup is EXACT: only the message whose
    persisted voice_turn_id matches may be labelled, and the time-window
    guesswork never runs - so a pass whose utterance produced no message
    (a dropped interjection) labels nothing, ever.

    Crosstalk segments (#28 phase 4) are gated HERE, where the message text
    is finally in hand: the best-effort split persists only when the batch
    words align with the realtime transcript the message actually carries
    (segments_align). Where the two transcribers disagree, the split is
    dropped and the crosstalk marker stands alone."""
    con = db.connect()
    try:
        if turn_id:
            target = db.get_message_by_voice_turn(con, chat_id, turn_id)
            if not target:
                return _NO_ROW_YET  # the /send race - worth a fast retry
            if target["id"] in session.labelled_ids \
                    or target.get("voice_labels"):
                return None  # exists but already labelled: nothing to do
        else:
            rows = db.get_voice_label_candidates(con, chat_id, commit_ts)
            target = pick_target(rows, session.labelled_ids)
            if not target:
                return None
        if payload.get("segments") and not segments_align(
                payload["segments"], target.get("content")):
            payload = {k: v for k, v in payload.items() if k != "segments"}
        db.set_message_voice_labels(con, target["id"], payload)
        session.labelled_ids.add(target["id"])
        return target["id"]
    finally:
        con.close()
