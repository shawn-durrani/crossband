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
    # #305: "use your fastest reasoning setting" reached no branch above,
    # so no check ran and the seat guessed. The superlatives and the word
    # "setting" are shapes people reach for when they know the knob.
    r"|(?:fastest|quickest|slowest|lowest|highest|least|most)\s+"
    r"(?:reasoning|thinking|effort)"
    r"|(?:reasoning|thinking|effort)\s+setting"
    r")\b")


def depth_prefilter(text: str) -> bool:
    """Is this turn shaped like a depth instruction, worth one utility-model
    call?

    Unused by the live scan since #412 (every turn gets one merged call
    instead); kept because eval_intent/today.py measures it against the
    merged path."""
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


def _notice(seat_name, depth, user, dropped_once=""):
    """The transcript line for one real change. `user` is who spoke the cue
    as resolve_speaker found them, or '' when the app could not say (#255):
    the line then names nobody rather than the wrong person. Anyone can
    clear a depth, so the line never says who has to."""
    by = f" by {user}" if user else ""
    if depth == "normal":
        text = (f"{seat_name} is back to its configured thinking depth "
                f"(spoken depth cleared{by}).")
        if dropped_once:
            word = LEVEL_WORDS.get(dropped_once, dropped_once)
            text += (f" The {word}-thinking override parked for its next "
                     "reply was dropped too.")
        return text
    word = LEVEL_WORDS[DEPTH_LEVELS[depth]]
    trade = ("replies here will take longer" if depth in ("deep", "max")
             else "replies here will be faster and shallower")
    return (f"{seat_name} set to {word} thinking{by} - {trade} until "
            "someone says back to normal.")


def _people():
    """The anchor store's people, or [] when voice identity is not set up."""
    try:
        from . import anchors
        return anchors.store().people()
    except Exception:
        return []


def resolve_speaker(con, chat_id, message_id, cfg) -> str:
    """Who spoke the turn that carried a depth cue, as the name to print,
    or '' when the app cannot say (#255). Resolved late, in apply_depth's
    worker thread, so a label attached a moment after insert is seen.

    The rule is the memory path's (memory_client.ingest_speaker), so the
    transcript credits exactly whom the ledger would: an open attribution
    doubt, a crosstalk turn, an uncertain or ordinal label, or several
    confident voices all read as nobody. A confident guest label prints the
    person's preferred name; the identity name only carries the wire. An
    unlabelled turn is the owner outside room mode, where one voice is one
    person, and nobody inside it, where the label may simply not have
    landed. The owner is never a fallback: a blank stays blank."""
    from . import memory_client
    owner = cfg.get("user_name", "User")
    if not message_id:
        return ""
    row = con.execute(
        "SELECT id, speaker, voice_labels FROM messages WHERE id=? AND chat_id=?",
        (message_id, chat_id)).fetchone()
    if not row or row["speaker"] != "user":
        return ""
    labels, _, _ = memory_client._parse_voice_labels(row["voice_labels"])
    if not labels:
        chat = con.execute("SELECT room_mode FROM chats WHERE id=?",
                           (chat_id,)).fetchone()
        return "" if (chat and chat["room_mode"]) else owner
    flagged = {f["message_id"] for f in db.get_room_flags(con, chat_id, open_only=True)
               if f.get("message_id")}
    people = _people()
    wire = memory_client.ingest_speaker(dict(row), flagged, owner,
                                        memory_client.identity_name_map(people))
    if wire == "user":
        return owner
    if not wire.startswith("guest:") or wire == memory_client.GUEST_UNKNOWN:
        return ""
    name = wire[len("guest:"):]
    for p in people:
        if (p.get("name") or "").casefold() == name.casefold():
            return p.get("preferred_name") or p["name"]
    return name


