"""Spoken control of per-seat reasoning depth (#105, slice 1).

The owner's framing: latency and intelligence should stop being one knob.
A spoken cue sets a PERSISTENT per-chat, per-seat reasoning depth ("slow
down, think harder", "quick answers from now on") that holds until
explicitly changed ("back to normal") - no automatic de-escalation, since
a hard question unfolds over several turns. Addressed by name it moves
one seat; unaddressed it moves every seat in the chat.

Same machinery as room commands (introductions.py): a cheap lexical
prefilter decides whether to spend one utility-model call, the model is
the judge of intent, apply writes durable state and says what changed.
Nothing here reads the transcript beyond the one turn being scanned, and
nothing persisted contains the turn's text.

The stored value IS a provider `reasoning_effort` level ('low' | 'high' |
'max'); providers.py already translates per vendor (OpenAI caps max at
high) and gates models that reject effort, so a stored level can never
400 a seat that its configured default would not.
"""

import json
import logging
import re

from . import db

log = logging.getLogger("crossband.depth")

# Spoken level -> stored reasoning_effort. "normal" clears the row: the
# seat returns to its configured default, whatever that is.
DEPTH_LEVELS = {"deep": "high", "quick": "low", "max": "max", "normal": ""}

# What a stored level is called when spoken about (notices, prompt note).
LEVEL_WORDS = {"high": "deep", "low": "quick", "max": "maximum"}

# Over-inclusive on purpose; the utility model confirms intent. Bounded
# head, same as every other scan prefilter.
_DEPTH_RE = re.compile(
    r"(?i)\b(?:"
    r"think(?:ing)?\s+(?:hard(?:er)?|deep(?:er)?|more|less|quick(?:ly)?|fast(?:er)?|longer)"
    r"|(?:reasoning|thinking)\s+(?:depth|effort|budget|level)"
    r"|take\s+your\s+time"
    r"|go(?:ing)?\s+deep(?:er)?"
    r"|slow\s+down"
    r"|(?:quick|fast|short|snappy)\s+(?:answers?|repl(?:y|ies))"
    r"|back\s+to\s+normal"
    r"|max(?:imum)?\s+(?:effort|thinking|reasoning)"
    r")\b")


def depth_prefilter(text: str) -> bool:
    """Is this turn shaped like a depth instruction, worth one utility-model
    call?"""
    head = (text or "")[:600]
    return bool(head.strip()) and bool(_DEPTH_RE.search(head))


def build_depth_prompt(text: str, seat_names: list) -> str:
    """The depth-confirmation prompt. The transcript text stays in the
    request, never in anything persisted."""
    seats = ", ".join(seat_names) or "(none)"
    return (
        "You watch one message from a conversation with several AI "
        "assistants and decide whether the human is INSTRUCTING a change "
        "to how hard an assistant should think from now on. The seats are: "
        f"{seats}.\n"
        "Depth words: 'deep' means think harder / take your time / slow "
        "down; 'quick' means faster, shallower answers; 'max' means the "
        "hardest possible thinking; 'normal' means back to the default. "
        "A named seat means that seat; no name means every seat. Scope: an "
        "instruction limited to the next reply ('just answer this one "
        "quickly', 'think hard about just this next one') is a ONE-OFF - "
        "set \"once\": true; a standing instruction ('from now on', "
        "'until I say otherwise', or no limit stated) is not. Merely "
        "DISCUSSING thinking is not an instruction: a question ('are you "
        "thinking hard?'), praise ('good thinking'), or asking for more "
        "thought about a TOPIC in this one answer ('think harder about "
        "whether X is true') must return no changes.\n"
        "Reply with ONLY JSON: {\"changes\": [{\"seat\": \"<seat name or "
        "all>\", \"depth\": \"deep\"|\"quick\"|\"max\"|\"normal\", "
        "\"once\": true|false}]} - an empty list when nothing is "
        "instructed.\n\n"
        f"Message: {text[:600]}"
    )


