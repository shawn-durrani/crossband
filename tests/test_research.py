"""Spoken research mode (#253, #417): "research more", "look into that
properly" or "go deeper" turns on a bigger tool budget and a research
routine for the rest of the chat - plan before the first search, weigh
sources, say plainly when the evidence isn't enough, cite what was found,
memory included. "Back to normal" turns it off. A new chat starts at the
defaults.

Pinned here:
1. apply_research: sets the mode, names the speaker, posts the notice once
   and returns "no_change" (never a second notice) while it is already on.
2. clear_research: posts its own notice only when the mode was actually on.
3. The engine overlay: a research-mode chat hands every seat's call the
   larger max_tool_rounds and a research_note in the volatile block, never
   the stable one (cache layout law, same as depth_note).
4. depth's #260 rule extended: a reset spoken to everyone also clears
   research mode and counts as a change; a reset naming one seat leaves it
   alone.
5. The v27 to v28 migration lands old chats with the mode off.
6. The running-cost line's research clause: the chat-wide metered figure,
   never folded into a seat's own total.

Keyless like everything else; the utility model is always mocked (the
scan-level integration lives in tests/test_room_commands.py and
tests/test_intent.py)."""

import pytest
from fastapi.testclient import TestClient

from backend import db, depth, engine, research, spend_note
from backend.app import create_app
from backend.config import Settings
from backend.providers import split_system_prompt

CFG = {"user_name": "Shawn"}


@pytest.fixture
def app(tmp_path):
    settings = Settings(data_dir=str(tmp_path / "data"),
                        memory_url="http://127.0.0.1:1")
    return create_app(settings)


def _state(chat_id):
    con = db.connect()
    try:
        row = con.execute(
            "SELECT research_mode, research_set_by, research_set_at "
            "FROM chats WHERE id=?", (chat_id,)).fetchone()
        return bool(row["research_mode"]), row["research_set_by"], row["research_set_at"]
    finally:
        con.close()


def _messages(client, chat_id):
    return client.get(f"/api/chats/{chat_id}").json()["messages"]


def _system_lines(client, chat_id):
    return [m["content"] for m in _messages(client, chat_id)
            if m["speaker"] == "system"]


def _say(chat_id, text):
    """Persist one plain user turn, the way POST /send does, and return its
    id - resolve_speaker (depth.py, shared by research.py) reads the real
    row, so naming the speaker needs a real message, not a bare cfg."""
    con = db.connect()
    try:
        return db.insert_message(con, chat_id, "user", text)["id"]
    finally:
        con.close()


# ---------- apply_research ----------

def test_apply_research_sets_the_mode_names_the_speaker_and_is_idempotent(app):
    with TestClient(app, base_url="http://127.0.0.1") as c:
        chat_id = c.post("/api/chats", json={}).json()["id"]
        message_id = _say(chat_id, "research more please")
        out = research.apply_research(chat_id, {**CFG, "user_name": "Shawn"},
                                       message_id)
        assert out == "research_set"
        on, set_by, set_at = _state(chat_id)
        assert on is True
        assert set_by == "Shawn"
        assert set_at > 0
        lines = _system_lines(c, chat_id)
        assert len(lines) == 1
        assert "Research mode on for this chat, set by Shawn" in lines[0]
        assert "Anyone can say back to normal" in lines[0]

        # already on: no second notice, an honest no-change
        out2 = research.apply_research(chat_id, CFG)
        assert out2 == "no_change"
        assert len(_system_lines(c, chat_id)) == 1


def test_apply_research_names_nobody_when_the_app_cannot_say(app):
    """No message_id, or a turn resolve_speaker can't attribute: 'set by'
    is left out of the notice entirely, same rule as depth (#255)."""
    with TestClient(app, base_url="http://127.0.0.1") as c:
        chat_id = c.post("/api/chats", json={}).json()["id"]
        research.apply_research(chat_id, CFG, message_id=None)
        lines = _system_lines(c, chat_id)
        assert "Research mode on for this chat:" in lines[0]
        assert " by " not in lines[0]


