"""Room mode's label plumbing (#28, #482): every voiced turn's check is
scheduled here, and every label it writes goes out through here.

THE CORE PRINCIPLE - zero added latency on the live voice path. The realtime
STT relay (routers/voice.py) stays byte-for-byte identical upstream whatever
the room is doing; it only TEES already-decoded utterance audio into a
per-session buffer and, on each commit boundary, fires a fire-and-forget
check from here. Nothing in the live path ever awaits that check, and every
blocking step inside it (every embedding, every database touch) runs on
this module's own worker threads, so even the shared event loop never
stalls on it.

What lives here:

  * schedule_turn_check: one check per voiced turn, whichever path heard
    it (the realtime relay, or the batch /stt POST with its audio copy).
    The check itself is backend/voice_pass.py, which names the turn from
    the session naming (backend/voice_sessions.py).
  * The label path: a finished label parks under the client's turn id and
    /send claims it inside the message insert (park_label, claim_label),
    so the seats read the name with the turn; a late label is written to
    the row directly (_attach_labels), and the turn's audio is remembered
    for tap-to-correct (_deliver_label).
  * The room's writes the pass makes through room_state: the known-voice
    arm (_arm_known), the unknown-voice arm (_arm_ambient_unknown) and the
    one open "who's this?" ask per chat (_raise_unknown_voice).
  * The content-free decision record the voice health strip reads.

Failure posture: a failed or slow check leaves the message unlabelled and
everything else untouched. No retry ever feeds back into the live path.

The id-less label-attachment branch (the time-window candidate scan
behind the turn-id lookup) is belt and braces for a commit frame
arriving without an id for any reason, not a co-equal strategy.
"""

import asyncio
import concurrent.futures
import functools
import json
import logging
import re
import struct
import time

from . import db, voiceid

log = logging.getLogger("crossband.diarize")

# #133: ALL of this module's thread work runs on its own bounded executor,
# never the default asyncio.to_thread pool. The identity check is
# fire-and-forget by design, but its heavy steps (clip banking, the
# pairwise hygiene audit, the naming's embeddings) were queueing on the
# SAME default executor /send needs to persist the user's message - so a
# still-learning guest's every utterance starved round dispatch and the
# room sat in "listening". Two workers: identity work is sequential
# per-utterance anyway, and a bounded queue here can never crowd out a
# request thread again.
_VOICE_EXECUTOR = concurrent.futures.ThreadPoolExecutor(
    max_workers=2, thread_name_prefix="voiceid")


def _in_voice_thread(fn, /, *args, **kwargs):
    """Awaitable run of `fn` on the DEDICATED voice executor - the drop-in
    replacement for asyncio.to_thread everywhere in the voice check."""
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
# target row is a direct lookup, so the pass attaches the moment its label is
# ready instead of riding a probe cadence. The only reason the row can be
# missing is the /send race - the client dispatches once the committed
# transcript returns - and these two constants bound a short fast retry for
# exactly that race. A row that never appears is a dropped interjection,
# which must label nothing; giving up early is correct there. The
# MATCH_PROBE_SECS cadence above survives ONLY for id-less commits (older
# clients), where time-window matching genuinely has to wait.
ID_ATTACH_RETRY_SECS = 0.05
ID_ATTACH_WINDOW_SECS = 2.0

# _attach_labels' "the row is not persisted yet - retry" verdict, distinct
# from None ("nothing to label, stop"): only the /send race retries.
_NO_ROW_YET = object()

# Strong references to in-flight checks: asyncio only holds weak refs to
# tasks, and a garbage-collected fire-and-forget task silently never runs.
_TASKS: set = set()

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

