"""The merged intent scan (#258, #412): one utility call per user turn that
reads every instruction the live scan acts on - a room-mode command, an
introduction or departure (with aliases), a name correction, a reasoning-
depth change, a research cue (#253/#417) - and returns them all in one JSON
verdict.

The prompt and the parser live here, not in eval_intent, so the harness that
justified the switch (`python -m eval_intent`) and the live scan
(introductions.scan_user_turn) share exactly one judge forever: a prompt fix
happens in one place, and the harness can never silently drift from what the
app actually sends.

RESEARCH_MORE is applied by backend/research.py (introductions.py: outcome
"research_set").
"""

import json
import re

RESEARCH_MORE = "more"


def empty_verdict() -> dict:
    return {"mode_command": "none", "introductions": [], "departures": [],
            "aliases": {}, "corrections": [], "depth": [], "research": "none"}


def build_merged_prompt(text: str, user_name: str, seat_names: list,
                        present_names: list, known_names: list) -> str:
    """The one prompt every user turn goes to. `seat_names` are the chat's AI
    participants, `present_names` the roster's present people, `known_names`
    every name the app might resolve a correction against - the same three
    inputs the four prompts this replaces took, gathered into one call."""
    seats = ", ".join(seat_names) or "(none)"
    present = ", ".join(present_names) if present_names else "(nobody yet)"
    known = ", ".join(known_names) if known_names else "(nobody yet)"
    return (
        "You watch one message from a conversation between a person and "
        f"several AI assistants, sometimes with other people in the room. "
        f"The device owner is {user_name}. The assistants are: {seats}. "
        f"People already known present: {present}. People known by name: "
        f"{known}.\n"
        "Decide, all at once, which of these the message does. Merely "
        "talking ABOUT any of them (a question, praise, a recollection, a "
        "mention in passing) counts for none of them.\n"
        "1. mode_command: does it ask BY NAME to switch ROOM MODE (group, "
        "multi-user or multi-person mode: several people sharing one "
        "microphone) on or off? Requests and announcements both count "
        "('group mode please', 'we're in group mode now' mean on; 'solo "
        "mode', 'it's just me now' mean off). Introducing a person or "
        "saying someone is here is NOT a mode command, even though it "
        "implies company: the app switches the room on by itself when "
        "someone is introduced, so return \"none\" unless the mode is "
        "named. Otherwise \"none\".\n"
        "2. introductions and departures: does it INTRODUCE another human "
        "who is physically present and may speak, or ANNOUNCE that a "
        "present person has left? The owner introducing someone, a guest "
        "introducing themselves, and a handover to someone about to speak "
        "all count. Talking about someone not in the room does not. Use "
        "the proper name as spoken, never the relationship word when a "
        "name is given; a relationship-only introduction returns the "
        "relationship word itself. A stated short form ('call me Sam') "
        f"goes in aliases keyed by the name. Never return {user_name} "
        "or an assistant.\n"
        "3. corrections: does it CORRECT how a person's name is spelt or "
        "what they should be called, or DECLARE two forms of one name (a "
        "spelling plus how it is pronounced)? `name` is the corrected "
        "name as the message spells it, `also` the second form or \"\", "
        "`who` one of the known names, \"owner\" when the speaker corrects "
        "their own name, or \"\" when the message does not say.\n"
        "4. depth: does it INSTRUCT a change to how hard an assistant "
        "should think from now on? 'deep' means think harder, take your "
        "time, slow down; 'quick' means faster, shorter, shallower "
        "answers, whatever the wording; 'max' means the hardest possible "
        "thinking; 'normal' means back to the default, whatever the "
        "wording ('back to normal', 'return to defaults', 'reset'). A named "
        "assistant means that one, no name means all. An instruction "
        "limited to the next reply is a one-off: \"once\": true. Asking "
        "for more thought about a TOPIC within one answer is not an "
        "instruction.\n"
        "5. research: does it ask the assistants to research or look into "
        "things more thoroughly ('research more', 'look into that "
        "properly', 'search more', 'dig into this', 'dig deeper', 'go "
        "deeper')? 'Go deeper' and 'dig deeper' are research requests, "
        "not depth changes, unless the message also says how hard to "
        "think. A request to use a tool once ('can you search for a "
        "flight', 'look up the weather') is not. \"more\" or \"none\".\n"
        "Reply with ONLY JSON: {\"mode_command\": \"on\"|\"off\"|\"none\", "
        "\"introductions\": [names], \"departures\": [names], \"aliases\": "
        "{name: preferred}, \"corrections\": [{\"who\": ..., \"name\": ..., "
        "\"also\": ...}], \"depth\": [{\"seat\": \"<assistant or all>\", "
        "\"depth\": \"deep\"|\"quick\"|\"max\"|\"normal\", \"once\": "
        "true|false}], \"research\": \"more\"|\"none\"}. Empty lists, "
        "\"none\" and {} when the message does none of it.\n\n"
        f"Message: {text[:1200]}"
    )


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
    """One reply to the merged prompt into the verdict shape, reusing the
    app's own per-axis parsers (introductions.py, depth.py) so this is the
    same judge the harness measured. Anything off-shape degrades to nothing
    heard on that axis, never to an error. Imports the two modules lazily:
    both import this one back (introductions.py builds and parses the
    merged prompt), so a top-level import would cycle."""
    from . import depth as depth_mod
    from . import introductions as intro
    out = empty_verdict()
    data = _json_object(text)
    if data is None:
        return out
    mode = {intro.COMMAND_ARM: "on", intro.COMMAND_DISARM: "off", "": "none"}
    out["mode_command"] = mode[intro.parse_command_verdict(text)]
    names = intro.parse_verdict(text)
    out["introductions"] = names["introductions"]
    out["departures"] = names["departures"]
    out["aliases"] = names["aliases"]
    out["corrections"] = intro.parse_correction_verdict(text)
    if isinstance(data.get("depth"), list):
        out["depth"] = depth_mod.parse_depth_verdict(
            json.dumps({"changes": data["depth"]}))
    out["research"] = RESEARCH_MORE if data.get("research") == RESEARCH_MORE \
        else "none"
    return out