# ---------- clear_research ----------

def test_clear_research_posts_only_when_it_was_on(app):
    with TestClient(app, base_url="http://127.0.0.1") as c:
        chat_id = c.post("/api/chats", json={}).json()["id"]
        con = db.connect()
        try:
            assert research.clear_research(con, chat_id) is False
        finally:
            con.close()
        assert _system_lines(c, chat_id) == []

        research.apply_research(chat_id, CFG)
        con = db.connect()
        try:
            assert research.clear_research(con, chat_id) is True
        finally:
            con.close()
        on, set_by, set_at = _state(chat_id)
        assert on is False and set_by == "" and set_at == 0
        lines = _system_lines(c, chat_id)
        assert lines[-1] == "Research mode off for this chat."

        # off already: no second notice
        con = db.connect()
        try:
            assert research.clear_research(con, chat_id) is False
        finally:
            con.close()
        assert len(_system_lines(c, chat_id)) == 2


# ---------- the engine overlay ----------

def test_round_carries_research_mode_and_tells_the_seat(app, monkeypatch):
    captured = []

    async def stream_reply(participant, roster, transcript, names, cfg, project,
                           chat_summary, voice_mode, tools=None, memory=None):
        captured.append((dict(participant), dict(cfg)))
        yield ("text", "ok")

    monkeypatch.setattr(engine.providers, "stream_reply", stream_reply)
    with TestClient(app, base_url="http://127.0.0.1") as c:
        chat_id = c.post("/api/chats", json={}).json()["id"]
        message_id = _say(chat_id, "research more please")
        research.apply_research(chat_id, {**CFG, "user_name": "Shawn"},
                                message_id)
        with c.stream("POST", f"/api/chats/{chat_id}/send",
                      json={"text": "hi both"}) as r:
            "".join(r.iter_text())
    by_slug = {p["slug"]: (p, cfg) for p, cfg in captured}
    claude, claude_cfg = by_slug["claude"]
    gpt, gpt_cfg = by_slug["gpt"]
    default_cfg = Settings().as_cfg()
    assert claude_cfg["max_tool_rounds"] == default_cfg["research_tool_rounds"]
    assert claude_cfg["max_tool_rounds"] != default_cfg["max_tool_rounds"]
    assert gpt_cfg["max_tool_rounds"] == default_cfg["research_tool_rounds"]
    assert "Shawn turned research mode on" in claude_cfg["research_note"]
    # and the seat is told, in the VOLATILE block (cache layout law)
    stable, volatile = split_system_prompt(
        claude, [claude, gpt], claude_cfg, None, "", False)
    assert "Research mode" in volatile
    assert "Research mode" not in stable


def test_round_without_research_mode_keeps_the_ordinary_budget(app, monkeypatch):
    captured = []

    async def stream_reply(participant, roster, transcript, names, cfg, project,
                           chat_summary, voice_mode, tools=None, memory=None):
        captured.append(dict(cfg))
        yield ("text", "ok")

    monkeypatch.setattr(engine.providers, "stream_reply", stream_reply)
    with TestClient(app, base_url="http://127.0.0.1") as c:
        chat_id = c.post("/api/chats", json={}).json()["id"]
        with c.stream("POST", f"/api/chats/{chat_id}/send",
                      json={"text": "hi both"}) as r:
            "".join(r.iter_text())
    default_cfg = Settings().as_cfg()
    for cfg in captured:
        assert cfg["max_tool_rounds"] == default_cfg["max_tool_rounds"]
        assert (cfg.get("research_note") or "") == ""


# ---------- #260 extended: a reset means research mode too ----------

def test_reset_to_everyone_also_clears_research_mode(app):
    with TestClient(app, base_url="http://127.0.0.1") as c:
        chat_id = c.post("/api/chats", json={}).json()["id"]
        research.apply_research(chat_id, CFG)
        assert _state(chat_id)[0] is True
        out = depth.apply_depth(
            chat_id, [{"seat": "all", "depth": "normal"}], CFG)
        # no seat had a spoken depth: clearing research alone is still a
        # real change, not a silent no-op
        assert out == "depth_cleared"
        assert _state(chat_id)[0] is False


