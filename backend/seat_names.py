"""The names a seat is heard as (#663).

In a voice chat the transcriber writes some seats' names its own way:
"Qwen" comes out as "Quinn", and no spelling rule links the two. A seat's
heard-as names are those other spellings. They address the seat, cut its
reply when said over it, and can never be seated as a person in the room,
and every seat is told them, so the seat knows it's being spoken to and
the others know who's meant.

Each seat's own list is the participants.heard_as column, which the owner
edits on the Models page. KNOWN adds the spellings the transcriber is known
to use for a model's name, so a new Qwen seat needs no setting. The
transcriber's spelling hints keep the real name (routers/voice.py), so they
still pull towards it.

Pure: nothing here reads the database or imports the app.
"""

import re

# A model name the transcriber is known to mishear, by the seat's name or
# slug in lower case, and how it writes it.
KNOWN = {"qwen": ("Quinn",)}
MAX_NAMES = 8
MAX_CHARS = 30


def parse(text) -> list:
    """A comma-separated list as clean names: trimmed, no empties, no
    repeats (ignoring case), at most MAX_NAMES of MAX_CHARS each."""
    out, seen = [], set()
    for raw in str(text or "").split(","):
        name = re.sub(r"\s+", " ", raw).strip()[:MAX_CHARS].strip()
        if name and name.casefold() not in seen:
            seen.add(name.casefold())
            out.append(name)
        if len(out) >= MAX_NAMES:
            break
    return out


def clean(text) -> str:
    """What's stored: the parsed list joined with commas."""
    return ", ".join(parse(text))


def heard_names(seat) -> list:
    """The seat's own heard-as names, then the known ones for its name,
    without its real name or slug."""
    seat = seat or {}
    name = str(seat.get("name") or "")
    slug = str(seat.get("slug") or "")
    keys = {name.casefold(), slug.casefold(), (name.split() or [""])[0].casefold()}
    known = [n for k in keys if k for n in KNOWN.get(k, ())]
    real = {name.casefold(), slug.casefold()}
    return [n for n in parse(", ".join([*parse(seat.get("heard_as")), *known]))
            if n.casefold() not in real]


def described(seat) -> str:
    """The seat's name, with how the transcriber writes it when it has
    heard-as names: 'Qwen (the transcriber often writes it as Quinn)'."""
    names = heard_names(seat)
    if not names:
        return seat.get("name") or ""
    return f"{seat.get('name')} (the transcriber often writes it as {_or(names)})"


def _or(names) -> str:
    return names[0] if len(names) == 1 else ", ".join(names[:-1]) + " or " + names[-1]


def own_line(seat) -> str:
    """The line that tells a seat its own heard-as names, or ''."""
    names = heard_names(seat)
    if not names:
        return ""
    return (f"In a voice chat the transcriber often writes your name as "
            f"{_or(names)}. Someone who says {names[0]} means you, {seat.get('name')}.")
