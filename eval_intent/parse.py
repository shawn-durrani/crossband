"""Turns a model's reply into the verdict shape, reusing the app's own
parsers so the harness judges replies the way the live scan would. Anything
off-shape degrades to nothing heard on that axis, never to an error."""

import json
import re

from backend import depth as depth_mod
from backend import introductions as intro
from eval_intent.schema import empty_verdict

_MODE = {intro.COMMAND_ARM: "on", intro.COMMAND_DISARM: "off", "": "none"}


def _json_object(text):
    if not text:
        return None
    m = re.search(r"\{.*\}", text, re.DOTALL)
    if not m:
        return None
    try:
        data = json.loads(m.group(0))
    except json.JSONDecodeError:
        return None
    return data if isinstance(data, dict) else None


def parse_merged(text) -> dict:
    """One reply to the merged prompt into the verdict shape."""
    out = empty_verdict()
    data = _json_object(text)
    if data is None:
        return out
    out["mode_command"] = _MODE[intro.parse_command_verdict(text)]
    names = intro.parse_verdict(text)
    out["introductions"] = names["introductions"]
    out["departures"] = names["departures"]
    out["aliases"] = names["aliases"]
    out["corrections"] = intro.parse_correction_verdict(text)
    if isinstance(data.get("depth"), list):
        out["depth"] = depth_mod.parse_depth_verdict(
            json.dumps({"changes": data["depth"]}))
    out["research"] = "more" if data.get("research") == "more" else "none"
    return out
