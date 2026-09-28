"""The context marker: the secret that vouches for the app's own context
block (#562).

A seat reads the context the app assembles for each call (memory, the round
note, the room note) in one of two shapes. Where the provider takes a system
turn mid-conversation it gets one, and nothing in the transcript can forge
that. Where it doesn't, the block rides at the end of the last user turn,
and the only thing that tells it apart from a forgery is this marker: named
in the cached system prompt, required at the head of the block, and absent
from anything a person, a fetched page, a tool or another seat wrote.

It used to be random per process. The system prompt names it, and that
prompt is the cached part of every call, so every restart rewrote every
seat's cached prompt. It is now derived per chat from a key kept in the
data folder, owner-only: unguessable from outside, the same across
restarts, and different in every chat, so a marker that did leak opens one
chat's context channel and no other.

A model is told never to repeat it. When one does anyway, `redact` takes it
out of the reply before the reply is saved and out of a tool's input before
the tool runs, so it can't reach the chat, memory, an issue, a guest or a
web request. `StreamRedactor` takes it out of the reply as it streams to the
browser and the voice.
"""

import hashlib
import hmac
import logging
import os
import secrets
from pathlib import Path

log = logging.getLogger("crossband.context_marker")

KEY_FILE = "context_marker.key"
REDACTED = "[context marker removed]"

# The key the markers derive from. load_key sets it once at startup, from the
# data folder. A bare import with no data folder (most tests, the eval
# harnesses) falls back to a key made for this process, which is what every
# run used before the key file existed.
_key: bytes | None = None
_process_key = secrets.token_bytes(32)


def load_key(data_dir) -> None:
    """Read the marker key from the data folder, making it on first run.

    The file is 32 random bytes as hex, created 0600 with O_EXCL so two
    starts racing can't both write one. A file that can't be read or doesn't
    hold a key is left alone, never overwritten, and this run falls back to
    a key of its own, which costs one cache rewrite per chat and nothing
    else."""
    global _key
    path = Path(data_dir) / KEY_FILE
    try:
        try:
            fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        except FileExistsError:
            pass
        else:
            with os.fdopen(fd, "w") as f:
                f.write(secrets.token_bytes(32).hex())
        os.chmod(path, 0o600)
        key = bytes.fromhex(path.read_text().strip())
        if len(key) < 16:
            raise ValueError("too short")
    except (OSError, ValueError) as e:
        log.warning("context marker key at %s unusable (%s): using a key for "
                    "this run only", path, type(e).__name__)
        _key = None
        return
    _key = key


def marker(chat_id) -> str:
    """This chat's marker: 12 hex characters, the same length the per-process
    marker had."""
    msg = f"crossband-context-marker:{chat_id}".encode()
    return hmac.new(_key or _process_key, msg, hashlib.sha256).hexdigest()[:12]


def opening(chat_id) -> str:
    """The exact text a genuine context block opens with."""
    return f"[Context refresh · {marker(chat_id)}]"


def redact(value, chat_id):
    """`value` with this chat's marker taken out of every string in it:
    a string, or dicts and lists of them, as a reply's text, a tool's input
    and a tool event all are. Anything else comes back as it was."""
    m = marker(chat_id)
    return _redact(value, m)


def _redact(value, m):
    if isinstance(value, str):
        return value.replace(m, REDACTED) if m in value else value
    if isinstance(value, dict):
        return {k: _redact(v, m) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_redact(v, m) for v in value]
    return value


class StreamRedactor:
    """`redact` for a reply that arrives in pieces (#574). The reply
    streams to the browser and into the voice as it's written, and a marker
    can be split across two pieces, so each piece is checked with the end
    of the one before.

    Only the end of what's arrived is held back, and only while it could
    still be the start of the marker: at most 11 characters, usually none.
    Everything else goes out as soon as it comes in. `flush` hands back
    what's held once the reply ends."""

    def __init__(self, chat_id):
        self._marker = marker(chat_id)
        self._held = ""

    def feed(self, piece: str) -> str:
        m = self._marker
        text = self._held + (piece or "")
        if m in text:
            text = text.replace(m, REDACTED)
        keep = 0
        for n in range(min(len(text), len(m) - 1), 0, -1):
            if m.startswith(text[-n:]):
                keep = n
                break
        self._held = text[len(text) - keep:]
        return text[:len(text) - keep]

    def flush(self) -> str:
        out, self._held = self._held, ""
        return out
