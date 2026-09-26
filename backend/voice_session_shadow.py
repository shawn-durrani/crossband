"""The session shadow (#482 stage 2): the redesigned naming, measured on
real turns beside today's, changing nothing.

Today a voice turn is named on its own, from the whole turn. The redesign
(docs/VOICE_ID_REDESIGN.md) follows each voice through the whole voice
session with a local diariser and names each VOICE once, from everything
it has said so far. This module runs that idea in shadow:

  1. Per chat, it opens a streaming tracking session on the loopback
     diariser (workbench's diarserve, its /sessions routes) the first
     time a turn arrives, and reopens one after SESSION_IDLE_S of quiet.
  2. Each turn's audio is pushed to that session, then end-turn pushes
     a short silence so the tracker labels the turn's last second. The
     answer is spans in session time: which voice slot, start, end, and
     whether another slot spoke over it. A slot keeps its number for the
     session, so each slot is a session voice.
  3. Every span of MIN_SPAN_S or more that one voice has alone, and that
     passes the live matcher's speech gates, is fingerprinted with the live
     model (TitaNet-Small) and added to that session voice's evidence.
  4. After each turn every session voice is named from its pooled
     fingerprint, against every kept clip of every candidate (the #477
     multi scorer and its bar, so the only difference from the per-turn
     multi measure is the pooling), one person per voice.
  5. One content-free row per turn records the turn's spans and main
     voice, every session voice's state, and today's live label beside it.

THE RULES, pinned in tests/test_voice_session_shadow.py:

  * Never alters anything. It writes one JSON line per turn to
    <data_dir>/voice_session_shadow.jsonl and nothing else. It never
    labels, seats, banks or asks.
  * It rides the shadow test's own worker, after the shadow row, so it
    can't delay the live path, and it needs the shadow's loopback diariser
    URL plus its own switch, `voice_session_shadow`. Both default off.
  * Clips added to a bank after the tracking session opened are left out
    of the comparison, so a voice is never scored against audio from the
    same session that the live path banked (the flaw found in the
    #465 shadow on 26 September).
  * Content-free rows: ids, slots, names, scores, seconds and timings. The
    turn's audio lives in memory for the pass, and the diariser keeps the
    session's audio in memory only.

FILLING IN (`voice_session_labels`, off by default, the first live step of
the cut-over, asked for by the owner on 26 September): when a session
voice is named, every turn of the session whose main voice it is, and
that today's pass left with no name, takes that name as its label, with
source "session". Rules, pinned in the same test file:

  * Only a turn with no label is filled. A name from the live pass, a
    turn a person corrected or confirmed, a crosstalk turn and a label that
    doesn't parse are never touched. A label this module wrote may move
    when its voice's name changes.
  * Nothing else happens: no seat, no banked clip, no ask. Memory reads
    source "session" as by-elimination, the weakest method, which membro
    never binds on by itself.
  * The owner's own name (or a spelling of it) is written with the owner
    marker, as the live pass writes it.
"""

import collections
import json
import logging
import os
import queue
import threading
import time
from pathlib import Path

from . import db, voice_shadow, voiceid

log = logging.getLogger("crossband.voice_session_shadow")

SESSION_IDLE_S = 600.0          # a quieter chat gets a fresh session
MIN_SPAN_S = 0.8                # shorter spans aren't fingerprinted
LISTEN_MIN_S = 1.5              # a voice under this much clean speech listens
NEW_VOICE_MIN_S = 4.0           # clean speech before a voice can be "new"
REQUEST_TIMEOUT_S = 5.0
SAMPLE_RATE = 16000
MAX_SPANS = 64                  # spans read per turn; the rest are ignored
ROWS_FILE = "voice_session_shadow.jsonl"
ROWS_MAX = 5000
ROWS_KEEP = 4000
ROW_VERSION = 1
SESSION_SOURCE = "session"      # the label source a filled-in turn carries
TURNS_KEPT = 400                # turns per session a late name can fill
LIVE_WAIT_S = 0.8               # longest the live check waits for a name
FEED_CHUNK_BYTES = int(0.25 * 16000) * 2   # audio pushed at a time, live
FEED_QUEUE_MAX = 4000           # chunks held before new ones are dropped
RESULTS_MAX = 256               # turns whose live result is remembered

_lock = threading.Lock()        # guards _sessions and _stats
_rows_lock = threading.Lock()
_sessions: dict = {}            # chat_id -> session dict
_stats = {"rows": 0, "diariser": "", "sessions_opened": 0}