def test_reset_naming_one_seat_leaves_research_mode_alone(app):
    with TestClient(app, base_url="http://127.0.0.1") as c:
        chat_id = c.post("/api/chats", json={}).json()["id"]
        research.apply_research(chat_id, CFG)
        depth.apply_depth(chat_id, [{"seat": "Claude", "depth": "deep"}], CFG)
        out = depth.apply_depth(
            chat_id, [{"seat": "Claude", "depth": "normal"}], CFG)
        assert out == "depth_cleared"
        assert _state(chat_id)[0] is True  # untouched


def test_reset_to_everyone_with_nothing_at_all_is_still_a_no_change(app):
    with TestClient(app, base_url="http://127.0.0.1") as c:
        chat_id = c.post("/api/chats", json={}).json()["id"]
        out = depth.apply_depth(
            chat_id, [{"seat": "all", "depth": "normal"}], CFG)
        assert out == "no_change"


# ---------- schema migration (v27 -> v28) ----------

def test_v27_to_v28_migration_lands_old_chats_with_research_off(tmp_path):
    import sqlite3
    data = tmp_path / "data2"
    data.mkdir()
    con0 = sqlite3.connect(data / "chat.db")
    con0.executescript(
        "CREATE TABLE chats(id INTEGER PRIMARY KEY, title TEXT NOT NULL "
        "DEFAULT '', created_at REAL NOT NULL, updated_at REAL NOT NULL);"
        "INSERT INTO chats VALUES(1, 'old', 0, 0);")
    con0.execute("PRAGMA user_version = 27")
    con0.commit()
    con0.close()
    db.configure(data)
    db.init()
    con = db.connect()
    try:
        assert (con.execute("PRAGMA user_version").fetchone()[0]
                == db.SCHEMA_VERSION)
        row = con.execute(
            "SELECT research_mode, research_set_by, research_set_at "
            "FROM chats WHERE id=1").fetchone()
        assert row["research_mode"] == 0
        assert row["research_set_by"] == ""
        assert row["research_set_at"] == 0
    finally:
        con.close()


# ---------- the running-cost line's research clause ----------

def test_spend_note_gains_the_research_clause_never_a_combined_total(app):
    cfg = {**CFG, "spend_note_every": 2}
    with TestClient(app, base_url="http://127.0.0.1") as c:
        chat_id = c.post("/api/chats", json={}).json()["id"]
        research.apply_research(chat_id, cfg)
        con = db.connect()
        try:
            usage = {"input_tokens": 1000, "output_tokens": 1000, "cost": 0.75,
                     "cost_provenance": "rate_card_estimate", "auth": "api_key"}
            for slug, model, key in (("claude", "claude-sonnet-5", "ANTHROPIC_API_KEY"),):
                con.execute("UPDATE participants SET model=?, api_key_env=? "
                           "WHERE slug=?", (model, key, slug))
            con.commit()
            import json as _json
            db.insert_message(con, chat_id, "claude", "an answer",
                              usage_json=_json.dumps(usage))
            db.insert_message(con, chat_id, "user", "more")
            chat = dict(con.execute(
                "SELECT * FROM chats WHERE id=?", (chat_id,)).fetchone())
            posted = spend_note.maybe_spend_note(
                con, chat, db.get_chat_messages(con, chat_id), cfg)
        finally:
            con.close()
        assert posted is True
        note = [m for m in _messages(c, chat_id)
               if spend_note.is_spend_note(m)][0]["content"]
        assert "research mode since" in note
        assert "$0.75 metered across the chat since then" in note
        assert "$1.50" not in note and "$0.75; $0.75" not in note


def test_research_line_names_the_chat_not_a_seat():
    line = spend_note.research_line(0, {"metered": 1.25, "subscription_equiv": 0,
                                        "unknown": 0})
    assert line.startswith("research mode since ")
    assert "$1.25 metered across the chat since then" in line