# Label-flow outcomes (#304 evidence capture). The turn-handoff issue's open
# question: does a stalled /send leave a valid parked label unclaimed until
# its TTL expires? Until now expiry was silent - a swept entry just vanished,
# so the question was unanswerable after the fact. Every park/claim/expiry
# now leaves a bounded, content-free breadcrumb: a turn id (the client's own
# correlation id), an outcome word, and milliseconds waited. Never a name,
# never the payload. GET /api/voice/health surfaces the list.
_LABEL_EVENTS: list = []
_LABEL_EVENTS_MAX = 32
LABEL_PARKED = "parked"
LABEL_CLAIMED = "claimed"
LABEL_EXPIRED_UNCLAIMED = "expired_unclaimed"
LABEL_EXPIRED_AT_CLAIM = "expired_at_claim"
LABEL_EVICTED = "evicted"


def _label_event(turn_id, event, wait_ms):
    _LABEL_EVENTS.append({"turn_id": str(turn_id)[:64], "event": event,
                          "wait_ms": round(float(wait_ms), 1),
                          "at": time.monotonic()})
    del _LABEL_EVENTS[:-_LABEL_EVENTS_MAX]


def label_flow() -> list:
    """The recent park/claim/expiry outcomes, newest first, each with its
    age in seconds. Content-free: turn ids, outcome words and milliseconds."""
    now = time.monotonic()
    return [{"turn_id": e["turn_id"], "event": e["event"],
             "wait_ms": e["wait_ms"], "age_s": round(now - e["at"], 1)}
            for e in reversed(_LABEL_EVENTS)]


def park_label(turn_id, payload):
    """Park a finished label for a turn whose row may not exist yet."""
    if not turn_id or not payload:
        return
    now = time.monotonic()
    for k, (_, at) in list(_PENDING_LABELS.items()):
        if now - at > PENDING_LABEL_TTL_S:
            _PENDING_LABELS.pop(k, None)
            _label_event(k, LABEL_EXPIRED_UNCLAIMED, (now - at) * 1000)
    _PENDING_LABELS[turn_id] = (dict(payload), now)
    _label_event(turn_id, LABEL_PARKED, 0.0)
    while len(_PENDING_LABELS) > _PENDING_MAX:
        k = next(iter(_PENDING_LABELS))
        _, at = _PENDING_LABELS.pop(k)
        _label_event(k, LABEL_EVICTED, (now - at) * 1000)


def claim_label(turn_id, content=None):
    """Claim a parked label at insert time (single-use). None when the check
    has not finished yet - the pass then labels the row itself, so this is
    a fast path and never a dependency. With the message's `content`, a
    crosstalk split that doesn't read as that text is dropped here, as the
    pass's own write drops it (fit_to_message)."""
    entry = _PENDING_LABELS.pop(turn_id or "", None)
    if not entry:
        return None
    payload, at = entry
    wait_s = time.monotonic() - at
    if wait_s > PENDING_LABEL_TTL_S:
        _label_event(turn_id, LABEL_EXPIRED_AT_CLAIM, wait_s * 1000)
        return None
    _label_event(turn_id, LABEL_CLAIMED, wait_s * 1000)
    return fit_to_message(payload, content) if content is not None \
        else payload


def owner_sufficient(people, owner_name) -> bool:
    """Is the owner's OWN voice sufficiently enrolled? Only then can a voice
    that matches nobody mean 'a clear voice that is not the owner' rather
    than 'the owner, not yet learnt'. Without this, the pass must never arm
    or ask on a new voice - it could be the owner."""
    owner = (owner_name or "").strip().casefold()
    return any(p.get("sufficient")
               and (p.get("name") or "").strip().casefold() == owner
               for p in people or [])


# ---- the last identification decision (#28: the voice health strip) ----
#
# A tiny in-memory per-chat record of the most recent identification
# decision: whether the check named the turn, and how long it took.
# Content-free BY CONSTRUCTION - path, milliseconds and a monotonic
# timestamp; never a name, never transcript text - and written only from
# inside the never-awaited check, so the live voice path gains nothing.
# GET /api/voice/health surfaces it.

_LAST_DECISION: dict = {}
_DECISION_MAX_CHATS = 8

DECISION_LOCAL = "local"
# A turn the check declined to name (#28, thirteenth field test), with the
# reason, so "identity pending" says which of several problems it was.
DECISION_UNRESOLVED = "unresolved"

