"""Utility-model (Haiku) spend attribution.

`chat_memory.py`'s rolling summary / auto-title / project-distillation calls
never go through `db.insert_message` -- they write straight to `chats`/
`projects`, not the transcript -- so they had NO cost record at all, on top
of `llm_util.utility_complete_with_usage` (which already reports tokens)
sitting unused. This is the fix: the three call sites now route through it
and persist cost/model to `utility_usage` (mirroring `voice_usage`), and the
shared accounting module folds those rows in as their own SOURCE_UTILITY
bucket -- so the Spend page can finally show Claude chat vs Claude Code vs
Haiku/utility separately instead of the utility spend being invisible.
"""

import asyncio
import sqlite3

import pytest

from backend import accounting, chat_memory, db, provenance
from backend.config import Settings
from backend.llm_util import UtilityCompletion


@pytest.fixture
def con(tmp_path):
    db.configure(str(tmp_path / "data"))
    db.init(Settings(data_dir=str(tmp_path / "data")))
    c = db.connect()
    yield c
    c.close()


# ---------- db layer ----------

def test_log_utility_usage_roundtrip(con):
    now = db.now()
    con.execute("INSERT INTO chats(id, title, created_at, updated_at) VALUES(1,'T',?,?)",
                (now, now))
    con.commit()
    db.log_utility_usage(con, 1, "title", "claude-haiku-4-5", 120, 8, 0.00014)
    con.commit()
    row = con.execute("SELECT * FROM utility_usage").fetchone()
    assert row["chat_id"] == 1
    assert row["kind"] == "title"
    assert row["model"] == "claude-haiku-4-5"
    assert row["input_tokens"] == 120
    assert row["output_tokens"] == 8
    assert row["cost"] == pytest.approx(0.00014)
    assert row["provenance"] is None  # not passed -- accounting falls back to live pricing


def test_log_utility_usage_persists_provenance(con):
    """Provenance is a plain column now: a caller that
    resolves one gets it stored immutably, not recomputed later."""
    now = db.now()
    con.execute("INSERT INTO chats(id, title, created_at, updated_at) VALUES(1,'T',?,?)",
                (now, now))
    con.commit()
    db.log_utility_usage(con, 1, "title", "claude-haiku-4-5", 120, 8, 0.00014,
                         provenance=provenance.RATE_CARD_ESTIMATE)
    con.commit()
    row = con.execute("SELECT provenance FROM utility_usage").fetchone()
    assert row["provenance"] == provenance.RATE_CARD_ESTIMATE


def test_log_utility_usage_cost_may_be_none(con):
    """An unpriced utility model records as 'not tracked', never a silent
    $0.00 -- same honesty as voice_usage."""
    now = db.now()
    con.execute("INSERT INTO chats(id, title, created_at, updated_at) VALUES(1,'T',?,?)",
                (now, now))
    con.commit()
    db.log_utility_usage(con, 1, "summarize", "some-unpriced-model", 50, 5, None)
    con.commit()
    row = con.execute("SELECT cost FROM utility_usage").fetchone()
    assert row["cost"] is None


# ---------- chat_memory call sites ----------

def _mk_chat(con, **cols):
    now = db.now()
    fields = {"title": "T", "summary": "", "summary_upto": 0, "distilled_upto": 0,
             "title_upto": 1, "created_at": now, "updated_at": now}
    fields.update(cols)
    keys = ", ".join(fields)
    qs = ", ".join("?" for _ in fields)
    cid = con.execute(f"INSERT INTO chats({keys}) VALUES({qs})",
                      tuple(fields.values())).lastrowid
    con.commit()
    return cid


def _msg(con, cid, mid, speaker, content, created_at):
    con.execute("INSERT INTO messages(id, chat_id, speaker, content, created_at) "
               "VALUES(?,?,?,?,?)", (mid, cid, speaker, content, created_at))
    con.commit()


