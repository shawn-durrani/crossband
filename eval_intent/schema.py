"""One fixture is one user turn and what the app should hear in it, graded
by hand, across the five axes the merged classifier returns: a room mode
switch, introductions and departures (with aliases), name corrections, a
reasoning depth change, and a research request. Every fixture is made up:
placeholder people, placeholder topics."""

from dataclasses import dataclass, field

MODE_VALUES = ("on", "off", "none")
RESEARCH_VALUES = ("more", "none")
DEPTHS = ("deep", "quick", "max", "normal")
AXES = ("mode_command", "introductions", "departures", "aliases",
        "corrections", "depth", "research")


class FixtureError(ValueError):
    """A fixture file or object failed validation."""


def empty_verdict() -> dict:
    return {"mode_command": "none", "introductions": [], "departures": [],
            "aliases": {}, "corrections": [], "depth": [], "research": "none"}


@dataclass
class Fixture:
    id: str
    category: str
    text: str
    expected: dict
    user_name: str = "Alex"
    present: list = field(default_factory=list)
    known: list = field(default_factory=list)
    seats: list = field(default_factory=lambda: ["Claude", "GPT"])
    notes: str = ""

    @property
    def has_intent(self) -> bool:
        e = self.expected
        return (e["mode_command"] != "none" or bool(e["introductions"])
                or bool(e["departures"]) or bool(e["corrections"])
                or bool(e["depth"]) or e["research"] != "none")

    @staticmethod
    def from_dict(d: dict, source: str = "<unknown>") -> "Fixture":
        for key in ("id", "category", "text"):
            if not d.get(key):
                raise FixtureError(f"{source}: fixture missing {key!r}")
        raw = d.get("expected")
        if not isinstance(raw, dict):
            raise FixtureError(f"{source}: fixture {d['id']!r} has no expected dict")
        unknown = set(raw) - set(AXES)
        if unknown:
            raise FixtureError(f"{source}: fixture {d['id']!r} has unknown axes {sorted(unknown)}")
        expected = empty_verdict()
        expected.update(raw)
        if expected["mode_command"] not in MODE_VALUES:
            raise FixtureError(f"{source}: fixture {d['id']!r} mode_command "
                               f"{expected['mode_command']!r} not in {MODE_VALUES}")
        if expected["research"] not in RESEARCH_VALUES:
            raise FixtureError(f"{source}: fixture {d['id']!r} research "
                               f"{expected['research']!r} not in {RESEARCH_VALUES}")
        for key in ("introductions", "departures"):
            if not isinstance(expected[key], list) or not all(
                    isinstance(n, str) and n for n in expected[key]):
                raise FixtureError(f"{source}: fixture {d['id']!r} {key} must be a list of names")
        if not isinstance(expected["aliases"], dict):
            raise FixtureError(f"{source}: fixture {d['id']!r} aliases must be a dict")
        for c in expected["corrections"]:
            if not isinstance(c, dict) or not c.get("name"):
                raise FixtureError(f"{source}: fixture {d['id']!r} has a correction without a name")
            c.setdefault("who", "")
            c.setdefault("also", "")
        for ch in expected["depth"]:
            if not isinstance(ch, dict) or not ch.get("seat") or ch.get("depth") not in DEPTHS:
                raise FixtureError(f"{source}: fixture {d['id']!r} has a bad depth change {ch!r}")
            ch["once"] = bool(ch.get("once"))
        return Fixture(id=d["id"], category=d["category"], text=d["text"],
                       expected=expected, user_name=d.get("user_name") or "Alex",
                       present=list(d.get("present") or []),
                       known=list(d.get("known") or []),
                       seats=list(d.get("seats") or ["Claude", "GPT"]),
                       notes=d.get("notes") or "")