def parse_depth_verdict(text) -> list:
    """Parse the utility model's verdict, defensively: anything that is not
    the documented shape degrades to no changes rather than raising. Returns
    a bounded list of {"seat": str, "depth": "deep"|"quick"|"max"|"normal"}."""
    if not text:
        return []
    m = re.search(r"\{.*\}", text, re.DOTALL)
    if not m:
        return []
    try:
        data = json.loads(m.group(0))
    except json.JSONDecodeError:
        return []
    if not isinstance(data, dict) or not isinstance(data.get("changes"), list):
        return []
    out = []
    for ch in data["changes"][:8]:
        if not isinstance(ch, dict):
            continue
        seat = ch.get("seat")
        depth = ch.get("depth")
        if isinstance(seat, str) and seat.strip() and depth in DEPTH_LEVELS:
            out.append({"seat": seat.strip()[:80], "depth": depth,
                        "once": ch.get("once") is True})
    return out


def _notice(seat_name, depth, user):
    if depth == "normal":
        return (f"{seat_name} is back to its configured thinking depth "
                f"(spoken depth cleared by {user}).")
    word = LEVEL_WORDS[DEPTH_LEVELS[depth]]
    trade = ("replies here will take longer" if depth in ("deep", "max")
             else "replies here will be faster and shallower")
    return (f"{seat_name} set to {word} thinking - {trade} until "
            f"{user} says back to normal.")


def apply_depth(chat_id, changes, cfg) -> str:
    """Apply confirmed depth changes (synchronous; worker thread). Resolves
    seat names against the CHAT's participants (name or slug, spoken case),
    'all' meaning every one of them. Writes chat_seat_state, and inserts one
    system notice per real change so the transcript shows the trade the
    moment it is made. Unknown seat names change nothing - a misheard name
    must not move a different seat. Returns the scan outcome word."""
    if not changes:
        return "no_change"
    user = cfg.get("user_name", "User")
    changed = cleared = 0
    con = db.connect()
    try:
        roster = db.get_chat_participants(con, chat_id)
        by_key = {}
        for p in roster:
            by_key[p["slug"].casefold()] = p
            by_key[(p["name"] or "").casefold()] = p
        current = db.get_chat_seat_state(con, chat_id)
        once_set = 0
        for ch in changes:
            key = ch["seat"].casefold()
            targets = roster if key == "all" else \
                ([by_key[key]] if key in by_key else [])
            if not targets:
                log.info("depth change named no known seat: chat=%s", chat_id)
            for p in targets:
                effort = DEPTH_LEVELS[ch["depth"]]
                if ch.get("once"):
                    # Slice 2: one reply only. Consumed by the seat's next
                    # call, so no mode notice - nothing persistent changed
                    # and the effect is over by the time anyone reads it.
                    if effort:
                        db.set_chat_seat_once(con, chat_id, p["slug"], effort)
                        once_set += 1
                    continue  # "normal, just this once" instructs nothing
                if current.get(p["slug"], "") == effort:
                    continue  # already there - no state write, no notice
                if not effort and p["slug"] not in current:
                    continue  # clearing a seat already at default
                db.set_chat_seat_depth(con, chat_id, p["slug"], effort)
                current[p["slug"]] = effort
                db.insert_message(con, chat_id, "system",
                                  _notice(p["name"] or p["slug"],
                                          ch["depth"], user))
                if effort:
                    changed += 1
                else:
                    cleared += 1
    finally:
        con.close()
    if changed:
        return "depth_set"
    if cleared:
        return "depth_cleared"
    if once_set:
        return "depth_once"
    return "no_change"


def once_note(level, user) -> str:
    """The volatile prompt note for a consumed one-reply override (#105
    slice 2) - scoped to THIS reply so the seat neither adopts it as a mode
    nor announces a change of one."""
    if not level:
        return ""
    word = LEVEL_WORDS.get(level, level)
    return (f"\n## Your reasoning depth (THIS reply only)\n{user} asked for "
            f"{word} thinking for just this one reply. It applies now and "
            f"reverts by itself - do not treat it as a standing mode.")


def depth_note(level, user) -> str:
    """The volatile prompt note telling a seat its own current spoken depth
    (engine threads it per seat). Empty level = no note."""
    if not level:
        return ""
    word = LEVEL_WORDS.get(level, level)
    return (f"\n## Your reasoning depth (this chat)\n{user} set your "
            f"thinking to {word} for this conversation. It persists until "
            f"they change it (\"back to normal\" clears it). If asked how "
            f"hard you are thinking, say this honestly.")
