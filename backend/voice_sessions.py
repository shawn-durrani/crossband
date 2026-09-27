"""Voice sessions (#482): follow each voice through a voice session, and
name each voice once, from everything it has said.

A voice turn used to be named on its own, from the whole turn. This
module follows each voice through the whole voice session with a local
diariser instead (docs/VOICE_ID.md), and backend/voice_pass.py reads its
answer for every spoken turn:

  1. The relay hands every audio chunk inside a turn to feed() as it
     arrives, and end_turn() at the commit. A per-chat feed thread opens a
     streaming tracking session on the loopback diariser (workbench's
     diarserve, its /sessions routes) the first time a turn arrives, and
     reopens one after SESSION_IDLE_S of quiet. It pushes the chunks in
     quarter-second pieces, and at the end of a turn pushes a short
     silence so the tracker labels the turn's last second. The answer is
     spans in session time: which voice slot, start, end, and whether
     another slot spoke over it. A slot keeps its number for the session,
     so each slot is a session voice.
  2. Every span of MIN_SPAN_S or more that one voice has alone, and that
     passes the speech gates, is fingerprinted with TitaNet-Small and,
     once the calibrated scorer has loaded it, ERes2Net, and added to that
     session voice's evidence. A span the tracker calls final while the
     person is still talking is fingerprinted then. Before it's added,
     THE SPAN CHECK may move it to the session voice it plainly sounds
     like.
  3. After each turn every session voice is named from its pooled
     fingerprints, one person per voice: by the calibrated two-model
     scorer when its snapshot is ready (backend/voice_calibration.py),
     else by the multi scorer on the matcher's bar carried onto its scale.
  4. When a voice is named, its earlier turns in the session that carry
     no name, or a name this module wrote, take the name (FILLING IN).
  5. One content-free row per turn records the turn's spans and main
     voice and every session voice's state.
  6. When the tracking session ends, THE END-OF-SESSION PASS names every
     voice once more and relabels any turn whose name changed.

With no diariser configured, or with it down, no feed answers and the
pass names the turn on its own as one voice (name_single_turn), with the
same scorer and no session behind it.

LONG TURNS. The browser cuts a long turn into pieces, each committed with
its own turn id, and the message carries one of them: the last piece's,
or the last cut piece's when the turn ended in the pause after it. Each
piece after the first names the piece before it (`after`), so a turn's
pieces are known the moment each one is committed. The feed answers a
piece with the voice its pieces had together: every piece's spans, end
to end, with each session voice named as it stands now. The main voice
is the one with the most time alone across the pieces, and a second
voice that spoke a second or more makes it a two-voice turn, so pieces
whose voices disagree never give one name, and a piece too short to
judge adds nothing. A piece the feed didn't answer is named on its own,
and the pass then joins what each piece heard by the same rules
(join_pieces), counting only the voices a piece named or found new.

THE SPAN CHECK. The tracker can split one person into two voices, when
their voice changes, or give one person's span to another voice. Each
clean span's fingerprint is compared with every session voice's pooled
fingerprint (the cosine, averaged over TitaNet-Small and ERes2Net where
both have one), before the span is added. It moves to another voice
only when all of these hold, so a normal session moves nothing:

  * that voice has SPAN_MOVE_MIN_S of clean speech behind it;
  * the span scores SPAN_MOVE_SIM or more against it;
  * it beats every other voice with any evidence, the tracker's own
    included, by SPAN_MOVE_MARGIN or more. A voice the tracker has only
    just started has no evidence to compare, which is how a split shows.

A moved span is that voice's from then on: its evidence, and the turn's
spans, so the turn is labelled by it. The turn's row lists each move
(`moved`: times, from, to, both scores and the lead) and, for the spans
that stayed, the most any scored higher against another voice than its
own (`span_lead`, below zero in a normal session). Numbers only.

THE END-OF-SESSION PASS. A tracking session ends after SESSION_IDLE_S of
quiet (the feed thread exits, or the next turn finds it stale) or when
the diariser URL changes. Before it's closed, on the feed thread and
after any turn in hand has its answer, every session voice is named once
more over every fingerprint the session collected, and fill_labels
writes the names onto the session's turns, the last turn included.
Only a turn whose name changes is written. A turn whose voice ends the
session unnamed keeps the label it has: the pass writes names and never
takes one off, so a doubt at the end can't erase a name a turn earned
on its own evidence. A session dropped because the diariser failed gets
no pass, and its turns keep their labels. One content-free row records
the pass: why the session ended, its turns, every voice's final state,
which voices' names changed and how many turns were relabelled.

THE RULES, pinned in tests/test_voice_sessions.py:

  * The diariser URL must name this machine, or the feed stays off.
    Redirects are not followed and proxy environment variables are
    ignored, so the audio can't be sent anywhere else.
  * Nothing here runs on the event loop, and a failure never reaches the
    relay or the pass: a turn with no answer is named on its own.
  * Clips added to a bank after the tracking session opened are left out
    of the multi scorer's comparison, so a voice is never scored against
    audio from the same session that the pass saved.
  * Content-free rows and logs: ids, slots, names, scores, seconds and
    timings. The turn's audio lives in memory for the pass, and the
    diariser keeps the session's audio in memory only.

FILLING IN, rules pinned in the same test file:

  * Only a turn with no label is filled, or one whose label this module
    or the pass wrote (source "session"). A turn a person corrected or
    confirmed, a crosstalk turn and a label that doesn't parse are never
    touched, and neither is the turn being named right now: the pass
    labels that one, and it may hold two voices. The end-of-session pass
    fills the session's last turn too.
  * A turn that already carries the name as a sure label isn't written
    again, so only a change reaches the chat.
  * Nothing else happens: no seat, no saved clip, no ask.
  * The owner's own name (or a spelling of it) is written with the owner
    marker, as the pass writes it.
"""

import collections
import ipaddress
import json
import logging
import math
import os
import queue
import threading
import time
from pathlib import Path
from urllib.parse import urlsplit

from . import db, voiceid

log = logging.getLogger("crossband.voice_sessions")

SESSION_IDLE_S = 600.0          # a quieter chat gets a fresh session
MIN_SPAN_S = 0.8                # shorter spans aren't fingerprinted
LISTEN_MIN_S = 1.5              # a voice under this much clean speech listens
NEW_VOICE_MIN_S = 4.0           # clean speech before a voice can be "new"
REQUEST_TIMEOUT_S = 5.0
SAMPLE_RATE = 16000
MAX_SPANS = 64                  # spans read per turn; the rest are ignored
# The rows file keeps the name it had while the session naming ran in
# shadow, so the rows written then stay readable.
ROWS_FILE = "voice_session_shadow.jsonl"
ROWS_MAX = 5000
ROWS_KEEP = 4000
ROW_VERSION = 1
SESSION_SOURCE = "session"      # the label source the pass and filling write
TURNS_KEPT = 400                # turns per session a late name can fill
LIVE_WAIT_S = 0.8               # longest the pass waits for a name
FEED_CHUNK_BYTES = int(0.25 * 16000) * 2   # audio pushed at a time
FEED_QUEUE_MAX = 4000           # chunks held before new ones are dropped
RESULTS_MAX = 256               # turns whose result is remembered
MAX_PIECES = 8                  # pieces of one long turn joined, newest kept
# THE SPAN CHECK's bar, in cosine units. Strict on purpose: the tracker is
# right about 99 times in 100, and a wrong move feeds one person's speech
# into another's evidence.
SPAN_MOVE_MIN_S = 6.0           # clean speech behind a voice a span joins
SPAN_MOVE_SIM = 0.7             # the span's score against that voice
SPAN_MOVE_MARGIN = 0.25         # its lead over every other voice
# The multi scorer (#477): a person's score is the mean of their best
# MULTI_TOP_K clip scores, so one lucky clip can't carry a name alone. A
# bank with fewer clips uses what it has.
MULTI_TOP_K = 2
MIN_IMPOSTOR_SCORES = 4
SPREAD_FLOOR = 0.02             # a near-zero spread would blow z up
CLIP_CACHE_MAX = 1024           # per-clip embeddings kept for the scorer
ANCHOR_CACHE_MAX = 64           # per-person centroids kept for the bar

