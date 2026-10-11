"""What every candidate judge is asked, and how its answer is read (#640).

One moment: an AI seat was speaking, someone said something over it, and
the reply is paused. The judge says which of three it was:

- interrupt: said to the AIs (a question, an answer, a correction, a stop),
  so the reply should stop and the words be answered
- aside: not said to the AIs (someone else in the room, the TV, a remark to
  nobody), so the reply should carry on
- listening: only a sign of listening ("yeah", "mm-hm", "wow"), so the reply
  should carry on

The same description of the moment goes to every judge, so they're compared
on one footing.
"""

import json
import re
from dataclasses import dataclass, field
from pathlib import Path

LABELS = ("interrupt", "aside", "listening")
OWNER = "Alex"
SEATS = ("Claude", "GPT")
# The made-up roster: nothing here may name a real person.
ROSTER = ("Alex", "Sam", "Dave", "Mateo")
CASES = Path(__file__).with_name("cases.json")

CRITERIA = {
    "interrupt": "Said to the AIs: a question, an answer, a correction, or "
                 "asking them to stop. The AI should stop and listen.",
    "aside": "Not said to the AIs: talk to someone else in the room, the TV "
             "or radio, or a remark to nobody. The AI should carry on.",
    "listening": "Only a sign of listening or a reaction, like 'yeah', "
                 "'mm-hm' or 'wow'. The AI should carry on.",
}


@dataclass
class Case:
    id: str
    category: str
    seat: str
    ai_said: str
    speaker: str
    room: list = field(default_factory=list)
    words: str = ""
    expected: str = ""


def load_cases(path=CASES) -> list:
    data = json.loads(Path(path).read_text())
    cases = [Case(**d) for d in data]
    ids = [c.id for c in cases]
    if len(ids) != len(set(ids)):
        raise ValueError("case ids must be unique")
    for c in cases:
        if c.expected not in LABELS:
            raise ValueError(f"{c.id}: expected must be one of {LABELS}")
        if c.speaker not in ("owner", "tv", "unknown") and c.speaker not in c.room:
            raise ValueError(f"{c.id}: a person speaking must be in the room")
        if c.seat not in SEATS:
            raise ValueError(f"{c.id}: unknown seat {c.seat}")
    return cases


def as_heard(case: Case) -> Case:
    """The moment with its words as a live transcript often arrives: lower
    case, and no punctuation but apostrophes."""
    import dataclasses
    plain = re.sub(r"[^\w\s'’]", " ", case.words.lower())
    return dataclasses.replace(case, words=" ".join(plain.split()))


def _who(case: Case) -> str:
    if case.speaker == "owner":
        return f"{OWNER}, the owner"
    if case.speaker == "tv":
        return "a voice the app knows is the TV"
    if case.speaker == "unknown":
        return "a voice the app doesn't know"
    return f"{case.speaker}, who is in the room with {OWNER}"


def describe(case: Case) -> str:
    """The moment as plain text: the state every judge reads."""
    room = (f"Also in the room: {', '.join(case.room)}." if case.room
            else f"Nobody else is in the room.")
    return (
        f"A voice chat at home. {OWNER} is talking out loud with AI assistants "
        f"({', '.join(SEATS)}). {room}\n"
        f"{case.seat} was in the middle of saying: \"{case.ai_said}\"\n"
        f"While it spoke, {_who(case)} said: \"{case.words}\""
    )


def prompt(case: Case) -> str:
    """The moment and the question, for a judge that writes its answer."""
    options = "\n".join(f"{k}: {v}" for k, v in CRITERIA.items())
    return (
        f"{describe(case)}\n\n"
        f"Was that said to the AIs, so that {case.seat} should stop and listen? "
        "Answer with exactly one word, one of these:\n"
        f"{options}"
    )


_THINK = re.compile(r"<think>.*?</think>", re.S | re.I)


def parse_label(text) -> str:
    """The first of the three labels in a written answer, or '' for none."""
    t = _THINK.sub(" ", str(text or "")).lower()
    hits = [(t.find(lab), lab) for lab in LABELS if lab in t]
    return min(hits)[1] if hits else ""


def jev_body(case: Case, model: str = "jev-latest") -> dict:
    """The request Jev takes: the moment as its state, and one choice."""
    return {
        "state": describe(case),
        "model": model,
        "questions": {"verdict": {
            "type": "choice",
            "instructions": f"Was that said to the AIs, so that {case.seat} "
                            "should stop and listen?",
            "criteria": dict(CRITERIA),
        }},
    }


def parse_jev(data) -> tuple:
    """Jev's choice and its confidence, or ('', None) when it gave none."""
    try:
        ans = data["answers"]["verdict"]
        choice = str(ans.get("choice") or "")
        conf = ans.get("confidence")
        return (choice if choice in LABELS else "",
                float(conf) if conf is not None else None)
    except (KeyError, TypeError, AttributeError, ValueError):
        return "", None
