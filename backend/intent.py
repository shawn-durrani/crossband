"""The merged intent scan (#258, #412): one utility call per user turn that
reads every instruction the live scan acts on - a room-mode command, an
introduction or departure (with aliases), a name correction, a reasoning-
depth change, a research cue (#253/#417), an explicit ask for a stronger
model (#254), and while the app asks who a new voice is, "that's the TV"
(#523) - and returns them all in one JSON verdict.

The prompt and the parser live here, not in eval_intent, so the harness that
justified the switch (`python -m eval_intent`) and the live scan
(introductions.scan_user_turn) share exactly one judge forever: a prompt fix
happens in one place, and the harness can never silently drift from what the
app actually sends.

RESEARCH_MORE is applied by backend/research.py (introductions.py: outcome
"research_set"). A research cue never asks for a different model (the
owner's decision of 27 September, which replaces the one of 4 September):
only `stronger_model`, or a standing "think harder" on the depth axis, sends
a seat to model_step.step_up.
"""

import json
import re

RESEARCH_MORE = "more"


# The most seats one stronger-model ask can name. A chat has a handful.
MAX_MODEL_SEATS = 8


def empty_verdict() -> dict:
    return {"mode_command": "none", "introductions": [], "departures": [],
            "aliases": {}, "corrections": [], "depth": [], "research": "none",
            "stronger_model": [], "media": False}


# While the app is asking who a new voice is (#523), the turn may be the
# answer. Only then does the prompt say so: every other turn gets the prompt
# exactly as it was, so an answer's wording can't change how anything else
# is heard.
ASKING_NOTE = (
    "The app has just asked who a new voice in the room is, so the message "
    "may answer it. An answer saying who the voice is ('that's Dave', 'that "
    "was my brother Dave', 'it's Dave') introduces that name. An answer "
    "saying the voice isn't a person but a TV, radio, video, podcast or "
    "other recording ('that's the TV', 'it's just the radio', 'ignore "
    "that, it was a video') introduces nobody: add \"media\": true to the "
    "reply, whatever else it asks.\n")


