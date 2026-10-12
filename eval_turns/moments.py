"""The made-up moments the turn-taking rig plays, and their audio.

A moment is one of two kinds:

- pause: one person says one sentence with a silence in the middle. The
  right answer is one turn, sent once they've finished.
- over: someone talks while a seat's reply is playing. An interruption
  should cut the reply. A "yeah", a remark to someone else in the room and
  the TV should let it carry on.

Each line is spoken once in its person's ElevenLabs voice by the voice
rig's renderer and kept in its cache. The audio is put together here, and
the truth of every moment, when the words start and stop, is written
down as it goes.
"""

import json
from dataclasses import dataclass
from pathlib import Path

import numpy as np

from eval_voice import cast as cast_mod
from eval_voice import mix as mix_mod

MOMENTS = Path(__file__).with_name("moments.json")
KINDS = ("pause", "over")
GROUPS = ("interrupt", "backchannel", "aside", "background")
SHOULD_CUT = {"interrupt"}
NOISES = ("cafe", "road")
LEAD_S = 0.3
TAIL_S = 0.3
NOISE_SNR_DB = 15.0
# How loud the words reach the page, as the RMS of the speech itself:
# someone talking at a laptop from a normal distance. The renderer's lines
# come back quieter than that, and the page's floors were set on real
# microphones (0.013 to open a turn, 0.03 to talk over a reply).
SPEECH_DBFS = -23.0
RATE = mix_mod.SAMPLE_RATE


@dataclass(frozen=True)
class Moment:
    id: str
    kind: str
    who: str
    text: str = ""
    parts: tuple = ()
    pause_s: float = 0.0
    group: str = ""
    noise: str = ""
    gain_db: float = 0.0     # quieter for someone across the room, or the TV

    @property
    def lines(self) -> tuple:
        return self.parts if self.kind == "pause" else (self.text,)

    @property
    def should_cut(self) -> bool:
        return self.group in SHOULD_CUT


@dataclass(frozen=True)
class Plan:
    opening_who: str
    opening: str
    reply: str
    moments: tuple


@dataclass
class Clip:
    pcm: bytes          # 16 kHz mono 16-bit
    onset_s: float      # the first word
    end_s: float        # the last word
    pause: tuple = ()   # (start, end) of the silence inside a pause moment

    @property
    def seconds(self) -> float:
        return len(self.pcm) / 2 / RATE


def _moment(d, voices, where) -> Moment:
    kind = d.get("kind")
    if kind not in KINDS:
        raise ValueError(f"{where}: kind must be one of {KINDS}")
    who = d.get("who")
    if who not in voices:
        raise ValueError(f"{where}: {who!r} isn't on the made-up roster")
    noise = d.get("noise", "")
    if noise and noise not in NOISES:
        raise ValueError(f"{where}: noise must be one of {NOISES}")
    gain_db = float(d.get("gain_db") or 0.0)
    if not -20.0 <= gain_db <= 6.0:
        raise ValueError(f"{where}: gain_db must be -20 to 6")
    if kind == "pause":
        parts = tuple(d.get("parts") or ())
        pause = float(d.get("pause_s") or 0)
        if len(parts) != 2 or not all(isinstance(p, str) and p.strip() for p in parts):
            raise ValueError(f"{where}: a pause moment has two parts")
        if not 0.2 <= pause <= 5.0:
            raise ValueError(f"{where}: pause_s must be 0.2 to 5 seconds")
        return Moment(d["id"], kind, who, parts=parts, pause_s=pause, noise=noise,
                      gain_db=gain_db)
    group = d.get("group")
    if group not in GROUPS:
        raise ValueError(f"{where}: group must be one of {GROUPS}")
    text = d.get("text")
    if not isinstance(text, str) or not text.strip():
        raise ValueError(f"{where}: an over moment has text")
    return Moment(d["id"], kind, who, text=text, group=group, noise=noise,
                  gain_db=gain_db)


