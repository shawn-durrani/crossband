"""Whether words said over a paused reply were meant for the AIs (#642).

When someone talks over a spoken reply, the browser pauses it and the word
rules (frontend/src/interruptWords.js) settle the clear cases. What's left
is a few words of their own that might be for the AIs, or for someone else
in the room. For those the browser asks here, and this asks Jev, TypeSafe's
fast classifier, one choice question:

- interrupt: said to the AIs, so the reply stops
- aside: said to someone else, or to nobody, so it carries on
- listening: only a "yeah" or a "wow", so it carries on

The trial harness (eval_bargein) sends the same description, so it measures
exactly what the app asks. On 136 made-up moments, with the speaker unknown
as here, Jev behind the word rules cut 11 of 89 replies not meant for the
AIs, against 53 for the rules alone, and missed 1 of 47 real interruptions.

What goes to TypeSafe: the words said over the reply, the last thing the AI
said, the seats' names and the names of the people in the room. Nothing is
kept here but the verdict, the confidence and the time it took. With no
`TYPESAFE_API_KEY`, `interrupt_judge` off, or an answer slower than the
timeout, the browser falls back to the word rules.
"""

import logging
import os
import time

import httpx

log = logging.getLogger("crossband.judge")

JEV_URL = "https://api.typesafe.ai/v1/systemone"
JEV_MODEL = "jev-latest"
LABELS = ("interrupt", "aside", "listening")
TIMEOUT_S = 1.0
# The browser sends no more than this of each, and anything longer is cut.
MAX_WORDS_CHARS = 300
MAX_SAID_CHARS = 400

CRITERIA = {
    "interrupt": "Said to the AIs: a question, an answer, a correction, or "
                 "asking them to stop. The AI should stop and listen.",
    "aside": "Not said to the AIs: talk to someone else in the room, the TV "
             "or radio, or a remark to nobody. The AI should carry on.",
    "listening": "Only a sign of listening or a reaction, like 'yeah', "
                 "'mm-hm' or 'wow'. The AI should carry on.",
}

SOMEONE = "someone in the room (the app can't tell who yet)"


def describe(owner, seats, room, seat, ai_said, who, words) -> str:
    """The moment as plain text: what Jev reads."""
    room_line = (f"Also in the room: {', '.join(room)}." if room
                 else "Nobody else is in the room.")
    return (
        f"A voice chat at home. {owner} is talking out loud with AI assistants "
        f"({', '.join(seats)}). {room_line}\n"
        f"{seat} was in the middle of saying: \"{ai_said}\"\n"
        f"While it spoke, {who} said: \"{words}\""
    )


def question(seat) -> str:
    return f"Was that said to the AIs, so that {seat} should stop and listen?"


def jev_body(state, seat, model=JEV_MODEL) -> dict:
    """The request Jev takes: the moment as its state, and one choice."""
    return {"state": state, "model": model,
            "questions": {"verdict": {"type": "choice",
                                      "instructions": question(seat),
                                      "criteria": dict(CRITERIA)}}}


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


def api_key() -> str:
    return os.environ.get("TYPESAFE_API_KEY", "").strip()


def off_reason(cfg) -> str:
    """Why the judge won't be asked, or '' when it will."""
    if str(cfg.get("interrupt_judge", "jev")).lower() != "jev":
        return "off"
    if not api_key():
        return "no_key"
    return ""


async def ask(state, seat, timeout_s=TIMEOUT_S, client=None) -> dict:
    """Ask Jev about one moment. Returns {verdict, confidence, model, usage,
    ms}, with verdict '' when there was no usable answer in time."""
    t0 = time.monotonic()
    out = {"verdict": "", "confidence": None, "model": JEV_MODEL, "usage": {}, "ms": 0}
    own = client is None
    client = client or httpx.AsyncClient(timeout=timeout_s)
    try:
        r = await client.post(JEV_URL, json=jev_body(state, seat),
                              headers={"Authorization": f"Bearer {api_key()}"},
                              timeout=timeout_s)
        r.raise_for_status()
        data = r.json()
        out["verdict"], out["confidence"] = parse_jev(data)
        out["model"] = str(data.get("model") or JEV_MODEL)
        out["usage"] = data.get("usage") or {}
    except Exception as e:  # any failure leaves the word rules to decide
        out["error"] = type(e).__name__
    finally:
        if own:
            await client.aclose()
        out["ms"] = round((time.monotonic() - t0) * 1000)
    return out
