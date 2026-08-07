"""What a reply actually re-reads - text AND attachments.

Three separate surfaces independently decided a conversation's weight by
counting `len(message["content"])`, and all three were therefore blind to
attachments:

* the header's context ring (`routers/chats._context_estimate`),
* the auto-summary fold trigger (`chat_memory.maybe_summarize`),
* and, before upload downscaling, nothing at all watched upload size.

Attach photos and all three go wrong at once. Character counting sees only the
typed text, so the header gauge reads a few thousand tokens and shows green,
the fold sits far below its 60,000-character threshold and never fires, and
each turn quietly ships tens of megabytes over the wire to every provider in
the room. Every surface says "small" while the chat becomes unusable.

So weight is computed HERE, once, and the surfaces read it. A future surface
that needs to know how heavy a conversation is has somewhere correct to ask.

Two units, because they answer different questions and the second is the one
that actually hurt:

* **tokens** - what dilutes the model's attention and costs money.
* **upload bytes** - what goes over the wire on *every* turn, to *every*
  provider. Invisible everywhere until upload downscaling landed, and the
  likeliest cause of the slowness that started this.
"""

# Providers downscale images to ~1568px on the long edge before tokenising, so
# a photo's token cost saturates: a 12 MP original and a 1568px copy cost the
# same. Anthropic's published rule is ~(w*h)/750 tokens, which at the cap is
# ~1568*1176/750. We don't store dimensions, so use the capped figure - right
# for any photo at or above the cap (i.e. every phone photo) and a modest
# over-estimate for genuinely small images.
IMAGE_TOKENS = 2400

# A rough but honest chars-per-token for prose.
CHARS_PER_TOKEN = 4

# Group rules, personas, shared instructions and tool schemas. Measured at ~3.6k
# for the current tool set; the old estimate said 1200 and was never revisited.
OVERHEAD_TOKENS = 3600


def toks(chars: int) -> int:
    return int((chars or 0) / CHARS_PER_TOKEN)


def attachment_tokens(att: dict) -> int:
    """Token cost of one attachment as the providers will count it."""
    mime = (att.get("mime") or "").lower()
    if mime.startswith("image/"):
        return IMAGE_TOKENS
    # PDFs and text files travel as text; `size` is the byte length on disk,
    # which for text is close enough to character count for a gauge.
    return toks(att.get("size") or 0)


def message_weight(messages) -> dict:
    """{text_tokens, attachment_tokens, upload_bytes, image_count} for a list
    of messages (each with an `attachments` list)."""
    text = sum(len(m.get("content") or "") for m in messages)
    att_toks = upload = images = 0
    for m in messages:
        for a in m.get("attachments") or []:
            att_toks += attachment_tokens(a)
            # base64 inflates by ~4/3 on the wire, and this payload is sent
            # again for every participant on every turn.
            upload += int((a.get("size") or 0) * 4 / 3)
            if (a.get("mime") or "").lower().startswith("image/"):
                images += 1
    return {"text_tokens": toks(text), "attachment_tokens": att_toks,
            "upload_bytes": upload, "image_count": images}


def fold_weight(messages) -> int:
    """The number the summary-fold trigger should compare against its
    threshold: everything a reply re-reads, in CHARACTER-equivalents so the
    existing `summary_threshold_chars` setting keeps its meaning.

    Text alone let an image-heavy chat sail far under a 60,000-character
    threshold while it was genuinely one of the heaviest conversations in the
    database.
    """
    w = message_weight(messages)
    return (w["text_tokens"] + w["attachment_tokens"]) * CHARS_PER_TOKEN


def estimate(chat: dict, messages, cfg: dict, memory_summary_len: int = 0) -> dict:
    """Per-reply context, by component, for the header gauge and the
    conversation_performance diagnostic.

    Only messages after the fold watermark count: anything older has been
    replaced by the summary and is no longer sent verbatim.
    """
    recent = [m for m in messages if m["id"] > (chat.get("summary_upto") or 0)]
    w = message_weight(recent)
    research = toks(sum(
        min(len(t.get("output_text") or ""), cfg.get("tool_log_chars") or 1200)
        + len(t.get("input_json") or "")
        for m in recent for t in m.get("tool_events") or []))
    chat_summary = toks(len(chat.get("summary") or ""))
    memory = toks(memory_summary_len) if chat.get("memory_enabled") else 0
    total = (w["text_tokens"] + w["attachment_tokens"] + research
             + chat_summary + memory + OVERHEAD_TOKENS)
    return {
        "history": w["text_tokens"],
        "attachments": w["attachment_tokens"],
        "research": research,
        "chat_summary": chat_summary,
        "memory": memory,
        "overhead": OVERHEAD_TOKENS,
        "total": total,
        # Per turn, per participant - the figure no surface used to show.
        "upload_bytes": w["upload_bytes"],
        "image_count": w["image_count"],
    }
