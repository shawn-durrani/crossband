"""A cut reply reads only as far as it was heard (#634).

When someone cut a spoken reply off, crossband saved everything the seat had
written, and a reply that had finished writing before the cut was saved
whole with no mark. The models and memory then treated words nobody heard
as said. Now the browser's stop says how much of each reply played, and the
server records that point beside the reply.

Pins: the point is found by the last words heard, near the word count, and
by the count alone when those words aren't there; a reply heard in full is
left alone; readers see the heard part, the cut-off note, and the rest
apart as `unheard`, while the saved content stays as written; the stop route
records a finished reply by its id and the reply it just cut by its seat;
memory's rows read the same way; a reply with a written part is left alone;
and an older database gains the column.
"""

import asyncio
import json
import sqlite3

import pytest
from fastapi.testclient import TestClient

from backend import db, engine, heard, rounds
from backend.app import create_app
from backend.config import Settings

REPLY = ("Soak the breadcrumbs in milk for a few minutes. Then mix them through "
         "the mince with an egg, and chill the balls before browning.")


# ---------- finding the point ----------

def test_the_last_words_heard_find_the_point():
    assert REPLY[:heard.heard_offset(REPLY, 5, ["in", "milk"])] \
        == "Soak the breadcrumbs in milk"


def test_punctuation_straight_after_the_last_word_counts_as_heard():
    assert REPLY[:heard.heard_offset(REPLY, 9, ["a", "few", "minutes"])] \
        == "Soak the breadcrumbs in milk for a few minutes."


def test_the_last_words_win_over_a_count_that_is_off():
    # Speech leaves things out, so the browser's count can run a word or two
    # off the saved text. The words themselves settle it.
    assert REPLY[:heard.heard_offset(REPLY, 13, ["then", "mix"])] \
        == "Soak the breadcrumbs in milk for a few minutes. Then mix"


def test_without_the_last_words_the_count_decides():
    assert REPLY[:heard.heard_offset(REPLY, 3, [])] == "Soak the breadcrumbs"
    assert REPLY[:heard.heard_offset(REPLY, 3, ["nowhere", "found"])] \
        == "Soak the breadcrumbs"


def test_a_link_left_out_of_speech_doesnt_throw_the_point():
    body = "Read the guide at https://example.com/guide first, then sand it down."
    # Spoken: "Read the guide at first, then" - six words, the address unsaid.
    assert body[:heard.heard_offset(body, 6, ["first", "then"])] \
        == "Read the guide at https://example.com/guide first, then"


def test_heard_in_full_or_nothing_heard():
    assert heard.heard_offset(REPLY, 999, []) is None
    assert heard.heard_offset(REPLY, 23, ["before", "browning"]) is None
    assert heard.heard_offset(REPLY, 0, []) == 0


def test_the_cut_off_note_comes_off_before_the_point_is_found():
    assert heard.strip_cut_marker("Hello there.\n\n[cut off by Sam]") == "Hello there."
    assert heard.strip_cut_marker("[cut off by Sam]") == ""
    assert heard.strip_cut_marker("A [cut off by] aside, kept.") == "A [cut off by] aside, kept."


# ---------- reading a cut reply ----------

def _row(content, upto=None, note="[cut off by Sam]"):
    return {"content": content,
            "heard": json.dumps({"upto": upto, "note": note}) if upto is not None else ""}


def test_readers_see_the_heard_part_then_the_note_and_the_rest_apart():
    m = db.heard_view(_row(REPLY, 47))
    assert m["content"] == ("Soak the breadcrumbs in milk for a few minutes."
                            "\n\n[cut off by Sam]")
    assert m["unheard"] == ("Then mix them through the mince with an egg, "
                            "and chill the balls before browning.")


def test_a_reply_saved_with_its_cut_off_note_moves_the_note():
    m = db.heard_view(_row(REPLY + "\n\n[cut off by Sam]", 28))
    assert m["content"] == "Soak the breadcrumbs in milk\n\n[cut off by Sam]"


def test_nothing_heard_reads_as_the_note_alone():
    m = db.heard_view(_row(REPLY, 0))
    assert m["content"] == "[cut off by Sam]"
    assert m["unheard"] == REPLY


