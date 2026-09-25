"""The seat ledger (#162): a content-free record of what each completion
did, so a seat that replays an old reply can be told from a client that
sent twice or a stream that stalled, after the fact and without a word
of what anyone said.

The 25 Sep reproduction added the cross-seat half: local seats copied
another seat's latest reply 15 times and the same-seat repeat flag saw
none of it. A reply matching any seat's recent reply, exactly or after
normalising case, whitespace and an echoed "[Name · time]:" head, is
flagged with the seat and round it copied. Flag only: every copy is
still posted, and the round runs as before."""

import asyncio
import json
import logging
import time

import pytest
from fastapi.testclient import TestClient

from backend import engine, rounds, seat_trace
from backend.app import create_app
from backend.config import Settings

LONG = "the same long answer, word for word, as the last time it was asked"

# Long enough for the echo guard to judge (echo.MIN_REPLY_CHARS), so a copy
# of it in a round meets both the ledger and the guard.
ROOM_LONG = (
    "Sam's bench plan holds up: sand the top first, fit the vice on the "
    "left, drill the dog holes on a 96mm grid, and finish with two coats of "
    "hard wax oil. Bolt the frame to the wall before any planing so nothing "
    "racks, and leave the tool well until the top is flat and dry."
)


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


def _scripted(fn, calls):
    """fn(call_index) -> the reply text. Records each call's seat."""
    async def stream_reply(participant, roster, transcript, names, cfg,
                           project, chat_summary, voice_mode, tools=None,
                           memory=None):
        calls.append(participant["slug"])
        yield ("text", fn(len(calls) - 1))
    return stream_reply


def _wait_idle(chat_id, timeout=5.0):
    deadline = time.time() + timeout
    while time.time() < deadline:
        if not rounds.active(chat_id):
            return
        time.sleep(0.02)
    raise AssertionError("round did not finish")


def _send(c, chat_id, text="hi @claude"):
    events = []
    with c.stream("POST", f"/api/chats/{chat_id}/send", json={"text": text}) as r:
        for line in r.iter_lines():
            if line.startswith("data: "):
                events.append(json.loads(line[6:]))
    _wait_idle(chat_id)
    return events


def _warnings(caplog, marker):
    return [r.getMessage() for r in caplog.records
            if r.levelno >= logging.WARNING and marker in r.getMessage()]


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
    # the any-seat check sees it too, and one finding is one warning line
    assert again["copy_of"]["seat"] == "qwen" and again["copy_of"]["back"] == 1
    assert len(_warnings(caplog, "seat_trace")) == 1


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


def test_a_copy_of_another_seats_reply_is_flagged(caplog):
    a = seat_trace.begin(1, 20, {"slug": "qwen-a"})
    seat_trace.finish(a, LONG, "ok")
    b = seat_trace.begin(1, 20, {"slug": "qwen-b"})
    seat_trace.finish(b, LONG, "ok")
    assert a["copy_of"] is None
    assert b["repeat_of"] is None                       # not its own reply
    assert b["copy_of"] == {"seat": "qwen-a", "round_id": 20,
                            "seq": a["seq"], "back": 1, "match": "exact"}
    (line,) = _warnings(caplog, "seat_trace copy")
    assert "seat=qwen-b" in line and "copies seat=qwen-a round=20" in line
    assert "back=1 match=exact" in line
    assert LONG not in caplog.text and LONG not in repr(seat_trace.entries(1))


def test_a_near_copy_with_an_echoed_label_is_flagged():
    a = seat_trace.begin(1, 30, {"slug": "qwen-a"})
    seat_trace.finish(a, LONG, "ok")
    echoed = ("[Qwen A · 2026-09-25T08:14+10:00]:  "
              + LONG.upper().replace(", ", ",\n   ") + "  ")
    b = seat_trace.begin(1, 31, {"slug": "qwen-b"})
    seat_trace.finish(b, echoed, "ok")
    assert b["text_sha"] != a["text_sha"]
    assert b["norm_sha"] == a["norm_sha"]
    assert b["copy_of"] == {"seat": "qwen-a", "round_id": 30,
                            "seq": a["seq"], "back": 1, "match": "normalised"}


