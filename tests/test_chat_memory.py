"""Auto-title refresh triggers.

The 8-message TITLE_REFRESH_DELTA re-title used to fire ONLY when the leave/
reflection pass ran (summary fold on chat-switch/idle sweep). A chat that stays
continuously active never triggered that pass, so it outran its title forever.
The fix runs maybe_title_chat after EVERY round via post_round_reflect_job,
decoupled from the fold — while keeping the cheap utility-model path and the
"never overwrite a user-renamed chat" (title_upto == -1) rule intact."""

import asyncio

import pytest

from backend import chat_memory, db, engine
from backend.app import create_app
from backend.config import Settings
from backend.llm_util import UtilityCompletion


@pytest.fixture
def app(tmp_path):
    settings = Settings(data_dir=str(tmp_path / "data"),
                        memory_url="http://127.0.0.1:1")
    return create_app(settings)


def _mk_chat(con, title_upto, n_messages, title="Old Title"):
    now = db.now()
    cid = con.execute(
        "INSERT INTO chats(title, title_upto, memory_enabled, created_at, updated_at) "
        "VALUES(?, ?, 0, ?, ?)", (title, title_upto, now, now)).lastrowid
    for i in range(n_messages):
        # Short bodies: total chars stay well under summary_threshold_chars, so
        # maybe_summarize never folds — isolating the title trigger.
        con.execute("INSERT INTO messages(chat_id, speaker, content, created_at) "
                    "VALUES(?, ?, ?, ?)",
                    (cid, "user" if i % 2 == 0 else "claude", f"msg {i}", now + i))
    con.commit()
    return cid


def _patch_utility(monkeypatch, reply="Fresh Topic"):
    """Patches the usage-reporting entry point chat_memory now calls
    (utility_complete_with_usage), not the plain utility_complete wrapper."""
    calls = []

    async def fake_utility_complete_with_usage(prompt, cfg, max_tokens=2000,
                                                model=None, timeout=None):
        calls.append({"prompt": prompt, "max_tokens": max_tokens})
        return UtilityCompletion(text=reply, input_tokens=7, output_tokens=3)

    monkeypatch.setattr(chat_memory, "utility_complete_with_usage",
                        fake_utility_complete_with_usage)
    return calls


def test_active_chat_retitles_per_round_without_fold(app, monkeypatch):
    """(1) A chat that never hits the summary-fold path still re-titles once it
    is past TITLE_REFRESH_DELTA messages: the per-round trigger."""
    cfg = app.state.settings.as_cfg()
    calls = _patch_utility(monkeypatch, reply="Titles Refreshed")
    con = db.connect()
    # titled through msg 1; 12 messages total → last_id - title_upto = 11 >= 8
    cid = _mk_chat(con, title_upto=1, n_messages=12)
    con.close()

    asyncio.run(engine.post_round_reflect_job(cid, cfg))

    con = db.connect()
    row = con.execute("SELECT title, title_upto FROM chats WHERE id=?", (cid,)).fetchone()
    last_id = con.execute("SELECT MAX(id) FROM messages WHERE chat_id=?", (cid,)).fetchone()[0]
    con.close()
    assert row["title"] == "Titles Refreshed"
    assert row["title_upto"] == last_id
    # Only the title call hit the utility model — the fold no-opped (proves the
    # retitle is decoupled from the summary fold, not riding on it).
    assert len(calls) == 1
    assert calls[0]["max_tokens"] == 16


def test_per_round_retitle_noops_below_delta(app, monkeypatch):
    """The per-round check is a cheap no-op until the delta is exceeded, so a
    freshly titled active chat is not re-titled (and the model isn't called)."""
    cfg = app.state.settings.as_cfg()
    calls = _patch_utility(monkeypatch)
    con = db.connect()
    cid = _mk_chat(con, title_upto=3, n_messages=5)  # 5 - 3 = 2 < 8
    con.close()

    asyncio.run(engine.post_round_reflect_job(cid, cfg))

    con = db.connect()
    title = con.execute("SELECT title FROM chats WHERE id=?", (cid,)).fetchone()["title"]
    con.close()
    assert title == "Old Title"
    assert calls == []


def test_summary_fold_leave_pass_still_retitles(app, monkeypatch):
    """(2) The original leave/reflection pass still re-titles a stale chat."""
    cfg = app.state.settings.as_cfg()
    _patch_utility(monkeypatch, reply="Leave Pass Title")
    con = db.connect()
    cid = _mk_chat(con, title_upto=1, n_messages=12)
    con.close()

    asyncio.run(engine.leave_chat_job(cid, cfg, memory=None))

    con = db.connect()
    title = con.execute("SELECT title FROM chats WHERE id=?", (cid,)).fetchone()["title"]
    con.close()
    assert title == "Leave Pass Title"


def test_user_renamed_chat_never_retitled(app, monkeypatch):
    """(3) title_upto == -1 (user-renamed) is locked — never touched by either
    the per-round trigger or the leave pass, and the model is never called."""
    cfg = app.state.settings.as_cfg()
    calls = _patch_utility(monkeypatch)
    con = db.connect()
    cid = _mk_chat(con, title_upto=-1, n_messages=20, title="My Name For It")
    con.close()

    asyncio.run(engine.post_round_reflect_job(cid, cfg))
    asyncio.run(engine.leave_chat_job(cid, cfg, memory=None))

    con = db.connect()
    row = con.execute("SELECT title, title_upto FROM chats WHERE id=?", (cid,)).fetchone()
    con.close()
    assert row["title"] == "My Name For It"
    assert row["title_upto"] == -1
    assert calls == []