# ---------- the "heard but changed nothing" line (#412) ----------
#
# A miss must never be silent again (#258, the 15 September session): when
# the merged verdict holds at least one instruction and every apply call
# returned "no_change" (or its per-axis equivalent), one system line says
# what was heard and that nothing changed. Built from the OUTCOME each apply
# returned, never from the turn's own words, so no transcript text ever
# reaches a persisted system row.

# Outcomes that mean "heard, confirmed, and changed nothing" - the set
# nothing_changed_line and scan_user_turn both need agree on.
_NOTHING_CHANGED = {"no_change", "correction_unmatched"}


def _mode_line(direction) -> str:
    state = "on" if direction == "on" else "off"
    return (f"Heard an instruction to turn room mode {state}, and nothing "
            f"changed: the room was already {state}.")


def _intro_line(verdict) -> str:
    if verdict.get("departures") and not verdict.get("introductions"):
        return ("Heard an announcement that someone left, and nothing "
                "changed: nobody by that name was present.")
    return ("Heard an introduction, and nothing changed: that person is "
            "already present.")


def _correction_line() -> str:
    return ("Heard a name correction, and nothing changed: it did not "
            "resolve to anyone the app knows.")


def _depth_line() -> str:
    return ("Heard an instruction to change thinking depth, and nothing "
            "changed: the seats named are already at that depth, or no "
            "known seat was named.")


def _research_line() -> str:
    return ("Heard a request to research more, and nothing changed: "
            "research mode is already on for this chat.")


def nothing_changed_line(verdict: dict, outcomes: dict) -> str:
    """The plain-words line for a confirmed instruction that changed
    nothing, or "" when there is nothing to say - no instruction was heard
    at all, or ANY confirmed axis actually changed something. `outcomes`
    maps the axes the scan applied - "mode_command", "introductions",
    "corrections", "depth", "research" - to the outcome string apply_command
    / apply_scan / apply_corrections / apply_depth / research.apply_research
    returned.

    A turn that instructs on more than one axis at once only gets the line
    when EVERY instructed axis was a no-op; the wording then names the
    first one, in the order the scan applies them - a real change on any
    axis means the verdict line already reports it, and no line is owed."""
    axes = (
        ("mode_command", verdict.get("mode_command", "none") != "none",
         lambda: _mode_line(verdict["mode_command"])),
        ("introductions",
         bool(verdict.get("introductions") or verdict.get("departures")),
         lambda: _intro_line(verdict)),
        ("corrections", bool(verdict.get("corrections")), _correction_line),
        ("depth", bool(verdict.get("depth")), _depth_line),
        ("research", verdict.get("research") == RESEARCH_MORE, _research_line),
    )
    if outcomes.get("model") == "model_stepped":
        return ""  # #254: the depth or research cue moved a model - a change
    first_no_op = None
    for key, instructed, line_fn in axes:
        if not instructed:
            continue
        if outcomes.get(key) not in _NOTHING_CHANGED:
            return ""  # something changed somewhere: no line is owed
        if first_no_op is None:
            first_no_op = line_fn
    return first_no_op() if first_no_op else ""