_lock = threading.Lock()        # guards _sessions, _stats and the caches
_rows_lock = threading.Lock()
_sessions: dict = {}            # chat_id -> session dict
_stats = {"rows": 0, "diariser": "", "sessions_opened": 0}
_clip_cache: dict = {}          # (clip file, bytes) -> TitaNet-Small embedding
_anchor_cache: dict = {}        # (pid, fingerprint) -> {"emb", "clips"}
_warned: set = set()


# ================= settings =================================================

def _is_loopback_host(host) -> bool:
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
    """The configured diariser base URL (`diarize_shadow_url`, named for
    the stage that introduced it), or None: unset, or refused as not
    loopback (logged once)."""
    raw = ((cfg or {}).get("diarize_shadow_url") or "").strip()
    if not raw:
        return None
    url = loopback_base_url(raw)
    if url is None:
        _warn_once("url", "diarize_shadow_url refused: it must be an http "
                          "URL on this machine (127.0.0.1, ::1 or "
                          "localhost); each voice turn is named on its own")
    return url


def enabled(cfg) -> bool:
    """Does the feed run? Only with the matcher on and a loopback
    diariser configured."""
    return bool(voiceid.enabled(cfg) and diariser_url(cfg))


# ================= log once =================================================

def _warn_once(key, msg, *args):
    if key in _warned:
        return
    _warned.add(key)
    log.warning("voice sessions: " + msg, *args)


def _recovered(key, msg):
    if key in _warned:
        _warned.discard(key)
        log.info("voice sessions: %s", msg)


# ================= the live feed ============================================
# The relay hands every audio chunk inside a turn to feed() as it arrives,
# and end_turn() at the commit. A per-chat feed thread pushes the chunks to
# the tracking session in quarter-second pieces, so when the turn ends only
# the end-turn flush and the naming are left to do (about a tenth of a
# second). The pass asks await_turn() for the turn's main voice, for at
# most LIVE_WAIT_S, and names the turn on its own when nothing comes.

_feeds: dict = {}               # chat_id -> _Feed
_results: "collections.OrderedDict" = collections.OrderedDict()
# turn id -> {"after": the piece before it, "verdict", "seconds"}
_pieces: "collections.OrderedDict" = collections.OrderedDict()


def _turn_key(turn_id):
    return str(turn_id or "")[:64]


def _result_slot(turn_id):
    with _lock:
        slot = _results.get(turn_id)
        if slot is None:
            slot = {"event": threading.Event(), "result": None}
            _results[turn_id] = slot
            while len(_results) > RESULTS_MAX:
                _results.popitem(last=False)
        return slot


def _resolve(turn_id, result):
    if not turn_id:
        return
    slot = _result_slot(turn_id)
    slot["result"] = result
    slot["event"].set()


def wait_turn(turn_id, timeout=LIVE_WAIT_S):
    """The result for one turn: the main voice's {"voice", "state", "name",
    "score", ...}, plus every voice heard in the turn and the spans in turn
    time for the crosstalk split (see _name_turn), or None when there is
    none within `timeout` (blocking; call it off the event loop)."""
    tid = str(turn_id or "")[:64]
    if not tid:
        return None
    with _lock:
        slot = _results.get(tid)
    if slot is None:
        return None
    slot["event"].wait(timeout)
    return slot["result"]


def peek_turn(turn_id):
    """(known, done, result) for one turn without waiting: known is False
    when no slot was opened for it (no feed saw the turn)."""
    tid = str(turn_id or "")[:64]
    with _lock:
        slot = _results.get(tid) if tid else None
    if slot is None:
        return False, False, None
    return True, slot["event"].is_set(), slot["result"]


async def await_turn(turn_id, timeout=LIVE_WAIT_S, step=0.02):
    """wait_turn for the event loop: polls every `step` seconds instead of
    holding a thread, so the pass never borrows a worker to wait."""
    import asyncio
    deadline = time.monotonic() + timeout
    while True:
        known, done, result = peek_turn(turn_id)
        if not known:
            return None
        if done:
            return result
        if time.monotonic() >= deadline:
            return None
        await asyncio.sleep(step)


def feed(chat_id, pcm, sample_rate, cfg):
    """One audio chunk from inside a turn (the relay, on the event loop):
    queued for the chat's feed thread, never blocking."""
    if not chat_id or not pcm or sample_rate != SAMPLE_RATE \
            or not enabled(cfg):
        return
    _feed_for(chat_id, cfg).put(("audio", bytes(pcm)))


def end_turn(chat_id, turn_id, cfg, after=None):
    """The relay's commit: the turn so far is done. Opens the turn's result
    slot at once, so the pass can wait on it. `after` is the piece before
    this one when a long turn was cut (LONG TURNS)."""
    tid = _turn_key(turn_id) or None
    note_piece(tid, after)
    if not chat_id or not enabled(cfg):
        return
    if tid:
        _result_slot(tid)
    with _lock:
        f = _feeds.get(chat_id)
    if f is None:
        _resolve(tid, None)
        return
    f.put(("end", (tid, _turn_key(after) or None)))


def note_piece(turn_id, after=None):
    """Record which piece a committed turn follows (the relay and the batch
    path, as each piece arrives). No `after` starts a turn of its own,
    and never unlinks a piece the relay already linked."""
    tid, prev = _turn_key(turn_id), _turn_key(after)
    if not tid:
        return
    with _lock:
        entry = _pieces.pop(tid, None) or {}
        entry["after"] = (prev if prev != tid else None) or entry.get("after")
        _pieces[tid] = entry
        while len(_pieces) > RESULTS_MAX:
            _pieces.popitem(last=False)


def pieces_before(turn_id):
    """The ids of the pieces before this one in its long turn, oldest
    first, at most MAX_PIECES - 1 of them. [] for a turn of one piece."""
    out, seen = [], {_turn_key(turn_id)}
    with _lock:
        prev = (_pieces.get(_turn_key(turn_id)) or {}).get("after")
        while prev and prev not in seen and len(out) < MAX_PIECES - 1:
            out.append(prev)
            seen.add(prev)
            prev = (_pieces.get(prev) or {}).get("after")
    return list(reversed(out))


_VERDICT_KEYS = ("state", "name", "pid", "score", "prob", "human")


def piece_voices(got, seconds):
    """Every voice one piece's answer heard in that piece, each with its
    naming, its seconds in the piece and the method, for joining pieces.
    [] with no answer."""
    if not got:
        return []
    voices = got.get("piece_voices") or got.get("voices")
    if not voices:
        voices = {got.get("voice", 0): dict(got, seconds=seconds)}
    return [dict({k: v.get(k) for k in _VERDICT_KEYS},
                 seconds=v.get("seconds") or 0.0, method=got.get("method"))
            for v in voices.values()]


def join_pieces(turn_id, got, seconds):
    """The pass's answer for one piece, joined with the pieces before it
    when the feed's answer doesn't already cover them all (LONG TURNS).
    Remembers what this piece heard for the pieces after it. Returns `got`
    unchanged for a turn of one piece."""
    tid = _turn_key(turn_id)
    if not tid:
        return got
    heard = piece_voices(got, seconds)
    with _lock:
        entry = _pieces.pop(tid, None) or {"after": None}
        entry.update(voices=heard, seconds=float(seconds or 0.0))
        _pieces[tid] = entry
        while len(_pieces) > RESULTS_MAX:
            _pieces.popitem(last=False)
    before = pieces_before(tid)
    if len(before) + 1 <= ((got or {}).get("pieces") or 1):
        return got
    with _lock:
        earlier = [(_pieces[t]["seconds"], _pieces[t]["voices"])
                   for t in before if "voices" in (_pieces.get(t) or {})]
    return join_verdicts(earlier + [(seconds, heard)], got)


def _feed_for(chat_id, cfg):
    with _lock:
        f = _feeds.get(chat_id)
        fresh = f is None or not f.alive
        if fresh:
            f = _Feed(chat_id, cfg)
            _feeds[chat_id] = f
            f.start()
    if fresh:
        # A voice chat is starting: make sure every saved voice is embedded
        # before its first turn ends. A no-op when the caches are warm.
        start_warm(cfg)
    return f


# ================= warming ==================================================
# The first turn after a restart took 5.4 s on 27 September, because every
# saved clip was embedded then, on the turn's clock, while the pass waited.
# start_warm() does that work at startup and when a voice chat begins, on
# its own thread, so the first turn finds the caches full.

