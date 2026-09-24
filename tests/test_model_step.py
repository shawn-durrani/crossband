"""A stronger model for one seat in one chat (#254): the plumbing.

Nothing here decides WHICH model a seat steps up to; that is the finder's
job. These tests pin what a stored step-up does once it exists:

1. The store: chat_seat_state carries the step-up beside the spoken depth,
   and a row stays alive while any of depth, a parked one-reply override
   or a step-up is set, so clearing one never drops another.
2. live_step: an owner's edit to the seat's model in settings beats a
   spoken step-up, because the row names the model it replaced.
3. The round: the seat's call runs on the stepped-up model with its own
   slug, name and persona, is told so in the VOLATILE block (cache layout
   law), and the persisted turn records both the model it ran on and the
   configured one. The speaker_start event names the model too, for the
   client's latency traces. Other chats stay on the configured model.
4. A provider refusal (a 4xx that means "not this model") moves the seat
   back and says so. A rate limit or a stall never does.
5. The v28 to v29 migration lands every old row with no step-up.

Keyless: the provider stream is always mocked. Names are the synthetic
roster (Alex)."""

import json

import pytest
from fastapi.testclient import TestClient

from backend import db, engine, model_step
from backend.app import create_app
from backend.config import Settings
from backend.providers import split_system_prompt


@pytest.fixture
def app(tmp_path):
    settings = Settings(data_dir=str(tmp_path / "data"),
                        memory_url="http://127.0.0.1:1",
                        anthropic_model="claude-sonnet-5",
                        user_name="Alex")
    return create_app(settings)


def _chat(c):
    return c.post("/api/chats", json={}).json()["id"]


def _step(chat_id, slug="claude", model="claude-opus-5",
          model_from="claude-sonnet-5", set_by="Alex"):
    con = db.connect()
    try:
        db.set_chat_seat_model(con, chat_id, slug, model, model_from=model_from,
                               label="Claude Opus 5",
                               from_label="Claude Sonnet 5",
                               set_by=set_by, source="example.org")
    finally:
        con.close()


def _models(chat_id):
    con = db.connect()
    try:
        return db.get_chat_seat_models(con, chat_id)
    finally:
        con.close()


def _send(c, chat_id, text="hi both"):
    with c.stream("POST", f"/api/chats/{chat_id}/send",
                  json={"text": text}) as r:
        return "".join(r.iter_text())


def _events(body):
    out = []
    for line in body.splitlines():
        if line.startswith("data: "):
            try:
                out.append(json.loads(line[6:]))
            except json.JSONDecodeError:
                pass
    return out


def _messages(c, chat_id):
    return c.get(f"/api/chats/{chat_id}").json()["messages"]


# ---------- the store ----------

def test_store_round_trip_and_clear(app):
    with TestClient(app, base_url="http://127.0.0.1") as c:
        chat_id = _chat(c)
        assert _models(chat_id) == {}
        _step(chat_id)
        got = _models(chat_id)["claude"]
        assert got["model"] == "claude-opus-5"
        assert got["from"] == "claude-sonnet-5"
        assert got["label"] == "Claude Opus 5"
        assert got["from_label"] == "Claude Sonnet 5"
        assert got["set_by"] == "Alex"
        assert got["source"] == "example.org"
        assert got["set_at"] > 0
        con = db.connect()
        try:
            assert db.clear_chat_seat_model(con, chat_id, "claude") is True
            assert db.clear_chat_seat_model(con, chat_id, "claude") is False
            assert db.get_chat_seat_rows(con, chat_id) == {}
        finally:
            con.close()


def test_a_row_stays_alive_while_any_field_is_set(app):
    """Clearing the depth must not drop a step-up, consuming a one-reply
    override must not drop it either, and clearing the step-up must leave
    the depth where it was."""
    with TestClient(app, base_url="http://127.0.0.1") as c:
        chat_id = _chat(c)
        con = db.connect()
        try:
            db.set_chat_seat_depth(con, chat_id, "claude", "high", set_by="Alex")
            _step(chat_id)
            db.set_chat_seat_depth(con, chat_id, "claude", "")
            assert db.get_chat_seat_state(con, chat_id) == {}
            assert "claude" in db.get_chat_seat_models(con, chat_id)

            db.set_chat_seat_once(con, chat_id, "claude", "low")
            assert db.take_chat_seat_once(con, chat_id, "claude") == "low"
            assert "claude" in db.get_chat_seat_models(con, chat_id)

            db.set_chat_seat_depth(con, chat_id, "claude", "max")
            db.set_chat_seat_depth(con, chat_id, "claude", "", keep_once=True)
            assert "claude" in db.get_chat_seat_models(con, chat_id)

            db.set_chat_seat_depth(con, chat_id, "claude", "high")
            assert db.clear_chat_seat_model(con, chat_id, "claude") is True
            assert db.get_chat_seat_state(con, chat_id) == {"claude": "high"}
            assert db.get_chat_seat_models(con, chat_id) == {}

            db.set_chat_seat_depth(con, chat_id, "claude", "")
            assert db.get_chat_seat_rows(con, chat_id) == {}
        finally:
            con.close()


