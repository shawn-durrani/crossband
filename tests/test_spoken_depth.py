"""Spoken reasoning depth (#105, slice 1): a spoken cue sets a PERSISTENT
per-chat, per-seat reasoning depth, until explicitly changed - latency and
intelligence stop being one knob.

Pinned here:
1. The pure pieces: prefilter shapes, defensive verdict parsing, the level
   words.
2. apply_depth semantics: named seat vs all, unknown names change nothing,
   "normal" clears (and clearing an already-default seat is a no-change),
   every real change leaves a system notice stating the trade.
3. The engine overlay: a stored depth rides the provider call as the seat's
   reasoning_effort for that chat, and the seat is told its mode in the
   volatile block (never the stable one - cache layout law).
4. The scan end to end at the llm_util seam, same fire-and-forget shape as
   room commands: confirmed changes apply after dispatch, and talk ABOUT
   thinking confirms as no changes.

Keyless like everything else; the utility model is always mocked."""

import asyncio
import json

import pytest
from fastapi.testclient import TestClient

from backend import db, depth, engine, introductions
from backend.app import create_app
from backend.config import Settings
from backend.providers import split_system_prompt
from roomkit import as_utility_completion

CFG = {"user_name": "Shawn"}


@pytest.fixture
def app(tmp_path):
    settings = Settings(data_dir=str(tmp_path / "data"),
                        memory_url="http://127.0.0.1:1")
    return create_app(settings)


def _seat_state(chat_id):
    con = db.connect()
    try:
        return db.get_chat_seat_state(con, chat_id)
    finally:
        con.close()


def _messages(client, chat_id):
    return client.get(f"/api/chats/{chat_id}").json()["messages"]


# ---------- the pure pieces ----------

def test_prefilter_shapes():
    yes = ["slow down and think harder about this",
           "take your time, Claude",
           "quick answers from now on please",
           "back to normal now",
           "maximum effort on this one",
           "can you think deeper"]
    no = ["what do you think about the plan?",
          "good thinking",
          "the quick brown fox",
          "let's talk about depth charges"]
    for t in yes:
        assert depth.depth_prefilter(t), t
    for t in no:
        assert not depth.depth_prefilter(t), t


def test_parse_verdict_is_defensive():
    ok = json.dumps({"changes": [{"seat": "Claude", "depth": "deep"},
                                 {"seat": "all", "depth": "normal"}]})
    assert depth.parse_depth_verdict(ok) == [
        {"seat": "Claude", "depth": "deep", "once": False},
        {"seat": "all", "depth": "normal", "once": False}]
    # anything off-shape degrades to nothing, never raises
    for bad in (None, "", "no json here", '{"changes": "deep"}',
                json.dumps({"changes": [{"seat": "Claude", "depth": "warp"}]}),
                json.dumps({"changes": [{"seat": "", "depth": "deep"}]}),
                json.dumps({"changes": [42]})):
        assert depth.parse_depth_verdict(bad) == []


# ---------- apply semantics ----------

def test_apply_named_seat_all_and_unknown(app):
    with TestClient(app, base_url="http://127.0.0.1") as c:
        chat_id = c.post("/api/chats", json={}).json()["id"]
        out = depth.apply_depth(
            chat_id, [{"seat": "Claude", "depth": "deep"}], CFG)
        assert out == "depth_set"
        assert _seat_state(chat_id) == {"claude": "high"}
        # the trade is stated in the transcript, by the system
        notices = [m for m in _messages(c, chat_id) if m["speaker"] == "system"]
        assert len(notices) == 1
        assert "deep thinking" in notices[0]["content"]
        assert "take longer" in notices[0]["content"]

        # "all" moves every seat; a misheard name moves nobody
        assert depth.apply_depth(
            chat_id, [{"seat": "all", "depth": "quick"}], CFG) == "depth_set"
        assert _seat_state(chat_id) == {"claude": "low", "gpt": "low"}
        assert depth.apply_depth(
            chat_id, [{"seat": "Mysteron", "depth": "deep"}], CFG) == "no_change"
        assert _seat_state(chat_id) == {"claude": "low", "gpt": "low"}