def test_maybe_title_chat_persists_utility_usage(con, monkeypatch):
    cfg = Settings().as_cfg()
    cid = _mk_chat(con, title_upto=0)
    now = db.now()
    _msg(con, cid, 1, "user", "hi", now)
    _msg(con, cid, 2, "claude", "hello!", now + 1)

    async def fake(prompt, cfg_, max_tokens=2000, model=None, timeout=None):
        return UtilityCompletion(text="A Title", input_tokens=40, output_tokens=4)

    monkeypatch.setattr(chat_memory, "utility_complete_with_usage", fake)
    chat = dict(con.execute("SELECT * FROM chats WHERE id=?", (cid,)).fetchone())
    messages = [dict(r) for r in con.execute(
        "SELECT *, '[]' AS attachments FROM messages WHERE chat_id=? ORDER BY id", (cid,))]
    for m in messages:
        m["attachments"] = []

    title = asyncio.run(chat_memory.maybe_title_chat(con, chat, messages, cfg))
    assert title == "A Title"

    row = con.execute("SELECT * FROM utility_usage WHERE chat_id=?", (cid,)).fetchone()
    assert row is not None
    assert row["kind"] == "title"
    assert row["model"] == cfg["utility_model"]
    assert row["input_tokens"] == 40
    assert row["output_tokens"] == 4
    assert row["cost"] is not None and row["cost"] > 0
    assert row["provenance"] == provenance.RATE_CARD_ESTIMATE  # recorded at write time


def test_maybe_title_chat_commits_utility_usage_even_when_title_rejected(con, monkeypatch):
    """Regression from review: a degenerate title -- empty, or quote-marks-
    only, which strips down to '' -- is falsy but NOT None, so it slips past
    _run_utility's `is None` guard: the utility_usage INSERT already happened
    (the API call was made and cost real tokens) before maybe_title_chat's own
    `if not title: return None` exits early, with no commit of its own.
    Callers (engine.py) close their connection right after with nothing in
    between, so SQLite used to implicitly roll back that pending insert --
    silently dropping the cost record for exactly the degenerate-output case
    this table exists to catch. _run_utility must commit its own insert
    immediately, independent of what the caller does with the title text."""
    cfg = Settings().as_cfg()
    cid = _mk_chat(con, title_upto=0)
    now = db.now()
    _msg(con, cid, 1, "user", "hi", now)
    _msg(con, cid, 2, "claude", "hello!", now + 1)

    async def fake(prompt, cfg_, max_tokens=2000, model=None, timeout=None):
        # A haiku reply that's nothing but quote marks: maybe_title_chat's
        # `.strip().strip('"\'“”‘’')` reduces this to "", so the SECOND
        # `if not title: return None` fires -- after the call, after the log.
        return UtilityCompletion(text='""', input_tokens=30, output_tokens=3)

    monkeypatch.setattr(chat_memory, "utility_complete_with_usage", fake)
    chat = dict(con.execute("SELECT * FROM chats WHERE id=?", (cid,)).fetchone())
    messages = [dict(r) for r in con.execute(
        "SELECT *, '[]' AS attachments FROM messages WHERE chat_id=? ORDER BY id", (cid,))]
    for m in messages:
        m["attachments"] = []

    title = asyncio.run(chat_memory.maybe_title_chat(con, chat, messages, cfg))
    assert title is None  # degenerate output -- existing title kept, as before

    # Close this connection with NO commit of our own in between -- exactly
    # what engine.py does right after calling maybe_title_chat. Before the
    # fix, this close silently rolled back the pending utility_usage insert.
    con.close()
    fresh = db.connect()
    try:
        row = fresh.execute(
            "SELECT * FROM utility_usage WHERE chat_id=?", (cid,)).fetchone()
    finally:
        fresh.close()
    assert row is not None, "utility_usage row was rolled back on connection close"
    assert row["kind"] == "title"
    assert row["input_tokens"] == 30
    assert row["output_tokens"] == 3
    assert row["cost"] is not None and row["cost"] > 0


