"""Fixture data model for the  silence/speak-vs-pass eval.

A fixture is one scenario: a short conversation, which roster member's
speak-or-pass decision is being judged, and the two axes a human reviewer
graded it on -- `informational_value` (would speaking add a new fact,
disagreement, or angle?) and `relational_cost_of_silence` (would staying
quiet read as absence/ignoring, versus simply "nothing more to add"?) --
plus the `expected_verdict` those two axes should imply under the 
principle in backend/providers.py: pass only when BOTH are low.
"""

from dataclasses import dataclass, field

VERDICTS = ("speak", "pass")
AXIS_VALUES = ("low", "high")


class FixtureError(ValueError):
    """A fixture file/object failed schema validation."""


@dataclass
class Fixture:
    id: str
    category: str
    responder: str
    conversation: list[dict]
    informational_value: str
    relational_cost_of_silence: str
    expected_verdict: str
    already_answered_by: list[str] = field(default_factory=list)
    notes: str = ""

    @staticmethod
    def from_dict(d: dict, source: str = "<unknown>") -> "Fixture":
        required = ("id", "category", "responder", "conversation",
                    "informational_value", "relational_cost_of_silence",
                    "expected_verdict")
        missing = [k for k in required if not d.get(k)]
        if missing:
            raise FixtureError(f"{source}: fixture missing required field(s) {missing}")
        if d["expected_verdict"] not in VERDICTS:
            raise FixtureError(
                f"{source}: fixture {d['id']!r} has expected_verdict "
                f"{d['expected_verdict']!r} not in {VERDICTS}")
        for axis in ("informational_value", "relational_cost_of_silence"):
            if d[axis] not in AXIS_VALUES:
                raise FixtureError(
                    f"{source}: fixture {d['id']!r} has {axis}={d[axis]!r} not in "
                    f"{AXIS_VALUES}")
        if not isinstance(d["conversation"], list) or not d["conversation"]:
            raise FixtureError(
                f"{source}: fixture {d['id']!r} conversation must be a non-empty list")
        for turn in d["conversation"]:
            if not isinstance(turn, dict) or not turn.get("speaker") or not turn.get("content"):
                raise FixtureError(
                    f"{source}: fixture {d['id']!r} has a conversation turn missing "
                    "speaker/content")
        return Fixture(
            id=d["id"],
            category=d["category"],
            responder=d["responder"],
            conversation=d["conversation"],
            informational_value=d["informational_value"],
            relational_cost_of_silence=d["relational_cost_of_silence"],
            expected_verdict=d["expected_verdict"],
            already_answered_by=d.get("already_answered_by") or [],
            notes=d.get("notes") or "",
        )