def test_a_step_up_leaves_depth_and_a_parked_override_alone(app):
    with TestClient(app, base_url="http://127.0.0.1") as c:
        chat_id = _chat(c)
        con = db.connect()
        try:
            db.set_chat_seat_depth(con, chat_id, "claude", "high", set_by="Alex")
            db.set_chat_seat_once(con, chat_id, "claude", "low")
            _step(chat_id)
            assert db.get_chat_seat_rows(con, chat_id) == {
                "claude": {"reasoning_effort": "high", "once_effort": "low"}}
            assert db.get_chat_seat_setters(con, chat_id) == {"claude": "Alex"}
        finally:
            con.close()


# ---------- live_step ----------

def test_live_step_needs_the_model_it_replaced():
    row = {"model": "claude-opus-5", "from": "claude-sonnet-5"}
    seat = {"slug": "claude", "model": "claude-sonnet-5"}
    assert model_step.live_step(seat, row) is row
    # the owner changed the seat in settings: their choice wins
    assert model_step.live_step({**seat, "model": "claude-fable-5"}, row) is None
    # a step-up naming the configured model changes nothing
    assert model_step.live_step({**seat, "model": "claude-opus-5"},
                                {**row, "from": "claude-opus-5"}) is None
    assert model_step.live_step(seat, None) is None
    assert model_step.live_step(seat, {"model": "", "from": ""}) is None


def test_model_note_names_who_and_both_models():
    step = {"model": "claude-opus-5", "from": "claude-sonnet-5",
            "label": "Claude Opus 5", "from_label": "Claude Sonnet 5",
            "set_by": "Alex"}
    note = model_step.model_note(step, "Claude")
    assert "Alex asked for a stronger model" in note
    assert "Claude Opus 5 instead of your configured Claude Sonnet 5" in note
    assert "You're still Claude" in note
    assert "back to normal" in note
    assert "never say you have" in note
    nobody = model_step.model_note({**step, "set_by": ""}, "Claude")
    assert nobody.split("\n")[2].startswith("Someone in this chat asked")
    assert model_step.model_note(None, "Claude") == ""


# ---------- the round ----------

def test_the_round_runs_the_stepped_up_model_and_tells_the_seat(app, monkeypatch):
    captured = []

    async def stream_reply(participant, roster, transcript, names, cfg, project,
                           chat_summary, voice_mode, tools=None, memory=None):
        captured.append((dict(participant), dict(cfg)))
        yield ("text", "ok")
        yield ("usage", {"input": 10, "cache_read": 0, "cache_creation": 0,
                         "output": 5})

    monkeypatch.setattr(engine.providers, "stream_reply", stream_reply)
    with TestClient(app, base_url="http://127.0.0.1") as c:
        chat_id = _chat(c)
        other_id = _chat(c)
        _step(chat_id)
        events = _events(_send(c, chat_id))
        by_slug = {p["slug"]: (p, cfg) for p, cfg in captured}
        claude, claude_cfg = by_slug["claude"]
        gpt, gpt_cfg = by_slug["gpt"]
        # the model moved, and nothing that says who the seat is did
        assert claude["model"] == "claude-opus-5"
        assert claude["stepped_from"] == "claude-sonnet-5"
        assert claude["name"] == "Claude" and claude["slug"] == "claude"
        assert gpt["model"] == "gpt-5.1" and "stepped_from" not in gpt
        assert "Claude Opus 5 instead of" in claude_cfg["model_note"]
        assert gpt_cfg["model_note"] == ""
        # the note rides the VOLATILE block, never the cached stable one
        stable, volatile = split_system_prompt(
            claude, [claude, gpt], claude_cfg, None, "", False)
        assert "Your model (this chat)" in volatile
        assert "Your model (this chat)" not in stable
        # speaker_start names the model the turn runs on
        starts = {e["speaker"]: e for e in events if e["type"] == "speaker_start"}
        assert starts["claude"]["model"] == "claude-opus-5"
        assert starts["gpt"]["model"] == "gpt-5.1"
        # the stored turn records both models
        reply = next(m for m in _messages(c, chat_id) if m["speaker"] == "claude")
        usage = json.loads(reply["usage_json"])
        assert usage["model"] == "claude-opus-5"
        assert usage["stepped_from"] == "claude-sonnet-5"

        # another chat is untouched: the step-up is per chat
        captured.clear()
        _send(c, other_id)
        other = {p["slug"]: p for p, _ in captured}
        assert other["claude"]["model"] == "claude-sonnet-5"
        reply = next(m for m in _messages(c, other_id) if m["speaker"] == "claude")
        assert "stepped_from" not in json.loads(reply["usage_json"])