WARM_READY_WAIT_S = 120.0       # how long a warm waits for the model to load
_warming = threading.Event()


def start_warm(cfg) -> bool:
    """Embed every remembered person's clips in the background, one warm at
    a time, while the matcher is on. True when a warm started."""
    if not voiceid.enabled(cfg) or _warming.is_set():
        return False
    _warming.set()
    threading.Thread(target=_warm, args=(dict(cfg),), daemon=True,
                     name="voice-session-warm").start()
    return True


def _warm(cfg):
    """The warm itself (its own thread): wait for the speaker model, then
    fill the clip cache the naming reads and the centroids its bar uses.
    It waits on the matcher's state and never starts the model loading
    itself: startup and the first voice check do that."""
    from . import diarize
    t0 = time.perf_counter()
    try:
        deadline = time.monotonic() + WARM_READY_WAIT_S
        while voiceid.matcher_status(cfg) != "ready":
            if time.monotonic() >= deadline:
                log.info("session naming warm: speaker model not ready")
                return
            time.sleep(0.25)
        candidates = diarize.remembered_candidates()
        people = bank(candidates, SAMPLE_RATE, cfg,
                      before=time.time() + 1.0,
                      embed_fn=lambda pcm: embed_live(pcm, SAMPLE_RATE, cfg))
        _bar(people, candidates, SAMPLE_RATE, cfg)
        log.info("session naming warm: people=%d clips=%d ms=%.0f",
                 len(people), sum(len(p["clips"]) for p in people.values()),
                 (time.perf_counter() - t0) * 1000)
    except Exception:
        log.debug("session naming warm failed", exc_info=True)
    finally:
        _warming.clear()


def embed_live(pcm, sample_rate, cfg):
    """TitaNet-Small on one clean span or clip, L2-normalised, or None
    while the matcher isn't ready. Tests replace this one function."""
    audio = voiceid._pcm_to_float(pcm)
    if audio is None or len(audio) == 0:
        return None
    ex = voiceid._get_extractor(cfg)
    if ex is None:
        return None
    return voiceid._embed(ex, audio, sample_rate)


def embed_eres_live(pcm, sample_rate, cfg):
    """ERes2Net on one clean span for the calibrated naming. None until
    the calibrated scorer has loaded ERes2Net. Tests replace this
    function."""
    from . import voice_calibration as vc
    audio = voiceid._pcm_to_float(pcm)
    if audio is None or len(audio) == 0:
        return None
    ex = vc._eres2net_extractor(cfg)
    if ex is None:
        return None
    try:
        with vc._eres_embed_lock:
            stream = ex.create_stream()
            stream.accept_waveform(sample_rate=sample_rate, waveform=audio)
            stream.input_finished()
            vec = list(ex.compute(stream))
        return voiceid.l2_normalize(vec) if vec else None
    except Exception:
        log.debug("ERes2Net embedding failed", exc_info=True)
        return None


def voice_of_turn(chat_id, turn_id):
    """The session voice that spoke most of a turn in the chat's open
    session, or None: no session, or the turn isn't one of its turns."""
    tid = str(turn_id or "")[:64]
    if not tid:
        return None
    with _lock:
        sess = _sessions.get(chat_id)
        pairs = list(sess.get("turn_voice") or ()) if sess else []
    return next((s for t, s in reversed(pairs) if t == tid), None)


def human_named(chat_id, turn_id, name, person_id, cfg):
    """A person named a turn, by tapping it or answering the ask out loud:
    the session voice that spoke it is that person from now on, and its
    other turns in the session that carry no name or a session name take
    it at once. True when a session voice took the name."""
    tid = str(turn_id or "")[:64]
    if not tid or not person_id:
        return False
    with _lock:
        sess = _sessions.get(chat_id)
    if not sess:
        return False
    slot = voice_of_turn(chat_id, tid)
    voice = sess["voices"].get(slot) if slot is not None else None
    if voice is None:
        return False
    for other in sess["voices"].values():
        if (other.get("human") or {}).get("pid") == person_id:
            other.pop("human", None)
    voice["human"] = {"name": name, "pid": person_id}
    fill_labels(chat_id, sess, {slot: {"state": "named", "name": name}}, cfg)
    return True


def take_bank_allowance(chat_id, voice, limit):
    """One of `limit` clips a session voice may save per session: True and
    counted, or False once they're used, or with no session."""
    with _lock:
        sess = _sessions.get(chat_id)
        if not sess:
            return False
        used = sess.setdefault("banked", {})
        if used.get(voice, 0) >= limit:
            return False
        used[voice] = used.get(voice, 0) + 1
        return True


def name_single_turn(chat_id, pcm, sample_rate, cfg):
    """No tracker saw this turn (no diariser configured, the diariser is
    down, or the backup transcript path carried it): treat the whole turn
    as one voice with no session behind it, and name it with the same
    scorer. Returns the same shape as a feed's result, or None when the
    audio isn't speech or the speaker model isn't ready."""
    if not pcm or gate(pcm, sample_rate):
        return None
    secs = len(pcm) / 2 / sample_rate
    emb = embed_live(pcm, sample_rate, cfg)
    if emb is None:
        return None
    voice = {"prints": [(emb, secs)], "clean_s": secs}
    emb_e = embed_eres_live(pcm, sample_rate, cfg)
    if emb_e is not None:
        voice["prints_eres"] = [(emb_e, secs)]
    solo = {"voices": {0: voice}, "opened_at": time.time() + 1.0}
    named, _, _, method = _name_all(
        solo, _live_candidates(chat_id), sample_rate, cfg,
        lambda seg: embed_live(seg, sample_rate, cfg),
        (lambda seg: embed_eres_live(seg, sample_rate, cfg)))
    v = named.get(0) or {}
    whole = [{"slot": 0, "start": 0.0, "end": round(secs, 3),
              "overlap": False}]
    return {"voice": 0, "state": v.get("state", "listening"),
            "name": v.get("name", ""), "pid": v.get("pid", ""),
            "score": v.get("score"), "prob": v.get("prob"), "human": False,
            "method": method, "voice_clean_s": secs,
            "clean_spans": [(0.0, secs)], "voices_in_turn": 1,
            "overlap_s": 0.0, "single": True,
            "voices": turn_voices(whole, named), "spans": whole,
            "turn_s": round(secs, 3)}


def _live_candidates(chat_id):
    """The naming's candidates: every remembered person, with the people
    seated in this chat kept even while their bank is paused."""
    from . import diarize
    con = db.connect()
    try:
        seated = {r["person_id"] for r in db.get_room_roster(
            con, chat_id, present_only=True) if r["person_id"]}
    finally:
        con.close()
    return diarize.remembered_candidates(rostered_ids=frozenset(seated))


