"""Recording a voice on purpose (#504, item B of the #482 plan).

The Voices page can record someone reading a short passage aloud, about
30 seconds, and bank it. Clips otherwise arrive only by chance, when a
voice chat hears someone and is sure who it was, and most banks hold too
little clean speech to pass the readiness test (voice_calibration.py).

The route lives in routers/room.py beside the other people routes. This
module holds its rules, pure apart from the store calls in `save`:

1. The page sends a PCM-16 mono WAV at 16 kHz, the rate every clip the
   calibrated scorer reads is stored at. Anything else is refused.
2. The speech check. The dead air at both ends is trimmed
   (voiceid.trim_dead_air), and what's left must be audible, mostly
   speech (voiceid.is_speech) and hold at least MIN_SPEECH_SECONDS of
   speech, counted the way the readiness test counts it
   (voiceid.speech_spans). Each refusal carries a plain reason the page
   shows as it is.
3. The cut. The speech is cut into clips of at most
   anchors.MAX_CLIP_SECONDS, each a contiguous slice of the recording,
   cut in the pauses between words wherever a pause falls, so no clip is
   spliced. A stretch with no pause in it is cut evenly.
4. The save. Each clip goes through AnchorStore.add_clip with source
   "introduction", so the clip gate, the keep-best-N rotation and the
   protected rule apply exactly as they do to a spoken introduction: the
   bank counts as vouched, and no automated clip can rotate these out.

Privacy: the recording is read in memory and goes only to the local
store, like any clip. Nothing here logs a name, a person id or audio.
"""

import io
import math
import wave

from . import anchors, voiceid

SAMPLE_RATE = 16000
# The page stops by itself at 45 s. A minute leaves room for a slow stop
# and refuses anything that isn't a recording from the page.
MAX_RECORD_SECONDS = 60.0
MAX_UPLOAD_BYTES = int(MAX_RECORD_SECONDS * SAMPLE_RATE * 2) + 4096
# The readiness test needs 20 two-second pieces, one starting every
# second. Ten seconds is half that from one recording, so anything
# shorter is refused as too short to be worth a slot in the bank.
MIN_SPEECH_SECONDS = 10.0
SOURCE = "introduction"


class Refused(Exception):
    """A recording the route turns away. `reason` is a fixed word for
    tests and logs, `message` the plain sentence the page shows."""

    def __init__(self, reason: str, message: str):
        super().__init__(message)
        self.reason = reason
        self.message = message


def read_wav(data: bytes) -> bytes:
    """The PCM-16 samples of a 16 kHz mono WAV, or Refused."""
    if not data:
        raise Refused("empty", "Nothing was recorded. Try again.")
    if len(data) > MAX_UPLOAD_BYTES:
        raise Refused("too_long", "That recording ran over a minute. "
                                  "Record about 30 seconds.")
    try:
        with wave.open(io.BytesIO(data)) as w:
            ok = (w.getsampwidth() == 2 and w.getnchannels() == 1
                  and w.getframerate() == SAMPLE_RATE)
            pcm = w.readframes(w.getnframes()) if ok else b""
    except (wave.Error, EOFError, ValueError):
        ok, pcm = False, b""
    if not ok:
        raise Refused("bad_audio", "The recording didn't arrive as 16 kHz "
                                   "mono audio. Reload the page and try "
                                   "again.")
    return pcm[:len(pcm) - len(pcm) % 2]


def speech_seconds(pcm: bytes, sample_rate: int = SAMPLE_RATE) -> float:
    """Seconds of speech, pauses left out, as the readiness test counts
    them (voice_calibration.clip_layout)."""
    return sum(hi - lo for lo, hi in voiceid.speech_spans(pcm, sample_rate)) \
        / sample_rate