def enabled(cfg) -> bool:
    """On only with its own switch AND the shadow's loopback diariser."""
    return bool((cfg or {}).get("voice_session_shadow")
                and voice_shadow.diariser_url(cfg))


def labels_enabled(cfg) -> bool:
    """Filling in unnamed turns needs the session shadow on as well."""
    return bool(enabled(cfg) and (cfg or {}).get("voice_session_labels"))


def live_enabled(cfg) -> bool:
    """The live step: the relay feeds the tracker as audio arrives, and the
    live check names an otherwise unnamed turn from it. Needs the session
    shadow on as well."""
    return bool(enabled(cfg) and (cfg or {}).get("voice_session_live"))


# ================= the live feed ============================================
# LIVE (`voice_session_live`, #482 stage 3). The relay hands every audio
# chunk inside a turn to feed() as it arrives, and end_turn() at the commit.
# A per-chat feed thread pushes the chunks to the tracking session in
# quarter-second pieces, so when the turn ends only the end-turn flush and
# the naming are left to do (about a tenth of a second). The live check
# asks wait_turn() for the turn's main voice, for at most LIVE_WAIT_S, and
# falls back to today's behaviour when nothing comes. Nothing here runs on
# the event loop, and a failure never reaches the relay or the live check.

_feeds: dict = {}               # chat_id -> _Feed
_results: "collections.OrderedDict" = collections.OrderedDict()


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
    """The live result for one turn: {"voice", "state", "name", "score"},
    or None when there is none within `timeout` (blocking; call it off the
    event loop)."""
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
    when no slot was opened for it (no live feed saw the turn)."""
    tid = str(turn_id or "")[:64]
    with _lock:
        slot = _results.get(tid) if tid else None
    if slot is None:
        return False, False, None
    return True, slot["event"].is_set(), slot["result"]


async def await_turn(turn_id, timeout=LIVE_WAIT_S, step=0.02):
    """wait_turn for the event loop: polls every `step` seconds instead of
    holding a thread, so the live check never borrows a worker to wait."""
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
            or not live_enabled(cfg):
        return
    _feed_for(chat_id, cfg).put(("audio", bytes(pcm)))


def end_turn(chat_id, turn_id, cfg):
    """The relay's commit: the turn so far is done. Opens the turn's result
    slot at once, so the live check can wait on it."""
    if not chat_id or not live_enabled(cfg):
        return
    tid = str(turn_id or "")[:64] or None
    if tid:
        _result_slot(tid)
    with _lock:
        f = _feeds.get(chat_id)
    if f is None:
        _resolve(tid, None)
        return
    f.put(("end", tid))


def _feed_for(chat_id, cfg):
    with _lock:
        f = _feeds.get(chat_id)
        if f is None or not f.alive:
            f = _Feed(chat_id, cfg)
            _feeds[chat_id] = f
            f.start()
        return f


def embed_live(pcm, sample_rate, cfg):
    """TitaNet-Small on one clean span, for the live step. Unlike the
    shadow's embed it doesn't wait for the live check to finish, because
    the live check is waiting on this. Tests replace this one function."""
    audio = voiceid._pcm_to_float(pcm)
    if audio is None or len(audio) == 0:
        return None
    ex = voiceid._get_extractor(cfg)
    if ex is None:
        return None
    return voiceid._embed(ex, audio, sample_rate)


def _live_candidates(chat_id):
    """The live check's own candidates: every remembered person, with the
    people seated in this chat kept even while their bank is paused."""
    from . import diarize
    con = db.connect()
    try:
        seated = {r["person_id"] for r in db.get_room_roster(
            con, chat_id, present_only=True) if r["person_id"]}
    finally:
        con.close()
    return diarize.remembered_candidates(rostered_ids=frozenset(seated))


class _Feed:
    """One chat's feed thread. It owns the chat's tracking session while
    live, so nothing else pushes audio to it."""

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
        finally:
            self.alive = False
            with _lock:
                if _feeds.get(self.chat_id) is self:
                    _feeds.pop(self.chat_id, None)
                sess = _sessions.pop(self.chat_id, None)
            if sess:
                _close(sess)

    def _session(self):
        base = voice_shadow.diariser_url(self.cfg)
        if not base:
            raise _SessionError("no_diariser")
        return _session_for(self.chat_id, base, time.time())

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
        except _SessionError as err:
            self._fail(err.reason)
        except Exception:
            log.debug("session feed push failed", exc_info=True)
            self._fail("error")

    def _fail(self, reason):
        self.broken = True
        self.pending.clear()
        with _lock:
            _sessions.pop(self.chat_id, None)
            _stats["diariser"] = reason
        voice_shadow._warn_once(
            "session", "the diariser's tracking session failed (%s); live "
                       "naming falls back until it answers", reason)

    def _end(self, turn_id):
        t0 = time.perf_counter()
        pcm = bytes(self.turn_pcm)
        result = None
        row = {"v": ROW_VERSION, "at": round(time.time(), 3),
               "chat_id": self.chat_id, "turn_id": turn_id or "",
               "message_id": None, "live": True,
               "seconds": round(len(pcm) / 2 / SAMPLE_RATE, 3)}
        try:
            if self.pending:
                self._push()
            if self.broken:
                raise _SessionError(_stats.get("diariser") or "error")
            if not pcm or self.turn_start is None:
                return
            sess = self._session()
            got = clean_spans(_call(
                "POST", f"{sess['base']}/sessions/{sess['id']}/end-turn"))
            if got is None:
                raise _SessionError("bad_response")
            sess["last_at"] = time.time()
            sess["turns"] += 1
            result = _name_turn(
                self.chat_id, sess, turn_id, pcm, SAMPLE_RATE,
                self.spans + got, self.turn_start, self.cfg,
                _live_candidates(self.chat_id), False,
                lambda seg: embed_live(seg, SAMPLE_RATE, self.cfg), row)
            with _lock:
                _stats["diariser"] = "ok"
            voice_shadow._recovered("session", "the tracking sessions "
                                               "answer again")
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
            self.turn_start = None
            self.broken = False
        row["ms"] = round((time.perf_counter() - t0) * 1000, 1)
        try:
            write_row(row)
        except Exception:
            log.debug("session feed row failed", exc_info=True)


def status(cfg) -> dict:
    with _lock:
        return {"on": enabled(cfg), "live": live_enabled(cfg),
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


def name_voices(voices, people, bar):
    """Name every session voice from its pooled evidence, one person per
    voice. `voices` is {slot: {"prints": [(emb, secs)], "clean_s"}},
    `people` is {pid: {"name", "clips": [emb]}}, `bar` carries the multi
    scorer's "threshold" and "margin". Returns {slot: {"state", "name",
    "pid", "score", "second", "clean_s"}} where state is listening, named
    or new.

    One-to-one is greedy on score, highest first: exact for the two or
    three people a home room holds, and never gives one person two voices.
    A voice beaten to its best person stays listening, because its margin
    over that person fails, which is what the tracker splitting one person
    into two voices looks like."""
    threshold = bar.get("threshold", 0.5)
    margin = bar.get("margin", 0.0)
    scores = {}
    for slot, v in voices.items():
        pool = pooled(v.get("prints"))
        if pool is None:
            continue
        scores[slot] = {pid: s for pid, s in (
            (pid, voice_shadow.topk_mean(pool, p["clips"]))
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


def _session_for(chat_id, base, now):
    """This chat's open tracking session, opening one when there is none,
    it went quiet for SESSION_IDLE_S, or the diariser URL changed."""
    with _lock:
        sess = _sessions.get(chat_id)
    if sess and (now - sess["last_at"] > SESSION_IDLE_S
                 or sess["base"] != base):
        _close(sess)
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


# ================= the bank, as it stood when the session opened ============

def bank(candidates, sample_rate, cfg, before):
    """{pid: {"name", "clips"}} like voice_shadow.build_multi, but without
    any clip added at or after `before` (epoch seconds), so audio the live
    path banked during this session never scores this session's voices.
    Clip embeddings come from, and go to, the shadow's own clip cache."""
    from . import anchors as anchor_store
    ids = [c["person_id"] for c in candidates or () if c.get("person_id")]
    if not ids:
        return {}
    names = {c["person_id"]: c.get("name") for c in candidates}
    store = anchor_store.store()
    clips = store.enrollment_clips(ids, sample_rate, max_clips=None)
    out = {}
    for pid, info in clips.items():
        added = {c["file"]: c.get("added_at") or 0
                 for c in store.clips_of(pid) or ()}
        pcms = info["pcms"]
        files = tuple(info["fingerprint"])[-len(pcms):] if pcms else ()
        embs = []
        for fname, pcm in zip(files, pcms):
            if added.get(fname, 0) >= before:
                continue
            key = (fname, len(pcm))
            with voice_shadow._lock:
                emb = voice_shadow._clip_cache.get(key)
            if emb is None:
                emb = voice_shadow.embed("small", pcm, sample_rate, cfg)
                if emb is None:
                    continue
                with voice_shadow._lock:
                    voice_shadow._clip_cache[key] = emb
            embs.append(emb)
        if embs:
            out[pid] = {"name": names.get(pid) or info["name"], "clips": embs}
    return out