class _Feed:
    """One chat's feed thread. It owns the chat's tracking session, so
    nothing else pushes audio to it."""

    def __init__(self, chat_id, cfg):
        self.chat_id = chat_id
        self.cfg = dict(cfg)
        self.q = queue.Queue(maxsize=FEED_QUEUE_MAX)
        self.alive = True
        self.pending = bytearray()      # not yet pushed
        self.turn_pcm = bytearray()     # this turn's audio, pushed or not
        self.spans = []                 # this turn's spans so far
        self.turn_start = None          # session time the turn began at
        self.broken = False             # the session failed mid-turn
        self.early = {}                 # span key -> fingerprints so far
        # The long turn the last piece belongs to, for a next piece that
        # names it: {"session", "last", "pieces": [{"offset", "spans"}]}
        # with each piece's spans in its own turn time (LONG TURNS).
        self.chain = None
        self.ended = []                 # (session, why) awaiting their pass
        self.thread = threading.Thread(
            target=self._run, daemon=True,
            name=f"voice-session-feed-{chat_id}")

    def start(self):
        self.thread.start()

    def put(self, item):
        try:
            self.q.put_nowait(item)
        except queue.Full:
            with _lock:
                _stats["feed_dropped"] = _stats.get("feed_dropped", 0) + 1

    def _run(self):
        try:
            while True:
                try:
                    kind, value = self.q.get(timeout=SESSION_IDLE_S)
                except queue.Empty:
                    break
                if kind == "audio":
                    self.turn_pcm += value
                    self.pending += value
                    if len(self.pending) >= FEED_CHUNK_BYTES:
                        self._push()
                elif kind == "end":
                    self._end(value)
                    # After the turn has its answer, never before it.
                    self._finish_ended()
        finally:
            self.alive = False
            with _lock:
                if _feeds.get(self.chat_id) is self:
                    _feeds.pop(self.chat_id, None)
                sess = _sessions.pop(self.chat_id, None)
            if sess:
                self.ended.append((sess, "idle"))
            self._finish_ended()

    def _finish_ended(self):
        """THE END-OF-SESSION PASS for every session that ended, then its
        close (feed thread)."""
        while self.ended:
            sess, why = self.ended.pop(0)
            try:
                end_session(self.chat_id, sess, self.cfg, why)
            finally:
                _close(sess)

    def _session(self):
        base = diariser_url(self.cfg)
        if not base:
            raise _SessionError("no_diariser")
        return _session_for(self.chat_id, base, time.time(),
                            ended=self.ended)

    def _push(self):
        if not self.pending or self.broken:
            self.pending.clear()
            return
        try:
            sess = self._session()
            if self.turn_start is None:
                self.turn_start = sess["pushed_s"]
            chunk = bytes(self.pending)
            self.pending.clear()
            got = clean_spans(_call(
                "POST", f"{sess['base']}/sessions/{sess['id']}/audio",
                content=chunk))
            if got is None:
                raise _SessionError("bad_response")
            sess["pushed_s"] += len(chunk) / 2 / SAMPLE_RATE
            sess["last_at"] = time.time()
            self.spans += got
            self._fingerprint_early(got)
        except _SessionError as err:
            self._fail(err.reason)
        except Exception:
            log.debug("session feed push failed", exc_info=True)
            self._fail("error")

    def _fingerprint_early(self, spans):
        """Fingerprint each clean span as soon as the tracker calls it
        final, while the person is still talking, so the end of the turn
        only has to name. Anything that fails here is simply done again
        at the end."""
        if self.turn_start is None:
            return
        pcm = bytes(self.turn_pcm)
        for s in spans:
            if s["overlap"] or s["end"] - s["start"] < MIN_SPAN_S:
                continue
            try:
                a, b = s["start"] - self.turn_start, s["end"] - self.turn_start
                seg = span_pcm(pcm, SAMPLE_RATE, [(a, b)])
                if len(seg) < int((b - a) * SAMPLE_RATE) * 2 - 64 \
                        or gate(seg, SAMPLE_RATE):
                    continue
                emb = embed_live(seg, SAMPLE_RATE, self.cfg)
                if emb is None:
                    continue
                emb_e = embed_eres_live(seg, SAMPLE_RATE, self.cfg)
                self.early[_span_key(s)] = (emb, emb_e,
                                            len(seg) / 2 / SAMPLE_RATE)
            except Exception:
                log.debug("early fingerprint failed", exc_info=True)

    def _fail(self, reason):
        self.broken = True
        self.pending.clear()
        with _lock:
            _sessions.pop(self.chat_id, None)
            _stats["diariser"] = reason
        _warn_once("session", "the diariser's tracking session failed (%s); "
                              "each voice turn is named on its own until it "
                              "answers", reason)

    def _end(self, value):
        turn_id, after = value if isinstance(value, tuple) else (value, None)
        # This piece joins the long turn only when it names the last piece.
        chain = self.chain if after and self.chain \
            and self.chain["last"] == after else None
        self.chain = None
        t0 = time.perf_counter()
        pcm = bytes(self.turn_pcm)
        result = None
        row = {"v": ROW_VERSION, "at": round(time.time(), 3),
               "chat_id": self.chat_id, "turn_id": turn_id or "",
               "message_id": None,
               "seconds": round(len(pcm) / 2 / SAMPLE_RATE, 3)}
        try:
            if self.pending:
                self._push()
            if self.broken:
                raise _SessionError(_stats.get("diariser") or "error")
            if not pcm or self.turn_start is None:
                if chain and turn_id:   # no audio: the pieces carry on
                    self.chain = dict(chain, last=turn_id)
                return
            sess = self._session()
            if chain and chain["session"] != sess["id"]:
                chain = None
            got = clean_spans(_call(
                "POST", f"{sess['base']}/sessions/{sess['id']}/end-turn"))
            if got is None:
                raise _SessionError("bad_response")
            sess["last_at"] = time.time()
            sess["turns"] += 1
            earlier = chain["pieces"] if chain else []
            result = _name_turn(
                self.chat_id, sess, turn_id, pcm, SAMPLE_RATE,
                self.spans + got, self.turn_start, self.cfg,
                _live_candidates(self.chat_id),
                lambda seg: embed_live(seg, SAMPLE_RATE, self.cfg), row,
                eres_fn=lambda seg: embed_eres_live(seg, SAMPLE_RATE,
                                                    self.cfg),
                precomputed=dict(self.early), earlier=earlier)
            if turn_id:
                piece = {"offset": self.turn_start,
                         "spans": row.get("spans") or []}
                self.chain = {"session": sess["id"], "last": turn_id,
                              "pieces": (earlier + [piece])[
                                  -(MAX_PIECES - 1):]}
            with _lock:
                _stats["diariser"] = "ok"
            _recovered("session", "the tracking sessions answer again")
        except _SessionError as err:
            if not self.broken:
                self._fail(err.reason)
            row["error"] = err.reason
        except Exception:
            log.debug("session feed turn failed", exc_info=True)
            row["error"] = "error"
            with _lock:
                _sessions.pop(self.chat_id, None)
        finally:
            _resolve(turn_id, result)
            self.turn_pcm.clear()
            self.spans = []
            self.early = {}
            self.turn_start = None
            self.broken = False
        row["ms"] = round((time.perf_counter() - t0) * 1000, 1)
        try:
            write_row(row)
        except Exception:
            log.debug("session feed row failed", exc_info=True)


def status(cfg) -> dict:
    """Content-free state for the read route."""
    raw = bool((cfg or {}).get("diarize_shadow_url"))
    with _lock:
        return {"on": enabled(cfg),
                "diariser_refused": raw and not diariser_url(cfg),
                "feeds": len(_feeds), "open_sessions": len(_sessions),
                "sessions_opened": _stats["sessions_opened"],
                "rows_written": _stats["rows"],
                "diariser": _stats["diariser"]}


# ================= pure maths (unit-tested with synthetic vectors) ==========

def pooled(prints):
    """The length-weighted mean of a voice's fingerprints, L2-normalised, or
    None with none. `prints` is [(embedding, seconds)]."""
    total = sum(s for _, s in prints or ())
    if not prints or total <= 0:
        return None
    dim = len(prints[0][0])
    acc = [0.0] * dim
    for emb, secs in prints:
        for i, v in enumerate(emb):
            acc[i] += v * secs
    return voiceid.l2_normalize([v / total for v in acc])


def pool_of(voice, key="prints"):
    """A session voice's pooled fingerprint for one model ("prints" for
    TitaNet-Small, "prints_eres" for ERes2Net), kept on the voice until it
    gains a fingerprint, so a turn pools each voice once."""
    prints = voice.get(key) or []
    kept = voice.setdefault("pools", {})
    if key in kept and kept[key][0] == len(prints):
        return kept[key][1]
    pool = pooled(prints)
    kept[key] = (len(prints), pool)
    return pool


def span_home(voices, slot, emb, emb_e=None):
    """THE SPAN CHECK for one clean span the tracker gave to `slot`, before
    it's added: (the voice it plainly belongs to or None, the scores). The
    scores are {"sim": its score against the best other voice, "own": its
    score against its own voice, or None with no evidence there, "gap":
    how far that best voice leads every rival}, or {} when no other voice
    has any evidence. Pure: nothing is moved here."""
    sims = {}
    for other, v in voices.items():
        if (v.get("clean_s") or 0.0) <= 0.0:
            continue
        pool = pool_of(v)
        if pool is None:
            continue
        score = voiceid.cosine(emb, pool)
        pool_e = pool_of(v, "prints_eres") if emb_e is not None else None
        if pool_e is not None:
            score = (score + voiceid.cosine(emb_e, pool_e)) / 2
        sims[other] = score
    others = [(score, other) for other, score in sims.items()
              if other != slot]
    if not others:
        return None, {}
    best_sim, best = max(others, key=lambda so: (so[0], -so[1]))
    rivals = [score for other, score in sims.items() if other != best]
    gap = best_sim - max(rivals) if rivals else None
    scores = {"sim": round(best_sim, 3),
              "own": round(sims[slot], 3) if slot in sims else None,
              "gap": round(gap, 3) if gap is not None else None}
    if (voices[best].get("clean_s") or 0.0) < SPAN_MOVE_MIN_S \
            or best_sim < SPAN_MOVE_SIM \
            or (gap is not None and gap < SPAN_MOVE_MARGIN):
        return None, scores
    return best, scores