def test_normalising_touches_only_case_whitespace_and_a_leading_head():
    n = seat_trace.normalised
    assert n("[Sam · 2026-09-25T08:14+10:00]:  Hello\n  THERE ") == "hello there"
    assert n("[Qwen · t]: [Qwen · t]: hello") == "hello"     # a doubled head
    assert n("hello [Qwen · t]: there") == "hello [qwen · t]: there"
    assert n("[1] a footnote") == "[1] a footnote"            # no colon, no head
    assert n("Hello, there.") != n("Hello there")             # punctuation counts


def test_agreement_that_shares_words_is_not_a_copy(caplog):
    a = seat_trace.begin(1, 1, {"slug": "qwen-a"})
    seat_trace.finish(a, LONG, "ok")
    for i, reply in enumerate([
            "Agreed: that is the same long answer I'd give, word for word.",
            LONG + " One more thing: check the date it was asked.",
            "the same long answer, word for word, as the first time it was asked",
    ]):
        b = seat_trace.begin(1, 2 + i, {"slug": "qwen-b"})
        seat_trace.finish(b, reply, "ok")
        assert b["copy_of"] is None and b["repeat_of"] is None
    assert not _warnings(caplog, "seat_trace")


def test_a_copy_is_judged_against_the_latest_replies_only():
    def run(chat_id, fillers):
        a = seat_trace.begin(chat_id, 1, {"slug": "qwen-a"})
        seat_trace.finish(a, LONG, "ok")
        for i in range(fillers):
            f = seat_trace.begin(chat_id, 2 + i, {"slug": "qwen-c"})
            seat_trace.finish(f, f"a different reply of forty characters or more, {i}", "ok")
        b = seat_trace.begin(chat_id, 99, {"slug": "qwen-b"})
        seat_trace.finish(b, LONG, "ok")
        return b
    at_edge = run(1, seat_trace.COPY_WINDOW - 1)
    assert at_edge["copy_of"]["back"] == seat_trace.COPY_WINDOW
    assert run(2, seat_trace.COPY_WINDOW)["copy_of"] is None


def test_a_reply_the_chat_never_showed_is_not_a_copy_source():
    a = seat_trace.begin(1, 1, {"slug": "qwen-a"})
    seat_trace.finish(a, LONG, "ok")
    seat_trace.echo_hit(a, "retry", "round", seat="qwen-z", source_text="x")
    b = seat_trace.begin(1, 2, {"slug": "qwen-b"})
    seat_trace.finish(b, LONG, "cancelled")             # never completed
    c = seat_trace.begin(1, 3, {"slug": "qwen-c"})
    seat_trace.finish(c, LONG, "ok")
    assert c["copy_of"] is None


def test_the_echo_guard_names_the_restated_reply_in_ledger_terms():
    a = seat_trace.begin(1, 40, {"slug": "qwen-a"})
    seat_trace.finish(a, ROOM_LONG, "ok")
    b = seat_trace.begin(1, 40, {"slug": "qwen-b"})
    seat_trace.finish(b, ROOM_LONG, "ok")
    hit = seat_trace.echo_hit(b, "logged", "round", seat="qwen-a",
                              source_text=ROOM_LONG)
    assert hit == {"action": "logged", "ref": "round", "seat": "qwen-a",
                   "round_id": 40, "seq": a["seq"]}
    assert b["echo_guard"] == hit
    # a cut-off reply is stored with a marker, so the ledger can't place it,
    # and the record still names the seat
    hit = seat_trace.echo_hit(b, "logged", "round", seat="qwen-a",
                              source_text=ROOM_LONG + "\n\n[cut off by Alex]")
    assert hit["seat"] == "qwen-a" and hit["round_id"] is None
    assert "bench" not in repr(seat_trace.entries(1))


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


