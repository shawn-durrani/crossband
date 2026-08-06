"""The  general principle, expressed as code -- for fixture consistency
checks ONLY.

This is NOT a live classifier. It is not imported by backend/engine.py or
backend/providers.py, and it never runs against a real message: engine.py's
pick_responders routing is unchanged by , and the actual speak-or-pass
call in production stays a model judgment call under the reworded prompt
rule, not a function like this one. Its only job here is to prove the fixture
set in eval_silence/fixtures/*.json is internally consistent -- that the
human-graded axes on each fixture actually imply the human-graded verdict --
so the matrix can't silently drift out of sync with the principle it's meant
to illustrate.
"""

from eval_silence.schema import AXIS_VALUES


def decide(informational_value: str, relational_cost_of_silence: str) -> str:
    """Mirrors backend/providers.py's reworded "don't pile on" rule: pass
    only when BOTH the informational value of speaking and the relational
    cost of staying silent are low. Either one being high means speak."""
    for value, name in ((informational_value, "informational_value"),
                        (relational_cost_of_silence, "relational_cost_of_silence")):
        if value not in AXIS_VALUES:
            raise ValueError(f"{name} must be one of {AXIS_VALUES}, got {value!r}")
    if informational_value == "low" and relational_cost_of_silence == "low":
        return "pass"
    return "speak"
