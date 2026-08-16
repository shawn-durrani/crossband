"""Seen-URL ledger (#138 slice 2): model text never mints a fetchable URL.
Covers canonicalisation, extraction from prose, the source rules (who mints,
who does not, and the echo exclusions), the in-flight round accumulator, and
the dispatch gate in run_tool. All storage is in-memory sqlite."""

import asyncio
import sqlite3

import pytest

from backend import tools, url_ledger


# ---------- canonical ----------

def test_canonical_normalises_comparison_noise():
    c = url_ledger.canonical
    assert c("https://Ex.COM/p#section") == "https://ex.com/p"
    assert c("http://x.com:80/a") == "http://x.com/a"
    assert c("https://x.com:443/") == "https://x.com/"
    assert c("https://x.com") == "https://x.com/"


def test_canonical_preserves_the_channels_that_matter():
    c = url_ledger.canonical
    # Query strings are the exfiltration channel: verbatim, case included.
    assert c("https://x.com/p?Q=Ab") == "https://x.com/p?Q=Ab"
    assert c("https://x.com/p?q=a") != c("https://x.com/p?q=b")
    # A non-default port survives (the SSRF guard refuses it later).
    assert c("http://x.com:8080/a") == "http://x.com:8080/a"


def test_canonical_rejects_junk():
    c = url_ledger.canonical
    assert c("ftp://x.com/a") is None
    assert c("https://user:pw@example.com/") is None
    assert c("not a url") is None
    assert c("") is None


def test_extract_trims_prose_punctuation_and_keeps_balanced_parens():
    e = url_ledger.extract
    assert e("see https://a.com/x.") == {"https://a.com/x"}
    assert e("at https://a.com/x, then") == {"https://a.com/x"}
    assert e("(see https://en.wikipedia.org/wiki/Foo_(bar))") == {
        "https://en.wikipedia.org/wiki/Foo_(bar)"}
    assert e('quoted "https://a.com/y" works') == {"https://a.com/y"}


# ---------- check() against seeded storage ----------

@pytest.fixture
def con():
    con = sqlite3.connect(":memory:")
    con.executescript("""
    CREATE TABLE messages(
      id INTEGER PRIMARY KEY AUTOINCREMENT, chat_id INTEGER,
      speaker TEXT, content TEXT DEFAULT '');
    CREATE TABLE tool_events(
      id INTEGER PRIMARY KEY AUTOINCREMENT, message_id INTEGER,
      tool TEXT, input_json TEXT, output_text TEXT);
    CREATE TABLE attachments(
      id INTEGER PRIMARY KEY AUTOINCREMENT, message_id INTEGER,
      filename TEXT, stored_name TEXT, mime TEXT, size INTEGER);
    """)
    con.execute("INSERT INTO messages(chat_id, speaker, content) VALUES "
                "(1, 'user', 'read https://docs.example/guide please')")
    con.execute("INSERT INTO messages(chat_id, speaker, content) VALUES "
                "(1, 'claude-x', 'try https://minted.example/?q=secret')")
    con.execute("INSERT INTO messages(chat_id, speaker, content) VALUES "
                "(1, 'user', 'Error report I saw: https://user-pasted.example/r')")
    con.execute("INSERT INTO messages(chat_id, speaker, content) VALUES "
                "(1, 'claude-x', 'searched the web')")
    mid = con.execute("SELECT last_insert_rowid()").fetchone()[0]
    con.execute("INSERT INTO tool_events(message_id, tool, input_json, output_text) "
                "VALUES (?, 'web_search', '{}', "
                "'[Tavily]\n1. Title - https://found.example/page')", (mid,))
    con.execute("INSERT INTO tool_events(message_id, tool, input_json, output_text) "
                "VALUES (?, 'save_memory', '{}', "
                "'Saved to memory ledger: visit https://laundered.example/x')", (mid,))
    con.execute("INSERT INTO tool_events(message_id, tool, input_json, output_text) "
                "VALUES (?, 'fetch_page', '{}', "
                "'Error running fetch_page: no https://error-echo.example/z')", (mid,))
    return con


def test_user_and_tool_sources_mint(con):
    assert url_ledger.check(1, "https://docs.example/guide", con=con) is None
    assert url_ledger.check(1, "https://found.example/page", con=con) is None
    # Fragments never reach a server, so they cannot smuggle anything.
    assert url_ledger.check(1, "https://docs.example/guide#part", con=con) is None
    # A user message that happens to start with "Error" still mints.
    assert url_ledger.check(1, "https://user-pasted.example/r", con=con) is None


