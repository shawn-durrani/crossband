"""Is a turn for the AIs, and which of them? (#648, measured, not live)

Every turn in a voice chat goes to every seat, and each decides for itself
whether to answer or pass. In a room that costs twice over: a question
between people has a "?", so the first seat isn't allowed to pass and is
asked again (#497), and side chatter gets a round of calls that end in a
pass, or a spoken "Still listening" (#606). One quick check per turn could
say three things before the round starts:

- for: the AIs, someone in the room, nobody (thinking aloud, a reaction),
  or background (the TV, the radio)
- asks: whether the AIs owe a reply (a question, a request, an answer to
  something they asked)
- seat: which seat it names, all of them, or none

The trial harness (eval_addressee) measures TypeSafe's Jev and the small
Claude model on this, beside today's rules. The description and the
questions live here, so a live trial would send what was measured.
"""

import json
import re

FOR = {
    # Listed first on purpose: Jev leans towards the first option, and a
    # wrong "for the AIs" costs a pass, where a wrong skip leaves a real
    # question unanswered.
    "ais": "Said to the AI assistants, all or some of them",
    "someone": "Said to a person in the room, not to the assistants. That "
               "includes asking for something only a person could do or "
               "know, like passing something, the oven, or what's in the house",
    "nobody": "Thinking aloud, a reaction or an exclamation, to nobody",
    "background": "Background audio, like the TV or radio, not a person "
                  "talking to anyone here",
}
ASKS = {
    "yes": "The assistants owe a reply: a question or request to them, or "
           "an answer to something they asked",
    "no": "Nothing the assistants need to answer",
}


# What the assistants can and can't do, so a request only a person could
# carry out reads as said to a person (the research in #631: a request only
# one party can fulfil is for that party).
CAPABLE = ("The AI assistants can talk, explain, advise and look things up. "
           "They can't see or hear the room, touch or fetch anything, or know "
           "what's in the house.")


def _who(who, owner):
    if who == "tv":
        return "a voice the app knows is the TV"
    if who == "unknown":
        return "a voice the app doesn't know"
    if who == owner:
        return f"{owner}, the owner"
    return who


def describe(owner, seats, room, recent, who, text) -> str:
    """The turn and what came just before it, as plain text."""
    others = [p for p in room if p != owner]
    lines = [
        f"A voice chat at home. {owner} talks out loud with AI assistants "
        f"({', '.join(seats)}).",
        f"Also in the room: {', '.join(others)}." if others
        else "Nobody else is in the room.",
    ]
    if recent:
        lines.append("Just before:")
        for t in recent[-3:]:
            lines.append(f"- {_who(t['who'], owner)}: \"{t['text']}\"")
    lines.append(f"Now {_who(who, owner)} says: \"{text}\"")
    return "\n".join(lines)


def jev_questions(seats) -> dict:
    seat_criteria = {"all": "All the assistants, or any of them: no one is "
                            "named, or the words are for the assistants generally"}
    seat_criteria.update({s: f"{s}, by name" for s in seats})
    seat_criteria["none"] = "No assistant: it isn't said to the assistants"
    return {
        "for": {"type": "choice",
                "instructions": CAPABLE + " Who is the newest message said to?",
                "criteria": dict(FOR)},
        "asks": {"type": "choice",
                 "instructions": "Do the AI assistants owe the newest "
                                 "message a reply?",
                 "criteria": dict(ASKS)},
        "seat": {"type": "choice",
                 "instructions": "Which assistant is the newest message "
                                 "for?",
                 "criteria": seat_criteria},
    }


def parse_jev(answers, seats) -> dict:
    """Jev's answers as {for, asks, seat}, '' for anything missing."""
    def pick(name, allowed):
        try:
            c = str(answers[name]["choice"])
        except (KeyError, TypeError):
            return ""
        return c if c in allowed else ""
    out = {"for": pick("for", FOR), "asks": pick("asks", ASKS),
           "seat": pick("seat", {"all", "none", *seats})}
    # The three answers hang together: a turn not for the assistants owes
    # them nothing and names no seat, and one for them names a seat or all.
    if out["for"] and out["for"] != "ais":
        out["asks"], out["seat"] = "no", "none"
    elif out["for"] == "ais" and out["seat"] in ("", "none"):
        out["seat"] = "all"
    return out


def claude_prompt(state, seats) -> str:
    options = lambda d: "; ".join(f"\"{k}\": {v}" for k, v in d.items())  # noqa: E731
    return (
        f"{state}\n\n{CAPABLE}\n"
        "Answer three things about the newest message.\n"
        f"for, who it's said to, one of: {options(FOR)}.\n"
        f"asks, one of: {options(ASKS)}.\n"
        f"seat, which assistant it's for: \"all\" when none is named, one of "
        f"{', '.join(seats)} when one is named or clearly meant, or \"none\" "
        "when it isn't for the assistants.\n"
        "Reply with ONLY JSON: {\"for\": ..., \"asks\": ..., \"seat\": ...}"
    )


def parse_claude(text, seats) -> dict:
    m = re.search(r"\{.*\}", str(text or ""), re.S)
    try:
        data = json.loads(m.group(0)) if m else {}
    except json.JSONDecodeError:
        data = {}
    answers = {k: {"choice": str(v)} for k, v in data.items()
               if k in ("for", "asks", "seat")}
    return parse_jev(answers, seats)
