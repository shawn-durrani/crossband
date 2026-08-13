"""Auto-title refresh triggers.

The 8-message TITLE_REFRESH_DELTA re-title used to fire ONLY when the leave/
reflection pass ran (summary fold on chat-switch/idle sweep). A chat that stays
continuously active never triggered that pass, so it outran its title forever.
The fix runs maybe_title_chat after EVERY round via post_round_reflect_job,
decoupled from the fold - while keeping the cheap utility-model path and the
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
        # maybe_summarize never folds - isolating the title trigger.
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
    # Only the title call hit the utility model - the fold no-opped (proves the
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
    """(3) title_upto == -1 (user-renamed) is locked - never touched by either
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


# ── attribution survives compression, enforced (#22) ────────────────────────
#
# Compression is where who-said-what quietly dies: the fold prompt demands
# [Speaker] tags, but a prompt is guidance - a summary that ignored it used to
# replace the original turns and ride back in as trusted context, which is how
# one agent later reads another's point as its own. The floor is structural
# now: a tag-free summary is REFUSED, the originals stay in context, and the
# scripted replay below catches a regression mechanically rather than by ear.

def _mk_big_chat(con, cfg):
    """Enough transcript weight that maybe_summarize actually folds."""
    now = db.now()
    cid = con.execute(
        "INSERT INTO chats(title, memory_enabled, created_at, updated_at) "
        "VALUES('t', 0, ?, ?)", (now, now)).lastrowid
    body = "x" * 4000
    speakers = ["user", "claude", "gpt"]
    for i in range(30):
        con.execute(
            "INSERT INTO messages(chat_id, speaker, content, created_at) "
            "VALUES(?, ?, ?, ?)",
            (cid, speakers[i % 3], f"turn {i}: {body}", now + i))
    con.commit()
    return cid


def _fold_once(app, monkeypatch, reply):
    cfg = dict(app.state.settings.as_cfg())
    cfg["summary_threshold_chars"] = 1000
    cfg["keep_recent_messages"] = 4
    con = db.connect()
    try:
        cid = _mk_big_chat(con, cfg)
        _patch_utility(monkeypatch, reply=reply)
        chat = dict(con.execute("SELECT * FROM chats WHERE id=?", (cid,)).fetchone())
        messages = [dict(r) for r in con.execute(
            "SELECT * FROM messages WHERE chat_id=? ORDER BY id", (cid,))]
        for m in messages:
            m["attachments"] = []
        summary, recent = asyncio.run(
            chat_memory.maybe_summarize(con, chat, messages, cfg))
        after = dict(con.execute("SELECT * FROM chats WHERE id=?", (cid,)).fetchone())
        return summary, recent, after, len(messages)
    finally:
        con.close()


def test_tagged_summary_folds_and_advances(app, monkeypatch):
    reply = ("[Alex] asked for the deploy to wait. [Claude] proposed the "
             "rollback plan; [GPT] disagreed on timing.")
    summary, recent, after, total = _fold_once(app, monkeypatch, reply)
    assert summary == reply
    assert after["summary"] == reply
    assert after["summary_upto"] > 0
    assert len(recent) == 4                      # only the keep-tail remains


def test_tagfree_summary_is_refused_originals_stay(app, monkeypatch):
    """The scripted replay the issue asks for: a summary that lost every
    speaker tag never replaces the turns it summarised."""
    reply = ("The group discussed the deploy and someone proposed a rollback "
             "plan; timing was disputed and a decision is pending.")
    summary, recent, after, total = _fold_once(app, monkeypatch, reply)
    assert not after["summary"]                   # nothing replaced
    assert summary == after["summary"] or not summary
    assert after["summary_upto"] == 0             # watermark never advanced
    assert len(recent) == total                   # every original still live


def test_single_voice_fold_needs_only_that_voice(app, monkeypatch):
    assert chat_memory.summary_attribution_ok(
        "[Alex] listed the tasks for the week.", {"Alex"})
    assert not chat_memory.summary_attribution_ok(
        "Tasks were listed for the week.", {"Alex"})


def test_attribution_floor_truth_table():
    labels = {"Alex", "Claude", "GPT"}
    ok = chat_memory.summary_attribution_ok
    # two distinct folded voices tagged: enough
    assert ok("[Alex] asked X. [GPT] answered Y.", labels)
    # one tag for three voices: single-voice mush, refused
    assert not ok("[Claude] did everything, apparently.", labels)
    # tags naming nobody in the fold do not count
    assert not ok("[Narrator] recaps. [Someone] agreed.", labels)
    # no labels folded (empty chunk): vacuously fine
    assert ok("anything", set())


def test_fold_labels_use_display_names(app):
    cfg = {"user_name": "Alex"}
    names = {"claude": "Claude", "gpt": "GPT"}
    msgs = [{"speaker": "user", "content": "hi"},
            {"speaker": "claude", "content": "hello"},
            {"speaker": "gpt", "content": "  "},          # blank: not counted
            {"speaker": "ext:watch", "content": "note"}]
    assert chat_memory.fold_labels(msgs, names, cfg) == {"Alex", "Claude",
                                                         "ext:watch"}
