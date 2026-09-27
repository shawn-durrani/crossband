"""Write new made-up scripts with a small model.

The committed scripts cover the cases the rig was built for. This writes
more, on the utility model, in the same shape: two or three people from
the synthetic roster, short spoken lines, and whichever of crosstalk, a
quiet voice, a noise bed, an introduction or a room command you ask for.
Every script it writes is checked by the same validator as the committed
ones, so a name off the roster or a malformed turn is refused, and it's
saved under the rig's cache, out of the repository.
"""

import hashlib
import json
import re

from eval_voice import cast as cast_mod
from eval_voice import script as script_mod

FEATURES = {
    "crosstalk": "one or two turns where a second person talks over the "
                 "first, using \"over\"",
    "quiet": "one turn from a person further from the microphone, using "
             "\"gain_db\" between -12 and -6",
    "noise": "a noise bed, cafe or road, with snr_db between 5 and 25",
    "introduce": "the owner introducing a person who hasn't spoken yet, by "
                 "name, with an introduce event, and that person speaking "
                 "afterwards",
    "room": "the owner saying \"group mode on\" as the first turn, with a "
            "room on event",
    "short": "one or two replies of a single word or two",
}

PROMPT = """You write short made-up conversations for testing a voice app that \
works out who is speaking. Reply with one JSON object and nothing else.

People: {people}. Use only these names, spelt as written. {owner} owns the app.
Topic: {topic}.
Length: {turns} turns. A turn is one person saying one or two short spoken \
sentences, 4 to 25 words, the way people talk out loud.

Include:
{features}

The JSON shape:
{{"id": "a-short-slug", "about": "one sentence on what the conversation tests",
 "cast": [the people who speak],
 "noise": null or {{"kind": "cafe" or "road", "snr_db": a number}},
 "turns": [{{"speaker": "Name", "text": "what they say"}}]}}

A turn can also carry:
- "over": {{"speaker": "Name", "text": "...", "at": 0.3 to 0.7}} for a second \
person talking over the first, part of the way through
- "gain_db": -12 to -6 for a quieter speaker
- "events": [{{"introduce": "Name"}}] when the text introduces that person by name
- "events": [{{"room": "on"}}] with text like "group mode on", or \
[{{"room": "off"}}] with "solo mode, it's just me now"

No other names, no real people, places, brands or events."""

TOPICS = ("weekend plans", "a household job", "cooking dinner", "a trip away",
          "a local sport match", "fixing something that broke")


class GenerateError(ValueError):
    pass


def build_prompt(people, turns=8, features=(), topic="", owner=cast_mod.OWNER):
    unknown = [f for f in features if f not in FEATURES]
    if unknown:
        raise GenerateError(f"unknown features {unknown}; pick from "
                            f"{sorted(FEATURES)}")
    lines = "\n".join(f"- {FEATURES[f]}" for f in features) or "- nothing extra"
    return PROMPT.format(people=", ".join(people), owner=owner,
                         topic=topic or TOPICS[len(people) % len(TOPICS)],
                         turns=turns, features=lines)


def parse_reply(text: str, people) -> script_mod.Script:
    """The model's reply as a checked script, with an id the committed
    scripts can't collide with."""
    if not text:
        raise GenerateError("the model sent nothing back")
    body = re.sub(r"^```(?:json)?\s*|\s*```$", "", text.strip())
    start, end = body.find("{"), body.rfind("}")
    if start < 0 or end < start:
        raise GenerateError("the reply holds no JSON object")
    try:
        data = json.loads(body[start:end + 1])
    except json.JSONDecodeError as e:
        raise GenerateError(f"the reply isn't valid JSON ({e})") from e
    if not isinstance(data, dict):
        raise GenerateError("the reply isn't a JSON object")
    spoken = {t.get("speaker") for t in data.get("turns") or ()
              if isinstance(t, dict)}
    data["cast"] = [p for p in people if p in spoken or p in data.get("cast", ())]
    digest = hashlib.sha256(body.encode()).hexdigest()[:8]
    slug = re.sub(r"[^a-z0-9-]+", "-", str(data.get("id") or "script").lower())
    data["id"] = f"gen-{slug.strip('-')[:32]}-{digest}"
    try:
        return script_mod.from_dict(data, source="generated", origin="generated")
    except script_mod.ScriptError as e:
        raise GenerateError(str(e)) from e


async def generate(caller, people, turns=8, features=(), topic="",
                   attempts=2):
    """One checked script from `caller(prompt) -> reply text`, with one
    retry on a reply that doesn't validate."""
    prompt = build_prompt(people, turns, features, topic)
    last = None
    for _ in range(attempts):
        reply = await caller(prompt)
        try:
            return parse_reply(reply, people)
        except GenerateError as e:
            last = e
    raise GenerateError(f"no usable script after {attempts} tries: {last}")