def test_a_copy_across_seats_in_a_round_is_flagged_and_still_posted(
        app, monkeypatch, caplog):
    calls = []
    monkeypatch.setattr(engine.providers, "stream_reply",
                        _scripted(lambda i: LONG, calls))
    with TestClient(app, base_url="http://127.0.0.1") as c:
        chat_id = c.post("/api/chats", json={}).json()["id"]
        events = _send(c, chat_id, "a note for the room, no question")
        msgs = c.get(f"/api/chats/{chat_id}").json()["messages"]
        rows = c.get(f"/api/models/seat_trace?chat_id={chat_id}").json()["entries"]
    first, second = calls
    assert first != second
    # posting is untouched: both replies land, nothing is dropped
    assert [(m["speaker"], m["content"]) for m in msgs[1:]] == \
        [(first, LONG), (second, LONG)]
    assert not [e for e in events if e["type"] in ("passed", "error")]
    a, b = [r for r in rows if r["kind"] == "completion"]
    assert a["copy_of"] is None
    assert b["copy_of"] == {"seat": first, "round_id": a["round_id"],
                            "seq": a["seq"], "back": 1, "match": "exact"}
    assert b["echo_guard"] is None          # too short for the echo guard
    assert len(_warnings(caplog, "seat_trace copy")) == 1
    assert LONG not in str(rows) and LONG not in caplog.text


def test_a_voice_copy_is_logged_by_the_guard_in_the_ledgers_terms(
        app, monkeypatch, caplog):
    calls = []
    monkeypatch.setattr(engine.providers, "stream_reply",
                        _scripted(lambda i: ROOM_LONG, calls))
    with TestClient(app, base_url="http://127.0.0.1") as c:
        chat_id = c.post("/api/chats", json={}).json()["id"]
        c.patch(f"/api/chats/{chat_id}", json={"voice_mode": True})
        events = _send(c, chat_id, "a note for the room, no question")
        msgs = c.get(f"/api/chats/{chat_id}").json()["messages"]
        rows = c.get(f"/api/models/seat_trace?chat_id={chat_id}").json()["entries"]
    first, second = calls
    # already spoken by completion: delivered, never retried or dropped
    assert [m["content"] for m in msgs[1:]] == [ROOM_LONG, ROOM_LONG]
    assert not [e for e in events if e["type"] == "passed"]
    a, b = [r for r in rows if r["kind"] == "completion"]
    assert b["copy_of"]["seat"] == first and b["copy_of"]["seq"] == a["seq"]
    assert b["echo_guard"] == {"action": "logged", "ref": "round",
                               "seat": first, "round_id": a["round_id"],
                               "seq": a["seq"]}
    (line,) = _warnings(caplog, "echo_guard")
    assert "action=logged" in line and f"speaker={second}" in line
    assert f"of_seat={first} of_round={a['round_id']} of_seq={a['seq']}" in line
    assert "bench" not in str(rows) and "bench" not in caplog.text


def test_a_text_copy_the_echo_guard_retries_is_recorded_as_dropped(
        app, monkeypatch):
    fresh = ("Nobody mentioned the vice: a quick-release screw saves more "
             "time than any grid layout, and it's worth buying before the "
             "top is drilled. Mateo's old one would fit if the chop is "
             "trimmed by a centimetre and the guide rods are cut to length.")
    calls = []
    monkeypatch.setattr(engine.providers, "stream_reply",
                        _scripted(lambda i: fresh if i == 2 else ROOM_LONG,
                                  calls))
    with TestClient(app, base_url="http://127.0.0.1") as c:
        chat_id = c.post("/api/chats", json={}).json()["id"]
        _send(c, chat_id, "a note for the room, no question")
        msgs = c.get(f"/api/chats/{chat_id}").json()["messages"]
        rows = c.get(f"/api/models/seat_trace?chat_id={chat_id}").json()["entries"]
    # the echo guard's text-mode behaviour is unchanged: one retry, fresh lands
    assert [m["content"] for m in msgs[1:]] == [ROOM_LONG, fresh]
    a, dropped, retry = [r for r in rows if r["kind"] == "completion"]
    assert dropped["copy_of"]["seq"] == a["seq"]
    assert dropped["echo_guard"]["action"] == "retry"
    assert dropped["echo_guard"]["seq"] == a["seq"]
    assert retry["copy_of"] is None and retry["echo_guard"] is None