# The allowlist for what may leave the process as a reason - content-free by
# construction, like every other value here. The pass writes "listening"
# and "new_voice"; the older reasons stay because labels already stored on
# messages carry them, and the projection and the browser still read those.
DEFER_REASONS = {"too_short", "below_threshold", "ambiguous", "multi",
                 "not_speech", "no_candidates", "unavailable", "disabled",
                 "error", "pending_present", "no_enrolled", "listening",
                 "new_voice"}


# Decision history (#304 evidence capture): the single freshest record
# above answers "what just happened", but a stalled turn's decision is
# overwritten by the next turn before anyone can look. A short per-chat
# ring keeps the last few decisions with the turn id each one was for, so
# the affected turn's path and reason survive the stall it is evidence of.
# Same content-free floor as everything here: path, ms, an allowlisted
# reason, the client's opaque turn id.
_DECISION_HISTORY: dict = {}
_DECISION_HISTORY_MAX = 12


def record_decision(chat_id, path, ms, reason="", turn_id=None):
    """Remember one chat's freshest identification decision. Bounded: at
    most _DECISION_MAX_CHATS chats, one record each."""
    if chat_id is None or path not in (DECISION_LOCAL, DECISION_UNRESOLVED):
        return
    reason = reason if reason in DEFER_REASONS else ""
    _LAST_DECISION.pop(chat_id, None)  # re-insert = newest (insert order)
    _LAST_DECISION[chat_id] = {"path": path, "ms": round(float(ms), 1),
                               "reason": reason, "at": time.monotonic()}
    while len(_LAST_DECISION) > _DECISION_MAX_CHATS:
        _LAST_DECISION.pop(next(iter(_LAST_DECISION)))
    hist = _DECISION_HISTORY.setdefault(chat_id, [])
    hist.append({"path": path, "ms": round(float(ms), 1), "reason": reason,
                 "turn_id": str(turn_id or "")[:64], "at": time.monotonic()})
    del hist[:-_DECISION_HISTORY_MAX]
    while len(_DECISION_HISTORY) > _DECISION_MAX_CHATS:
        _DECISION_HISTORY.pop(next(iter(_DECISION_HISTORY)))


def decision_history(chat_id) -> list:
    """The chat's recent identification decisions, newest first, each with
    its age in seconds and the turn id it decided (\"\" for an older client
    or a path with no id in hand)."""
    now = time.monotonic()
    return [{"path": d["path"], "ms": d["ms"], "reason": d["reason"],
             "turn_id": d["turn_id"], "age_s": round(now - d["at"], 1)}
            for d in reversed(_DECISION_HISTORY.get(chat_id) or [])]


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
    """Wrap raw PCM-16 mono in a minimal WAV container - how the anchor
    store writes a clip to disk."""
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


def _norm_text(text) -> str:
    return re.sub(r"\s+", " ",
                  re.sub(r"[^a-z0-9]+", " ", (text or "").casefold())).strip()


def segments_align(segments, content) -> bool:
    """May a crosstalk split be SHOWN against this message? Only when its
    words, joined in order, read as the same text the message carries
    (case, punctuation and spacing aside). Where they disagree, an
    attributed split would contradict the message it annotates - the
    marker alone is the honest fallback."""
    if not segments:
        return False
    joined = _norm_text(" ".join(s.get("text") or "" for s in segments))
    return bool(joined) and joined == _norm_text(content)


def fit_to_message(payload, content):
    """The payload as it may be stored on a message with this text (pure):
    a crosstalk split whose words don't read as the message is dropped and
    the marker stands alone (segments_align). One rule for both writes,
    the claim at insert and the pass's own, so carries_payload compares
    like with like."""
    if payload and payload.get("segments") \
            and not segments_align(payload["segments"], content):
        return {k: v for k, v in payload.items() if k != "segments"}
    return payload