def test_a_settings_edit_beats_the_step_up(app, monkeypatch):
    captured = []

    async def stream_reply(participant, roster, transcript, names, cfg, project,
                           chat_summary, voice_mode, tools=None, memory=None):
        captured.append((dict(participant), dict(cfg)))
        yield ("text", "ok")

    monkeypatch.setattr(engine.providers, "stream_reply", stream_reply)
    with TestClient(app, base_url="http://127.0.0.1") as c:
        chat_id = _chat(c)
        _step(chat_id, model_from="claude-haiku-4-5")  # replaced an older setting
        _send(c, chat_id)
        claude, cfg = next((p, g) for p, g in captured if p["slug"] == "claude")
        assert claude["model"] == "claude-sonnet-5"
        assert cfg["model_note"] == ""


class _Refusal(Exception):
    def __init__(self, status):
        super().__init__(f"provider said {status}")
        self.status_code = status


@pytest.mark.parametrize("status,reverts", [(400, True), (404, True),
                                            (429, False), (529, False)])
def test_a_provider_refusal_moves_the_seat_back(app, monkeypatch, status, reverts):
    async def stream_reply(participant, roster, transcript, names, cfg, project,
                           chat_summary, voice_mode, tools=None, memory=None):
        if participant.get("stepped_from"):
            raise _Refusal(status)
        yield ("text", "ok")

    monkeypatch.setattr(engine.providers, "stream_reply", stream_reply)
    with TestClient(app, base_url="http://127.0.0.1") as c:
        chat_id = _chat(c)
        _step(chat_id)
        _send(c, chat_id)
        system = [m["content"] for m in _messages(c, chat_id)
                  if m["speaker"] == "system"]
        if reverts:
            assert _models(chat_id) == {}
            assert system == [
                "Claude is back on its configured model, Claude Sonnet 5, "
                "for this chat: the provider refused Claude Opus 5."]
        else:
            assert "claude" in _models(chat_id)
            assert system == []


def test_refused_reads_the_status_code_only():
    assert model_step.refused(_Refusal(400)) is True
    assert model_step.refused(_Refusal(403)) is True
    assert model_step.refused(_Refusal(429)) is False
    assert model_step.refused(RuntimeError("produced nothing for 90s")) is False


# ---------- schema migration (v28 -> v29) ----------

def test_v28_to_v29_migration_lands_old_rows_with_no_step_up(tmp_path):
    import sqlite3
    data = tmp_path / "data2"
    data.mkdir()
    con0 = sqlite3.connect(data / "chat.db")
    con0.executescript(
        "CREATE TABLE chat_seat_state(chat_id INTEGER NOT NULL,"
        " slug TEXT NOT NULL, reasoning_effort TEXT NOT NULL DEFAULT '',"
        " once_effort TEXT NOT NULL DEFAULT '', set_by TEXT NOT NULL DEFAULT '',"
        " once_by TEXT NOT NULL DEFAULT '', set_at REAL NOT NULL DEFAULT 0,"
        " updated_at REAL NOT NULL, PRIMARY KEY (chat_id, slug));"
        "INSERT INTO chat_seat_state VALUES(1, 'claude', 'high', '', 'Alex', '',"
        " 5, 7);")
    con0.execute("PRAGMA user_version = 28")
    con0.commit()
    con0.close()
    db.configure(data)
    db.init()
    con = db.connect()
    try:
        assert (con.execute("PRAGMA user_version").fetchone()[0]
                == db.SCHEMA_VERSION)
        row = con.execute("SELECT * FROM chat_seat_state").fetchone()
        assert row["reasoning_effort"] == "high"
        assert row["model"] == "" and row["model_from"] == ""
        assert row["model_set_at"] == 0
        assert db.get_chat_seat_models(con, 1) == {}
        # clearing the old depth now deletes the row, as it always did
        db.set_chat_seat_depth(con, 1, "claude", "")
        assert db.get_chat_seat_rows(con, 1) == {}
    finally:
        con.close()
