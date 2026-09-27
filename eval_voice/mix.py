"""Mix rendered lines into turns, on this computer, and write down the
truth for every second as it goes.

For each turn the mixer trims the silence the renderer leaves around a
line, so a voice's span is when it's heard. It lays the line down after a
short lead-in, drops it by the turn's gain, starts a crosstalk line part
of the way through the first, and puts the script's noise bed under the
whole turn at the script's signal-to-noise ratio. The noise is made here
from random numbers, seeded by the script and turn, so the same script
always mixes to the same audio.

Nothing here calls anything. It needs numpy, which the app already uses.
"""

import hashlib
import io
import wave
from dataclasses import dataclass

import numpy as np

from eval_voice import truth as truth_mod

SAMPLE_RATE = 16000
LEAD_S = 0.35          # quiet before the first word, like a mic pre-roll
TAIL_S = 0.45          # and after the last one, before the turn is committed
FRAME_S = 0.02
TRIM_FLOOR = 0.003     # a frame quieter than this is silence, whatever else
TRIM_RATIO = 0.05      # ...and so is one this far under the loudest frame
PEAK = 0.95            # the mix is scaled down if it would clip


@dataclass
class MixedTurn:
    pcm: bytes                  # 16 kHz mono 16-bit
    truth: truth_mod.TurnTruth
    gap_s: float = 0.6

    @property
    def seconds(self) -> float:
        return len(self.pcm) / 2 / SAMPLE_RATE


def to_float(pcm: bytes) -> np.ndarray:
    return np.frombuffer(pcm, dtype="<i2").astype(np.float32) / 32768.0


def to_pcm(x: np.ndarray) -> bytes:
    return (np.clip(x, -1.0, 1.0) * 32767.0).astype("<i2").tobytes()


def gain(db: float) -> float:
    return float(10.0 ** (db / 20.0))


def _frame_rms(x: np.ndarray, frame: int) -> np.ndarray:
    n = len(x) // frame
    if n == 0:
        return np.zeros(0, dtype=np.float32)
    return np.sqrt(np.mean(x[:n * frame].reshape(n, frame) ** 2, axis=1))


def active_frames(x: np.ndarray, sample_rate: int = SAMPLE_RATE) -> np.ndarray:
    frame = int(FRAME_S * sample_rate)
    levels = _frame_rms(x, frame)
    if not len(levels):
        return levels.astype(bool)
    bar = max(TRIM_FLOOR, TRIM_RATIO * float(levels.max()))
    return levels >= bar


def trim(x: np.ndarray, sample_rate: int = SAMPLE_RATE) -> np.ndarray:
    """The line from its first heard frame to its last. A line with no
    frame above the bar is kept whole."""
    frame = int(FRAME_S * sample_rate)
    on = np.flatnonzero(active_frames(x, sample_rate))
    if not len(on):
        return x
    return x[on[0] * frame:min(len(x), (on[-1] + 1) * frame)]


def rms(x: np.ndarray) -> float:
    return float(np.sqrt(np.mean(x ** 2))) if len(x) else 0.0


def speech_rms(x: np.ndarray, sample_rate: int = SAMPLE_RATE) -> float:
    """Level over the frames that are speech, so pauses don't dilute it."""
    frame = int(FRAME_S * sample_rate)
    on = active_frames(x, sample_rate)
    if not on.any():
        return rms(x)
    n = len(on)
    frames = x[:n * frame].reshape(n, frame)[on]
    return float(np.sqrt(np.mean(frames ** 2)))


def _shaped(n: int, rng, power: float, low_hz: float) -> np.ndarray:
    """Noise whose power falls as 1/f**power, with nothing under low_hz."""
    white = rng.standard_normal(n)
    spec = np.fft.rfft(white)
    f = np.fft.rfftfreq(n, 1.0 / SAMPLE_RATE)
    shape = np.zeros_like(f)
    keep = f >= low_hz
    shape[keep] = 1.0 / np.power(f[keep], power / 2.0)
    out = np.fft.irfft(spec * shape, n)
    level = rms(out)
    return out / level if level else out


def noise_bed(kind: str, n: int, rng) -> np.ndarray:
    """Unit-level noise, `n` samples long. Cafe is pink noise with the odd
    clink of a cup, and road is a low rumble that swells as traffic
    passes."""
    if n <= 0:
        return np.zeros(0, dtype=np.float64)
    t = np.arange(n) / SAMPLE_RATE
    if kind == "cafe":
        bed = _shaped(n, rng, 1.0, 60.0)
        clinks = rng.poisson(0.6 * n / SAMPLE_RATE)
        length = int(0.06 * SAMPLE_RATE)
        env = np.exp(-np.arange(length) / (0.012 * SAMPLE_RATE))
        for _ in range(clinks):
            at = int(rng.integers(0, max(1, n - length)))
            hz = float(rng.uniform(2200, 4200))
            burst = np.sin(2 * np.pi * hz * np.arange(length) / SAMPLE_RATE)
            end = min(n, at + length)
            bed[at:end] += 3.0 * (burst * env)[:end - at]
    elif kind == "road":
        bed = _shaped(n, rng, 2.0, 25.0)
        phase = float(rng.uniform(0, 2 * np.pi))
        bed = bed * (0.6 + 0.4 * np.sin(2 * np.pi * 0.09 * t + phase))
    else:
        raise ValueError(f"unknown noise kind {kind!r}")
    level = rms(bed)
    return bed / level if level else bed


