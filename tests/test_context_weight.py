"""Conversation weight counts attachments, not just text.

The regression these pin: three surfaces independently measured a chat by
`len(message["content"])`, so the one chat that ever became unusable read
"~4,700 tokens, green", never triggered the auto-fold, and reported nothing
about the tens of megabytes it re-uploaded on every turn.
"""

from backend import chat_memory, context_weight


def _msg(mid, text="", attachments=()):
    return {"id": mid, "content": text, "speaker": "user",
            "attachments": [dict(a) for a in attachments], "tool_events": []}


PHOTO = {"mime": "image/jpeg", "size": 3_000_000}


def test_the_real_chat_that_broke_this_is_no_longer_read_as_tiny():
    """A short chat carrying a handful of phone photos. The gauge said a few thousand
    tokens; every surface said 'small' while it was unusable."""
    msgs = [_msg(1, "x" * 5127)] + [_msg(i + 2, "", [PHOTO]) for i in range(7)]
    chat = {"summary_upto": 0, "summary": "", "memory_enabled": 0}
    est = context_weight.estimate(chat, msgs, {"tool_log_chars": 1200})

    assert est["attachments"] == 7 * context_weight.IMAGE_TOKENS
    assert est["image_count"] == 7
    # attachments must dominate: that was the whole invisible story
    assert est["attachments"] > est["history"] * 5
    assert est["total"] > 20_000
    # tens of megabytes of base64 on the wire, per participant, per turn
    assert est["upload_bytes"] > 25_000_000


def test_fold_now_fires_on_an_image_heavy_chat_that_used_to_slip_under():
    """A few thousand text chars against a 60,000 threshold: the fold never ran on the
    heaviest conversation on record."""
    msgs = [_msg(1, "x" * 5127)] + [_msg(i + 2, "", [PHOTO]) for i in range(7)]
    text_only = sum(len(m["content"]) for m in msgs)
    assert text_only < 60_000, "the old trigger saw this as small"
    assert context_weight.fold_weight(msgs) > 60_000, "the new one must not"


def test_a_genuinely_light_chat_is_still_light():
    """The fix must not make every chat look heavy — that would just move the
    blindness to the other end."""
    msgs = [_msg(i, "a short message") for i in range(1, 12)]
    assert context_weight.fold_weight(msgs) < 60_000
    est = context_weight.estimate(
        {"summary_upto": 0, "summary": "", "memory_enabled": 0}, msgs,
        {"tool_log_chars": 1200})
    assert est["attachments"] == 0 and est["upload_bytes"] == 0


def test_folded_messages_stop_counting():
    """Anything before the watermark is represented by the summary and is no
    longer sent verbatim — counting it would over-report forever."""
    msgs = [_msg(1, "", [PHOTO]), _msg(2, "", [PHOTO])]
    chat = {"summary_upto": 1, "summary": "s", "memory_enabled": 0}
    est = context_weight.estimate(chat, msgs, {"tool_log_chars": 1200})
    assert est["image_count"] == 1


def test_pdfs_and_text_files_count_by_length_not_as_images():
    doc = {"mime": "application/pdf", "size": 40_000}
    est = context_weight.estimate(
        {"summary_upto": 0, "summary": "", "memory_enabled": 0},
        [_msg(1, "", [doc])], {"tool_log_chars": 1200})
    assert est["attachments"] == 10_000  # 40k chars / 4
    assert est["image_count"] == 0


def test_image_token_cost_is_capped_not_proportional_to_bytes():
    """Providers downscale to ~1568px before tokenising, so a 12 MP original
    and a 1568px copy cost the SAME tokens — only the upload differs."""
    big = context_weight.message_weight([_msg(1, "", [{"mime": "image/jpeg", "size": 5_000_000}])])
    small = context_weight.message_weight([_msg(1, "", [{"mime": "image/jpeg", "size": 300_000}])])
    assert big["attachment_tokens"] == small["attachment_tokens"]
    assert big["upload_bytes"] > small["upload_bytes"] * 10


# ---- the diagnostic: ask about performance, either modality -------------------

def test_conversation_performance_diagnostic_is_offered_and_dispatches():
    from backend import diagnostics
    assert "conversation_performance" in diagnostics.DIAGNOSTIC_NAMES
    schema = diagnostics.diagnostic_input_schema()
    assert "conversation_performance" in schema["properties"]["name"]["enum"]
    # the two allowlists must not drift — asserted in the module, re-asserted here
    assert set(diagnostics._DIAGNOSTIC_DISPATCH) == set(diagnostics.DIAGNOSTIC_NAMES)


def test_conversation_performance_reports_upload_and_names_the_cause(tmp_path, monkeypatch):
    """The point of the tool: answer 'why is this chat slow' in-chat, instead
    of someone reading the SQLite file by hand."""
    import asyncio

    from backend import db, diagnostics

    monkeypatch.setattr(db, "DATA_DIR", tmp_path)
    db.configure(tmp_path)
    db.init()
    con = db.connect()
    cur = con.execute(
        "INSERT INTO chats(title, web_enabled, code_enabled, created_at, updated_at) "
        "VALUES('photos',0,0,?,?)", (db.now(), db.now()))
    chat_id = cur.lastrowid
    for i in range(7):
        m = con.execute(
            "INSERT INTO messages(chat_id, speaker, content, created_at) "
            "VALUES(?,'user','look at this',?)", (chat_id, db.now()))
        con.execute(
            "INSERT INTO attachments(message_id, filename, stored_name, mime, size, created_at) "
            "VALUES(?,?,?,?,?,?)",
            (m.lastrowid, f"IMG_{i}.jpeg", f"s{i}", "image/jpeg", 3_000_000, db.now()))
    con.commit()
    con.close()

    out = asyncio.run(diagnostics.dispatch_diagnostic(
        "conversation_performance", {"chat_id": chat_id, "tool_log_chars": 1200}))

    assert out["images_in_context"] == 7
    assert out["per_turn_upload_mb"] >= 25
    assert out["context_tokens"]["attachments"] > out["context_tokens"]["history"]
    # it must SAY what is wrong, not just hand over numbers
    assert any("every turn" in f for f in out["findings"])
    assert "voice" in out, "one answer must cover both modalities"


def test_conversation_performance_never_returns_message_content(tmp_path, monkeypatch):
    """Content-free by construction, like every other diagnostic."""
    import asyncio
    import json

    from backend import db, diagnostics

    monkeypatch.setattr(db, "DATA_DIR", tmp_path)
    db.configure(tmp_path)
    db.init()
    con = db.connect()
    cur = con.execute(
        "INSERT INTO chats(title, web_enabled, code_enabled, created_at, updated_at) "
        "VALUES('t',0,0,?,?)", (db.now(), db.now()))
    chat_id = cur.lastrowid
    con.execute("INSERT INTO messages(chat_id, speaker, content, created_at) "
                "VALUES(?,'user','SECRET-PASSPHRASE-XYZ',?)", (chat_id, db.now()))
    con.commit(); con.close()

    out = asyncio.run(diagnostics.dispatch_diagnostic(
        "conversation_performance", {"chat_id": chat_id, "tool_log_chars": 1200}))
    assert "SECRET-PASSPHRASE-XYZ" not in json.dumps(out)