def carries_payload(raw, payload) -> bool:
    """Does a row's stored voice_labels JSON hold exactly this payload?
    (#461, pure.) True means /send claimed this very label at insert, so
    the pass that parked it still owns the row's follow-up work. Compared
    after a JSON round trip, so a tuple and a list read the same. Anything
    malformed reads as someone else's label."""
    if not raw or not isinstance(raw, str) or not payload:
        return False
    try:
        stored = json.loads(raw)
        wanted = json.loads(json.dumps(payload))
    except (TypeError, ValueError):
        return False
    return stored == wanted


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


class RoomSession:
    """Per-websocket-session state: the utterance tee buffer, and which
    messages this session already labelled. Session-scoped: a reconnect
    starts a fresh one."""

    def __init__(self):
        self.buffer = bytearray()
        self.sample_rate = 16000
        self.labelled_ids = set()

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


# ---------- one check per turn, whichever path heard it (#461) ----------
#
# A voiced turn reaches the server one of two ways: the realtime relay
# (routers/voice.py's stt-stream, which tees the audio as it streams) or
# the batch /stt POST (the fallback once realtime fails, and the salvage
# for a turn realtime lost). Both paths route through schedule_turn_check,
# and the turn id decides which one checks: the first to schedule a check
# for an id owns it, and the other stands down.

_CHECKED_TURNS: dict = {}
_CHECKED_TURNS_MAX = 256

# The batch path has no websocket session to hold its label bookkeeping, so
# each chat gets one RoomSession for its batch turns. Bounded; a chat that
# falls out simply starts fresh.
_BATCH_SESSIONS: dict = {}
_BATCH_SESSIONS_MAX = 8


def turn_checked(turn_id) -> bool:
    """Has a check already been scheduled for this turn id?"""
    return bool(turn_id) and turn_id in _CHECKED_TURNS


def _note_turn_checked(turn_id):
    if not turn_id:
        return
    _CHECKED_TURNS.pop(turn_id, None)  # re-insert = newest
    _CHECKED_TURNS[turn_id] = time.monotonic()
    while len(_CHECKED_TURNS) > _CHECKED_TURNS_MAX:
        _CHECKED_TURNS.pop(next(iter(_CHECKED_TURNS)))


def batch_session(chat_id) -> "RoomSession":
    """The RoomSession a chat's batch-transcribed turns share."""
    session = _BATCH_SESSIONS.pop(chat_id, None) or RoomSession()
    _BATCH_SESSIONS[chat_id] = session  # re-insert = newest
    while len(_BATCH_SESSIONS) > _BATCH_SESSIONS_MAX:
        _BATCH_SESSIONS.pop(next(iter(_BATCH_SESSIONS)))
    return session


def schedule_turn_check(chat_id, pcm, sample_rate, session, cfg, *,
                        turn_id=None, commit_ts=None):
    """Send one voiced turn to the voice check (backend/voice_pass.py) and
    return its task, IMMEDIATELY: the check is a never-awaited task, in
    every mode. None when nothing was scheduled: no audio, or the matcher
    is switched off (`voice_id_enabled`), which leaves every turn
    unchecked and unnamed. A turn id is noted only when a check was really
    scheduled, so a relay commit that carried no audio leaves the batch
    copy free to check the turn."""
    if not voiceid.enabled(cfg):
        return None
    commit_ts = db.now() if commit_ts is None else commit_ts
    from . import voice_pass
    task = voice_pass.schedule(chat_id, pcm, sample_rate, commit_ts,
                               session, cfg, turn_id)
    if task is not None:
        _note_turn_checked(turn_id)
    return task


