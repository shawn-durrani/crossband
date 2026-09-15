"""Compares what a strategy heard with what a human graded, axis by axis,
and adds up the numbers the decision needs: accuracy per axis and per
category for each strategy, the turns today's lists never send to a model,
cost and latency per turn, and every miss with the reply text."""

from dataclasses import dataclass, field
import statistics

from eval_intent.schema import AXES, Fixture

STRATEGIES = ("today", "merged")


def _fold(name) -> str:
    return " ".join(str(name or "").split()).casefold()


def normalise(verdict: dict) -> dict:
    """The verdict as comparable values: names casefolded, lists as sets."""
    return {
        "mode_command": verdict["mode_command"],
        "introductions": frozenset(_fold(n) for n in verdict["introductions"]),
        "departures": frozenset(_fold(n) for n in verdict["departures"]),
        "aliases": frozenset((_fold(k), _fold(v)) for k, v in verdict["aliases"].items()),
        "corrections": frozenset((_fold(c.get("who")), _fold(c.get("name")),
                                  _fold(c.get("also"))) for c in verdict["corrections"]),
        "depth": frozenset((_fold(ch["seat"]), ch["depth"], bool(ch.get("once")))
                           for ch in verdict["depth"]),
        "research": verdict["research"],
    }


@dataclass
class Result:
    fixture: Fixture
    strategy: str
    heard: dict
    replies: dict = field(default_factory=dict)   # axis or "merged" -> reply text
    cost_usd: float = 0.0
    latency_s: float = 0.0
    calls: int = 0
    missing_key: bool = False
    timed_out: bool = False

    def wrong_axes(self) -> list:
        want, got = normalise(self.fixture.expected), normalise(self.heard)
        return [a for a in AXES if want[a] != got[a]]


def _pct(hits, total):
    return hits / total if total else None


def aggregate(results: list, fixtures: list, silent: dict) -> dict:
    """silent: fixture id -> axes today's lists drop (today.silent_misses)."""
    by_strategy = {}
    for s in STRATEGIES:
        rows = [r for r in results if r.strategy == s]
        if not rows:
            continue
        per_axis = {a: _pct(sum(1 for r in rows if a not in r.wrong_axes()), len(rows))
                    for a in AXES}
        cats = sorted({r.fixture.category for r in rows})
        per_cat = {c: _pct(sum(1 for r in rows if r.fixture.category == c
                               and not r.wrong_axes()),
                           sum(1 for r in rows if r.fixture.category == c))
                   for c in cats}
        lat = [r.latency_s for r in rows if r.calls]
        by_strategy[s] = {
            "turns": len(rows),
            "all_axes_right": _pct(sum(1 for r in rows if not r.wrong_axes()), len(rows)),
            "per_axis": per_axis,
            "per_category": per_cat,
            "cost_per_turn_usd": (sum(r.cost_usd for r in rows) / len(rows)),
            "calls_per_turn": sum(r.calls for r in rows) / len(rows),
            "latency_p50_s": statistics.median(lat) if lat else None,
            "missing_key": sum(1 for r in rows if r.missing_key),
            "timeouts": sum(1 for r in rows if r.timed_out),
            "misses": [{"id": r.fixture.id, "category": r.fixture.category,
                        "wrong": r.wrong_axes(), "text": r.fixture.text,
                        "replies": r.replies} for r in rows if r.wrong_axes()],
        }
    with_intent = [f for f in fixtures if f.has_intent]
    dropped = [f for f in with_intent if silent.get(f.id)]
    return {
        "n_fixtures": len(fixtures),
        "n_with_intent": len(with_intent),
        "silent_today": {
            "turns": len(dropped),
            "share": _pct(len(dropped), len(with_intent)),
            "by_axis": {a: sum(1 for f in dropped if a in silent[f.id])
                        for a in ("mode_command", "introductions", "corrections",
                                  "depth", "research")},
            "ids": [f.id for f in dropped],
        },
        "strategies": by_strategy,
    }
