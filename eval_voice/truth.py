"""What the rig knows about every turn it plays: who spoke, from when to
when in the turn's own time, how loud, over what noise, and what they
said. The mixer writes it and the scorer reads it, and neither needs the
other: this module is the contract between them.

Times are seconds from the start of the turn's audio. A voice's span is
where its speech starts and stops, with the silence the renderer puts
around a line trimmed off, so it's the time the voice is heard.
"""

from dataclasses import asdict, dataclass, field

# A second voice counts as crosstalk once it speaks for this long in a
# turn. It's the app's own rule (docs/VOICE_ID.md, "When two people talk
# at once"), so a shorter interjection isn't expected to be split.
CROSSTALK_MIN_S = 1.0
# A line shorter than this is short: the app can't name a voice it has
# heard for less than 1.5 seconds on its own.
SHORT_S = 1.5
# A line this much quieter than the rest counts as a quiet voice.
QUIET_DB = -6.0


@dataclass
class VoiceSpan:
    name: str
    start: float
    end: float
    gain_db: float = 0.0
    words: str = ""

    @property
    def seconds(self) -> float:
        return round(self.end - self.start, 3)


@dataclass
class TurnTruth:
    script: str
    index: int
    voices: list                     # [VoiceSpan], the main line first
    seconds: float                   # the whole turn's audio
    noise: str = ""                  # "", "cafe" or "road"
    snr_db: float | None = None
    events: list = field(default_factory=list)
    enrolled: bool = True            # the main speaker had a bank before the run
    introduced: bool = False         # ...or was introduced earlier in the script

    @property
    def names(self) -> list:
        return [v.name for v in self.voices]

    def overlap_s(self) -> float:
        """Seconds where two voices are heard at once."""
        if len(self.voices) < 2:
            return 0.0
        total = 0.0
        for i, a in enumerate(self.voices):
            for b in self.voices[i + 1:]:
                total += max(0.0, min(a.end, b.end) - max(a.start, b.start))
        return round(total, 3)

    def alone_s(self, name: str) -> float:
        """Seconds this voice is heard with nobody over it."""
        mine = [v for v in self.voices if v.name == name]
        others = [v for v in self.voices if v.name != name]
        total = 0.0
        for v in mine:
            covered = 0.0
            for o in others:
                covered += max(0.0, min(v.end, o.end) - max(v.start, o.start))
            total += max(0.0, v.seconds - covered)
        return round(total, 3)

    @property
    def main(self) -> str:
        """The voice heard most on its own, the one the app should name.
        A tie goes to the main line."""
        best = max(self.voices, key=lambda v: self.alone_s(v.name))
        top = self.alone_s(best.name)
        return self.voices[0].name if self.alone_s(self.voices[0].name) == top \
            else best.name

    @property
    def crosstalk(self) -> bool:
        return any(v.seconds >= CROSSTALK_MIN_S for v in self.voices[1:])

    def tags(self) -> list:
        """The conditions the turn was played under, for the report's
        breakdown."""
        out = []
        if self.noise:
            out.append(f"{self.noise} noise")
        main = next(v for v in self.voices if v.name == self.main)
        if main.gain_db <= QUIET_DB:
            out.append("quiet voice")
        if self.crosstalk:
            out.append("crosstalk")
        if main.seconds < SHORT_S:
            out.append("short")
        if not self.enrolled:
            out.append("introduced voice" if self.introduced else "new voice")
        if not out:
            out.append("clean")
        return out

    def to_dict(self, content: bool = True) -> dict:
        d = asdict(self)
        if not content:
            for v in d["voices"]:
                v.pop("words", None)
        d.update(main=self.main, crosstalk=self.crosstalk,
                 overlap_s=self.overlap_s(), tags=self.tags())
        return d


def from_dict(d: dict) -> TurnTruth:
    voices = [VoiceSpan(**{k: v[k] for k in ("name", "start", "end",
                                               "gain_db", "words") if k in v})
              for v in d["voices"]]
    return TurnTruth(script=d["script"], index=d["index"], voices=voices,
                     seconds=d["seconds"], noise=d.get("noise", ""),
                     snr_db=d.get("snr_db"), events=list(d.get("events") or []),
                     enrolled=d.get("enrolled", True),
                     introduced=d.get("introduced", False))