def _bar(people, candidates, sample_rate, cfg, pending):
    """The multi scorer's bar (voice_shadow.multi_bar), the live bar carried
    onto multi's scale at the same z."""
    small = voice_shadow.build_anchors("small", candidates, sample_rate, cfg)
    return voice_shadow.multi_bar(
        cfg, voice_shadow.impostor_stats(small),
        voice_shadow.multi_impostor_stats(people), pending)


# ================= one turn =================================================

def observe(chat_id, turn_id, pcm, sample_rate, cfg, today):
    """Track and name one turn (worker thread), and write its row. Never
    raises: a failure is one row with an error and a log line once."""
    t0 = time.perf_counter()
    base = voice_shadow.diariser_url(cfg)
    if not base or not pcm or sample_rate != SAMPLE_RATE \
            or live_enabled(cfg):
        # Live, the relay's feed pushes the audio as it arrives; pushing it
        # again here would give the tracker every turn twice.
        return None
    now = time.time()
    seconds = len(pcm) / 2 / sample_rate
    row = {"v": ROW_VERSION, "at": round(now, 3), "chat_id": chat_id,
           "turn_id": str(turn_id or "")[:64],
           "message_id": voice_shadow._message_id(chat_id, turn_id),
           "seconds": round(seconds, 3),
           "today": voice_shadow._today(today)}
    try:
        sess = _session_for(chat_id, base, now)
        offset = sess["pushed_s"]
        url = f"{base}/sessions/{sess['id']}"
        pushed = clean_spans(_call("POST", url + "/audio", content=pcm))
        flushed = clean_spans(_call("POST", url + "/end-turn"))
        if pushed is None or flushed is None:
            raise _SessionError("bad_response")
        sess["pushed_s"] = offset + seconds
        sess["last_at"] = now
        sess["turns"] += 1
        _name_turn(chat_id, sess, turn_id, pcm, sample_rate, pushed + flushed,
                   offset, cfg, today.get("candidates") or [],
                   bool(today.get("pending")),
                   lambda seg: voice_shadow.embed("small", seg, sample_rate,
                                                  cfg), row)
        with _lock:
            _stats["diariser"] = "ok"
        voice_shadow._recovered("session", "the tracking sessions answer "
                                           "again")
    except _SessionError as err:
        with _lock:
            _sessions.pop(chat_id, None)
            _stats["diariser"] = err.reason
        voice_shadow._warn_once(
            "session", "the diariser's tracking session failed (%s); the "
                       "session shadow records the error until it answers",
            err.reason)
        row["error"] = err.reason
    except Exception:
        with _lock:
            _sessions.pop(chat_id, None)
        voice_shadow._warn_once("session_row", "a session shadow row could "
                                               "not be built; the live path "
                                               "is unaffected")
        log.debug("session shadow failure detail", exc_info=True)
        row["error"] = "error"
    row["ms"] = round((time.perf_counter() - t0) * 1000, 1)
    write_row(row)
    return row