def test_no_utility_model_available_logs_nothing(con, monkeypatch):
    """Missing key degrades exactly as before (title untouched) -- and since
    no call actually went out, nothing spurious is logged."""
    cfg = Settings().as_cfg()
    cid = _mk_chat(con, title_upto=0)
    now = db.now()
    _msg(con, cid, 1, "user", "hi", now)
    _msg(con, cid, 2, "claude", "hello!", now + 1)

    async def fake(prompt, cfg_, max_tokens=2000, model=None, timeout=None):
        return UtilityCompletion(text=None)

    monkeypatch.setattr(chat_memory, "utility_complete_with_usage", fake)
    chat = dict(con.execute("SELECT * FROM chats WHERE id=?", (cid,)).fetchone())
    messages = [dict(r) for r in con.execute(
        "SELECT *, '[]' AS attachments FROM messages WHERE chat_id=? ORDER BY id", (cid,))]
    for m in messages:
        m["attachments"] = []

    title = asyncio.run(chat_memory.maybe_title_chat(con, chat, messages, cfg))
    assert title is None
    assert con.execute("SELECT COUNT(*) c FROM utility_usage").fetchone()["c"] == 0


# ---------- accounting wiring ----------

def test_utility_usage_is_its_own_cost_source(con):
    now = db.now()
    con.execute("INSERT INTO chats(id, title, created_at, updated_at) VALUES(1,'T',?,?)",
                (now, now))
    con.commit()
    db.log_utility_usage(con, 1, "distill", "claude-haiku-4-5", 500, 100, 0.0007)
    con.commit()

    events = list(accounting.iter_cost_events(con))
    assert len(events) == 1
    e = events[0]
    assert e.source == accounting.SOURCE_UTILITY
    assert e.source != accounting.SOURCE_NORMAL  # never folded into chat-participant spend
    assert e.category == accounting.CAT_METERED
    assert e.provider == "anthropic"
    assert e.model == "claude-haiku-4-5"
    assert e.cost == pytest.approx(0.0007)
    assert e.tokens == 600
    # Row didn't record a provenance (log_utility_usage called without one, as
    # above) -- falls back to resolving against the live rate card, same as
    # ever, since it's the only source of truth available for that row.
    assert e.provenance == provenance.RATE_CARD_ESTIMATE


def test_utility_usage_prefers_recorded_provenance_over_live_rate_card(con):
    """A row's OWN recorded provenance wins even when it disagrees with what
    the live pricing table would compute today -- the same immutable-history
    discipline as messages/guest turns. Regression target: before this,
    iter_cost_events always recomputed from the live table, so a later
    rate-card edit could silently rewrite an already-logged row's provenance."""
    now = db.now()
    con.execute("INSERT INTO chats(id, title, created_at, updated_at) VALUES(1,'T',?,?)",
                (now, now))
    con.commit()
    # claude-haiku-4-5 is priced (-> rate_card_estimate live), but this row
    # recorded something else at write time -- that recorded value must win.
    db.log_utility_usage(con, 1, "title", "claude-haiku-4-5", 10, 2, 0.0,
                         provenance=provenance.SELF_HOSTED_ZERO_MARGINAL)
    con.commit()

    events = list(accounting.iter_cost_events(con))
    assert len(events) == 1
    assert events[0].provenance == provenance.SELF_HOSTED_ZERO_MARGINAL


def test_utility_usage_falls_back_when_provenance_unset(con):
    """A row with no recorded provenance (NULL -- either a pre-migration row,
    or a caller that didn't resolve one) still resolves against the live
    table, exactly as every utility_usage row did before this column
    existed."""
    now = db.now()
    con.execute("INSERT INTO chats(id, title, created_at, updated_at) VALUES(1,'T',?,?)",
                (now, now))
    con.commit()
    db.log_utility_usage(con, 1, "title", "claude-haiku-4-5", 10, 2, 0.0001)
    con.commit()

    events = list(accounting.iter_cost_events(con))
    assert events[0].provenance == provenance.RATE_CARD_ESTIMATE


