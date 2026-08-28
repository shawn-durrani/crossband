"""Attachment handling: type detection, storage paths, and provider-specific
content blocks (Anthropic Messages API blocks; OpenAI Responses API parts).

Reads are cached, because a round builds one message list per seat and every
seat re-reads the same files. A three-seat round over one 20MB PDF spent about
175ms of event loop re-encoding bytes that had not changed. Stored files are
immutable by construction: routers.attachments writes a uuid4-prefixed
stored_name once and nothing rewrites it, so the cache is keyed on the path
plus size and mtime rather than on content.
"""

import base64
import os
import threading

from . import db

IMAGE_MIMES = {"image/png", "image/jpeg", "image/gif", "image/webp"}
# Accepted on upload and converted to JPEG before storage; never sent to
# a provider in this form, since none of them take HEIC.
UPLOAD_ONLY_IMAGE_MIMES = {"image/heic", "image/heif"}
TEXT_EXTENSIONS = {
    ".txt", ".md", ".markdown", ".csv", ".tsv", ".json", ".xml", ".yaml", ".yml",
    ".toml", ".html", ".css", ".js", ".jsx", ".ts", ".tsx", ".py", ".rb", ".go",
    ".rs", ".java", ".c", ".cpp", ".h", ".hpp", ".sh", ".sql", ".log", ".ini",
    ".cfg", ".env", ".swift", ".kt", ".php",
}
MAX_TEXT_CHARS = 100_000

# Budgeted in bytes, not entries: one entry can be 28MB of base64 and another
# can be 200 bytes, so an entry count is the wrong unit for a memory ceiling.
CACHE_BUDGET_BYTES = 96 * 1024 * 1024

_cache = {}          # key -> decoded value
_cache_order = []    # keys, least recent first
_cache_bytes = 0
_cache_lock = threading.Lock()


def _cache_key(att, kind):
    """Path plus the two stat fields that would change if a file were ever
    rewritten. Immutability is the design, not something this module can
    verify, so the stat is the cheap insurance."""
    path = file_path(att)
    try:
        st = os.stat(path)
    except OSError:
        return None
    return (kind, path, st.st_size, st.st_mtime_ns)


def _cached(att, kind, produce):
    key = _cache_key(att, kind)
    if key is None:
        return produce()
    with _cache_lock:
        if key in _cache:
            _cache_order.remove(key)
            _cache_order.append(key)
            return _cache[key]
    value = produce()
    size = len(value)
    if size > CACHE_BUDGET_BYTES:
        return value  # one file bigger than the whole budget: never cache it
    with _cache_lock:
        global _cache_bytes
        if key not in _cache:
            _cache[key] = value
            _cache_order.append(key)
            _cache_bytes += size
            while _cache_bytes > CACHE_BUDGET_BYTES and len(_cache_order) > 1:
                evicted = _cache_order.pop(0)
                _cache_bytes -= len(_cache.pop(evicted, ""))
    return value


def clear_cache():
    """Tests, and any caller that has deleted attachment files underneath us."""
    with _cache_lock:
        global _cache_bytes
        _cache.clear()
        _cache_order.clear()
        _cache_bytes = 0


def kind_of(filename, mime):
    if mime in IMAGE_MIMES:
        return "image"
    if mime in UPLOAD_ONLY_IMAGE_MIMES or filename.lower().endswith((".heic", ".heif")):
        return "image"
    if mime == "application/pdf" or filename.lower().endswith(".pdf"):
        return "pdf"
    ext = os.path.splitext(filename.lower())[1]
    if mime.startswith("text/") or ext in TEXT_EXTENSIONS:
        return "text"
    return None  # unsupported


def file_path(att):
    return os.path.join(db.ATTACH_DIR, att["stored_name"])


def read_b64(att):
    def produce():
        with open(file_path(att), "rb") as f:
            return base64.standard_b64encode(f.read()).decode()
    return _cached(att, "b64", produce)


def read_text(att):
    def produce():
        with open(file_path(att), "rb") as f:
            text = f.read().decode("utf-8", errors="replace")
        if len(text) > MAX_TEXT_CHARS:
            text = text[:MAX_TEXT_CHARS] + "\n... [file truncated]"
        return text
    return _cached(att, "text", produce)


def _framed_text(att):
    return (f"--- Attached file: {att['filename']} ---\n{read_text(att)}\n"
            f"--- End of {att['filename']} ---")


def anthropic_blocks(att):
    """Content blocks for the Anthropic Messages API."""
    kind = kind_of(att["filename"], att["mime"])
    if kind == "image":
        return [{
            "type": "image",
            "source": {"type": "base64", "media_type": att["mime"], "data": read_b64(att)},
        }]
    if kind == "pdf":
        return [{
            "type": "document",
            "source": {"type": "base64", "media_type": "application/pdf", "data": read_b64(att)},
            "title": att["filename"],
        }]
    if kind == "text":
        return [{"type": "text", "text": _framed_text(att)}]
    return []


def openai_parts(att):
    """Content parts for the OpenAI Responses API (input_* types)."""
    kind = kind_of(att["filename"], att["mime"])
    if kind == "image":
        return [{
            "type": "input_image",
            "image_url": f"data:{att['mime']};base64,{read_b64(att)}",
        }]
    if kind == "pdf":
        return [{
            "type": "input_file",
            "filename": att["filename"],
            "file_data": f"data:application/pdf;base64,{read_b64(att)}",
        }]
    if kind == "text":
        return [{"type": "input_text", "text": _framed_text(att)}]
    return []


def text_description(att):
    """Plain-text stand-in used in summaries and non-multimodal paths."""
    return f"[attachment: {att['filename']} ({att['mime']}, {att['size']} bytes)]"
