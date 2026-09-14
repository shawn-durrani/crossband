"""The seat ledger (#162): a content-free record of what each completion
did, so a seat that replays an old reply can be told from a client that
sent twice or a stream that stalled, after the fact and without a word
of what anyone said."""

import asyncio
import time

import pytest
from fastapi.testclient import TestClient

from backend import engine, rounds, seat_trace
from backend.app import create_app
from backend.config import Settings

LONG = "the same long answer, word for word, as the last time it was asked"


@pytest.fixture
def app(tmp_path):
    return create_app(Settings(data_dir=str(tmp_path / "data"),
                               memory_url="http://127.0.0.1:1"))


def _stream(chunks, *, fail=None, meta=None):
    async def stream_reply(participant, roster, transcript, names, cfg, project,
                           chat_summary, voice_mode, tools=None, memory=None):
        for ch in chunks:
            await asyncio.sleep(0)
            yield ("text", ch)
        if meta:
            yield ("meta", meta)
        if fail:
            raise fail
        yield ("usage", {"input": 1, "cache_read": 0, "cache_creation": 0,
                         "output": 1})
    return stream_reply


def _wait_idle(chat_id, timeout=5.0):
    deadline = time.time() + timeout
    while time.time() < deadline:
        if not rounds.active(chat_id):
            return
        time.sleep(0.02)
    raise AssertionError("round did not finish")


def _send(c, chat_id, text="hi @claude"):
    with c.stream("POST", f"/api/chats/{chat_id}/send", json={"text": text}) as r:
        for _ in r.iter_lines():
            pass
    _wait_idle(chat_id)


# ── the ledger itself ───────────────────────────────────────────────────────

def test_an_entry_holds_timings_sizes_and_a_hash_never_the_text():
    seat = {"slug": "qwen", "model": "qwen3", "base_url": "http://127.0.0.1:8080/v1"}
    e = seat_trace.begin(7, 3, seat)
    seat_trace.text(e, "hello ")
    seat_trace.text(e, "world")
    seat_trace.finish_reason(e, "stop")
    seat_trace.finish(e, "hello world", "ok")
    assert e["upstream"] == "127.0.0.1:8080" and e["model"] == "qwen3"
    assert e["chunks"] == 2 and e["chars"] == 11
    assert e["first_token_ms"] is not None and e["duration_ms"] is not None
    assert e["finish"] == "stop" and e["outcome"] == "ok"
    assert e["text_sha"] == seat_trace.text_hash("hello world")
    assert "hello" not in repr(e)


def test_a_hosted_seat_names_its_provider_not_a_key():
    e = seat_trace.begin(1, 1, {"slug": "claude", "provider": "anthropic",
                                "api_key_env": "ANTHROPIC_API_KEY"})
    assert e["upstream"] == "anthropic"
    assert "ANTHROPIC" not in repr(e)


def test_a_repeated_reply_is_marked_as_a_repeat_of_the_earlier_round(caplog):
    seat = {"slug": "qwen"}
    first = seat_trace.begin(1, 10, seat)
    seat_trace.finish(first, LONG, "ok")
    other_chat = seat_trace.begin(2, 11, seat)
    seat_trace.finish(other_chat, LONG, "ok")           # another chat: not a repeat
    again = seat_trace.begin(1, 12, seat)
    seat_trace.finish(again, LONG, "ok")
    assert first["repeat_of"] is None and other_chat["repeat_of"] is None
    assert again["repeat_of"] == 10
    assert any("seat_trace repeat" in r.getMessage() for r in caplog.records)
    assert LONG not in caplog.text


def test_short_or_failed_replies_are_never_repeats():
    seat = {"slug": "qwen"}
    a = seat_trace.begin(1, 1, seat)
    seat_trace.finish(a, "Yes.", "ok")
    b = seat_trace.begin(1, 2, seat)
    seat_trace.finish(b, "Yes.", "ok")
    assert b["repeat_of"] is None                       # too short to mean anything
    c = seat_trace.begin(1, 3, seat)
    seat_trace.finish(c, LONG, "stalled", error="RuntimeError")
    d = seat_trace.begin(1, 4, seat)
    seat_trace.finish(d, LONG, "ok")
    assert d["repeat_of"] is None                       # the earlier one never completed
    assert c["error"] == "RuntimeError"


