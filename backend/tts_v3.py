"""How a reply is fed to Eleven v3, so its voice holds one accent (#493).

Every v3 model speaks through ElevenLabs' text-to-dialogue socket, and the
socket makes a fresh generation each time it has about 40 characters and 8
words buffered. Each generation can land in a slightly different accent.
Three settings steer that, and each applies to the dialogue socket only:

- `tts_v3_stability`: v3's three stability modes, by the names its
  prompting guide uses. "robust" holds the voice steadiest, "natural" was
  the value sent before the setting existed.
- `tts_v3_sentence_chunks`: the relay holds a reply's text and sends it a
  whole sentence at a time, so no generation starts or ends mid-sentence.
- `tts_v3_accent_tag`: an audio tag, such as "[Australian accent]", put in
  front of every piece sent upstream. A seat can set its own. It exists only
  in the frames sent to ElevenLabs, never in the chat.

Pure: no network, no clock, no database. backend/voice.py wires it into the
relay's upstream frames.
"""

import logging
import re

log = logging.getLogger("crossband.tts_v3")

# ElevenLabs' v3 guide names three stability modes. The socket takes a
# number from 0 to 1, and these are the three points the modes sit at.
STABILITY = {"creative": 0.0, "natural": 0.5, "robust": 1.0}
DEFAULT_STABILITY = "robust"

# A held reply is sent when a sentence ends or, with no sentence end in
# sight, once it passes this many characters. ElevenLabs advises v3 inputs
# over about 250 characters for consistent output.
SENTENCE_CAP = 250

# A sentence ends at a run of . ! or ?, with any closing quotes or brackets,
# once whitespace follows it. The whitespace is what tells "3." apart from
# "3.14", so the end is only known when the next piece starts. A newline
# ends a sentence on its own, which covers list items and headings.
_SENTENCE_END = re.compile(r"[.!?]+[\"'”’)\]]*(?=\s)|\n")
_SPACE = re.compile(r"\s")

TAG_MAX = 40
# One bracketed phrase: letters (accented Latin included), then letters,
# spaces, hyphens and apostrophes. No digits, no second bracket, nothing
# else, so a tag can't carry text to be spoken or a second tag.
_TAG = re.compile(r"\[[A-Za-zÀ-ɏ][A-Za-zÀ-ɏ' -]*\]")
TAG_RULE = ("one [bracketed phrase] of at most 40 characters, "
            "such as [Australian accent]")

_warned: set = set()


def stability(cfg) -> float:
    """The stability number the dialogue socket opens with. A value that
    isn't one of the three names speaks with the default."""
    want = (cfg or {}).get("tts_v3_stability")
    if isinstance(want, str) and want.strip().lower() in STABILITY:
        return STABILITY[want.strip().lower()]
    if want not in (None, "", DEFAULT_STABILITY):
        _warn_once("stability", want,
                   "tts_v3_stability %r is not creative, natural or robust; "
                   "speaking with %s", str(want)[:40], DEFAULT_STABILITY)
    return STABILITY[DEFAULT_STABILITY]


def valid_tag(value) -> bool:
    """Blank, or a single bracketed phrase of at most TAG_MAX characters.
    Surrounding whitespace is allowed and trimmed by clean_tag."""
    if not isinstance(value, str):
        return False
    value = value.strip()
    return value == "" or (len(value) <= TAG_MAX and bool(_TAG.fullmatch(value)))


def clean_tag(value) -> str:
    """The tag to send, or "" when the value is blank or not a valid tag."""
    if not valid_tag(value):
        return ""
    return value.strip()


def accent_tag(cfg, seat_tag="") -> str:
    """The tag in front of each piece of this reply: the seat's own when it
    has a valid one, else the app's. An invalid app value is ignored with a
    warning, the way a bad setting never stops the app."""
    seat = clean_tag(seat_tag)
    if seat:
        return seat
    app = (cfg or {}).get("tts_v3_accent_tag") or ""
    if app and not valid_tag(app):
        _warn_once("tag", app, "tts_v3_accent_tag is not %s; sending no tag",
                   TAG_RULE)
    return clean_tag(app)


def relay_state(cfg, seat_tag="") -> dict:
    """The per-reply state tts_upstream_frames reads and updates on the
    dialogue socket. The text-to-speech socket never reads it."""
    return {"chunks": bool((cfg or {}).get("tts_v3_sentence_chunks", True)),
            "tag": accent_tag(cfg, seat_tag)}


def with_tag(piece: str, tag: str) -> str:
    """The tag in front of a piece of text, after any leading whitespace, so
    the whitespace that keeps two pieces' words apart stays at the front."""
    if not tag:
        return piece
    body = piece.lstrip()
    return f"{piece[:len(piece) - len(body)]}{tag} {body}"


def take_ready(held: str) -> tuple:
    """Split held text into (ready to send, still held). Ready is everything
    up to the last sentence end. When what's left is still over SENTENCE_CAP
    characters, ready runs on to its last whitespace, so a word is never cut.
    Whitespace after a cut stays held, in front of the next piece."""
    cut = 0
    for m in _SENTENCE_END.finditer(held):
        if held[:m.end()].strip():
            cut = m.end()
    rest = held[cut:]
    if len(rest) > SENTENCE_CAP:
        spaces = [m.start() for m in _SPACE.finditer(rest) if m.start() > 0]
        cut += spaces[-1] if spaces else len(rest)
    if not held[:cut].strip():
        return "", held
    return held[:cut], held[cut:]


def _warn_once(kind, value, msg, *args) -> None:
    key = (kind, str(value))
    if key not in _warned:
        _warned.add(key)
        log.warning(msg, *args)