def topk_mean(query, clips, k=None):
    """A person's multi score: the mean of the query's best `k` cosines
    against that person's clip embeddings, or of all of them when there
    are fewer. None with no clips."""
    k = MULTI_TOP_K if k is None else k
    sims = sorted((voiceid.cosine(query, c) for c in clips or ()),
                  reverse=True)[:max(1, k)]
    return sum(sims) / len(sims) if sims else None


def _spread(scores):
    """Mean, spread (floored) and count of a list of impostor scores, or
    None with fewer than MIN_IMPOSTOR_SCORES of them."""
    if len(scores) < MIN_IMPOSTOR_SCORES:
        return None
    mean = sum(scores) / len(scores)
    spread = math.sqrt(sum((x - mean) ** 2 for x in scores) / len(scores))
    return {"mean": round(mean, 4), "std": round(max(spread, SPREAD_FLOOR), 4),
            "n": len(scores)}


def impostor_stats(anchors):
    """Mean and spread of the matcher's cross-speaker scores on this
    household: each person's clip embeddings against every OTHER person's
    centroid. None when there are fewer than MIN_IMPOSTOR_SCORES."""
    return _spread([voiceid.cosine(clip, other["emb"])
                    for pid, a in (anchors or {}).items()
                    for clip in a["clips"]
                    for opid, other in anchors.items() if opid != pid])


def multi_impostor_stats(multi):
    """Impostor statistics under the multi scorer's own rule: each person's
    clips scored against every OTHER person's clips by topk_mean. None
    with too few scores."""
    return _spread([topk_mean(clip, other["clips"])
                    for pid, a in (multi or {}).items() for clip in a["clips"]
                    for opid, other in multi.items()
                    if opid != pid and other["clips"]])


def multi_bar(cfg, small_stats, multi_stats):
    """The multi scorer's naming bar. The best of several clips scores
    higher than an average does, for strangers as much as for the right
    person, so the matcher's own threshold would name too much. The bar is
    the matcher's threshold carried onto the multi scale at the same z,
    measured against this household's impostors, with the margin scaled
    by the spread ratio. Without statistics it is the matcher's own bar,
    and `source` says so."""
    t = voiceid._threshold(cfg)
    m = voiceid._margin(cfg)
    if small_stats and multi_stats:
        ratio = multi_stats["std"] / small_stats["std"]
        z = (t - small_stats["mean"]) / small_stats["std"]
        return {"threshold": round(multi_stats["mean"]
                                   + z * multi_stats["std"], 4),
                "margin": round(m * ratio, 4), "stats": multi_stats,
                "source": "matched", "k": MULTI_TOP_K}
    return {"threshold": t, "margin": m, "stats": multi_stats,
            "source": "live", "k": MULTI_TOP_K}


def _human_first(voices, out, taken):
    """A voice a person named (by tapping a turn) is that person, whatever
    the scores say, and nobody else's voice can take the name."""
    for slot, v in voices.items():
        h = v.get("human")
        if h and h.get("pid") and slot in out:
            out[slot].update(state="named", name=h["name"], pid=h["pid"],
                             score=1.0, prob=1.0, human=True)
            taken.add(h["pid"])


def name_voices_calibrated(voices, allowed, snapshot):
    """Name every session voice on the calibrated two-model scorer: each
    voice's pooled TitaNet-Small and ERes2Net fingerprints, and its clean
    seconds, give a probability per allowed person (voice_calibration,
    with the prior of 1 in N+1). One person per voice, highest first:
    named at NAME_BAR or more with LISTEN_MIN_S of clean speech, new under
    NEW_BAR with NEW_VOICE_MIN_S, otherwise listening. `allowed` is
    {person_id: name}, the candidates the room may name."""
    from . import voice_calibration as vc
    out, probs = {}, {}
    for slot, v in voices.items():
        clean = v.get("clean_s") or 0.0
        entry = {"state": "listening", "name": "", "pid": "", "score": None,
                 "second": None, "prob": None, "clean_s": round(clean, 2)}
        ps, pe = pool_of(v), pool_of(v, "prints_eres")
        if ps is not None and pe is not None:
            got = vc.probability({vc.SMALL: ps, vc.ERES: pe}, clean,
                                 snapshot) or {}
            got = {pid: pr for pid, pr in got.items() if pid in allowed}
            probs[slot] = got
            ranked = sorted(got.values(), reverse=True)
            if ranked:
                entry["score"] = entry["prob"] = round(ranked[0], 4)
            if len(ranked) > 1:
                entry["second"] = round(ranked[1], 4)
        out[slot] = entry
    taken = set()
    _human_first(voices, out, taken)
    pairs = sorted(((pr, slot, pid) for slot, row in probs.items()
                    for pid, pr in row.items()), reverse=True)
    for pr, slot, pid in pairs:
        entry = out[slot]
        if entry["pid"] or pid in taken or pr < vc.NAME_BAR:
            continue
        if (voices[slot].get("clean_s") or 0.0) < LISTEN_MIN_S:
            continue
        entry.update(state="named", pid=pid, name=allowed[pid],
                     score=round(pr, 4), prob=round(pr, 4))
        taken.add(pid)
    for slot, entry in out.items():
        if entry["state"] == "named":
            continue
        best = max((probs.get(slot) or {}).values(), default=None)
        if (voices[slot].get("clean_s") or 0.0) >= NEW_VOICE_MIN_S \
                and (best is None or best < vc.NEW_BAR):
            entry["state"] = "new"
    return out


def name_voices(voices, people, bar):
    """Name every session voice from its pooled evidence, one person per
    voice, on the multi scorer. `voices` is {slot: {"prints": [(emb,
    secs)], "clean_s"}}, `people` is {pid: {"name", "clips": [emb]}},
    `bar` carries the multi scorer's "threshold" and "margin". Returns
    {slot: {"state", "name", "pid", "score", "second", "clean_s"}} where
    state is listening, named or new.

    One-to-one is greedy on score, highest first: exact for the two or
    three people a home room holds, and never gives one person two voices.
    A voice beaten to its best person stays listening, because its margin
    over that person fails, which is what the tracker splitting one person
    into two voices looks like."""
    threshold = bar.get("threshold", 0.5)
    margin = bar.get("margin", 0.0)
    scores = {}
    for slot, v in voices.items():
        pool = pool_of(v)
        if pool is None:
            continue
        scores[slot] = {pid: s for pid, s in (
            (pid, topk_mean(pool, p["clips"]))
            for pid, p in people.items()) if s is not None}
    out = {}
    for slot, v in voices.items():
        ranked = sorted((scores.get(slot) or {}).items(),
                        key=lambda kv: kv[1], reverse=True)
        out[slot] = {"state": "listening", "name": "", "pid": "",
                     "score": round(ranked[0][1], 4) if ranked else None,
                     "second": round(ranked[1][1], 4)
                     if len(ranked) > 1 else None,
                     "clean_s": round(v.get("clean_s") or 0.0, 2)}
    taken = set()
    _human_first(voices, out, taken)
    pairs = sorted(((s, slot, pid) for slot, row in scores.items()
                    for pid, s in row.items()), reverse=True)
    for score, slot, pid in pairs:
        entry = out[slot]
        if entry["pid"] or pid in taken:
            continue
        if (voices[slot].get("clean_s") or 0.0) < LISTEN_MIN_S:
            continue
        rivals = [s for p, s in scores[slot].items() if p != pid]
        runner = max(rivals) if rivals else None
        if score < threshold or (runner is not None
                                 and score - runner < margin):
            continue
        entry.update(state="named", pid=pid, name=people[pid]["name"],
                     score=round(score, 4))
        taken.add(pid)
    for slot, entry in out.items():
        if entry["state"] == "named":
            continue
        best = entry["score"]
        if (voices[slot].get("clean_s") or 0.0) >= NEW_VOICE_MIN_S \
                and (best is None or best < threshold):
            entry["state"] = "new"
    return out