def test_a_doubled_send_is_written_down_by_hash():
    assert seat_trace.note_send(5, "again please") is None
    dup = seat_trace.note_send(5, "again please")
    assert dup["kind"] == "duplicate_send" and dup["after_ms"] < 1000
    assert seat_trace.note_send(5, "something else") is None
    rows = seat_trace.entries(5)
    assert [r["kind"] for r in rows] == ["duplicate_send"]
    assert "again" not in repr(rows)


def test_the_ledger_is_bounded_and_filters_by_chat():
    for i in range(seat_trace.MAX_ENTRIES + 20):
        seat_trace.finish(seat_trace.begin(i % 3, i, {"slug": "s"}), "", "ok")
    assert len(seat_trace.entries(limit=10_000)) == seat_trace.MAX_ENTRIES
    assert all(r["chat_id"] == 1 for r in seat_trace.entries(1, limit=10_000))


# ── through a real round ────────────────────────────────────────────────────

def test_a_round_records_each_seat_and_a_verbatim_repeat(app, monkeypatch):
    monkeypatch.setattr(engine.providers, "stream_reply",
                        _stream([LONG[:20], LONG[20:]], meta={"finish": "stop"}))
    with TestClient(app, base_url="http://127.0.0.1") as c:
        chat_id = c.post("/api/chats", json={}).json()["id"]
        _send(c, chat_id)
        _send(c, chat_id, "and again @claude")
        rows = c.get(f"/api/models/seat_trace?chat_id={chat_id}").json()["entries"]
    done = [r for r in rows if r["kind"] == "completion"]
    assert len(done) == 2
    assert done[0]["seat"] == "claude" and done[0]["outcome"] == "ok"
    assert done[0]["chunks"] == 2 and done[0]["chars"] == len(LONG)
    assert done[0]["finish"] == "stop" and done[0]["round_id"]
    assert done[0]["repeat_of"] is None
    assert done[1]["repeat_of"] == done[0]["round_id"]
    assert LONG not in str(rows)


def test_a_seat_that_fails_or_stalls_is_recorded_as_such(app, monkeypatch):
    monkeypatch.setattr(engine.providers, "stream_reply",
                        _stream(["half a"], fail=ValueError("upstream said no")))
    with TestClient(app, base_url="http://127.0.0.1") as c:
        chat_id = c.post("/api/chats", json={}).json()["id"]
        _send(c, chat_id)
        rows = c.get(f"/api/models/seat_trace?chat_id={chat_id}").json()["entries"]
    (row,) = [r for r in rows if r["kind"] == "completion"]
    assert row["outcome"] == "error" and row["error"] == "ValueError"
    assert row["chars"] == 6 and row["text_sha"] is not None
    assert "upstream said no" not in str(rows)


def test_a_doubled_send_shows_in_the_chats_ledger(app, monkeypatch):
    monkeypatch.setattr(engine.providers, "stream_reply", _stream(["ok"]))
    with TestClient(app, base_url="http://127.0.0.1") as c:
        chat_id = c.post("/api/chats", json={}).json()["id"]
        _send(c, chat_id, "same words @claude")
        _send(c, chat_id, "same words @claude")
        rows = c.get(f"/api/models/seat_trace?chat_id={chat_id}").json()["entries"]
    assert [r["kind"] for r in rows] == ["completion", "duplicate_send", "completion"]


def test_the_voice_dump_carries_the_chats_ledger(app, monkeypatch):
    monkeypatch.setattr(engine.providers, "stream_reply", _stream(["ok"]))
    with TestClient(app, base_url="http://127.0.0.1") as c:
        chat_id = c.post("/api/chats", json={}).json()["id"]
        _send(c, chat_id)
        out = c.post("/api/voice/debug-dump",
                     json={"chat_id": chat_id, "entries": []}).json()
        import json
        from backend import db
        bundle = json.loads((db.DATA_DIR / "voice_debug" / out["file"]).read_text())
    assert bundle["seat_trace"] and bundle["seat_trace"][0]["seat"] == "claude"
