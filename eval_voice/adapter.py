"""The seam between the rig and the system it tests.

The rig's own parts (the scripts, the renderer, the mixer and the
scorer) know nothing about crossband. An adapter takes the mixed turns
of one conversation, plays them into a speaker-identification system,
and hands back what that system said about each turn as `Heard`. The
crossband adapter (crossband.py) is one. Another system plugs in by
writing its own, with the same three methods.

`Heard.names` holds the people the system named on the turn, sure or
not. A voice it heard but couldn't name isn't a name: it shows up as a
placeholder count and, when the system gives one, a reason.
"""

from dataclasses import asdict, dataclass, field


@dataclass
class Heard:
    index: int
    labelled: bool = False         # the system said anything at all
    names: list = field(default_factory=list)
    unsure: list = field(default_factory=list)
    placeholders: int = 0          # voices heard but left unnamed
    reason: str = ""               # why a voice was left unnamed
    learning: bool = False         # named by elimination, not yet trusted
    owner: bool = False
    crosstalk: bool = False
    segments: list = field(default_factory=list)   # [{"name", "text", "unsure"}]
    in_time: bool | None = None    # labelled by the time the turn was saved
    transcript: str | None = None  # what the transcriber heard, if anything
    note: str = ""                 # what went wrong on the way, if anything
    score: float | None = None

    def to_dict(self, content: bool = True) -> dict:
        d = asdict(self)
        if not content:
            d.pop("transcript", None)
            d["segments"] = [{"name": s.get("name"), "unsure": s.get("unsure"),
                              "words": len((s.get("text") or "").split())}
                             for s in self.segments]
        return d


@dataclass
class EventCheck:
    index: int
    kind: str          # "introduce" or "room"
    value: str         # the name, or "on" / "off"
    result: str        # "heard", "missed" or "already" (true before the turn)
    seen: str = ""     # what the system showed, e.g. the name it seated


@dataclass
class ConversationResult:
    script: str
    heard: list                      # [Heard], one per turn, in order
    events: list = field(default_factory=list)       # [EventCheck]
    diagnostics: dict = field(default_factory=dict)  # content-free extras


class Adapter:
    """What the rig asks of a system under test."""

    name = "adapter"

    def enrol(self, voices: dict) -> dict:
        """Give the system each person's voice before the run: {name: 16
        kHz mono 16-bit audio of them reading}. Returns content-free notes
        for the report."""
        raise NotImplementedError

    def converse(self, script_id: str, turns: list) -> ConversationResult:
        """Play one conversation's mixed turns (mix.MixedTurn) in order."""
        raise NotImplementedError

    def close(self) -> dict:
        """Tidy up. Returns content-free notes for the report, such as the
        system's own count of what it spent."""
        return {}
