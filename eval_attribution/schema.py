"""Fixture and result shapes for the attribution replay harness.

One fixture is one synthetic group chat plus a set of probes. A probe names
the seat being asked, the question, and the expected answer: a roster slug,
`user` for the owner, `self` for the probed seat, or `yes` / `no`."""

from dataclasses import dataclass, field

PROBE_KINDS = ("who", "yesno")


class FixtureError(ValueError):
    pass


@dataclass
class Probe:
    seat: str
    question: str
    expected: str
    kind: str = "who"
    tag: str = ""

    def resolve_expected(self) -> str:
        return self.seat if self.expected == "self" else self.expected


@dataclass
class Fixture:
    id: str
    category: str
    user_name: str
    roster: list[dict]
    transcript: list[dict]
    probes: list[Probe]
    notes: str = ""
    source: str = ""

    @property
    def names(self) -> dict:
        return {m["slug"]: m["name"] for m in self.roster}

    @classmethod
    def from_dict(cls, raw: dict, source: str = "") -> "Fixture":
        for key in ("id", "category", "user_name", "roster", "transcript", "probes"):
            if key not in raw:
                raise FixtureError(f"{source}: fixture missing {key!r}")
        slugs = {m["slug"] for m in raw["roster"]}
        speakers = {t["speaker"] for t in raw["transcript"]}
        if not speakers <= slugs | {"user"}:
            raise FixtureError(f"{source}: {raw['id']}: transcript speaker outside roster")
        probes = []
        for p in raw["probes"]:
            kind = p.get("kind", "who")
            if kind not in PROBE_KINDS:
                raise FixtureError(f"{source}: {raw['id']}: probe kind {kind!r}")
            if p["seat"] not in slugs:
                raise FixtureError(f"{source}: {raw['id']}: probe seat {p['seat']!r}")
            exp = p["expected"]
            if kind == "yesno" and exp not in ("yes", "no"):
                raise FixtureError(f"{source}: {raw['id']}: yesno probe expects yes/no")
            if kind == "who" and exp not in slugs | {"user", "self"}:
                raise FixtureError(f"{source}: {raw['id']}: who probe expects a slug")
            probes.append(Probe(seat=p["seat"], question=p["question"], expected=exp,
                                kind=kind, tag=p.get("tag", "")))
        if not probes:
            raise FixtureError(f"{source}: {raw['id']}: no probes")
        return cls(id=raw["id"], category=raw["category"], user_name=raw["user_name"],
                   roster=list(raw["roster"]), transcript=list(raw["transcript"]),
                   probes=probes, notes=raw.get("notes", ""), source=source)


@dataclass
class ProbeResult:
    fixture_id: str
    category: str
    probe_tag: str
    seat: str
    kind: str
    variant: str
    model: str
    expected: str
    answered: str | None
    correct: bool
    failure_mode: str
    latency_s: float = 0.0
    input_tokens: int = 0
    output_tokens: int = 0
    cost_usd: float | None = None
    raw_text: str = ""
