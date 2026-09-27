"""Crosstalk, split on this computer (#482 item D).

When two people talk in one spoken turn, the turn's label says so, and
where it can it says which words were whose. This module does it on this
computer, and no voice clip goes to the cloud for it, from two things the
app already has:

  1. The tracker's spans (backend/voice_sessions.py's feed):
     which session voice spoke when, in the turn's own time.
  2. Scribe Realtime's word times. The relay asks for them, and each
     commit's timed final carries every word with its start and end.

LINING THE TWO CLOCKS UP. Each counts the audio sent on its own stream:
Scribe from the first audio on its socket, the tracker from the first
audio of its tracking session. The relay feeds both the same chunks, but
either can restart on its own (the browser reconnects the socket, or the
tracking session is reopened after a failure or a long quiet), so their
absolute times are never compared. Each side is read in turn time
instead: the relay records how much audio its socket had carried when the
turn began and when it was committed, and the tracker's feed records the
session time of the turn's first chunk (turn_start) and how much audio
the turn held. The commit is the one moment both streams share exactly,
because the relay hands the commit's own chunk to both before either
hears the commit. So the words are lined up at the commit: a word that
ended half a second before Scribe's commit ended half a second before
the tracker's end of turn. When the tracker heard more of the turn than
Scribe did (its feed kept audio from a socket that died before it could
commit), that still holds. When it heard less (chunks dropped from a full
feed queue), the clocks can't be matched and the turn keeps the crosstalk
marker with no split.

ATTRIBUTION, per word, by the word's midpoint:

  * inside one voice's span, with nobody over it: that voice, sure;
  * inside an overlap (two voices at once): the turn's main voice when it
    is one of them, else the one of them that spoke longest, and never
    sure, because one microphone can't say whose word it was;
  * in a gap between spans: the voice within NEAR_S of it, sure when only
    one voice is that close, else as for an overlap; with nobody that
    close, the main voice, not sure.

A word that lands on a voice the label doesn't list (it spoke for less
than OTHER_VOICE_MIN_S) goes to the main voice, not sure.

THE LABEL. Every voice listed gets its session name when the naming has
named it, and "Voice N" (its session number) otherwise, which reads as
uncertain everywhere: the chips show it with a question mark, and the
AIs read "unidentified speaker", never the owner. The payload is the one
the UI and the seats already render: `crosstalk: true`, `overlap`, and
`segments` [{label, text, uncertain}] in time order, with a segment
uncertain when its voice isn't named or its words weren't sure. Memory
ingests a crosstalk turn as guest:unknown (memory_client.ingest_speaker),
whoever spoke.

The words hold transcript text, so they live in memory only, for at most
WORDS_TTL_S, and the pass takes them away when it reads them. Nothing
here is logged or written to disk; the label is the message's own words.
"""

import asyncio
import collections
import threading
import time

OTHER_VOICE_MIN_S = 1.0   # another voice is listed when it spoke this long
NEAR_S = 0.3              # a word in a gap goes to a voice this close to it
ALIGN_SLACK_S = 0.25      # the tracker may hear this much less than Scribe
WORDS_WAIT_S = 1.0        # the longest a two-voice turn waits for its words
WORDS_TTL_S = 30.0        # words nobody took are dropped after this long
WORDS_KEPT = 64           # turns whose words are held at once
MAX_WORDS = 2000          # words read from one commit
MAX_SEGMENTS = 12         # more changes of voice than this is noise
MAX_SEGMENT_CHARS = 400

_lock = threading.Lock()
_words: "collections.OrderedDict" = collections.OrderedDict()


# ================= the words, from the relay to the pass ====================

def turn_words(raw_words, start, limit=MAX_WORDS):
    """Scribe's word list for one commit as [(start, end, text)] in seconds
    from the turn's first audio as Scribe heard it (pure). Spacing entries,
    empty text and entries with no usable times are skipped."""
    out = []
    for w in raw_words or ():
        if not isinstance(w, dict) or w.get("type") == "spacing":
            continue
        text = w.get("text")
        if not isinstance(text, str) or not text.strip():
            continue
        try:
            a = float(w["start"]) - start
            b = float(w.get("end", w["start"])) - start
        except (KeyError, TypeError, ValueError):
            continue
        out.append((round(a, 3), round(max(a, b), 3), text.strip()))
        if len(out) >= limit:
            break
    return out


def put_words(turn_id, raw_words, turn):
    """The relay's timed final for one commit. `turn` is the commit as
    voice.CommitFinals keeps it: the socket's audio seconds when the turn
    began and when it was committed."""
    start, end = (turn or {}).get("start"), (turn or {}).get("end")
    if start is None or end is None:
        _put(turn_id, None)
        return
    _put(turn_id, {"words": turn_words(raw_words, start),
                   "stt_s": round(max(0.0, end - start), 3)})


def no_words(turn_id):
    """This commit's final went out with no word times: nothing is coming,
    so a pass waiting on it stops now."""
    _put(turn_id, None)


def _put(turn_id, entry):
    tid = str(turn_id or "")[:64]
    if not tid:
        return
    now = time.monotonic()
    with _lock:
        for k, (_, at) in list(_words.items()):
            if now - at > WORDS_TTL_S:
                _words.pop(k, None)
        _words.pop(tid, None)
        _words[tid] = (entry, now)
        while len(_words) > WORDS_KEPT:
            _words.popitem(last=False)


