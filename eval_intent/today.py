"""What the live scan hears today: four phrase lists, each gating one
prompt, and no research axis at all. The harness runs this path over the
same fixtures so the merged call is compared against what ships, not
against nothing."""

from backend import depth as depth_mod
from backend import introductions as intro
from eval_intent.schema import Fixture, empty_verdict

AXIS_PREFILTER = {
    "mode_command": intro.command_prefilter,
    "introductions": intro.prefilter,
    "corrections": intro.correction_prefilter,
    "depth": depth_mod.depth_prefilter,
}


def prefilters_fired(text: str) -> set:
    """Which of today's four lists would even send this turn to a model."""
    return {axis for axis, pre in AXIS_PREFILTER.items() if pre(text)}


def silent_misses(fx: Fixture) -> list:
    """The axes on which this turn carries an instruction that today's lists
    never send to a model, so the miss is silent. Introductions and
    departures share one list; research has no path today."""
    fired = prefilters_fired(fx.text)
    e = fx.expected
    out = []
    if e["mode_command"] != "none" and "mode_command" not in fired:
        out.append("mode_command")
    if (e["introductions"] or e["departures"]) and "introductions" not in fired:
        out.append("introductions")
    if e["corrections"] and "corrections" not in fired:
        out.append("corrections")
    if e["depth"] and "depth" not in fired:
        out.append("depth")
    if e["research"] != "none":
        out.append("research")
    return out


def today_prompts(fx: Fixture) -> dict:
    """axis -> prompt, for the lists that fire on this turn."""
    fired = prefilters_fired(fx.text)
    prompts = {}
    if "mode_command" in fired:
        prompts["mode_command"] = intro.build_command_prompt(fx.text)
    if "introductions" in fired:
        prompts["introductions"] = intro.build_prompt(
            fx.text, fx.user_name, fx.present, participant_names=fx.seats)
    if "corrections" in fired:
        prompts["corrections"] = intro.build_correction_prompt(
            fx.text, fx.user_name, fx.known)
    if "depth" in fired:
        prompts["depth"] = depth_mod.build_depth_prompt(fx.text, fx.seats)
    return prompts


def merge_today(replies: dict) -> dict:
    """axis -> reply text, into the verdict shape."""
    out = empty_verdict()
    if "mode_command" in replies:
        v = intro.parse_command_verdict(replies["mode_command"])
        out["mode_command"] = {intro.COMMAND_ARM: "on",
                               intro.COMMAND_DISARM: "off"}.get(v, "none")
    if "introductions" in replies:
        names = intro.parse_verdict(replies["introductions"])
        out["introductions"] = names["introductions"]
        out["departures"] = names["departures"]
        out["aliases"] = names["aliases"]
    if "corrections" in replies:
        out["corrections"] = intro.parse_correction_verdict(replies["corrections"])
    if "depth" in replies:
        out["depth"] = depth_mod.parse_depth_verdict(replies["depth"])
    return out