def test_utility_source_has_a_distinct_spend_page_label():
    assert accounting.SOURCE_LABELS[accounting.SOURCE_UTILITY] not in (
        accounting.SOURCE_LABELS[accounting.SOURCE_NORMAL],
        accounting.SOURCE_LABELS[accounting.SOURCE_CODE],
    )


def test_unpriced_utility_model_reads_as_not_tracked(con):
    now = db.now()
    con.execute("INSERT INTO chats(id, title, created_at, updated_at) VALUES(1,'T',?,?)",
                (now, now))
    con.commit()
    db.log_utility_usage(con, 1, "title", "some-unpriced-model", 10, 2, None)
    con.commit()
    s = accounting.summarize(list(accounting.iter_cost_events(con)))
    assert accounting.SOURCE_UTILITY in s["not_tracked"]


# ---------- schema migration (v10 -> v11) ----------

def _seed_v10_db(tmp_path):
    """Build a pre-provenance-column (v10) DB: utility_usage exists but has no
    `provenance` column yet, with one already-logged row -- mirrors
    test_lifecycle.py's `_seed_v6_db` pattern for the v7 migration."""
    data = tmp_path / "data"
    data.mkdir()
    c = sqlite3.connect(data / "chat.db")
    c.executescript(
        "CREATE TABLE chats(id INTEGER PRIMARY KEY, title TEXT, created_at REAL, "
        "updated_at REAL);"
        "CREATE TABLE utility_usage(id INTEGER PRIMARY KEY AUTOINCREMENT, "
        "chat_id INTEGER, kind TEXT NOT NULL, model TEXT NOT NULL DEFAULT '', "
        "input_tokens INTEGER NOT NULL DEFAULT 0, "
        "output_tokens INTEGER NOT NULL DEFAULT 0, cost REAL, created_at REAL NOT NULL);"
    )
    now = 1_700_000_000.0
    c.execute("INSERT INTO chats(id, title, created_at, updated_at) VALUES(1,'T',?,?)",
              (now, now))
    c.execute(
        "INSERT INTO utility_usage(chat_id, kind, model, input_tokens, output_tokens, "
        "cost, created_at) VALUES(1,'title','claude-haiku-4-5',40,4,0.00014,?)", (now,))
    c.execute("PRAGMA user_version = 10")
    c.commit()
    c.close()
    db.configure(data)
    db.init()


def test_v10_to_v11_migration_adds_provenance_column(tmp_path):
    _seed_v10_db(tmp_path)
    c = db.connect()
    try:
        assert c.execute("PRAGMA user_version").fetchone()[0] == db.SCHEMA_VERSION
        cols = [r["name"] for r in c.execute("PRAGMA table_info(utility_usage)")]
        assert "provenance" in cols
        # Pre-migration row: provenance NULL, data otherwise untouched.
        row = c.execute("SELECT * FROM utility_usage WHERE chat_id=1").fetchone()
        assert row["provenance"] is None
        assert row["model"] == "claude-haiku-4-5"
        assert row["cost"] == pytest.approx(0.00014)
    finally:
        c.close()


def test_v10_to_v11_migrated_row_still_resolves_via_live_rate_card(tmp_path):
    """A row migrated from before provenance existed has no recorded value --
    accounting falls back to the live table for it, same as it always did,
    rather than reading as 'unknown'."""
    _seed_v10_db(tmp_path)
    c = db.connect()
    try:
        events = list(accounting.iter_cost_events(c))
        assert len(events) == 1
        assert events[0].provenance == provenance.RATE_CARD_ESTIMATE
    finally:
        c.close()