def test_rows_with_nothing_recorded_or_a_bad_record_read_as_they_are():
    for row in (_row(REPLY), {"content": REPLY, "heard": "not json"},
                {"content": REPLY, "heard": '{"upto": "x"}'},
                _row(REPLY, 9999), {"content": REPLY}):
        before = dict(row)
        assert db.heard_view(row)["content"] == REPLY
        assert "unheard" not in db.heard_view(dict(before))


# ---------- the stop route ----------

@pytest.fixture
def app(tmp_path):
    settings = Settings(data_dir=str(tmp_path / "data"),
                        memory_url="http://127.0.0.1:1")
    return create_app(settings)


def _stream(chunks, delay=0.0):
    async def stream_reply(participant, roster, transcript, names, cfg, project,
                           chat_summary, voice_mode, tools=None, memory=None):
        for ch in chunks:
            await asyncio.sleep(delay)
            yield ("text", ch)
    return stream_reply


def _async_client(app):
    import httpx
    return httpx.AsyncClient(transport=httpx.ASGITransport(app=app),
                             base_url="http://127.0.0.1")


async def _until(cond, timeout=5.0):
    for _ in range(int(timeout / 0.02)):
        if cond():
            return
        await asyncio.sleep(0.02)
    raise AssertionError("condition never held")


def test_a_reply_cut_after_it_finished_writing_reads_as_far_as_it_was_heard(app, monkeypatch):
    monkeypatch.setattr(engine.providers, "stream_reply", _stream([REPLY]))
    with TestClient(app, base_url="http://127.0.0.1") as c:
        chat_id = c.post("/api/chats", json={}).json()["id"]
        c.post(f"/api/chats/{chat_id}/send", json={"text": "hi @claude"}).read()
        reply = c.get(f"/api/chats/{chat_id}").json()["messages"][-1]
        assert reply["content"] == REPLY, "saved whole: the round had finished"
        got = c.post(f"/api/chats/{chat_id}/round/abort", json={"heard": [
            {"seat": reply["speaker"], "message_id": reply["id"], "words": 9,
             "tail": ["a", "few", "minutes"]}]}).json()
        assert got == {"aborted": False, "heard": 1}
        after = c.get(f"/api/chats/{chat_id}").json()["messages"][-1]
        assert after["content"] == ("Soak the breadcrumbs in milk for a few minutes."
                                    "\n\n[cut off by User]")
        assert after["unheard"].startswith("Then mix them")
        # The second stop for the same reply changes nothing.
        again = c.post(f"/api/chats/{chat_id}/round/abort", json={"heard": [
            {"seat": reply["speaker"], "message_id": reply["id"], "words": 2,
             "tail": []}]}).json()
        assert again["heard"] == 0
        # The saved content is still as written, and memory reads the heard part.
        con = db.connect()
        try:
            raw = con.execute("SELECT content FROM messages WHERE id=?",
                              (reply["id"],)).fetchone()[0]
            rows = engine.ingest_rows(con, chat_id, 0)
        finally:
            con.close()
        assert raw == REPLY
        mem = [r for r in rows if r["id"] == reply["id"]][0]
        assert mem["content"].endswith("minutes.\n\n[cut off by User]")
        assert "unheard" not in mem and "heard" not in mem


def test_the_reply_a_stop_cuts_is_found_by_its_seat(app, monkeypatch):
    monkeypatch.setattr(engine.providers, "stream_reply",
                        _stream(["Soak the breadcrumbs in milk", " for a few minutes.",
                                 " Then mix them"], delay=0.3))

    async def go():
        async with _async_client(app) as c:
            chat_id = (await c.post("/api/chats", json={})).json()["id"]
            send = asyncio.create_task(
                c.post(f"/api/chats/{chat_id}/send", json={"text": "hi @claude"}))
            await _until(lambda: rounds.active(chat_id) and sum(
                '"delta"' in e for e in rounds.active(chat_id).events) >= 2)
            seat = [json.loads(e[6:]) for e in rounds.active(chat_id).events
                    if '"speaker_start"' in e][0]["speaker"]
            got = (await c.post(f"/api/chats/{chat_id}/round/abort", json={"heard": [
                {"seat": seat, "words": 3, "tail": ["the", "breadcrumbs"]}]})).json()
            assert got == {"aborted": True, "heard": 1}
            await send
            msgs = (await c.get(f"/api/chats/{chat_id}")).json()["messages"]
            assert msgs[-1]["content"] == "Soak the breadcrumbs\n\n[cut off by User]"
            assert msgs[-1]["unheard"] == "in milk for a few minutes."

    asyncio.run(go())


