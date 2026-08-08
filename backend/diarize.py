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
the next call. Phase 2 (#28) fixes comparability with ANCHORS: when the chat
has a roster, each request is prefixed with a couple of seconds of every
remembered present person's stored voice (backend/anchors.py) and hinted with
num_speakers = roster + 1. The diarizer's prefix clusters then read straight
back into NAMES, an utterance cluster matching a prefix cluster is that
person, and an unmatched cluster is genuinely someone new - resolved by
elimination when exactly one introduced person still lacks an anchor, and
surfaced as an 'unknown_voice' ask-fallback flag otherwise. With no roster
the pass behaves exactly as phase 1 (ordinals, label-only-when-interesting),
and the phase-1 byte-identity/latency pins all still hold.

Failure posture: a failed or slow pass leaves the message unlabelled and
everything else untouched. No retry ever feeds back into the live path.
"""

import asyncio
import logging
import re
import struct
import time

from . import db, voice, voiceid

log = logging.getLogger("crossband.diarize")

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

MISMATCH_MIN_WORDS = 4  # don't cross-check a grunt
# Fast-path (#28 part 2) mismatch gate: with no batch words to count, use the
# utterance duration as the "enough words to be worth cross-checking" proxy
# (~4 words is roughly 1.5s of speech).
FAST_MISMATCH_MIN_SECONDS = 1.5

# ---- session-start sniff (#28, third field test) ----
#
# The structural gap: in a fresh chat, REMEMBERED voices could not arm room
# mode - this pass only ran once room mode was on, and only an introduction
# or the toggle turned it on, so a known person with sufficient anchors was
# undetectable by construction. The sniff closes it: when a voice session
# starts with room mode OFF but sufficient remembered NON-OWNER voices
# exist, the first SNIFF_UTTERANCES committed utterances also run this
# module's existing pass machinery (anchor prefix, batch call, label
# plumbing - nothing new). A pass that matches a remembered non-owner voice,
# or finds two clusters, arms room mode server-side (the phase-2 plumbing),
# seeds the roster with the matched people, and labels that turn; otherwise
# the sniff simply ends. Bounded and stated honestly: at most
# SNIFF_UTTERANCES extra batch calls per session, each metered as real
# spend like any other pass. Same never-awaited discipline as every pass;
# with no remembered non-owner voices the relay never schedules a sniff at
# all, so the common no-household case costs nothing.
SNIFF_UTTERANCES = 2


def sniff_eligible(people, owner_name) -> bool:
    """Should a session in a room-mode-off chat sniff at all? Only when a
    SUFFICIENT remembered person other than the owner exists. Owner-only
    anchors buy nothing: there is nobody else to re-identify, and a
    genuinely new second voice still has the introduction and ask paths."""
    owner = (owner_name or "").strip().casefold()
    return any(
        p.get("sufficient")
        and (p.get("name") or "").strip().casefold() != owner
        for p in people or [])


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
        # Session-start sniff budget (#28): how many more committed
        # utterances may run a sniff pass. The relay sets it to
        # SNIFF_UTTERANCES at session open when the chat is eligible; it
        # only ever counts down, and an armed sniff zeroes it.
        self.sniff_remaining = 0

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

def schedule_pass(chat_id, pcm, sample_rate, commit_ts, session, cfg,
                  turn_id=None):
    """Fire the diarization pass for one committed utterance and return
    IMMEDIATELY. The caller (the realtime relay) never awaits the task; a
    strong reference is kept so the loop cannot garbage-collect it mid-run,
    and run_pass itself swallows every failure. `turn_id` is the client's
    voice-trace correlation id from the commit frame - with it the label
    write targets the exact message (#28 phase 3); without it the
    time-window fallback applies."""
    if not pcm:
        return None
    task = asyncio.get_running_loop().create_task(
        run_pass(chat_id, pcm, sample_rate, commit_ts, session, cfg,
                 turn_id=turn_id))
    _TASKS.add(task)
    task.add_done_callback(_TASKS.discard)
    return task


async def run_pass(chat_id, pcm, sample_rate, commit_ts, session, cfg,
                   turn_id=None):
    """One utterance's parallel pass: batch STT with diarize=true, then label
    reconciliation. Every blocking step runs on a worker thread; every
    failure ends here (log only - the live path must never notice).

    With a roster (#28 phase 2) the request grows an anchor prefix and a
    num_speakers hint, and labelling goes through the naming rules
    (_room_label_pass); with no roster this is byte-for-byte the phase-1
    request and the phase-1 ordinal rules."""
    t0 = time.perf_counter()
    plan = None
    try:
        plan = await asyncio.to_thread(_room_plan, chat_id, sample_rate)
    except Exception:
        # A broken roster read degrades to the phase-1 pass, never to a
        # dropped utterance.
        log.debug("room plan failed; running phase-1 pass", exc_info=True)
    prefix_pcm, segments, pending, num_speakers = plan or (b"", [], [], None)
    # Fast local identity path (#28 part 2): with a roster, try to NAME a
    # confident single known speaker locally and skip the ElevenLabs batch call
    # for this turn - the label then lands ~100-300ms after commit instead of
    # ~1-2s, usually before the round's first responder reads the turn. Only a
    # confident single match takes this path; the matcher defers multi-voice,
    # unknown, ambiguous and insufficient-anchor cases (and every disabled or
    # no-model case) to the unchanged EL path below, so crosstalk word-splitting,
    # ordinals and the ask-fallback are preserved exactly. Still inside the
    # never-awaited task; the live path is untouched, and metering is skipped
    # because no second transcription happened.
    if plan is not None and voiceid.enabled(cfg):
        verdict = None
        try:
            candidates = [{"person_id": s["person_id"], "name": s["name"]}
                          for s in segments]
            verdict = await asyncio.to_thread(
                voiceid.identify_utterance, pcm, sample_rate, candidates, cfg)
        except Exception:
            # The matcher must NEVER break voice: any failure here just falls
            # through to the unchanged ElevenLabs path below.
            log.info("voiceid identify failed; running the EL path: chat=%s",
                     chat_id)
            log.debug("voiceid identify failure detail", exc_info=True)
        if voiceid.matched(verdict):
            log.info("diarize pass (voiceid): chat=%s ms=%.0f score=%.3f",
                     chat_id, (time.perf_counter() - t0) * 1000,
                     verdict["score"])
            try:
                await _fast_label_pass(chat_id, pcm, sample_rate, commit_ts,
                                       session, cfg, verdict, turn_id=turn_id)
            except Exception:
                # We already committed to skipping the batch call; do NOT fall
                # through to a second (EL) attempt on the same turn - that risks
                # a double label. The turn stays unlabelled, exactly the failure
                # posture of a failed batch pass.
                log.info("voiceid fast-label failed: chat=%s", chat_id)
                log.debug("voiceid fast-label failure detail", exc_info=True)
            return
        if verdict is not None:
            log.info("diarize pass (voiceid defer): chat=%s reason=%s",
                     chat_id, verdict["reason"])
    request_pcm = prefix_pcm + pcm
    try:
        result = await asyncio.to_thread(
            voice.transcribe_diarized, pcm16_wav(request_pcm, sample_rate),
            "audio/wav", cfg, num_speakers=num_speakers)
    except Exception:
        # Content-free by design, like every log on the voice path.
        log.info("diarize pass failed: chat=%s ms=%.0f", chat_id,
                 (time.perf_counter() - t0) * 1000)
        log.debug("diarize pass failure detail", exc_info=True)
        return
    duration_ms = (time.perf_counter() - t0) * 1000
    # Labelling runs FIRST, metering after (#28, night test 4): from the
    # moment the batch reply is parsed, every millisecond spent before the
    # label write is identity latency the seats can observe, so nothing may
    # queue in front of the attach. The metering moves to the finally below -
    # the spend became real when the batch call returned, so a labelling
    # failure still meters it, just no longer ahead of the labels.
    try:
        if plan is not None:
            await _room_label_pass(chat_id, pcm, sample_rate, commit_ts,
                                   session, cfg, result, segments, pending,
                                   len(prefix_pcm) / 2 / (sample_rate or 16000),
                                   duration_ms, turn_id=turn_id)
        else:
            words = result.get("words")
            clusters = utterance_clusters(words)
            # EVERY cluster the session sees claims its ordinal, labelled or
            # not: the session's first voice is "Voice 1" even while it goes
            # unlabelled (the common lone-speaker case), so the first
            # DIFFERENT voice correctly surfaces as "Voice 2", not as a
            # misleading "Voice 1".
            mapped = session.assign(clusters)
            labels = mapped if should_label(clusters,
                                            session.prev_clusters) else []
            if clusters:
                session.prev_clusters = clusters
            # The latency record for the parallel pass. INFO and content-free:
            # durations and counts only, never transcript text. The live
            # path's own latency is measured elsewhere (voice_trace) and must
            # show no dependence on this number - pinned by
            # tests/test_room_mode.py.
            log.info("diarize pass: chat=%s ms=%.0f clusters=%d labels=%d",
                     chat_id, duration_ms, len(clusters), len(labels))
            if labels:
                payload = {"clusters": clusters, "labels": labels}
                # Crosstalk (#28 phase 4): two voices in one utterance get the
                # marker, and - when the words alternate cleanly - a
                # best-effort attributed split. Phase-1 labels are session
                # ordinals, uncertain by construction, so every segment is
                # marked uncertain too.
                ct = crosstalk_info(words)
                if ct:
                    payload.update(ct)
                    segs = split_segments(words, dict(zip(clusters, mapped)),
                                          uncertain_labels=set(mapped))
                    if segs:
                        payload["segments"] = segs
                await _attach_until_deadline(chat_id, commit_ts, payload,
                                             session, turn_id=turn_id)
    except Exception:
        log.info("diarize labelling failed: chat=%s", chat_id)
        log.debug("diarize labelling failure detail", exc_info=True)
    finally:
        try:
            # The second transcription pass is real, metered spend - the
            # reason the room-mode toggle warns that voice minutes roughly
            # double. The anchor prefix is transcribed audio too, so it is
            # metered with it.
            await asyncio.to_thread(_meter, chat_id, request_pcm, sample_rate,
                                    cfg)
        except Exception:
            log.info("diarize metering failed: chat=%s", chat_id)
            log.debug("diarize metering failure detail", exc_info=True)


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
        deadline = time.monotonic() + min(ID_ATTACH_WINDOW_SECS,
                                          MATCH_WINDOW_SECS)
        while True:
            outcome = await asyncio.to_thread(
                _attach_labels, chat_id, commit_ts, payload, session, turn_id)
            if outcome is not _NO_ROW_YET:
                return outcome
            if time.monotonic() >= deadline:
                return None
            await asyncio.sleep(ID_ATTACH_RETRY_SECS)
    deadline = time.monotonic() + MATCH_WINDOW_SECS
    while True:
        target_id = await asyncio.to_thread(
            _attach_labels, chat_id, commit_ts, payload, session, turn_id)
        if target_id or time.monotonic() >= deadline:
            return target_id
        await asyncio.sleep(MATCH_PROBE_SECS)


def _room_plan(chat_id, sample_rate):
    """Roster snapshot for one pass (worker thread): the anchor prefix for
    every SUFFICIENT present person, the names still pending an anchor, and
    the num_speakers hint (present + 1 - the plus-one is what lets an
    unannounced voice surface as an unmatched cluster). None when the chat
    has no roster - the phase-1 pass then runs untouched."""
    con = db.connect()
    try:
        present = db.get_room_roster(con, chat_id, present_only=True)
    finally:
        con.close()
    if not present:
        return None
    from . import anchors
    store = anchors.store()
    sufficient = {p["person_id"] for p in store.people() if p["sufficient"]}
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
    return prefix_pcm, segments, pending, num_speakers


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
        await asyncio.to_thread(_accumulate_anchor, chat_id, pcm, sample_rate,
                                clusters[0], resolved)
    if not resolved["labels"]:
        return
    if resolved["ask"]:
        # Someone the anchors don't know and elimination can't name: surface
        # the ask-fallback. The turn keeps its uncertain ordinal meanwhile.
        await asyncio.to_thread(_raise_unknown_voice, chat_id, target_id)
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


def _accumulate_anchor(chat_id, pcm, sample_rate, cluster, resolved):
    """Feed one single-speaker utterance to the right person's anchor set
    (worker thread). Matched person: a refresh candidate. Eliminated person:
    their first real anchor audio - link the roster row once accepted, so the
    UI's 'anchor pending' honestly ends."""
    from . import anchors
    store = anchors.store()
    name = resolved["matched"].get(cluster)
    if name:
        person = store.find_by_name(name)
        if person:
            store.add_clip(person["person_id"], pcm, sample_rate,
                           source="accumulated")
        return
    name = resolved["eliminated"].get(cluster)
    if not name:
        return
    pid = store.ensure_person(name)
    if store.add_clip(pid, pcm, sample_rate, source="accumulated"):
        con = db.connect()
        try:
            db.link_room_person(con, chat_id, name, pid)
        finally:
            con.close()


# ---------- the fast local-identity label pass (#28 part 2) ----------

async def _fast_label_pass(chat_id, pcm, sample_rate, commit_ts, session, cfg,
                           verdict, turn_id=None):
    """Label a turn from a confident LOCAL single-speaker match, with NO
    ElevenLabs batch call. Mirrors the roster path's post-label bookkeeping for
    one matched voice: the name is attached FIRST (it is what the seats are
    waiting on), then the single-speaker utterance refreshes that person's
    anchors, is remembered for tap-to-correct, and gets the LLM mismatch
    cross-check. With the batch pass skipped, that cross-check is now the sole
    detector of an unannounced second voice the matcher took for one (owner
    decision on the issue: the mismatch flag doubles as that detector)."""
    name = verdict["name"]
    # Payload shape matches the batch path's confident-named case: a single
    # certain label projects as "<name> (in the room)" and chips as a matched
    # voice. 'source' is content-free metadata; every consumer ignores unknown
    # keys. No crosstalk marker - a confident single match is one voice.
    payload = {"clusters": ["local"], "labels": [name], "uncertain": [],
               "source": "local"}
    target_id = await _attach_until_deadline(chat_id, commit_ts, payload,
                                             session, turn_id=turn_id)
    # Anchor food: a clean single-speaker utterance refreshes the matched
    # person's clips. The batch room path does the same for a matched single
    # cluster; on the fast path this is the ONLY thing that keeps the person's
    # anchors fresh, since the batch pass no longer runs for them.
    await asyncio.to_thread(_accumulate_fast_anchor, verdict["person_id"], pcm,
                            sample_rate)
    if not target_id:
        return
    from . import anchors
    anchors.remember_audio(target_id, pcm, sample_rate, 1)
    seconds = len(pcm) / 2 / (sample_rate or 16000)
    if seconds >= FAST_MISMATCH_MIN_SECONDS:
        from . import mismatch
        mismatch.schedule_check(chat_id, target_id, name, cfg)


def _accumulate_fast_anchor(person_id, pcm, sample_rate):
    """Refresh a fast-matched person's anchors (worker thread)."""
    from . import anchors
    anchors.store().add_clip(person_id, pcm, sample_rate, source="accumulated")


async def _fast_sniff_pass(chat_id, pcm, sample_rate, commit_ts, session, cfg,
                           verdict, t0, turn_id=None):
    """The sniff decided locally (#28 part 2). A confident OWNER-only match ends
    the sniff without arming (owner alone is not a household) and without a
    batch call. A confident remembered NON-OWNER match arms room mode via the
    same durable plumbing an introduction uses, seeds the roster, labels this
    turn, and refreshes the person's anchors - all with no batch call and no
    metering, since no second transcription happened."""
    name = verdict["name"]
    owner = (cfg.get("user_name") or "User").casefold()
    if name.casefold() == owner:
        log.info("diarize pass (sniff voiceid): chat=%s ms=%.0f armed=0",
                 chat_id, (time.perf_counter() - t0) * 1000)
        return
    log.info("diarize pass (sniff voiceid): chat=%s ms=%.0f armed=1",
             chat_id, (time.perf_counter() - t0) * 1000)
    session.sniff_remaining = 0  # armed; no second sniff needed
    await asyncio.to_thread(_arm_from_sniff, chat_id, {"local": name}, cfg)
    payload = {"clusters": ["local"], "labels": [name], "uncertain": [],
               "source": "local"}
    target_id = await _attach_until_deadline(chat_id, commit_ts, payload,
                                             session, turn_id=turn_id)
    await asyncio.to_thread(_accumulate_fast_anchor, verdict["person_id"], pcm,
                            sample_rate)
    if target_id:
        from . import anchors
        anchors.remember_audio(target_id, pcm, sample_rate, 1)


# ---------- the session-start sniff pass (#28, third field test) ----------

def schedule_sniff(chat_id, pcm, sample_rate, commit_ts, session, cfg,
                   turn_id=None):
    """Fire one sniff pass and return IMMEDIATELY - the relay never awaits
    it, exactly the schedule_pass contract. The relay has already spent one
    unit of session.sniff_remaining before calling."""
    if not pcm:
        return None
    task = asyncio.get_running_loop().create_task(
        run_sniff(chat_id, pcm, sample_rate, commit_ts, session, cfg,
                  turn_id=turn_id))
    _TASKS.add(task)
    task.add_done_callback(_TASKS.discard)
    return task


async def run_sniff(chat_id, pcm, sample_rate, commit_ts, session, cfg,
                    turn_id=None):
    """One utterance's sniff: the existing anchored pass run while room mode
    is OFF, listening for a remembered non-owner voice or a second cluster.
    Either arms room mode, seeds the roster with the matched people, and
    labels this turn; anything else - including every failure - ends here
    with room mode untouched."""
    t0 = time.perf_counter()
    try:
        plan = await asyncio.to_thread(_sniff_plan, chat_id, sample_rate, cfg)
        if plan is None:
            return
        prefix_pcm, segments, num_speakers = plan
        # Fast local identity (#28 part 2): decide the sniff locally when we can.
        # A confident remembered NON-OWNER match arms room mode and labels the
        # turn with no batch call; a confident OWNER-only match ends the sniff
        # (owner alone never arms) with no batch call either - saving the doubled
        # transcription on the common solo utterance. The matcher defers
        # multi-voice, unknown and ambiguous cases to the EL sniff below, so a
        # brand-new second voice (two clusters) still arms exactly as today.
        if voiceid.enabled(cfg):
            verdict = None
            try:
                candidates = [{"person_id": s["person_id"], "name": s["name"]}
                              for s in segments]
                verdict = await asyncio.to_thread(
                    voiceid.identify_utterance, pcm, sample_rate, candidates,
                    cfg)
            except Exception:
                # Never break the sniff: a matcher failure falls through to the
                # unchanged EL sniff below.
                log.info("voiceid sniff identify failed; EL sniff: chat=%s",
                         chat_id)
                log.debug("voiceid sniff identify failure detail", exc_info=True)
            if voiceid.matched(verdict):
                await _fast_sniff_pass(chat_id, pcm, sample_rate, commit_ts,
                                       session, cfg, verdict, t0,
                                       turn_id=turn_id)
                return
            if verdict is not None:
                log.info("diarize pass (sniff voiceid defer): chat=%s reason=%s",
                         chat_id, verdict["reason"])
        request_pcm = prefix_pcm + pcm
        result = await asyncio.to_thread(
            voice.transcribe_diarized, pcm16_wav(request_pcm, sample_rate),
            "audio/wav", cfg, num_speakers=num_speakers)
        try:
            prefix_seconds = len(prefix_pcm) / 2 / (sample_rate or 16000)
            prefix_words, utter_words = split_words_at(result.get("words"),
                                                       prefix_seconds)
            cmap = prefix_cluster_map(prefix_words, segments)
            clusters = utterance_clusters(utter_words)
            owner = (cfg.get("user_name") or "User").casefold()
            matched = {c: cmap[c] for c in clusters if c in cmap}
            non_owner = [n for n in matched.values() if n.casefold() != owner]
            armed = bool(non_owner) or len(clusters) >= 2
            # The sniff marker: content-free, and distinguishable from the
            # room-mode pass's own line at a grep.
            log.info("diarize pass (sniff): chat=%s ms=%.0f clusters=%d "
                     "matched=%d armed=%d", chat_id,
                     (time.perf_counter() - t0) * 1000, len(clusters),
                     len(matched), 1 if armed else 0)
            if not armed:
                return
            session.sniff_remaining = 0  # job done; no second sniff needed
            await asyncio.to_thread(_arm_from_sniff, chat_id, matched, cfg)
            if clusters:
                session.prev_clusters = clusters
            # Label this turn through the same naming rules as any roster
            # pass: matched clusters carry the person's name, anything else a
            # session ordinal (uncertain). No pending people exist yet and
            # the sniff itself never raises an ask - from the next commit the
            # armed room-mode pass owns identification, asks included.
            resolved = resolve_room_labels(clusters, cmap, [], session)
            if not resolved["labels"]:
                return
            payload = {"clusters": clusters, "labels": resolved["labels"],
                       "uncertain": resolved["uncertain"]}
            target_id = await _attach_until_deadline(chat_id, commit_ts,
                                                     payload, session,
                                                     turn_id=turn_id)
            if target_id:
                from . import anchors
                anchors.remember_audio(target_id, pcm, sample_rate,
                                       len(clusters))
        finally:
            # Real, metered spend - the sniff is the "first couple of
            # utterances transcribed twice" the changelog and UI copy warn
            # about. Metered AFTER the labels (#28, night test 4), same as
            # every pass: the spend became real when the batch call returned,
            # so it still books on any labelling outcome - just never in
            # front of the label write.
            await asyncio.to_thread(_meter, chat_id, request_pcm, sample_rate,
                                    cfg)
    except Exception:
        log.info("sniff pass failed: chat=%s", chat_id)
        log.debug("sniff pass failure detail", exc_info=True)


def _sniff_plan(chat_id, sample_rate, cfg):
    """Sniff snapshot for one pass (worker thread): None unless the sniff
    should still run - the chat's durable room mode must be OFF (another
    path may have armed it since session open) and sufficient remembered
    non-owner people must exist. The prefix carries EVERY sufficient person,
    the owner included when remembered: telling owner from guest is the
    whole question."""
    con = db.connect()
    try:
        row = con.execute("SELECT room_mode FROM chats WHERE id=?",
                          (chat_id,)).fetchone()
    finally:
        con.close()
    if not row or row["room_mode"]:
        return None
    from . import anchors
    store = anchors.store()
    people = store.people()
    if not sniff_eligible(people, cfg.get("user_name")):
        return None
    sufficient_ids = [p["person_id"] for p in people if p["sufficient"]]
    prefix_pcm, segments = store.build_prefix(sufficient_ids, sample_rate)
    if not prefix_pcm:
        return None
    num_speakers = min(32, len(segments) + 1)
    return prefix_pcm, segments, num_speakers


def _arm_from_sniff(chat_id, matched, cfg):
    """The arm itself (worker thread): the durable flip plus the live mirror
    - the same phase-2 control plumbing an introduction uses - and one
    linked roster row per matched remembered person. Idempotent: a second
    sniff pass racing this one re-runs the same writes harmlessly."""
    con = db.connect()
    try:
        db.set_chat_room_mode(con, chat_id, True)
        set_room_enabled(chat_id, True)
        log.info("room mode ON via sniff: chat=%s matched=%d",
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
            db.add_room_person(con, chat_id, name, person_id=pid)
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