def _name_turn(chat_id, sess, turn_id, pcm, sample_rate, raw_spans, offset,
               cfg, candidates, pending, embed_fn, row):
    """The naming step, shared by the shadow's observe and the live feed:
    move the tracker's spans into turn time, fingerprint the clean ones,
    name every session voice, fill in unnamed turns when that's on, and
    put it all on `row`. Returns the turn's main voice as {"voice",
    "state", "name", "score"}, or None when no voice spoke."""
    seconds = len(pcm) / 2 / sample_rate
    spans = []
    for s in raw_spans:
        a = max(0.0, s["start"] - offset)
        b = min(seconds, s["end"] - offset)
        if b > a:
            spans.append({**s, "start": round(a, 3), "end": round(b, 3)})
    if True:
        embedded = 0
        for s in spans:
            # Every voice heard is on the table, even one heard only over
            # someone else: it listens with no evidence.
            sess["voices"].setdefault(s["slot"],
                                      {"prints": [], "clean_s": 0.0})
            if s["overlap"] or s["end"] - s["start"] < MIN_SPAN_S:
                continue
            seg = voice_shadow.span_pcm(pcm, sample_rate,
                                        [(s["start"], s["end"])])
            if voice_shadow.gate(seg, sample_rate):
                continue
            emb = embed_fn(seg)
            if emb is None:
                continue
            secs = len(seg) / 2 / sample_rate
            v = sess["voices"].setdefault(s["slot"],
                                          {"prints": [], "clean_s": 0.0})
            v["prints"].append((emb, secs))
            v["clean_s"] += secs
            embedded += 1
        # A second's grace: a clip banked by the live pass that opened the
        # session carries a timestamp just after the session's own.
        people = bank(candidates, sample_rate, cfg,
                      before=sess["opened_at"] - 1.0)
        bar = _bar(people, candidates, sample_rate, cfg, pending)
        named = name_voices(sess["voices"], people, bar)
        main = main_voice(spans)
        if main is not None and turn_id:
            sess["turn_voice"].append((str(turn_id), main))
            del sess["turn_voice"][:-TURNS_KEPT]
        filled = fill_labels(chat_id, sess, named, cfg) \
            if labels_enabled(cfg) else None
        row.update(
            session=sess["id"], turn=sess["turns"],
            offset=round(offset, 3),
            spans=spans, main=main,
            main_state=(named.get(main) or {}).get("state", "listening")
            if main is not None else "",
            main_name=(named.get(main) or {}).get("name", "")
            if main is not None else "",
            voices={str(k): v for k, v in sorted(named.items())},
            bar={k: bar.get(k) for k in ("threshold", "margin", "source")},
            embedded=embedded, people=len(people))
        if filled is not None:
            row["filled"] = filled
    if main is None:
        return None
    voice = named.get(main) or {}
    return {"voice": main, "state": voice.get("state", "listening"),
            "name": voice.get("name", ""), "score": voice.get("score")}


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
    no name, or the name there is one this module wrote."""
    if old is None or old.get("corrected") or old.get("crosstalk"):
        return False
    names = [n for n in old.get("labels") or () if isinstance(n, str)
             and n.strip()]
    return not names or old.get("source") == SESSION_SOURCE


def fill_labels(chat_id, sess, named, cfg):
    """Write each named session voice's name onto its unnamed turns in this
    session (see FILLING IN above). Returns how many labels it wrote."""
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
                    or done.get(turn_id) == name:
                continue
            msg = db.get_message_by_voice_turn(con, chat_id, turn_id)
            if not msg or not fillable(_labels_of(msg["voice_labels"])):
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


# ================= the rows file ============================================

def rows_path() -> Path:
    return Path(db.DATA_DIR) / ROWS_FILE


def write_row(row):
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


def read_rows(limit=200, chat_id=None) -> list:
    """The newest rows first, at most `limit`, optionally for one chat."""
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
    return list(reversed(keep))


def compare(rows) -> dict:
    """Per turn, today's label beside the session voice's name at the time
    and at the end of its session, plus a tally. Rows newest first."""
    final = {}
    for row in rows:                    # newest first: first seen is final
        if row.get("session") and row["session"] not in final:
            final[row["session"]] = row.get("voices") or {}
    lines, tally = [], collections.Counter()
    for row in rows:
        if row.get("error"):
            tally["errors"] += 1
            continue
        main = row.get("main")
        at_end = (final.get(row.get("session")) or {}).get(str(main)) or {}
        today = row.get("today") or {}
        line = {"turn_id": row.get("turn_id"),
                "message_id": row.get("message_id"),
                "seconds": row.get("seconds"),
                "today": "+".join(today.get("labels") or []),
                "today_reason": today.get("reason", ""),
                "then": row.get("main_name") or row.get("main_state") or "",
                "at_end": at_end.get("name") or at_end.get("state", ""),
                "voice": main}
        lines.append(line)
        tally["turns"] += 1
        tally["today_named"] += bool(line["today"])
        tally["then_named"] += bool(row.get("main_name"))
        tally["at_end_named"] += bool(at_end.get("name"))
        if line["today"] and at_end.get("name") \
                and at_end["name"] not in line["today"].split("+"):
            tally["at_end_differs_from_today"] += 1
    return {"tally": dict(tally), "lines": lines}


def _reset_for_tests():
    with _lock:
        for f in _feeds.values():
            f.alive = False
        _feeds.clear()
        _results.clear()
        _sessions.clear()
        _stats.update(rows=0, diariser="", sessions_opened=0)