def test_a_stop_without_heard_counts_works_as_before(app, monkeypatch):
    monkeypatch.setattr(engine.providers, "stream_reply", _stream([REPLY]))
    with TestClient(app, base_url="http://127.0.0.1") as c:
        chat_id = c.post("/api/chats", json={}).json()["id"]
        c.post(f"/api/chats/{chat_id}/send", json={"text": "hi @claude"}).read()
        assert c.post(f"/api/chats/{chat_id}/round/abort").json() == \
            {"aborted": False, "heard": 0}
        assert c.get(f"/api/chats/{chat_id}").json()["messages"][-1]["content"] == REPLY


def test_a_heard_count_for_someone_else_or_a_written_reply_is_left_alone(app, monkeypatch):
    written = "Here's the summary.\n\n[written]\n\nThe full plan, step by step."
    monkeypatch.setattr(engine.providers, "stream_reply", _stream([written]))
    with TestClient(app, base_url="http://127.0.0.1") as c:
        chat_id = c.post("/api/chats", json={}).json()["id"]
        c.post(f"/api/chats/{chat_id}/send", json={"text": "hi @claude"}).read()
        msgs = c.get(f"/api/chats/{chat_id}").json()["messages"]
        reply, user = msgs[-1], msgs[-2]
        got = c.post(f"/api/chats/{chat_id}/round/abort", json={"heard": [
            {"seat": reply["speaker"], "message_id": reply["id"], "words": 1, "tail": []},
            {"seat": "user", "message_id": user["id"], "words": 0, "tail": []},
            {"seat": "nobody", "message_id": reply["id"], "words": 0, "tail": []},
        ]}).json()
        assert got["heard"] == 0
        assert c.get(f"/api/chats/{chat_id}").json()["messages"][-1]["content"] == written


def test_oversized_heard_counts_are_refused(app):
    with TestClient(app, base_url="http://127.0.0.1") as c:
        chat_id = c.post("/api/chats", json={}).json()["id"]
        too_many = [{"seat": "claude", "words": 1, "tail": []}] * (heard.MAX_ENTRIES + 1)
        assert c.post(f"/api/chats/{chat_id}/round/abort",
                      json={"heard": too_many}).status_code == 422
        long_tail = [{"seat": "claude", "words": 1, "tail": ["w"] * 20}]
        assert c.post(f"/api/chats/{chat_id}/round/abort",
                      json={"heard": long_tail}).status_code == 422


# ---------- an older database ----------

def test_a_database_before_the_column_gains_it(tmp_path):
    # Today's schema with the column taken back out, stamped v33.
    data = tmp_path / "old"
    data.mkdir()
    db.configure(data)
    db.init()
    # Rebuilt by copy: older SQLite builds can't DROP COLUMN from a table
    # whose definition carries comments, as this one does.
    con0 = sqlite3.connect(data / "chat.db")
    cols = [r[1] for r in con0.execute("PRAGMA table_info(messages)") if r[1] != "heard"]
    con0.execute(f"CREATE TABLE messages_old AS SELECT {', '.join(cols)} FROM messages")
    con0.execute("DROP TABLE messages")
    con0.execute("ALTER TABLE messages_old RENAME TO messages")
    con0.execute("PRAGMA user_version = 33")
    con0.commit()
    con0.close()
    db.init()
    con = db.connect()
    try:
        cols = {r[1] for r in con.execute("PRAGMA table_info(messages)")}
        assert "heard" in cols
        assert con.execute("PRAGMA user_version").fetchone()[0] == db.SCHEMA_VERSION
    finally:
        con.close()