def load(path=MOMENTS, only=None) -> Plan:
    raw = json.loads(Path(path).read_text())
    voices = cast_mod.voices()
    ids = [m.get("id") for m in raw["moments"]]
    if len(ids) != len(set(ids)) or not all(isinstance(i, str) and i for i in ids):
        raise ValueError("moment ids must be unique")
    moments = tuple(_moment(m, voices, f"moment {m.get('id')}") for m in raw["moments"])
    if only:
        wanted = {w.strip() for w in only.split(",") if w.strip()}
        unknown = wanted - {m.id for m in moments} - set(KINDS) - set(GROUPS)
        if unknown:
            raise ValueError(f"no moment or group called {', '.join(sorted(unknown))}")
        moments = tuple(m for m in moments
                        if wanted & {m.id, m.kind, m.group})
    opening = raw["opening"]
    if opening.get("who") not in cast_mod.roster():
        raise ValueError("the opening question is asked by someone on the roster")
    return Plan(opening["who"], opening["text"], raw["reply"], moments)


def _silence(seconds: float) -> np.ndarray:
    return np.zeros(int(round(seconds * RATE)), dtype=np.float64)


def _line(renderer, who: str, text: str) -> np.ndarray:
    return mix_mod.trim(mix_mod.to_float(renderer.line(who, text)).astype(np.float64))


def _level(x: np.ndarray, level_dbfs: float) -> np.ndarray:
    """The words at `level_dbfs`, measured on the speech alone."""
    ref = mix_mod.speech_rms(x)
    return x * (mix_mod.gain(level_dbfs) / ref) if ref else x


def _with_noise(x: np.ndarray, kind: str, seed: int) -> np.ndarray:
    if not kind:
        return x
    rng = np.random.default_rng(seed)
    ref = mix_mod.speech_rms(x)
    return x + mix_mod.noise_bed(kind, len(x), rng) * ref / mix_mod.gain(NOISE_SNR_DB)


def _clip(x: np.ndarray, onset: float, end: float, pause=()) -> Clip:
    peak = float(np.max(np.abs(x))) if len(x) else 0.0
    if peak > mix_mod.PEAK:
        x = x * (mix_mod.PEAK / peak)
    return Clip(mix_mod.to_pcm(x), round(onset, 3), round(end, 3), pause)


def clip_for(moment: Moment, renderer, level_dbfs: float = SPEECH_DBFS) -> Clip:
    """The moment's audio: a short lead-in, the words at the speaking
    level less the moment's gain, a short tail, and the moment's noise
    under all of it."""
    lead, tail = _silence(LEAD_S), _silence(TAIL_S)
    if moment.kind == "pause":
        first, second = (_line(renderer, moment.who, p) for p in moment.parts)
        gap = _silence(moment.pause_s)
        x = np.concatenate([lead, first, gap, second, tail])
        start = LEAD_S + len(first) / RATE
        pause = (round(start, 3), round(start + moment.pause_s, 3))
        end = LEAD_S + (len(first) + len(gap) + len(second)) / RATE
    else:
        words = _line(renderer, moment.who, moment.text)
        x = np.concatenate([lead, words, tail])
        pause, end = (), LEAD_S + len(words) / RATE
    x = _level(x, level_dbfs + moment.gain_db)
    x = _with_noise(x, moment.noise, mix_mod.seed_for(moment.id, 0))
    return _clip(x, LEAD_S, end, pause)


def opening_clip(plan: Plan, renderer, level_dbfs: float = SPEECH_DBFS) -> Clip:
    words = _line(renderer, plan.opening_who, plan.opening)
    x = _level(np.concatenate([_silence(LEAD_S), words, _silence(TAIL_S)]), level_dbfs)
    return _clip(x, LEAD_S, LEAD_S + len(words) / RATE)


def characters(plan: Plan) -> int:
    """What rendering every line costs the first time, in characters."""
    return len(plan.opening) + sum(len(t) for m in plan.moments for t in m.lines)
