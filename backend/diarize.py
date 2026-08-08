"""Room mode (#28, phase 1): the parallel diarization pass.

THE CORE PRINCIPLE - zero added latency on the live voice path. The realtime
STT relay (routers/voice.py) stays byte-for-byte identical upstream whether
room mode is on or off; when it is on, the relay only TEES already-decoded
utterance audio into a per-session buffer and, on each commit boundary, fires
a fire-and-forget task from here. Nothing in the live path ever awaits that
task, and every blocking step inside it (the ElevenLabs batch call, every
database touch) runs on a worker thread via asyncio.to_thread, so even the
shared event loop never stalls on it.

What the pass does: the buffered utterance goes to batch Scribe v2 with
diarize=true (the realtime model has no diarization - that asymmetry is the
whole reason this module exists). When the answer arrives a second or two
later, per-word speaker clusters are reduced to an utterance-level cluster
list, and if the utterance holds more than one cluster - or a different
cluster than the previous utterance - unnamed labels ("Voice 1", "Voice 2";
first-seen order within this session) are attached to the matching user
message and pushed over the live-events stream.

Honesty note, from the issue: batch diarization clusters are PER-REQUEST.
speaker_0 in one utterance's call is not guaranteed to be the same person in
the next call. Phase 1 labels are therefore best-effort per utterance;
per-person anchoring and naming are phase 2. The raw clusters are persisted
next to the labels so phase 2 can re-derive without a data migration.

Failure posture: a failed or slow pass leaves the message unlabelled and
everything else untouched. No retry ever feeds back into the live path.
"""

import asyncio
import logging
import struct
import time

from . import db, voice

log = logging.getLogger("crossband.diarize")

# Utterance buffer cap: ~2 minutes of PCM-16 mono. Past it we keep the TAIL
# (the newest audio) - an utterance that long has long since ceased to be one
# utterance, and an unbounded buffer is a memory leak with a microphone.
MAX_UTTERANCE_SECONDS = 120

# How long the pass keeps looking for the user message to label. The message
# is inserted by the client's own /send a moment after commit, so the match
# normally lands on the first probe; the window only bounds the give-up.
# No backward slack on the match: the utterance's message is ALWAYS stamped
# after its commit instant (the client dispatches only once the committed
# transcript returns, and both timestamps come from this process's clock), and
# reaching back before the commit could grab the PREVIOUS turn's unlabelled
# message when utterances arrive quickly.
MATCH_WINDOW_SECS = 8.0
MATCH_PROBE_SECS = 0.5

# Strong references to in-flight passes: asyncio only holds weak refs to
# tasks, and a garbage-collected fire-and-forget task silently never runs.
_TASKS: set = set()


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

def schedule_pass(chat_id, pcm, sample_rate, commit_ts, session, cfg):
    """Fire the diarization pass for one committed utterance and return
    IMMEDIATELY. The caller (the realtime relay) never awaits the task; a
    strong reference is kept so the loop cannot garbage-collect it mid-run,
    and run_pass itself swallows every failure."""
    if not pcm:
        return None
    task = asyncio.get_running_loop().create_task(
        run_pass(chat_id, pcm, sample_rate, commit_ts, session, cfg))
    _TASKS.add(task)
    task.add_done_callback(_TASKS.discard)
    return task


async def run_pass(chat_id, pcm, sample_rate, commit_ts, session, cfg):
    """One utterance's parallel pass: batch STT with diarize=true, then label
    reconciliation. Every blocking step runs on a worker thread; every
    failure ends here (log only - the live path must never notice)."""
    t0 = time.perf_counter()
    try:
        result = await asyncio.to_thread(
            voice.transcribe_diarized, pcm16_wav(pcm, sample_rate),
            "audio/wav", cfg)
    except Exception:
        # Content-free by design, like every log on the voice path.
        log.info("diarize pass failed: chat=%s ms=%.0f", chat_id,
                 (time.perf_counter() - t0) * 1000)
        log.debug("diarize pass failure detail", exc_info=True)
        return
    duration_ms = (time.perf_counter() - t0) * 1000
    try:
        clusters = utterance_clusters(result.get("words"))
        # EVERY cluster the session sees claims its ordinal, labelled or not:
        # the session's first voice is "Voice 1" even while it goes unlabelled
        # (the common lone-speaker case), so the first DIFFERENT voice
        # correctly surfaces as "Voice 2", not as a misleading "Voice 1".
        mapped = session.assign(clusters)
        labels = mapped if should_label(clusters, session.prev_clusters) else []
        if clusters:
            session.prev_clusters = clusters
        # The latency record for the parallel pass. INFO and content-free:
        # durations and counts only, never transcript text. The live path's
        # own latency is measured elsewhere (voice_trace) and must show no
        # dependence on this number - pinned by tests/test_room_mode.py.
        log.info("diarize pass: chat=%s ms=%.0f clusters=%d labels=%d",
                 chat_id, duration_ms, len(clusters), len(labels))
        # The second transcription pass is real, metered spend - the reason
        # the room-mode toggle warns that voice minutes roughly double.
        await asyncio.to_thread(_meter, chat_id, pcm, sample_rate, cfg)
        if not labels:
            return
        payload = {"clusters": clusters, "labels": labels}
        deadline = time.monotonic() + MATCH_WINDOW_SECS
        while True:
            target_id = await asyncio.to_thread(
                _attach_labels, chat_id, commit_ts, payload, session)
            if target_id or time.monotonic() >= deadline:
                return
            await asyncio.sleep(MATCH_PROBE_SECS)
    except Exception:
        log.info("diarize labelling failed: chat=%s", chat_id)
        log.debug("diarize labelling failure detail", exc_info=True)


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


def _attach_labels(chat_id, commit_ts, payload, session):
    """One synchronous probe (runs on a worker thread): find the utterance's
    user message and persist the labels through the single update path, which
    also rings the live-events bell. Returns the labelled id, or None to let
    the async loop probe again until the window closes."""
    con = db.connect()
    try:
        rows = db.get_voice_label_candidates(con, chat_id, commit_ts)
        target = pick_target(rows, session.labelled_ids)
        if not target:
            return None
        db.set_message_voice_labels(con, target["id"], payload)
        session.labelled_ids.add(target["id"])
        return target["id"]
    finally:
        con.close()
