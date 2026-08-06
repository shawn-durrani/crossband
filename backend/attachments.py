"""Attachment handling: type detection, storage paths, and provider-specific
content blocks (Anthropic Messages API blocks; OpenAI Responses API parts)."""

import base64
import os

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
    with open(file_path(att), "rb") as f:
        return base64.standard_b64encode(f.read()).decode()


def read_text(att):
    with open(file_path(att), "rb") as f:
        text = f.read().decode("utf-8", errors="replace")
    if len(text) > MAX_TEXT_CHARS:
        text = text[:MAX_TEXT_CHARS] + "\n... [file truncated]"
    return text


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