def test_normal_clears_and_default_seats_stay_silent(app):
    with TestClient(app, base_url="http://127.0.0.1") as c:
        chat_id = c.post("/api/chats", json={}).json()["id"]
        # clearing a chat with no spoken state is an honest no-change: no
        # row written, no notice inserted
        assert depth.apply_depth(
            chat_id, [{"seat": "all", "depth": "normal"}], CFG) == "no_change"
        assert [m for m in _messages(c, chat_id) if m["speaker"] == "system"] == []

        depth.apply_depth(chat_id, [{"seat": "gpt", "depth": "max"}], CFG)
        assert _seat_state(chat_id) == {"gpt": "max"}
        out = depth.apply_depth(
            chat_id, [{"seat": "all", "depth": "normal"}], CFG)
        assert out == "depth_cleared"
        assert _seat_state(chat_id) == {}
        cleared = [m for m in _messages(c, chat_id)
                   if m["speaker"] == "system"][-1]
        assert "back to its configured thinking depth" in cleared["content"]


# ---------- the engine overlay ----------

def test_round_carries_spoken_depth_and_tells_the_seat(app, monkeypatch):
    captured = []

    async def stream_reply(participant, roster, transcript, names, cfg, project,
                           chat_summary, voice_mode, tools=None, memory=None):
        captured.append((dict(participant), dict(cfg)))
        yield ("text", "ok")

    monkeypatch.setattr(engine.providers, "stream_reply", stream_reply)
    with TestClient(app, base_url="http://127.0.0.1") as c:
        chat_id = c.post("/api/chats", json={}).json()["id"]
        depth.apply_depth(chat_id, [{"seat": "Claude", "depth": "deep"}], CFG)
        with c.stream("POST", f"/api/chats/{chat_id}/send",
                      json={"text": "hi both"}) as r:
            "".join(r.iter_text())
    by_slug = {p["slug"]: (p, cfg) for p, cfg in captured}
    claude, claude_cfg = by_slug["claude"]
    gpt, gpt_cfg = by_slug["gpt"]
    # the spoken depth rides the call for that seat only
    assert claude["reasoning_effort"] == "high"
    assert (gpt.get("reasoning_effort") or "") == ""
    # and the seat is told, in the VOLATILE block (cache layout law)
    assert "Your reasoning depth" in claude_cfg["depth_note"]
    assert gpt_cfg["depth_note"] == ""
    stable, volatile = split_system_prompt(
        claude, [claude, gpt], claude_cfg, None, "", False)
    assert "Your reasoning depth" in volatile
    assert "Your reasoning depth" not in stable


# ---------- the scan, end to end at the llm_util seam ----------

@pytest.fixture
def utility(monkeypatch):
    state = {"depth": {"changes": []}, "calls": []}

    async def fake_utility(prompt, cfg, max_tokens=2000):
        state["calls"].append(prompt)
        if "INSTRUCTING a change" in prompt:  # the depth prompt
            return json.dumps(state["depth"])
        return json.dumps({"introductions": [], "departures": []})

    monkeypatch.setattr("backend.llm_util.utility_complete_with_usage",
                        as_utility_completion(fake_utility))
    return state


def test_scan_applies_confirmed_depth(app, utility):
    utility["depth"] = {"changes": [{"seat": "Claude", "depth": "deep"}]}
    with TestClient(app, base_url="http://127.0.0.1") as c:
        chat_id = c.post("/api/chats", json={}).json()["id"]
        asyncio.run(introductions.scan_user_turn(
            chat_id, None, "Claude, slow down and think harder here", CFG))
        assert _seat_state(chat_id) == {"claude": "high"}


def test_scan_leaves_discussion_alone(app, utility):
    # the model judged the turn as talk ABOUT thinking - nothing changes
    with TestClient(app, base_url="http://127.0.0.1") as c:
        chat_id = c.post("/api/chats", json={}).json()["id"]
        asyncio.run(introductions.scan_user_turn(
            chat_id, None, "do you ever slow down and think harder?", CFG))
        assert _seat_state(chat_id) == {}


# ---------- the one-reply override (#105 slice 2) ----------

def test_parse_carries_the_once_flag_defensively():
    ok = json.dumps({"changes": [
        {"seat": "Claude", "depth": "quick", "once": True},
        {"seat": "all", "depth": "deep"},
        {"seat": "gpt", "depth": "deep", "once": "yes"}]})  # non-bool: false
    assert depth.parse_depth_verdict(ok) == [
        {"seat": "Claude", "depth": "quick", "once": True},
        {"seat": "all", "depth": "deep", "once": False},
        {"seat": "gpt", "depth": "deep", "once": False}]