def wav_pcm16(data: bytes):
    """(pcm, sample_rate) out of a PCM-16 mono WAV, or None (pure; the
    inverse of pcm16_wav). The batch path's audio copy arrives in this
    shape, built by the browser from its own recording. Anything else, or
    anything malformed, is None: the turn then goes unchecked and never
    breaks the transcription beside it. Keeps the newest
    MAX_UTTERANCE_SECONDS, like the relay's buffer."""
    if not isinstance(data, (bytes, bytearray)) or len(data) < 44 \
            or data[:4] != b"RIFF" or data[8:12] != b"WAVE":
        return None
    pos, fmt, pcm = 12, None, None
    while pos + 8 <= len(data):
        cid = data[pos:pos + 4]
        size = struct.unpack("<I", data[pos + 4:pos + 8])[0]
        body = data[pos + 8:pos + 8 + size]
        if cid == b"fmt " and len(body) >= 16:
            fmt = struct.unpack("<HHIIHH", body[:16])
        elif cid == b"data":
            pcm = bytes(body)
            break
        pos += 8 + size + (size & 1)
    if fmt is None or pcm is None:
        return None
    audio_format, channels, rate, _, _, bits = fmt
    if audio_format != 1 or channels != 1 or bits != 16 \
            or not 8000 <= rate <= 48000:
        return None
    pcm = pcm[:len(pcm) - (len(pcm) % 2)]
    cap = MAX_UTTERANCE_SECONDS * rate * 2
    pcm = pcm[-cap:]
    return (pcm, rate) if pcm else None


# ---------- the label path ----------

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


def label_payload(labels, *, clusters=("local",), uncertain=(), source="local",
                  score=None, owner=False, learning=False, unresolved=""):
    """One label write's payload, the shape every consumer of labels_json
    reads. Build it in exactly one place (workbench review, #237).
    Consumers ignore unknown keys, so optional markers are simply absent
    rather than null. Pure, so tests can pin the shape without a room."""
    payload = {"clusters": list(clusters), "labels": list(labels),
               "uncertain": list(uncertain)}
    if source:
        payload["source"] = source
    if score is not None:
        payload["score"] = round(score or 0, 3)
    if owner:
        payload["owner"] = True
    if learning:
        payload["learning"] = True
    if unresolved:
        # #411: a turn the check could not name names nobody and says why,
        # in one of DEFER_REASONS. The projection turns it into a plain head
        # the seats can repeat, and memory reads it as a doubted turn, never
        # as the owner.
        payload["unresolved"] = unresolved
    return payload


async def _deliver_label(chat_id, pcm, sample_rate, commit_ts, session,
                         payload, *, turn_id=None, clusters_remembered=1):
    """Attach one label payload, then remember the turn's audio for
    tap-to-correct and for an introduction to claim. The attach comes
    FIRST (#28, night test 4): the label write is what the seats are
    waiting on, so no bookkeeping may queue in front of it; remember_audio
    is an in-memory ring, not a file write. Returns the target message id,
    or None when no row claimed the label - and None means the post-label
    work that keys off the row (the ask, the mismatch cross-check) must
    not run."""
    target_id = await _attach_until_deadline(chat_id, commit_ts, payload,
                                             session, turn_id=turn_id)
    if target_id:
        from . import anchors
        anchors.remember_audio(target_id, pcm, sample_rate,
                               clusters_remembered)
    return target_id


# ---------- the room's writes the pass makes ----------

def remembered_candidates(people=None, rostered_ids=frozenset()):
    """EVERY sufficient remembered person as naming candidates - THE
    candidate semantics, one construction for every caller (the session
    naming and its warm, #28 remembered-first). The fourteenth field test
    was a drift between two such lists: the armed check had narrowed its
    candidates to the present roster, and a remembered guest became
    unrecognisable the moment room mode armed. Reads the store when
    `people` is None - call it on a worker thread then.

    #83: a sufficient bank nobody vouched for (see anchors.needs_audition)
    has no remembered-first rights - it can neither name nor seat anyone
    in a session until the owner auditions it. `rostered_ids` is the one
    exception: a person already SEATED in this chat (a seat a human put
    there) keeps being identified, because the pause guards RE-seating,
    not the seat."""
    if people is None:
        from . import anchors
        people = anchors.store().people()
    return [{"person_id": p["person_id"], "name": p["name"]}
            for p in people
            if p.get("sufficient")
            and (not p.get("id_paused")
                 or p["person_id"] in rostered_ids)]