def join_verdicts(pieces, got=None):
    """One long turn's answer from what its pieces heard, for pieces named
    on their own (LONG TURNS). `pieces` is [(seconds, voices)] oldest
    first, `voices` each piece's piece_voices. A voice named as someone
    counts for that person, and a voice found new counts for "new". A
    voice still listening counts for nobody, and so does a piece with no
    answer. Each person, and "new", is one voice of the answer with its
    seconds, and the main voice is the one with the most, its score the
    length-weighted mean of its pieces'. A second voice is listed as
    crosstalk lists any. With no voice counting, `got` comes back as it
    was. The joined answer saves no clip: its clean spans are empty."""
    groups, index, spans, t = [], {}, [], 0.0
    for secs, voices in pieces:
        secs = max(0.0, float(secs or 0.0))
        start, t = t, t + secs
        for v in voices or ():
            state = v.get("state")
            if state == "named" and (v.get("pid") or v.get("name")):
                key = ("named", v.get("pid") or v["name"].casefold())
            elif state == "new":
                key = ("new",)
            else:
                continue
            heard = min(secs, max(0.0, float(v.get("seconds") or 0.0)))
            if key not in index:
                index[key] = len(groups)
                groups.append({"state": state, "name": v.get("name") or "",
                               "pid": v.get("pid") or "", "human": False,
                               "method": v.get("method"), "seconds": 0.0,
                               "first": start, "sums": {}})
            g = groups[index[key]]
            g["seconds"] += heard
            g["human"] = g["human"] or bool(v.get("human"))
            for k in ("score", "prob"):
                if v.get(k) is not None:
                    total, weight = g["sums"].get(k, (0.0, 0.0))
                    g["sums"][k] = (total + v[k] * heard, weight + heard)
            spans.append({"slot": index[key], "start": round(start, 3),
                          "end": round(start + heard, 3), "overlap": False})
    if not groups:
        return got
    voices = {}
    for i, g in enumerate(groups):
        mean = {k: (round(total / weight, 4) if weight > 0 else None)
                for k, (total, weight) in g["sums"].items()}
        voices[i] = {"state": g["state"], "name": g["name"], "pid": g["pid"],
                     "score": mean.get("score"), "prob": mean.get("prob"),
                     "human": g["human"], "seconds": round(g["seconds"], 3),
                     "first": round(g["first"], 3)}
    main = max(voices, key=lambda i: (voices[i]["seconds"], -i))
    m = voices[main]
    return {"voice": main, "state": m["state"], "name": m["name"],
            "pid": m["pid"], "score": m["score"], "prob": m["prob"],
            "human": m["human"],
            "method": groups[main]["method"] or (got or {}).get("method"),
            "voice_clean_s": m["seconds"], "clean_spans": [],
            "voices_in_turn": len(voices), "overlap_s": 0.0, "single": True,
            "voices": voices, "spans": spans, "turn_s": round(t, 3),
            "pieces": len(pieces)}


def clean_spans(payload, limit=MAX_SPANS):
    """The diariser's spans as checked dicts, or None when the answer isn't
    the documented shape. Accepts {"spans": [...]} or a bare list."""
    raw = payload.get("spans") if isinstance(payload, dict) else payload
    if not isinstance(raw, list):
        return None
    out = []
    for s in raw[:limit]:
        try:
            start, end = float(s["start"]), float(s["end"])
            slot = int(s["slot"])
        except (TypeError, ValueError, KeyError):
            return None
        if end > start:
            out.append({"slot": slot, "start": start, "end": end,
                        "overlap": bool(s.get("overlap"))})
    return out


def _span_key(s):
    """A span's identity in session time, for matching a fingerprint taken
    during the turn to the same span at its end."""
    return (s["slot"], round(s["start"], 3), round(s["end"], 3))


def main_voice(spans):
    """The slot with the most time alone in the turn, else the most time
    at all, or None with no spans."""
    alone = collections.Counter()
    total = collections.Counter()
    for s in spans:
        d = s["end"] - s["start"]
        total[s["slot"]] += d
        if not s["overlap"]:
            alone[s["slot"]] += d
    pick = alone or total
    return pick.most_common(1)[0][0] if pick else None


def span_pcm(pcm, sample_rate, spans):
    """The PCM-16 bytes of the given (start, end) second spans, joined."""
    out = bytearray()
    total = len(pcm) // 2
    for s, e in spans:
        a = max(0, min(total, int(round(s * sample_rate))))
        b = max(0, min(total, int(round(e * sample_rate))))
        out += pcm[a * 2:b * 2]
    return bytes(out)


def gate(pcm, sample_rate):
    """The matcher's audio gates, in its order: None when the audio may be
    judged, else the reason it may not (too_short or not_speech)."""
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


# ================= the diariser calls =======================================

def _request(method, url, content=None):
    """One loopback call. No redirects, no proxies, a short timeout."""
    import httpx
    with httpx.Client(trust_env=False, follow_redirects=False,
                      timeout=REQUEST_TIMEOUT_S) as client:
        resp = client.request(
            method, url, content=content,
            headers={"Content-Type": "application/octet-stream"}
            if content is not None else None)
    resp.raise_for_status()
    return resp.json() if resp.content else {}


class _SessionError(Exception):
    def __init__(self, reason):
        super().__init__(reason)
        self.reason = reason


def _call(method, url, content=None):
    import httpx
    try:
        return _request(method, url, content)
    except httpx.TimeoutException:
        raise _SessionError("timeout")
    except httpx.HTTPStatusError as exc:
        raise _SessionError(f"http_{exc.response.status_code}")
    except httpx.HTTPError:
        raise _SessionError("unreachable")
    except ValueError:
        raise _SessionError("bad_response")


def _close(sess):
    try:
        _call("DELETE", f"{sess['base']}/sessions/{sess['id']}")
    except Exception:
        pass            # the diariser expires idle sessions itself


def _session_for(chat_id, base, now, ended=None):
    """This chat's open tracking session, opening one when there is none,
    it went quiet for SESSION_IDLE_S, or the diariser URL changed. A
    session that ended is added to `ended` as (session, why) for the
    feed to finish (THE END-OF-SESSION PASS), or closed at once with no
    list."""
    with _lock:
        sess = _sessions.get(chat_id)
    if sess and (now - sess["last_at"] > SESSION_IDLE_S
                 or sess["base"] != base):
        if ended is None:
            _close(sess)
        else:
            ended.append((sess, "idle" if sess["base"] == base
                          else "diariser_changed"))
        sess = None
    if sess is None:
        payload = _call("POST", f"{base}/sessions")
        sid = payload.get("session") if isinstance(payload, dict) else None
        if not isinstance(sid, (str, int)) or str(sid) == "":
            raise _SessionError("bad_response")
        sess = {"id": str(sid), "base": base, "opened_at": now,
                "last_at": now, "pushed_s": 0.0, "voices": {}, "turns": 0,
                "turn_voice": [], "filled": {}}
        with _lock:
            _sessions[chat_id] = sess
            _stats["sessions_opened"] += 1
    return sess


# ================= the banks, as they stood when the session opened =========

def bank(candidates, sample_rate, cfg, before, embed_fn=None):
    """{pid: {"name", "clips"}} for the multi scorer: every kept clip of
    each candidate (anchors.enrollment_clips with no limit, so the same
    gates as the matcher), embedded one by one, but without any clip added
    at or after `before` (epoch seconds), so audio the pass saved during
    this session never scores this session's voices. Each clip's embedding
    is cached on its file and length, so a bank that gains a clip embeds
    only the new one."""
    from . import anchors as anchor_store
    ids = [c["person_id"] for c in candidates or () if c.get("person_id")]
    if not ids:
        return {}
    embed_fn = embed_fn or (lambda pcm: embed_live(pcm, sample_rate, cfg))
    names = {c["person_id"]: c.get("name") for c in candidates}
    store = anchor_store.store()
    clips = store.enrollment_clips(ids, sample_rate, max_clips=None)
    out = {}
    for pid, info in clips.items():
        added = {c["file"]: c.get("added_at") or 0
                 for c in store.clips_of(pid) or ()}
        pcms = info["pcms"]
        # The fingerprint ends with the clip files, in the order of pcms.
        files = tuple(info["fingerprint"])[-len(pcms):] if pcms else ()
        embs = []
        for fname, pcm in zip(files, pcms):
            if added.get(fname, 0) >= before:
                continue
            key = (fname, len(pcm))
            with _lock:
                emb = _clip_cache.get(key)
            if emb is None:
                emb = embed_fn(pcm)
                if emb is None:
                    continue
                with _lock:
                    _clip_cache[key] = emb
                    while len(_clip_cache) > CLIP_CACHE_MAX:
                        _clip_cache.pop(next(iter(_clip_cache)))
            embs.append(emb)
        if embs:
            out[pid] = {"name": names.get(pid) or info["name"], "clips": embs}
    return out