def seed_for(script_id: str, index: int) -> int:
    return int(hashlib.sha256(f"{script_id}:{index}".encode()).hexdigest()[:12],
               16)


def mix_turn(script_id, index, main_line, main_pcm, over=None, over_pcm=None,
             noise=None, events=(), enrolled=(), introduced=(), gap_s=0.6):
    """One turn's audio and truth. `main_line` and `over.line` are
    script.Line values, `*_pcm` their rendered audio, `noise` the script's
    {"kind", "snr_db"} or None. `enrolled` and `introduced` are the names
    with a bank before the run and those introduced earlier in the
    script, which the truth records for the main voice."""
    rng = np.random.default_rng(seed_for(script_id, index))
    lead, tail = int(LEAD_S * SAMPLE_RATE), int(TAIL_S * SAMPLE_RATE)
    a_raw = trim(to_float(main_pcm))
    a = a_raw * gain(main_line.gain_db)
    placed = [(main_line, a, lead)]
    if over is not None:
        b = trim(to_float(over_pcm)) * gain(over.line.gain_db)
        placed.append((over.line, b, lead + int(over.at * len(a))))
    total = max(start + len(x) for _, x, start in placed) + tail
    out = np.zeros(total, dtype=np.float64)
    for _, x, start in placed:
        out[start:start + len(x)] += x
    snr = None
    if noise:
        snr = float(noise["snr_db"])
        # Against the speech at its own level before any gain, so a quiet
        # voice is quiet against the room, not the room with it.
        ref = speech_rms(a_raw)
        out += noise_bed(noise["kind"], total, rng) * ref / gain(snr)
    peak = float(np.max(np.abs(out))) if total else 0.0
    if peak > PEAK:
        out *= PEAK / peak
    voices = [truth_mod.VoiceSpan(name=line.speaker,
                                  start=round(start / SAMPLE_RATE, 3),
                                  end=round((start + len(x)) / SAMPLE_RATE, 3),
                                  gain_db=line.gain_db, words=line.text)
              for line, x, start in placed]
    t = truth_mod.TurnTruth(script=script_id, index=index, voices=voices,
                            seconds=round(total / SAMPLE_RATE, 3),
                            noise=noise["kind"] if noise else "",
                            snr_db=snr, events=[dict(e) for e in events])
    t.enrolled = t.main in set(enrolled)
    t.introduced = t.main in set(introduced)
    return MixedTurn(pcm=to_pcm(out), truth=t, gap_s=gap_s)


def mix_script(script, renderer, enrolled=()) -> list:
    """Every turn of a script, rendered and mixed, in order."""
    turns, introduced = [], set()
    for i, turn in enumerate(script.turns):
        main_pcm = renderer.line(turn.line.speaker, turn.line.text)
        over_pcm = (renderer.line(turn.over.line.speaker, turn.over.line.text)
                    if turn.over else None)
        turns.append(mix_turn(script.id, i, turn.line, main_pcm, turn.over,
                              over_pcm, script.noise, turn.events, enrolled,
                              introduced, gap_s=turn.gap_s))
        for e in turn.events:
            if "introduce" in e:
                introduced.add(e["introduce"])
    return turns


def enrol_audio(renderer, name: str, passage: str) -> bytes:
    """A person reading their passage, trimmed, in a quiet room."""
    x = trim(to_float(renderer.line(name, passage)))
    pad = np.zeros(int(0.3 * SAMPLE_RATE))
    return to_pcm(np.concatenate([pad, x, pad]))


def wav_bytes(pcm: bytes, sample_rate: int = SAMPLE_RATE) -> bytes:
    buf = io.BytesIO()
    with wave.open(buf, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(sample_rate)
        w.writeframes(pcm)
    return buf.getvalue()


def conversation(turns: list) -> tuple:
    """The whole conversation as one recording, each turn followed by its
    gap, and who spoke when in that recording's time. The shape any
    speaker-identification system that takes a file can be scored on."""
    parts, timeline, at = [], [], 0.0
    for mt in turns:
        parts.append(mt.pcm)
        timeline.append({
            "turn": mt.truth.index, "start": round(at, 3),
            "end": round(at + mt.seconds, 3),
            "voices": [{"name": v.name, "start": round(at + v.start, 3),
                        "end": round(at + v.end, 3)}
                       for v in mt.truth.voices]})
        at += mt.seconds
        gap = b"\x00\x00" * int(mt.gap_s * SAMPLE_RATE)
        parts.append(gap)
        at += len(gap) / 2 / SAMPLE_RATE
    return b"".join(parts), timeline