def _arm_known(chat_id, matched, cfg):
    """Arm for a remembered voice (worker thread): room_state's durable flip
    - the same control plumbing an introduction uses - and one linked
    roster row per matched remembered person. clear_ambient=False: an
    automatic arm must never clear the sacred flag (it only ever fires
    while the flag is unset). seat_owner="never": a remembered guest match
    proves a household by itself; the owner is seated on the UNKNOWN path.
    Idempotent: two checks racing re-run the same writes harmlessly."""
    from . import room_state
    con = db.connect()
    try:
        room_state.arm(chat_id, cfg, source="ambient (known voice)",
                       clear_ambient=False, seat_owner="never", con=con)
        log.info("ambient arm matched: chat=%s matched=%d",
                 chat_id, len(matched))
        from . import anchors
        store = anchors.store()
        for name in dict.fromkeys(matched.values()):
            person = store.find_by_name(name)
            pid = person["person_id"] if person else ""
            room_state.seat(chat_id, name, cfg, via="voice-match",
                            person_id=pid, enforce_cap=True, con=con)
    finally:
        con.close()


def _arm_ambient_unknown(chat_id, cfg):
    """Arm room mode for a clear new voice nobody knows (worker thread).
    clear_ambient=False: an automatic arm only ever fires while the sacred
    flag is unset, and must never gain the power to clear it.
    seat_owner="on_arm": the owner joins the roster, since the owner's own
    voice is known and this one isn't theirs; a room some other path armed
    first has already seated them. Idempotent. The ask itself is raised by
    the caller once the label attaches (_raise_unknown_voice needs the
    message id, which does not exist yet here)."""
    from . import room_state
    room_state.arm(chat_id, cfg, source="ambient (unknown voice)",
                   clear_ambient=False, seat_owner="on_arm")


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


def _attach_labels(chat_id, commit_ts, payload, session, turn_id=None):
    """One synchronous attempt (runs on a worker thread): find the
    utterance's user message and persist the labels through the single update
    path, which also rings the live-events bell. Returns the labelled id,
    None for "nothing to label" (id-less callers retry that until their
    window closes), or _NO_ROW_YET when an exact-id target simply is not
    persisted yet - the one case the id path retries.

    With a turn id (#28 phase 3) the lookup is EXACT: only the message whose
    persisted voice_turn_id matches may be labelled, and the time-window
    guesswork never runs - so a check whose utterance produced no message
    (a dropped interjection) labels nothing, ever.

    Crosstalk segments are gated HERE, where the message text is finally in
    hand: the split persists only when its words read as the text the
    message carries (segments_align). Where they disagree, the split is
    dropped and the crosstalk marker stands alone."""
    con = db.connect()
    try:
        if turn_id:
            target = db.get_message_by_voice_turn(con, chat_id, turn_id)
            if not target:
                return _NO_ROW_YET  # the /send race - worth a fast retry
            if target["id"] in session.labelled_ids:
                return None  # this session already labelled it
            payload = fit_to_message(payload, target.get("content"))
            if target.get("voice_labels"):
                if carries_payload(target["voice_labels"], payload):
                    # #461: /send claimed THIS check's parked label inside
                    # the insert, which is the common case since labels
                    # ride the insert. The label is on the row, so the
                    # work that keys off the row (the who-joined ask, the
                    # mismatch cross-check, tap-to-correct's audio) still
                    # needs its id. Returning None here silenced all three
                    # for nearly every turn.
                    session.labelled_ids.add(target["id"])
                    return target["id"]
                return None  # someone else's label: nothing to do
        else:
            rows = db.get_voice_label_candidates(con, chat_id, commit_ts)
            target = pick_target(rows, session.labelled_ids)
            if not target:
                return None
            payload = fit_to_message(payload, target.get("content"))
        db.set_message_voice_labels(con, target["id"], payload)
        session.labelled_ids.add(target["id"])
        return target["id"]
    finally:
        con.close()