def build_anchors(candidates, sample_rate, cfg, embed_fn=None):
    """{pid: {"name", "emb", "clips"}}: each candidate's enrolment clips
    (the matcher's own, anchors.enrollment_clips) embedded with
    TitaNet-Small, and their centroid. Cached in memory per person and
    kept clip set, so a bank change re-embeds that person and nothing is
    ever written to the store."""
    from . import anchors as anchor_store
    ids = [c["person_id"] for c in candidates or () if c.get("person_id")]
    if not ids:
        return {}
    embed_fn = embed_fn or (lambda pcm: embed_live(pcm, sample_rate, cfg))
    names = {c["person_id"]: c.get("name") for c in candidates}
    clips = anchor_store.store().enrollment_clips(ids, sample_rate)
    out = {}
    for pid, info in clips.items():
        key = (pid, tuple(info["fingerprint"]))
        with _lock:
            entry = _anchor_cache.get(key)
        if entry is None:
            embs = [e for e in (embed_fn(pcm) for pcm in info["pcms"]) if e]
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


def _bar(people, candidates, sample_rate, cfg):
    """The multi scorer's bar (multi_bar), from this household's impostor
    statistics under both the matcher's rule and the multi rule."""
    small = build_anchors(candidates, sample_rate, cfg)
    return multi_bar(cfg, impostor_stats(small),
                     multi_impostor_stats(people))


# ================= one turn =================================================

def _name_turn(chat_id, sess, turn_id, pcm, sample_rate, raw_spans, offset,
               cfg, candidates, embed_fn, row, eres_fn=None,
               precomputed=None, earlier=()):
    """The naming step for one turn of the feed: move the tracker's spans
    into turn time, fingerprint the clean ones, name every session voice,
    fill in unnamed turns, and put it all on `row`. Returns the turn's
    main voice as {"voice", "state", "name", "score", ...} with every
    voice heard in the turn ("voices"), the spans in turn time and the
    turn's length ("turn_s"), or None when no voice spoke.

    `earlier` holds the long turn's pieces before this one, each
    {"offset", "spans"} (LONG TURNS). The answer then covers every piece:
    its spans and length run from the first piece's start, and "pieces"
    counts them. Only this piece's audio is fingerprinted, and its clean
    spans (for saving a clip) stay in this piece's own time."""
    seconds = len(pcm) / 2 / sample_rate
    spans, ready = [], {}
    for s in raw_spans:
        a = max(0.0, s["start"] - offset)
        b = min(seconds, s["end"] - offset)
        if b > a:
            spans.append({**s, "start": round(a, 3), "end": round(b, 3)})
            got = (precomputed or {}).get(_span_key(s))
            if got is not None:
                ready[(s["slot"], round(a, 3), round(b, 3))] = got
    embedded, moved, leads = 0, [], []
    for s in spans:
        # Every voice heard is on the table, even one heard only over
        # someone else: it listens with no evidence.
        sess["voices"].setdefault(s["slot"], {"prints": [], "clean_s": 0.0})
        if s["overlap"] or s["end"] - s["start"] < MIN_SPAN_S:
            continue
        early = ready.get((s["slot"], s["start"], s["end"]))
        if early is not None:
            # Fingerprinted while the person was still talking.
            emb, emb_e, secs = early
        else:
            seg = span_pcm(pcm, sample_rate, [(s["start"], s["end"])])
            if gate(seg, sample_rate):
                continue
            emb = embed_fn(seg)
            secs = len(seg) / 2 / sample_rate
            emb_e = eres_fn(seg) if (eres_fn is not None
                                     and emb is not None) else None
        if emb is None:
            continue
        home, scores = span_home(sess["voices"], s["slot"], emb, emb_e)
        if home is None and scores.get("own") is not None:
            leads.append(round(scores["sim"] - scores["own"], 3))
        if home is not None:
            # THE SPAN CHECK: the span is that voice's, for its evidence
            # and for the turn's spans.
            moved.append({"start": s["start"], "end": s["end"],
                          "from": s["slot"], "to": home, **scores})
            s["slot"] = home
        v = sess["voices"][s["slot"]]
        v["prints"].append((emb, secs))
        v["clean_s"] += secs
        if emb_e is not None:
            v.setdefault("prints_eres", []).append((emb_e, secs))
        embedded += 1
    named, bar, n_people, method = _name_all(
        sess, candidates, sample_rate, cfg, embed_fn, eres_fn)
    sess["named"] = named               # for the end-of-session pass
    # The whole turn: every piece's spans, end to end, in the first
    # piece's time. A turn of one piece is just this piece.
    base = earlier[0]["offset"] if earlier else offset
    whole = [{**s, "start": round(s["start"] + p["offset"] - base, 3),
              "end": round(s["end"] + p["offset"] - base, 3)}
             for p in list(earlier) + [{"offset": offset, "spans": spans}]
             for s in p["spans"]]
    main = main_voice(whole)
    if main is not None and turn_id:
        sess["turn_voice"].append((str(turn_id), main))
        del sess["turn_voice"][:-TURNS_KEPT]
    # The turn being named right now is the pass's to label: it may hold
    # two voices, and its message may already be saved.
    filled = fill_labels(chat_id, sess, named, cfg, skip=str(turn_id or ""))
    row.update(
        session=sess["id"], turn=sess["turns"], offset=round(offset, 3),
        spans=spans, main=main,
        main_state=(named.get(main) or {}).get("state", "listening")
        if main is not None else "",
        main_name=(named.get(main) or {}).get("name", "")
        if main is not None else "",
        voices={str(k): v for k, v in sorted(named.items())},
        bar={k: bar.get(k) for k in ("threshold", "margin", "source")},
        embedded=embedded, people=n_people, method=method, filled=filled)
    if earlier:
        row["pieces"] = len(earlier) + 1
    if moved:
        row["moved"] = moved
    if leads:
        row["span_lead"] = max(leads)
    if main is None:
        return None
    voice = named.get(main) or {}
    clean = [(s["start"], s["end"]) for s in spans
             if s["slot"] == main and not s["overlap"]
             and s["end"] - s["start"] >= MIN_SPAN_S]
    return {"voice": main, "state": voice.get("state", "listening"),
            "name": voice.get("name", ""), "pid": voice.get("pid", ""),
            "score": voice.get("score"), "prob": voice.get("prob"),
            "human": bool(voice.get("human")), "method": method,
            "voice_clean_s": voice.get("clean_s") or 0.0,
            "clean_spans": clean,
            "voices_in_turn": len({s["slot"] for s in whole}),
            "overlap_s": round(sum(s["end"] - s["start"] for s in whole
                                   if s["overlap"]), 3),
            # What the crosstalk split reads (#482 item D). Every voice
            # heard in the turn with its name and seconds, the spans in
            # turn time, and how much audio the turn held (its clock, for
            # lining up Scribe's word times). Content-free, like the rows.
            "voices": turn_voices(whole, named),
            "spans": [dict(s) for s in whole],
            "turn_s": round(offset + seconds - base, 3),
            "pieces": len(earlier) + 1,
            # What this piece alone heard, for joining it with pieces the
            # feed didn't answer (join_pieces).
            "piece_voices": turn_voices(spans, named)}