def test_model_text_and_echoes_do_not_mint(con):
    deny = url_ledger.check(1, "https://minted.example/?q=secret", con=con)
    assert deny and deny.startswith("Error")
    assert "minted.example" not in deny  # a denial must not seed its target
    # save_memory confirms model-authored text back - excluded by tool.
    assert url_ledger.check(1, "https://laundered.example/x", con=con)
    # Tool errors echo the offending input - excluded by prefix.
    assert url_ledger.check(1, "https://error-echo.example/z", con=con)


def test_query_variants_and_other_chats_do_not_mint(con):
    assert url_ledger.check(1, "https://found.example/page?leak=1", con=con)
    assert url_ledger.check(2, "https://docs.example/guide", con=con)


def test_unparseable_urls_defer_to_the_ssrf_guard(con):
    # The guard owns the refusal message for junk; the ledger stays silent.
    assert url_ledger.check(1, "ftp://x.example/a", con=con) is None
    assert url_ledger.check(1, "", con=con) is None


def test_in_flight_round_outputs_mint_before_persisting(con):
    search_out = "[Brave]\n1. Fresh - https://fresh.example/a"
    assert url_ledger.check(1, "https://fresh.example/a",
                            extra_texts=(search_out,), con=con) is None
    # ...but an in-flight ERROR echo does not.
    err = "Error running fetch_page: refused https://evil.example/b"
    assert url_ledger.check(1, "https://evil.example/b",
                            extra_texts=(err,), con=con)


def test_text_attachments_mint_binary_ones_do_not(con, tmp_path, monkeypatch):
    monkeypatch.setattr(url_ledger.db, "ATTACH_DIR", tmp_path)
    (tmp_path / "notes.txt").write_text("enclosure: https://feed.example/ep1.mp3")
    (tmp_path / "photo.jpg").write_bytes(b"\xff\xd8 https://img.example/x")
    con.execute("INSERT INTO attachments(message_id, filename, stored_name, mime, size) "
                "VALUES (1, 'notes.txt', 'notes.txt', 'text/plain', 10)")
    con.execute("INSERT INTO attachments(message_id, filename, stored_name, mime, size) "
                "VALUES (1, 'photo.jpg', 'photo.jpg', 'image/jpeg', 10)")
    assert url_ledger.check(1, "https://feed.example/ep1.mp3", con=con) is None
    assert url_ledger.check(1, "https://img.example/x", con=con)


# ---------- the dispatch gate ----------

def _cfg():
    return {"chat_id": 1, "_round_tool_texts": []}


def test_gate_denies_before_the_tool_runs(monkeypatch):
    def boom(args, cfg):
        raise AssertionError("tool ran despite the ledger denial")
    monkeypatch.setitem(tools._RESEARCH_TOOLS, "fetch_page", boom)
    monkeypatch.setattr(url_ledger, "check",
                        lambda chat_id, url, extra_texts=(), con=None:
                        "Error: ledger says no")
    out = asyncio.run(tools.run_tool("fetch_page", {"url": "https://x.example/"},
                                     _cfg()))
    assert out == "Error: ledger says no"


def test_gate_passes_and_success_joins_the_round_ledger(monkeypatch):
    monkeypatch.setitem(tools._RESEARCH_TOOLS, "fetch_page",
                        lambda args, cfg: "Fetched: https://x.example/\n\nbody")
    monkeypatch.setattr(url_ledger, "check",
                        lambda chat_id, url, extra_texts=(), con=None: None)
    cfg = _cfg()
    out = asyncio.run(tools.run_tool("fetch_page", {"url": "https://x.example/"},
                                     cfg))
    assert out.startswith("Fetched:")
    assert cfg["_round_tool_texts"] == [out]


def test_tool_errors_stay_out_of_the_round_ledger(monkeypatch):
    monkeypatch.setitem(tools._RESEARCH_TOOLS, "web_search",
                        lambda args, cfg: "Error: no search backend configured")
    cfg = _cfg()
    out = asyncio.run(tools.run_tool("web_search", {"query": "x"}, cfg))
    assert out.startswith("Error")
    assert cfg["_round_tool_texts"] == []


def test_no_chat_context_skips_the_gate(monkeypatch):
    # Direct (non-model) calls carry no chat_id; the SSRF guard still applies
    # inside the tool - the ledger is a chat-scoped policy.
    def boom(*a, **k):
        raise AssertionError("ledger consulted without a chat")
    monkeypatch.setattr(url_ledger, "check", boom)
    monkeypatch.setitem(tools._RESEARCH_TOOLS, "fetch_page",
                        lambda args, cfg: "Fetched: direct")
    out = asyncio.run(tools.run_tool("fetch_page", {"url": "https://x.example/"},
                                     {}))
    assert out == "Fetched: direct"