def build_merged_prompt(text: str, user_name: str, seat_names: list,
                        present_names: list, known_names: list,
                        asking: bool = False) -> str:
    """The one prompt every user turn goes to. `seat_names` are the chat's AI
    participants, `present_names` the roster's present people, `known_names`
    every name the app might resolve a correction against - the same three
    inputs the four prompts this replaces took, gathered into one call.
    `asking` is whether an open "who's this?" ask points at a turn, which
    adds ASKING_NOTE and nothing else."""
    seats = ", ".join(seat_names) or "(none)"
    present = ", ".join(present_names) if present_names else "(nobody yet)"
    known = ", ".join(known_names) if known_names else "(nobody yet)"
    return (
        "You watch one message from a conversation between a person and "
        f"several AI assistants, sometimes with other people in the room. "
        f"The device owner is {user_name}. The assistants are: {seats}. "
        f"People already known present: {present}. People known by name: "
        f"{known}.\n"
        + (ASKING_NOTE if asking else "") +
        "Decide, all at once, which of these the message does. Merely "
        "talking ABOUT any of them (a question, praise, a recollection, a "
        "mention in passing) counts for none of them. Asking the "
        "assistants to HOLD BACK counts for none of them either, wherever "
        "it sits in the message: 'you don't need to respond', 'just "
        "listen', 'eavesdrop', 'stay quiet', 'silent mode', 'listening "
        "mode', 'don't answer unless we ask' are about whether the "
        "assistants reply, which they handle themselves. Anything else "
        "the same message asks still counts.\n"
        "1. mode_command: does it ask BY NAME to switch ROOM MODE (group, "
        "multi-user or multi-person mode: several people sharing one "
        "microphone) on or off? Requests and announcements both count "
        "('group mode please', 'we're in group mode now' mean on). \"off\" "
        "needs an unambiguous statement: solo mode or room mode off by "
        "name, or the owner saying they are alone now ('it's just me "
        "now', 'everyone's gone home'). Any other kind of 'mode' "
        "('eavesdropping mode', 'quiet mode', 'listening mode') is a hold "
        "back request, not room mode. Introducing a person, saying "
        "someone is here, or people talking among themselves ('we're "
        "just talking', 'we're chatting between ourselves') is NOT a mode "
        "command, even though it implies company: the app switches the "
        "room on by itself when someone is introduced, so return "
        "\"none\" unless the mode is named. Talking among themselves "
        "also says people ARE here, the opposite of alone. When unsure, "
        "\"none\": a wrong \"off\" empties the room and stops the app "
        "noticing new voices. Otherwise \"none\".\n"
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
        "3. corrections: does it CORRECT what a PERSON is called (how "
        "their name is spelt, or the name to use for them), or DECLARE two "
        "forms of one person's name (a spelling plus how it is "
        "pronounced)? Messages are often spoken and transcribed, and "
        "people spell out a word the transcript got wrong. Most spelt out "
        "words are not names, and spelling one out is a transcript fix, "
        "never a correction: 'no, no, M-O-R-T-I-S-E', 'I said chisel, "
        "C-H-I-S-E-L', 'Alex, it's spelt R-O-U-T-E-R, the tool' all "
        "return no correction, whoever the message is spoken to. Saying "
        "a person's name first ('Alex, it's spelt ...') does not make the "
        "spelt word their name, and a word the message calls a thing "
        "('the tool', 'as in the ...', 'like the ...') is never a name. "
        "Only a word that is a person's name counts: 'no, it's Dave, "
        "D-A-V-E' "
        "and 'her name is spelt D-A-Y-V' do. Introducing someone with "
        "their name spelt out is an introduction, not a correction. When "
        "unsure whether a spelt word is a person's name, no correction. "
        "`name` is the corrected name as one ordinary word, with spelt "
        "out letters joined ('D-A-V-E' is Dave), `also` a genuinely "
        "different second form or \"\" (never the same name spelt out), "
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
        "deeper', 'do some research', 'research this properly', 'find me "
        "an answer', 'can you look it up?')? 'Go deeper' and 'dig deeper' "
        "are research requests, not depth changes, unless the message also "
        "says how hard to think. 'Look it up' about the question being "
        "discussed counts. A request to use a tool once for something the "
        "message names ('can you search for a flight', 'look up the "
        "weather') is not. Research never asks for a different model. "
        "\"more\" or \"none\".\n"
        "6. stronger_model: does it ask an assistant to use a stronger, "
        "better or its best MODEL ('use your best model', 'use a stronger "
        "model')? A named assistant means that one, \"all\" when no name "
        "is given. Only an ask that says model counts.\n"
        "Reply with ONLY JSON: {\"mode_command\": \"on\"|\"off\"|\"none\", "
        "\"introductions\": [names], \"departures\": [names], \"aliases\": "
        "{name: preferred}, \"corrections\": [{\"who\": ..., \"name\": ..., "
        "\"also\": ...}], \"depth\": [{\"seat\": \"<assistant or all>\", "
        "\"depth\": \"deep\"|\"quick\"|\"max\"|\"normal\", \"once\": "
        "true|false}], \"research\": \"more\"|\"none\", "
        "\"stronger_model\": [\"<assistant or all>\"]}. Empty lists, "
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


def parse_merged(text, message="") -> dict:
    """One reply to the merged prompt into the verdict shape, reusing the
    app's own per-axis parsers (introductions.py, depth.py) so this is the
    same judge the harness measured. Anything off-shape degrades to nothing
    heard on that axis, never to an error. Imports the two modules lazily:
    both import this one back (introductions.py builds and parses the
    merged prompt), so a top-level import would cycle.

    `message` is the turn the reply is about. With it, a correction in a
    turn that spells a word out has to be marked as a name by the turn
    itself (introductions.keep_name_corrections, #494), and a room-mode
    "off" in a turn that only says someone will be back soon is
    dropped (introductions.keep_disarm, #540). The live scan and the
    harness both pass it, so both measure the same rules."""
    from . import depth as depth_mod
    from . import introductions as intro
    out = empty_verdict()
    data = _json_object(text)
    if data is None:
        return out
    mode = {intro.COMMAND_ARM: "on", intro.COMMAND_DISARM: "off", "": "none"}
    out["mode_command"] = mode[intro.keep_disarm(
        intro.parse_command_verdict(text), message)]
    names = intro.parse_verdict(text)
    out["introductions"] = names["introductions"]
    out["departures"] = names["departures"]
    out["aliases"] = names["aliases"]
    out["corrections"] = intro.keep_name_corrections(
        intro.parse_correction_verdict(text), message)
    if isinstance(data.get("depth"), list):
        out["depth"] = depth_mod.parse_depth_verdict(
            json.dumps({"changes": data["depth"]}))
    out["research"] = RESEARCH_MORE if data.get("research") == RESEARCH_MORE \
        else "none"
    out["stronger_model"] = parse_model_seats(data.get("stronger_model"))
    # #523: only a turn the model was told might answer the ask can say
    # the voice is a TV, and only a plain true counts.
    out["media"] = data.get("media") is True
    return out


def parse_model_seats(raw) -> list:
    """The seats a stronger-model ask names, as spoken, once each: "all" or
    assistant names. A bare string counts as one name, and an object with a
    "seat" key (the depth shape) is read the same way. Anything else is
    nothing heard, never an error. The names resolve against the chat's own
    seats later (model_step.targets), so a misheard one moves nobody."""
    if isinstance(raw, str):
        raw = [raw]
    if not isinstance(raw, list):
        return []
    out = []
    for item in raw:
        if isinstance(item, dict):
            item = item.get("seat")
        if not isinstance(item, str):
            continue
        name = item.strip()
        if name and name.casefold() not in {n.casefold() for n in out}:
            out.append(name)
        if len(out) >= MAX_MODEL_SEATS:
            break
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


# ---------- the "heard and changed" line for room mode ----------
#
# The 25 September field test: the scan read "just eavesdrop" as the
# solo-mode disarm, room mode went off, everyone left the roster and
# automatic re-arming stopped, and nothing in the chat said so. The owner
# found out only when a seat told them the room was off. A spoken room-mode
# change now always posts one line: what changed, and the words that undo
# it. Depth and research changes already post theirs (depth._notice,
# research._on_notice); room mode was the one silent axis. Built from what
# the apply call changed, never from the turn's words, so no transcript text
# reaches a persisted row.

def mode_changed_line(direction, *, was_on=False, cleared_roster=False) -> str:
    """The line for a spoken room-mode command that changed something.
    `direction` is "on" or "off". For "off", `was_on` says whether the room
    itself flipped (False: it was already off and only the automatic
    re-arming stopped) and `cleared_roster` whether anyone was taken off the
    room list."""
    if direction == "on":
        return ("Heard an instruction to turn room mode on, so it's on now. "
                'Say "room mode off" to turn it off.')
    if was_on:
        gone = " and nobody is listed in the room" if cleared_roster else ""
        return ("Heard an instruction to turn room mode off, so it's off "
                f"now{gone}. It won't switch itself back on when it hears "
                'another voice. Say "room mode on" to turn it back on.')
    return ("Heard an instruction to turn room mode off. It was already off, "
            "and now it won't switch itself back on when it hears another "
            'voice. Say "room mode on" to undo that.')


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


def _media_line() -> str:
    return ("Heard that a voice is a TV or radio, and nothing changed: the "
            "app wasn't asking about one new voice.")


def _model_line() -> str:
    return ("Heard a request for a stronger model, and nothing changed: "
            "the seats named are already on one for this chat, or can't "
            "be moved.")


def nothing_changed_line(verdict: dict, outcomes: dict) -> str:
    """The plain-words line for a confirmed instruction that changed
    nothing, or "" when there is nothing to say - no instruction was heard
    at all, or ANY confirmed axis actually changed something. `outcomes`
    maps the axes the scan applied - "mode_command", "introductions",
    "corrections", "depth", "research", "stronger_model", "media" - to the
    outcome
    string apply_command / apply_scan / apply_corrections / apply_depth /
    research.apply_research / model_step.step_up / voice_ask.answer_media
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
        ("stronger_model", bool(verdict.get("stronger_model")), _model_line),
        ("media", verdict.get("media") is True, _media_line),
    )
    if outcomes.get("model") == "model_stepped":
        return ""  # #254: a depth cue or a stronger-model ask moved a model
    first_no_op = None
    for key, instructed, line_fn in axes:
        if not instructed:
            continue
        if outcomes.get(key) not in _NOTHING_CHANGED:
            return ""  # something changed somewhere: no line is owed
        if first_no_op is None:
            first_no_op = line_fn
    return first_no_op() if first_no_op else ""
