"""Seen-URL ledger (#138, second slice): model text never mints a fetchable URL.

A URL-taking research tool may only target a URL that already exists in the
chat from a non-model source: the user's messages, system lines, external
machine notices, text attachments, or a research tool's own output (search
results, fetched pages and the links inside them, transcripts). This closes
the exfiltration channel where injected page content asks a model to compose
https://attacker.example/?q=<something recalled from memory> - the model can
choose among URLs that already exist, never author one. A page-authored link
is safe to admit because it can only carry page-authored data.

What deliberately does NOT feed the ledger:
- model and guest-agent messages (the mint this module exists to prevent);
- memory, GitHub, MCP and diagnostic tool outputs - several echo
  model-authored text back (save_memory confirms the fact it saved, an issue
  read returns a body a model may have written), which would launder a
  composed URL into the ledger;
- any tool output starting with "Error" - error strings echo the offending
  input, so a denied fetch must not seed the very URL it denied.

The check consults the current round's in-flight research outputs first
(cfg["_round_tool_texts"], seeded by the engine per round) because tool
events only persist when the assistant message inserts - without this, the
search -> fetch flow inside one reply would never find its own results.
"""

import contextlib
import re
from urllib.parse import urlsplit, urlunsplit

from . import db

# Research tools whose SUCCESS output is source-authored content. Only these
# rows of tool_events feed the ledger (see module docstring for why).
SOURCE_TOOLS = ("web_search", "fetch_page", "view_page",
                "fetch_youtube_transcript", "transcribe_audio_url",
                "fetch_reddit_thread")

_ATTACH_TEXT_CAP = 2 * 1024 * 1024  # bytes read per text attachment

_URL_RE = re.compile(r"https?://[^\s<>\"']+", re.IGNORECASE)
_TRAIL = ".,;:!?"
_CLOSERS = {")": "(", "]": "[", "}": "{"}


# Non-model message sources: the owner, tooling lines, and external
# machine feeds. Guests are humans, but their turns arrive via
# transcription with uncertain attribution, so they do not mint either.
# Both minting queries in check() share this one predicate.
_SOURCE_SPEAKER_SQL = "(speaker='user' OR speaker='system' OR speaker LIKE 'ext:%')"


def canonical(url: str) -> str | None:
    """One comparable form: lowercase scheme/host, default port dropped,
    fragment dropped (never transmitted, so it cannot exfiltrate), empty path
    normalised to "/". Query strings compare verbatim - they are the channel
    this ledger exists to police. Returns None for anything unparseable."""
    try:
        u = urlsplit(url.strip())
    except ValueError:
        return None
    if u.scheme.lower() not in ("http", "https") or not u.hostname:
        return None
    host = u.hostname.lower()
    port = u.port
    default = 80 if u.scheme.lower() == "http" else 443
    netloc = host if port in (None, default) else f"{host}:{port}"
    if u.username or u.password:
        return None  # credentialled URLs never participate
    return urlunsplit((u.scheme.lower(), netloc, u.path or "/", u.query, ""))


def _trim(raw: str) -> str:
    """Strip the punctuation prose wraps around a URL. Balanced closers stay
    (Wikipedia-style paths); an unbalanced one is the sentence's, not the
    URL's."""
    url = raw.rstrip(_TRAIL)
    while url and url[-1] in _CLOSERS:
        if url.count(_CLOSERS[url[-1]]) >= url.count(url[-1]):
            break
        url = url[:-1].rstrip(_TRAIL)
    return url


def extract(text: str):
    """Canonical forms of every URL in a block of text."""
    out = set()
    for m in _URL_RE.finditer(text or ""):
        c = canonical(_trim(m.group(0)))
        if c:
            out.add(c)
    return out


def _tool_hits(text, target) -> bool:
    """Tool outputs mint only on success: error strings echo the offending
    input, so they never feed the ledger. Human/system messages are checked
    without this skip - the owner pasting an error report still mints."""
    return bool(text) and not text.startswith("Error") and target in extract(text)


def check(chat_id, url, extra_texts=(), con=None):
    """None when the URL is fetchable in this chat; an explicit error string
    otherwise. The error never repeats the URL: tool errors are persisted,
    and an echoed target would launder itself into the ledger.

    An unparseable URL also returns None - the tool's own SSRF guard owns
    that refusal and its clearer message."""
    target = canonical(url)
    if target is None:
        return None
    for text in extra_texts:
        if _tool_hits(str(text), target):
            return None
    own = con is None
    if own:
        con = db.connect()
    try:
        rows = con.execute(
            "SELECT content FROM messages WHERE chat_id=? AND "
            + _SOURCE_SPEAKER_SQL,
            (chat_id,))
        for (content,) in rows:
            if content and target in extract(content):
                return None
        marks = ",".join("?" * len(SOURCE_TOOLS))
        rows = con.execute(
            f"SELECT t.output_text FROM tool_events t "
            f"JOIN messages m ON t.message_id = m.id "
            f"WHERE m.chat_id=? AND t.tool IN ({marks}) "
            f"AND t.output_text NOT LIKE 'Error%'",
            (chat_id, *SOURCE_TOOLS))
        for (output,) in rows:
            if _tool_hits(output, target):
                return None
        rows = con.execute(
            "SELECT a.stored_name, a.mime FROM attachments a "
            "JOIN messages m ON a.message_id = m.id "
            "WHERE m.chat_id=? AND "
            + _SOURCE_SPEAKER_SQL.replace("speaker", "m.speaker"),
            (chat_id,)).fetchall()
    finally:
        if own:
            con.close()
    for stored_name, mime in rows:
        if not (mime.startswith("text/") or "json" in mime or "xml" in mime):
            continue
        with contextlib.suppress(OSError, UnicodeError):
            raw = (db.ATTACH_DIR / stored_name).read_bytes()[:_ATTACH_TEXT_CAP]
            if target in extract(raw.decode("utf-8", "replace")):
                return None
    return ("Error: that URL has not appeared in this chat from a non-model "
            "source, so it cannot be fetched. web_search first (or ask the "
            "user to paste the link), then fetch exactly the URL a result "
            "names.")