def turn_voices(spans, named):
    """{slot: {"state", "name", "pid", "score", "prob", "human", "seconds",
    "first"}} for every voice heard in one turn's spans (turn time):
    `seconds` is its time in the turn, overlap included, and `first` when
    it first spoke."""
    out = {}
    for s in spans:
        v = named.get(s["slot"]) or {}
        entry = out.setdefault(s["slot"], {
            "state": v.get("state", "listening"), "name": v.get("name", ""),
            "pid": v.get("pid", ""), "score": v.get("score"),
            "prob": v.get("prob"), "human": bool(v.get("human")),
            "seconds": 0.0, "first": s["start"]})
        entry["seconds"] = round(entry["seconds"] + s["end"] - s["start"], 3)
        entry["first"] = min(entry["first"], s["start"])
    return out


def _name_all(sess, candidates, sample_rate, cfg, embed_fn, eres_fn):
    """Name every session voice: the calibrated two-model scorer when its
    snapshot is ready and the voices carry ERes2Net fingerprints, else the
    multi scorer on the matcher's bar. Returns (named, bar, people,
    method)."""
    from . import voice_calibration as vc
    snap = vc.current() if eres_fn is not None else None
    allowed = {c["person_id"]: c["name"] for c in candidates or ()
               if c.get("person_id")}
    if snap and snap.get("calibrated") and allowed:
        named = name_voices_calibrated(sess["voices"], allowed, snap)
        return (named, {"threshold": vc.NAME_BAR, "margin": None,
                        "source": "calibrated"}, len(allowed), "calibrated")
    # A second's grace: a clip saved by the pass as the session opened
    # carries a timestamp just after the session's own.
    people = bank(candidates, sample_rate, cfg,
                  before=sess["opened_at"] - 1.0, embed_fn=embed_fn)
    bar = _bar(people, candidates, sample_rate, cfg)
    return name_voices(sess["voices"], people, bar), bar, len(people), "multi"


# ================= filling in unnamed turns ==================================

def _labels_of(raw):
    """A message's voice labels as a dict, {} when it has none, None when
    they don't parse (left alone)."""
    if not raw:
        return {}
    try:
        data = json.loads(raw)
    except (TypeError, ValueError):
        return None
    return data if isinstance(data, dict) else None


def fillable(old):
    """May a turn with these labels take a session name? Only when it has
    no name, or the name there is one this module or the pass wrote."""
    if old is None or old.get("corrected") or old.get("crosstalk"):
        return False
    names = [n for n in old.get("labels") or () if isinstance(n, str)
             and n.strip()]
    return not names or old.get("source") == SESSION_SOURCE


def fill_labels(chat_id, sess, named, cfg, skip=""):
    """Write each named session voice's name onto its unnamed turns in this
    session (see FILLING IN above), all but the turn `skip`. Returns how
    many labels it wrote."""
    from . import diarize, introductions
    owner = ((cfg or {}).get("user_name") or "").strip()
    done = sess.setdefault("filled", {})
    count = 0
    con = db.connect()
    try:
        for turn_id, slot in sess.get("turn_voice") or ():
            voice = named.get(slot) or {}
            name = voice.get("name")
            if voice.get("state") != "named" or not name \
                    or done.get(turn_id) == name or turn_id == skip:
                continue
            msg = db.get_message_by_voice_turn(con, chat_id, turn_id)
            old = _labels_of(msg["voice_labels"]) if msg else None
            if not msg or not fillable(old):
                continue
            if old.get("labels") == [name] and not (
                    old.get("uncertain") or old.get("learning")
                    or old.get("unresolved")):
                done[turn_id] = name        # already says so
                continue
            is_owner = bool(owner) and introductions.owner_alias(name, owner)
            db.set_message_voice_labels(con, msg["id"], diarize.label_payload(
                [name], clusters=(SESSION_SOURCE,), source=SESSION_SOURCE,
                score=voice.get("score") or 0.0, owner=is_owner))
            done[turn_id] = name
            count += 1
    finally:
        con.close()
    return count


def end_session(chat_id, sess, cfg, why):
    """THE END-OF-SESSION PASS (the feed thread, before the session is
    closed): name every session voice once more over every fingerprint
    the session collected, and relabel each turn whose name changed,
    through fill_labels' rules with no turn skipped. Writes one
    content-free row and returns it, or None for a session with no turns
    to label. Never raises."""
    if not sess or not sess.get("turn_voice"):
        return None
    t0 = time.perf_counter()
    row = {"v": ROW_VERSION, "at": round(time.time(), 3), "chat_id": chat_id,
           "turn_id": "", "message_id": None, "session": sess.get("id"),
           "end": why, "turns": sess.get("turns", 0)}
    try:
        named, bar, n_people, method = _name_all(
            sess, _live_candidates(chat_id), SAMPLE_RATE, cfg,
            lambda seg: embed_live(seg, SAMPLE_RATE, cfg),
            lambda seg: embed_eres_live(seg, SAMPLE_RATE, cfg))
        last = sess.get("named") or {}
        row.update(
            voices={str(k): v for k, v in sorted(named.items())},
            renamed=sorted(slot for slot, v in named.items()
                           if v.get("name") != (last.get(slot) or {})
                           .get("name")),
            method=method, people=n_people,
            filled=fill_labels(chat_id, sess, named, cfg))
        sess["named"] = named
    except Exception:
        log.debug("end-of-session pass failed", exc_info=True)
        row["error"] = "error"
    row["ms"] = round((time.perf_counter() - t0) * 1000, 1)
    try:
        write_row(row)
    except Exception:
        log.debug("end-of-session row failed", exc_info=True)
    return row


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
        with _lock:
            _stats["rows"] += 1
            trim = _stats["rows"] % 200 == 0
        if trim:
            with open(path) as f:
                lines = f.readlines()
            if len(lines) > ROWS_MAX:
                tmp = path.with_name(path.name + ".tmp")
                fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC,
                             0o600)
                with os.fdopen(fd, "w") as f:
                    f.writelines(lines[-ROWS_KEEP:])
                os.replace(tmp, path)


def _message_id(chat_id, turn_id):
    """The user message a turn became, found by its voice turn id, so a
    row can be joined to the chat. None when there is no id or the row
    isn't there."""
    if not turn_id:
        return None
    con = db.connect()
    try:
        row = db.get_message_by_voice_turn(con, chat_id, turn_id)
        return row["id"] if row else None
    finally:
        con.close()


def read_rows(limit=200, chat_id=None) -> list:
    """The newest rows first, at most `limit`, optionally for one chat. A
    row is written before its message is saved, so its message id is
    filled in from the turn id now."""
    keep = collections.deque(maxlen=max(1, int(limit)))
    try:
        with _rows_lock, open(rows_path()) as f:
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


def view(rows) -> dict:
    """Per turn, its voice's name at the time and at the end of its
    session, plus a tally. Rows newest first."""
    final = {}
    for row in rows:                    # newest first: first seen is final
        if row.get("session") and row["session"] not in final:
            final[row["session"]] = row.get("voices") or {}
    lines, tally = [], collections.Counter()
    for row in rows:
        if row.get("end"):
            # THE END-OF-SESSION PASS, not a turn: its naming is the
            # session's final one above.
            tally["sessions_ended"] += 1
            tally["relabelled_at_end"] += row.get("filled") or 0
            continue
        if row.get("error"):
            tally["errors"] += 1
            continue
        main = row.get("main")
        at_end = (final.get(row.get("session")) or {}).get(str(main)) or {}
        then = row.get("main_name") or ""
        line = {"turn_id": row.get("turn_id"),
                "message_id": row.get("message_id"),
                "seconds": row.get("seconds"),
                "then": then or row.get("main_state") or "",
                "at_end": at_end.get("name") or at_end.get("state", ""),
                "voice": main, "method": row.get("method", "")}
        lines.append(line)
        tally["turns"] += 1
        tally["named_then"] += bool(then)
        tally["named_at_end"] += bool(at_end.get("name"))
        if then and at_end.get("name") and at_end["name"] != then:
            tally["renamed_by_end"] += 1
    return {"tally": dict(tally), "lines": lines}


def _reset_for_tests():
    with _lock:
        for f in _feeds.values():
            f.alive = False
        _feeds.clear()
        _results.clear()
        _pieces.clear()
        _sessions.clear()
        _clip_cache.clear()
        _anchor_cache.clear()
        _stats.update(rows=0, diariser="", sessions_opened=0)
    _warned.clear()