def apply_depth(chat_id, changes, cfg, message_id=None) -> str:
    """Apply confirmed depth changes (synchronous; worker thread). Resolves
    seat names against the CHAT's participants (name or slug, spoken case),
    'all' meaning every one of them. Writes chat_seat_state, and inserts one
    system notice per real change so the transcript shows the trade the
    moment it is made. Unknown seat names change nothing - a misheard name
    must not move a different seat. Returns the scan outcome word.

    `message_id` is the user turn that carried the cue; the notice and the
    stored setter name whoever spoke it, or nobody (#255).

    "Normal" also returns a seat stepped up to a stronger model for this
    chat to its configured model (#254), with its own line; that counts as
    a clear even when the seat had no spoken depth. Moving a model UP is
    not done here - it needs a live search, so the scan runs it afterwards
    (model_step.step_up)."""
    if not changes:
        return "no_change"
    from . import model_step  # lazy: model_step reads depth's speaker rule
    changed = cleared = 0
    con = db.connect()
    try:
        user = resolve_speaker(con, chat_id, message_id, cfg)
        roster = db.get_chat_participants(con, chat_id)
        by_key = {}
        for p in roster:
            by_key[p["slug"].casefold()] = p
            by_key[(p["name"] or "").casefold()] = p
        rows = db.get_chat_seat_rows(con, chat_id)
        current = {slug: r["reasoning_effort"] for slug, r in rows.items()
                   if r["reasoning_effort"]}
        parked = {slug: r["once_effort"] for slug, r in rows.items()
                  if r["once_effort"]}
        # A reset spoken in the same breath as a one-reply override is the
        # compound instruction ("think hard about just this next one, and
        # from now on go back to normal"): the override must survive it. A
        # reset on its own means everything, override included (#260).
        once_targets = set()
        for ch in changes:
            if ch.get("once") and DEPTH_LEVELS[ch["depth"]]:
                key = ch["seat"].casefold()
                once_targets |= ({p["slug"] for p in roster} if key == "all"
                                 else {by_key[key]["slug"]} if key in by_key
                                 else set())
        once_set = 0
        # #260: a reset means everything. Spoken to EVERYONE ("all", not a
        # named seat) and standing (not a one-off, which instructs nothing),
        # it also clears research mode (#253/#417) - the mode has no seat of
        # its own to name.
        reset_all = any(not ch.get("once") and ch["seat"].casefold() == "all"
                        and ch["depth"] == "normal" for ch in changes)
        for ch in changes:
            key = ch["seat"].casefold()
            targets = roster if key == "all" else \
                ([by_key[key]] if key in by_key else [])
            if not targets:
                log.info("depth change named no known seat: chat=%s", chat_id)
            for p in targets:
                effort = DEPTH_LEVELS[ch["depth"]]
                slug = p["slug"]
                if ch.get("once"):
                    # Slice 2: one reply only. Consumed by the seat's next
                    # call, so no mode notice - nothing persistent changed
                    # and the effect is over by the time anyone reads it.
                    if effort:
                        db.set_chat_seat_once(con, chat_id, slug, effort,
                                              once_by=user)
                        parked[slug] = effort
                        once_set += 1
                    continue  # "normal, just this once" instructs nothing
                if effort:
                    if current.get(slug, "") == effort:
                        continue  # already there - no state write, no notice
                    db.set_chat_seat_depth(con, chat_id, slug, effort,
                                           set_by=user)
                    current[slug] = effort
                    db.insert_message(con, chat_id, "system",
                                      _notice(p["name"] or p["slug"],
                                              ch["depth"], user))
                    changed += 1
                    continue
                keep = slug in once_targets
                dropped = "" if keep else parked.get(slug, "")
                if slug in current or dropped:
                    db.set_chat_seat_depth(con, chat_id, slug, "",
                                           keep_once=keep)
                    current.pop(slug, None)
                    if dropped:
                        parked.pop(slug, None)
                    db.insert_message(con, chat_id, "system",
                                      _notice(p["name"] or p["slug"], "normal",
                                              user, dropped_once=dropped))
                    cleared += 1
                # #254: back to normal means the model too. A seat stepped
                # up to a stronger model for this chat returns to its
                # configured one, with its own line; a seat already there
                # gets nothing.
                if model_step.clear_for_reset(con, chat_id, p):
                    cleared += 1
        if reset_all:
            from . import research  # lazy: research.py imports this module
            if research.clear_research(con, chat_id):
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


def _who(user) -> str:
    """The subject of a depth note: the person who spoke the cue, or the
    plain truth that someone did when the app could not say who (#255)."""
    return user or "Someone in this chat"


def once_note(level, user) -> str:
    """The volatile prompt note for a consumed one-reply override (#105
    slice 2) - scoped to THIS reply so the seat neither adopts it as a mode
    nor announces a change of one. `user` is who parked it, or ''."""
    if not level:
        return ""
    word = LEVEL_WORDS.get(level, level)
    return (f"\n## Your reasoning depth (THIS reply only)\n{_who(user)} asked for "
            f"{word} thinking for just this one reply. It applies now and "
            f"reverts by itself - do not treat it as a standing mode.")


def depth_note(level, user, configured="") -> str:
    """The volatile prompt note telling a seat its own current depth (engine
    threads it per seat). With no spoken level the seat is told its
    configured setting instead (#305): a seat that knew nothing about its
    effort once claimed to have changed it, which it cannot do. Either way
    the note names the one door, a person in the chat saying so. `user` is
    who set the depth, or '' when the app could not say (#255)."""
    doors = ("Anyone in this chat can change it by saying so (\"think "
             "harder\", \"quick answers\", \"back to normal\"), and it "
             "applies to this chat only. You cannot change it yourself, "
             "so never say you have. If asked how hard you are thinking, "
             "say this honestly.")
    if not level:
        word = LEVEL_WORDS.get(configured, configured) or "default"
        return (f"\n## Your reasoning depth (this chat)\nYou are at your "
                f"configured setting, {word}. Nobody has changed it in "
                f"this conversation. {doors}")
    word = LEVEL_WORDS.get(level, level)
    return (f"\n## Your reasoning depth (this chat)\n{_who(user)} set your "
            f"thinking to {word} for this conversation. It persists until "
            f"someone changes it (\"back to normal\" clears it). {doors}")