def take_words(turn_id):
    """(known, entry) for one turn, taken away when known."""
    tid = str(turn_id or "")[:64]
    with _lock:
        got = _words.pop(tid, None)
    if got is None:
        return False, None
    return True, got[0]


async def await_words(turn_id, timeout=None, step=0.01):
    """The words for one turn, waiting at most `timeout` (WORDS_WAIT_S by
    default; polling, so no thread is held). None when there are none: no
    word times for this commit, or they didn't come in time."""
    deadline = time.monotonic() + (WORDS_WAIT_S if timeout is None
                                   else timeout)
    while True:
        known, entry = take_words(turn_id)
        if known:
            return entry
        if time.monotonic() >= deadline:
            return None
        await asyncio.sleep(step)


# ================= the rules (pure) =========================================

def listed_voices(got):
    """The session voices a turn's label lists: the main voice first, then
    every other voice that spoke OTHER_VOICE_MIN_S or more in the turn, in
    the order they started. Two or more means crosstalk. [] when the
    naming's answer carries no per-voice breakdown."""
    voices = (got or {}).get("voices") or {}
    main = (got or {}).get("voice")
    if main not in voices:
        return []
    others = sorted(
        (slot for slot, v in voices.items() if slot != main
         and (v.get("seconds") or 0.0) >= OTHER_VOICE_MIN_S),
        key=lambda slot: (voices[slot].get("first") or 0.0, slot))
    return [main] + others


def align(entry, tracker_turn_s):
    """The words moved into the tracker's turn time, lined up at the commit
    (see LINING THE TWO CLOCKS UP). None when there are no words or the
    tracker heard less of the turn than Scribe did."""
    if not entry or entry.get("words") is None or tracker_turn_s is None:
        return None
    shift = tracker_turn_s - (entry.get("stt_s") or 0.0)
    if shift < -ALIGN_SLACK_S:
        return None
    return [(a + shift, b + shift, text) for a, b, text in entry["words"]]


def _distance(t, span):
    if t < span["start"]:
        return span["start"] - t
    if t >= span["end"]:
        return t - span["end"]
    return 0.0


def _pick(slots, main, weight):
    if main in slots:
        return main
    return max(slots, key=lambda s: (weight.get(s, 0.0), -s))


def attribute(words, spans, main, weight):
    """[(voice, sure, text)] for each word, by its midpoint (see
    ATTRIBUTION). `spans` are the tracker's spans in turn time, `weight`
    each voice's seconds in the turn."""
    out = []
    for a, b, text in words:
        mid = (a + b) / 2
        covering = [s for s in spans if s["start"] <= mid < s["end"]]
        slots = {s["slot"] for s in covering}
        if len(slots) == 1 and not any(s["overlap"] for s in covering):
            out.append((slots.pop(), True, text))
            continue
        if not slots:
            near = {s["slot"] for s in spans if _distance(mid, s) <= NEAR_S}
            if len(near) == 1:
                out.append((near.pop(), True, text))
                continue
            slots = near
        out.append((_pick(slots, main, weight) if slots else main, False,
                    text))
    return out


def split(attributed, label_of, main, unsure_labels=()):
    """Consecutive words of one voice, and one sureness, grouped into
    segments [{label, text, uncertain}] in time order. [] when there are no
    words or more than MAX_SEGMENTS changes of voice."""
    segments = []
    for slot, sure, text in attributed:
        if slot not in label_of:
            slot, sure = main, False
        label = label_of[slot]
        unsure = (not sure) or label in unsure_labels
        last = segments[-1] if segments else None
        if last and last["label"] == label and last["uncertain"] == unsure:
            last["text"] = (last["text"] + " " + text)[:MAX_SEGMENT_CHARS]
            continue
        segments.append({"label": label, "text": text[:MAX_SEGMENT_CHARS],
                         "uncertain": unsure})
        if len(segments) > MAX_SEGMENTS:
            return []
    return segments


def ordinal(slot) -> str:
    """The label a session voice carries until it is named."""
    return f"Voice {slot}"


def label(got, listed, entry, source="session"):
    """The label payload for a turn with two or more voices (see THE
    LABEL). `got` is the naming's answer for the turn, `listed` the voices
    listed_voices chose, `entry` the relay's words for the turn or None."""
    from . import diarize
    voices = got.get("voices") or {}
    labels, unsure, label_of = [], [], {}
    for slot in listed:
        v = voices.get(slot) or {}
        name = v.get("name") if v.get("state") == "named" else ""
        text = name or ordinal(slot)
        if not name:
            unsure.append(text)
        label_of[slot] = text
        if text not in labels:
            labels.append(text)
    payload = diarize.label_payload(labels, clusters=(source,),
                                    uncertain=unsure, source=source)
    payload["crosstalk"] = True
    payload["overlap"] = (got.get("overlap_s") or 0.0) > 0
    words = align(entry, got.get("turn_s"))
    if words:
        weight = {slot: (v.get("seconds") or 0.0)
                  for slot, v in voices.items()}
        segments = split(attribute(words, got.get("spans") or [],
                                   got.get("voice"), weight),
                         label_of, got.get("voice"), set(unsure))
        if segments:
            payload["segments"] = segments
    return payload