def test_once_parks_quietly_and_normal_once_is_nothing(app):
    with TestClient(app, base_url="http://127.0.0.1") as c:
        chat_id = c.post("/api/chats", json={}).json()["id"]
        out = depth.apply_depth(
            chat_id, [{"seat": "Claude", "depth": "quick", "once": True}], CFG)
        assert out == "depth_once"
        # nothing persistent changed and no mode notice was posted
        assert _seat_state(chat_id) == {}
        assert [m for m in _messages(c, chat_id)
                if m["speaker"] == "system"] == []
        # "normal, just this once" instructs nothing
        assert depth.apply_depth(
            chat_id, [{"seat": "gpt", "depth": "normal", "once": True}],
            CFG) == "no_change"


def test_once_is_consumed_exactly_once_and_survives_a_clear(app):
    with TestClient(app, base_url="http://127.0.0.1") as c:
        chat_id = c.post("/api/chats", json={}).json()["id"]
        con = db.connect()
        try:
            # pending once on a seat with a persistent depth underneath
            db.set_chat_seat_depth(con, chat_id, "claude", "high")
            db.set_chat_seat_once(con, chat_id, "claude", "low")
            # clearing the persistent depth must not kill the pending once
            db.set_chat_seat_depth(con, chat_id, "claude", "")
            assert db.take_chat_seat_once(con, chat_id, "claude") == "low"
            assert db.take_chat_seat_once(con, chat_id, "claude") == ""
            # and a consumed once on a persistent seat leaves the depth alone
            db.set_chat_seat_depth(con, chat_id, "gpt", "max")
            db.set_chat_seat_once(con, chat_id, "gpt", "low")
            assert db.take_chat_seat_once(con, chat_id, "gpt") == "low"
            assert db.get_chat_seat_state(con, chat_id) == {"gpt": "max"}
        finally:
            con.close()


def test_round_consumes_the_once_then_reverts_to_the_standing_depth(
        app, monkeypatch):
    """'Just answer this one quickly' over a standing 'think hard': the next
    reply runs quick with a THIS-reply-only note; the reply after is back on
    the standing depth with the standing note."""
    captured = []

    async def stream_reply(participant, roster, transcript, names, cfg, project,
                           chat_summary, voice_mode, tools=None, memory=None):
        captured.append((participant.get("reasoning_effort"),
                         cfg.get("depth_note", "")))
        yield ("text", "ok")

    monkeypatch.setattr(engine.providers, "stream_reply", stream_reply)
    with TestClient(app, base_url="http://127.0.0.1") as c:
        chat_id = c.post("/api/chats", json={}).json()["id"]
        depth.apply_depth(chat_id, [{"seat": "Claude", "depth": "deep"}], CFG)
        depth.apply_depth(
            chat_id, [{"seat": "Claude", "depth": "quick", "once": True}], CFG)
        for text in ("first", "second"):
            with c.stream("POST", f"/api/chats/{chat_id}/send",
                          json={"text": f"@claude {text}"}) as r:
                "".join(r.iter_text())
    (eff1, note1), (eff2, note2) = captured
    assert eff1 == "low" and "THIS reply only" in note1
    assert eff2 == "high" and "THIS reply only" not in note2
    assert "Your reasoning depth" in note2  # the standing mode note is back


# ---------- schema migration (v22 -> v23) ----------

def test_v22_to_v23_migration_leaves_old_rows_without_an_override(tmp_path):
    import sqlite3
    data = tmp_path / "data2"
    data.mkdir()
    con0 = sqlite3.connect(data / "chat.db")
    con0.executescript(
        "CREATE TABLE chat_seat_state(chat_id INTEGER NOT NULL,"
        " slug TEXT NOT NULL, reasoning_effort TEXT NOT NULL DEFAULT '',"
        " updated_at REAL NOT NULL, PRIMARY KEY (chat_id, slug));"
        "INSERT INTO chat_seat_state VALUES(1, 'claude', 'high', 0);")
    con0.execute("PRAGMA user_version = 22")
    con0.commit()
    con0.close()
    db.configure(data)
    db.init()
    con = db.connect()
    try:
        assert (con.execute("PRAGMA user_version").fetchone()[0]
                == db.SCHEMA_VERSION)
        row = con.execute("SELECT * FROM chat_seat_state").fetchone()
        assert row["once_effort"] == "" and row["reasoning_effort"] == "high"
    finally:
        con.close()
