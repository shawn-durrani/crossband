"""Tests for the recall replay harness itself (crossband#252): reading user
turns out of chat.db, the summary containment check, the floor sweep, the
per-rank table, the content-free corpus, and the keyless mock run. No
membro and no key anywhere; what the live recall returns on an install is
what `python -m eval_recall` measures against a running membro."""

import asyncio
import json
import sqlite3

import pytest

from eval_recall import runner
from eval_recall.ledger import QUERY_CHARS, user_turns
from eval_recall.mock import FACTS, MockMemory, mock_turns
from eval_recall.report import render_markdown
from eval_recall.scoring import (Hit, TurnResult, aggregate, carried_by_summary,
                                 content_words, floor_sweep, hits_from_facts,
                                 rank_table)


def _ledger(tmp_path):
    db = tmp_path / "chat.db"
    con = sqlite3.connect(db)
    con.executescript("""
        CREATE TABLE chats(id INTEGER PRIMARY KEY, memory_enabled INTEGER NOT NULL DEFAULT 1);
        CREATE TABLE messages(id INTEGER PRIMARY KEY, chat_id INTEGER NOT NULL,
            speaker TEXT NOT NULL, content TEXT NOT NULL DEFAULT '', created_at REAL NOT NULL);
        INSERT INTO chats VALUES (1, 1), (2, 1), (3, 0);
        INSERT INTO messages VALUES
            (10, 1, 'user', 'first question', 1.0),
            (11, 1, 'claude', 'a reply', 2.0),
            (12, 1, 'user', '/deploy crossband', 3.0),
            (13, 1, 'user', '   ', 4.0),
            (14, 2, 'user', 'second chat question', 5.0),
            (15, 2, 'guest:sam', 'a guest turn', 6.0),
            (16, 3, 'user', 'memory is off here', 7.0);
    """)
    con.execute("INSERT INTO messages VALUES (17, 1, 'user', ?, 8.0)", ("x" * 900,))
    con.commit()
    con.close()
    return db


# ---------- ledger ----------

def test_ledger_reads_user_turns_that_would_have_recalled(tmp_path):
    turns = user_turns(_ledger(tmp_path))
    assert [t.message_id for t in turns] == [10, 14, 17]
    assert turns[0].query == "first question"
    assert len(turns[-1].query) == QUERY_CHARS


def test_ledger_filters_and_limits(tmp_path):
    db = _ledger(tmp_path)
    assert [t.message_id for t in user_turns(db, include_memory_off=True)] == [10, 14, 16, 17]
    assert [t.message_id for t in user_turns(db, chat_ids=[2])] == [14]
    # newest first while reading, so a cap keeps the latest turns
    assert [t.message_id for t in user_turns(db, max_turns=2)] == [14, 17]


# ---------- summary containment ----------

def test_carried_by_summary_needs_most_content_words_in_the_summary():
    summary = content_words("Alex keeps bees on the roof of the flat in the city")
    assert carried_by_summary("Alex keeps bees on the roof", summary)
    assert not carried_by_summary("Alex's sister Priya is moving to Hobart", summary)
    # too short to judge counts as new, never as carried
    assert not carried_by_summary("bees roof", summary)


def test_hits_keep_rank_and_score_and_tolerate_an_older_membro():
    hits = hits_from_facts([{"id": 3, "content": "a fact about Hobart moving plans",
                             "score": 0.71},
                            {"id": 4, "content": "no score on this one"}], set())
    assert [(h.rank, h.fact_id, h.score) for h in hits] == [(1, 3, 0.71), (2, 4, 0.0)]


# ---------- sweep and ranks ----------

def _results():
    def turn(mid, hits):
        return TurnResult(chat_id=1, message_id=mid, created_at=0.0, query_chars=10,
                          recall_ms=20.0, hits=hits)
    return [
        turn(1, [Hit(1, 1, 0.9, carried=True), Hit(2, 2, 0.5, carried=False),
                 Hit(3, 3, 0.2, carried=False)]),
        turn(2, [Hit(1, 4, 0.4, carried=False)]),
        turn(3, []),
    ]


def test_floor_sweep_counts_within_the_live_count():
    rows = {r["floor"]: r for r in floor_sweep(_results(), [0.0, 0.45, 0.95], live_k=2)}
    assert rows[0.0]["turns_injecting"] == pytest.approx(2 / 3)
    assert rows[0.0]["facts_per_turn"] == pytest.approx(3 / 3)   # 2 + 1 + 0, capped at 2
    assert rows[0.0]["new_facts_per_turn"] == pytest.approx(2 / 3)
    assert rows[0.45]["facts_per_turn"] == pytest.approx(2 / 3)  # 0.9, 0.5 kept; 0.4 dropped
    assert rows[0.45]["share_new"] == pytest.approx(0.5)
    assert rows[0.95]["turns_injecting"] == 0.0


def test_rank_table_says_what_each_rank_adds():
    rows = rank_table(_results(), top_k=3)
    assert rows[0]["turns_with_hit"] == pytest.approx(2 / 3)
    assert rows[0]["share_new"] == pytest.approx(0.5)
    assert rows[2]["turns_with_hit"] == pytest.approx(1 / 3)
    assert rows[2]["mean_score"] == pytest.approx(0.2)


def test_corpus_is_content_free_unless_asked():
    results = _results()
    results[0].query = "the question text"
    results[0].hits[0] = Hit(1, 1, 0.9, carried=True, content="a fact", event_date="2026-01-01")
    bare = aggregate(results)["corpus"][0]
    assert "query" not in bare and "content" not in bare["hits"][0]
    full = aggregate(results, with_content=True)["corpus"][0]
    assert full["query"] == "the question text"
    assert full["hits"][0]["content"] == "a fact"


# ---------- the mock run ----------

def test_mock_run_replays_every_turn_and_renders(capsys):
    args = runner.build_arg_parser().parse_args(["--mock"])
    report = asyncio.run(runner.run(args))
    assert report["n_turns"] == len(mock_turns())
    assert report["turns_with_hits"] < report["n_turns"]  # the joke finds nothing
    assert report["hits_total"] <= len(FACTS) * report["n_turns"]
    text = render_markdown(report, mock=True)
    assert "MOCK RUN" in text and "## Floor sweep" in text and "## By rank" in text
    assert runner.main(["--mock", "--format", "json"]) == 0
    json.loads(capsys.readouterr().out)


def test_replay_sends_the_live_query_shape():
    seen = []

    class Spy(MockMemory):
        async def recall(self, query, limit=10, origin="http", chat_id=None):
            seen.append((query, limit, origin, chat_id))
            return await super().recall(query, limit, origin, chat_id)

    turns = mock_turns()[:2]
    asyncio.run(runner.replay(turns, Spy(), top_k=7))
    assert seen == [(t.query, 7, "eval", t.chat_id) for t in turns]


def test_floors_parse_and_reject_empty():
    assert runner.parse_floors("0.5, 0, 0.3") == [0.0, 0.3, 0.5]
    with pytest.raises(SystemExit):
        runner.parse_floors(" , ")