def check(pcm: bytes, sample_rate: int = SAMPLE_RATE):
    """The speech check. Returns (trimmed pcm, seconds of speech), or
    raises Refused with the first thing wrong, in this order: too long,
    nothing audible, not speech, too little speech."""
    if len(pcm) / 2 / sample_rate > MAX_RECORD_SECONDS:
        raise Refused("too_long", "That recording ran over a minute. "
                                  "Record about 30 seconds.")
    trimmed = voiceid.trim_dead_air(pcm, sample_rate)
    speech = speech_seconds(trimmed, sample_rate)
    if not speech or anchors.pcm_rms(trimmed) < anchors.MIN_CLIP_RMS:
        raise Refused("too_quiet", "It didn't hear anyone. Check the right "
                                   "microphone is on, then read the passage "
                                   "close to it.")
    if not voiceid.is_speech(trimmed, sample_rate):
        raise Refused("not_speech", "That didn't sound like someone "
                                    "speaking. Try somewhere quieter, and "
                                    "read the passage aloud.")
    if speech < MIN_SPEECH_SECONDS:
        heard = int(speech)
        raise Refused("too_short", f"That was only {heard} second"
                                   f"{'' if heard == 1 else 's'} of speech. "
                                   "Read the whole passage, which takes "
                                   "about 30 seconds.")
    return trimmed, speech


def cut_points(pcm: bytes, sample_rate: int = SAMPLE_RATE,
               max_seconds: float | None = None,
               min_seconds: float | None = None) -> list:
    """Where to cut one recording into clips, as (start, end) sample
    offsets, in order and never overlapping. Pure.

    The speech runs are voiceid.speech_spans with no pad, so every dip
    between words is a place a cut can go. Runs join into a clip while
    the clip still fits in `max_seconds` less the trim margin at each
    end, a run too long for any clip is cut evenly, and each clip keeps
    the trim margin either side, never past halfway to its neighbour.
    Clips under `min_seconds` are left out: the store's gate would
    refuse them anyway."""
    sr = sample_rate or SAMPLE_RATE
    usable = len(pcm) // 2
    cap = int((max_seconds or anchors.MAX_CLIP_SECONDS) * sr)
    shortest = int((anchors.MIN_CLIP_SECONDS if min_seconds is None
                    else min_seconds) * sr)
    margin = int(voiceid.SPEECH_TRIM_MARGIN_SECONDS * sr)
    room = max(1, cap - 2 * margin)
    runs = []
    for lo, hi in voiceid.speech_spans(pcm, sr, pad_seconds=0.0):
        parts = max(1, math.ceil((hi - lo) / room))
        step = math.ceil((hi - lo) / parts)
        for k in range(parts):
            runs.append((lo + k * step, min(hi, lo + (k + 1) * step)))
    groups = []
    for lo, hi in runs:
        if groups and hi - groups[-1][0] <= room:
            groups[-1] = (groups[-1][0], hi)
        else:
            groups.append((lo, hi))
    out = []
    for i, (lo, hi) in enumerate(groups):
        left = 0 if i == 0 else (groups[i - 1][1] + lo) // 2
        right = usable if i == len(groups) - 1 \
            else (hi + groups[i + 1][0]) // 2
        start, end = max(left, lo - margin), min(right, hi + margin)
        if end - start >= shortest:
            out.append((start, end))
    return out


def save(store, person_id: str, pcm: bytes,
         sample_rate: int = SAMPLE_RATE) -> dict:
    """Cut a checked recording into clips and offer each to the store as
    an introduction. Returns counts only: clips offered, accepted by the
    gate, and still in the bank once rotation has run (`saved`), with
    their seconds. A clip the gate accepts can still lose its place to
    a better one from the same bank, which is why `saved` is counted
    from the bank afterwards."""
    before = {c["file"] for c in store.clips_of(person_id) or []}
    offered = accepted = 0
    for start, end in cut_points(pcm, sample_rate):
        offered += 1
        if store.add_clip(person_id, pcm[start * 2:end * 2], sample_rate,
                          source=SOURCE):
            accepted += 1
    new = [c for c in store.clips_of(person_id) or []
           if c["file"] not in before]
    return {"offered": offered, "accepted": accepted, "saved": len(new),
            "seconds": round(sum(c["seconds"] for c in new), 1)}
