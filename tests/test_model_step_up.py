"""The spoken cues that step a chat up to a stronger model (#254, slice 3).

Pinned here, keyless, with the intent verdict, the model list, the search
and the ranking all mocked:

1. Who moves: a standing "think harder" or "maximum thinking" moves the
   seats it names (or all), "research more" moves every seat. A one-off,
   "quick" and "normal" never move a model up, and a misheard name moves
   nobody.
2. The scan end to end: the depth or research line first, then the cost
   line, then the step-up written. A step is a change, so no "heard,
   nothing changed" line. The `model_step_up` setting off means no
   research at all, and a keyless seat is left alone without a word.
3. Back to normal: to everyone it returns every seat to its configured
   model with its own line, even when no depth was set. Naming one seat
   returns that seat only. After a settings edit the line names the model
   the seat is really on.
4. The running-cost line names the stronger model, alone or beside the
   depth, and ignores a step-up a settings edit has ended.

Names are the synthetic roster (Alex, Sam)."""

import asyncio
import json

import pytest
from fastapi.testclient import TestClient

from backend import db, depth, introductions, model_step, spend_note
from backend.app import create_app
from backend.config import Settings

CAPS = {"image_input": {"supported": True}, "pdf_input": {"supported": True},
        "effort": {"supported": True}}
MODELS = [
    {"id": "claude-sonnet-5", "label": "Claude Sonnet 5", "max_input_tokens":
     1_000_000, "max_tokens": 128_000, "capabilities": CAPS},
    {"id": "claude-opus-5", "label": "Claude Opus 5", "max_input_tokens":
     1_000_000, "max_tokens": 128_000, "capabilities": CAPS},
]
RESULTS = [{"title": "Best models this month", "url": "https://example.org/a",
            "content": "Claude Opus 5 leads, Claude Sonnet 5 follows."}]

ROSTER = [{"slug": "claude", "name": "Claude"}, {"slug": "gpt", "name": "GPT"}]


@pytest.fixture
def app(tmp_path):
    settings = Settings(data_dir=str(tmp_path / "data"),
                        memory_url="http://127.0.0.1:1", user_name="Alex",
                        anthropic_model="claude-sonnet-5")
    return create_app(settings)


@pytest.fixture
def world(monkeypatch):
    """The intent verdict and the ranking share the utility seam; the kind
    tells them apart. The GPT seat has no key, so only Claude moves."""
    state = {"verdict": {}, "calls": {"list": 0, "kinds": []}}

    async def utility(chat_id, kind, prompt, cfg, max_tokens=2000):
        state["calls"]["kinds"].append(kind)
        if kind == "model_step_up":
            return json.dumps({"ranking": ["claude-opus-5", "claude-sonnet-5"]})
        return json.dumps(state["verdict"])

    def list_models(provider, api_key_env=None, base_url=None):
        state["calls"]["list"] += 1
        return MODELS

    monkeypatch.setattr("backend.llm_util.utility_complete_logged", utility)
    monkeypatch.setattr(model_step.providers, "list_models", list_models)
    monkeypatch.setattr(model_step.tools, "available_backends",
                        lambda: {"tavily": True, "brave": False})
    monkeypatch.setattr(model_step.tools, "search_results",
                        lambda query, cfg, recency=None: RESULTS)
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    return state


def _cfg(app, **over):
    return {**app.state.settings.as_cfg(), **over}


def _chat(c):
    return c.post("/api/chats", json={}).json()["id"]


def _say(chat_id, text):
    con = db.connect()
    try:
        return db.insert_message(con, chat_id, "user", text)["id"]
    finally:
        con.close()


def _system(c, chat_id):
    return [m["content"] for m in c.get(f"/api/chats/{chat_id}").json()["messages"]
            if m["speaker"] == "system"]


def _models(chat_id):
    con = db.connect()
    try:
        return db.get_chat_seat_models(con, chat_id)
    finally:
        con.close()


def _scan(app, chat_id, text, **cfg):
    message_id = _say(chat_id, text)
    asyncio.run(introductions.scan_user_turn(chat_id, message_id, text,
                                             _cfg(app, **cfg)))


# ---------- who moves ----------

@pytest.mark.parametrize("verdict,moved", [
    ({"research": "more"}, ["claude", "gpt"]),
    ({"depth": [{"seat": "Claude", "depth": "deep"}]}, ["claude"]),
    ({"depth": [{"seat": "all", "depth": "max"}]}, ["claude", "gpt"]),
    ({"depth": [{"seat": "gpt", "depth": "deep"}]}, ["gpt"]),
    ({"depth": [{"seat": "Claude", "depth": "deep", "once": True}]}, []),
    ({"depth": [{"seat": "all", "depth": "quick"}]}, []),
    ({"depth": [{"seat": "all", "depth": "normal"}]}, []),
    ({"depth": [{"seat": "Clod", "depth": "deep"}]}, []),
    ({}, []),
])
def test_targets(verdict, moved):
    assert [p["slug"] for p in model_step.targets(verdict, ROSTER)] == moved


# ---------- the scan, end to end ----------

def test_think_harder_steps_claude_up_and_says_what_it_costs(app, world,
                                                             monkeypatch):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "test-key")
    world["verdict"] = {"depth": [{"seat": "Claude", "depth": "deep"}]}
    with TestClient(app, base_url="http://127.0.0.1") as c:
        chat_id = _chat(c)
        _scan(app, chat_id, "Claude, think harder about this from now on")
        lines = _system(c, chat_id)
        step = _models(chat_id)
    assert lines[0].startswith("Claude set to deep thinking by Alex")
    assert lines[1].startswith(
        "Claude moves from Claude Sonnet 5 to Claude Opus 5 for this chat, "
        "set by Alex. A web search ranked it the strongest Claude model the "
        "app can use here (example.org).")
    assert lines[1].endswith("Back to normal returns Claude to Claude Sonnet 5.")
    assert len(lines) == 2  # a real change: no "heard, nothing changed" line
    assert step["claude"]["model"] == "claude-opus-5"
    assert step["claude"]["from"] == "claude-sonnet-5"
    assert step["claude"]["set_by"] == "Alex"
    assert step["claude"]["source"] == "example.org"
    assert "gpt" not in step
    assert world["calls"]["kinds"] == ["intent_scan", "model_step_up"]


def test_research_more_steps_every_seat_it_can(app, world, monkeypatch):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "test-key")
    world["verdict"] = {"research": "more"}
    with TestClient(app, base_url="http://127.0.0.1") as c:
        chat_id = _chat(c)
        _scan(app, chat_id, "research more")
        lines = _system(c, chat_id)
    assert lines[0].startswith("Research mode on for this chat, set by Alex")
    assert lines[1].startswith("Claude moves from Claude Sonnet 5 to Claude Opus 5")
    assert len(lines) == 2  # GPT has no key: left alone without a word
    assert set(_models(chat_id)) == {"claude"}


def test_saying_it_again_does_not_research_again(app, world, monkeypatch):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "test-key")
    world["verdict"] = {"depth": [{"seat": "Claude", "depth": "deep"}]}
    with TestClient(app, base_url="http://127.0.0.1") as c:
        chat_id = _chat(c)
        _scan(app, chat_id, "Claude, think harder")
        _scan(app, chat_id, "Claude, think harder")
        lines = _system(c, chat_id)
    assert world["calls"]["list"] == 1
    # depth was already deep and the model already stepped: the honest line
    assert lines[-1].startswith("Heard an instruction to change thinking depth, "
                                "and nothing changed")


def test_a_stay_says_why_beside_the_depth_line(app, world, monkeypatch):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "test-key")
    monkeypatch.setattr(model_step.tools, "available_backends",
                        lambda: {"tavily": False, "brave": False})
    world["verdict"] = {"depth": [{"seat": "Claude", "depth": "deep"}]}
    with TestClient(app, base_url="http://127.0.0.1") as c:
        chat_id = _chat(c)
        _scan(app, chat_id, "Claude, think harder")
        lines = _system(c, chat_id)
    assert lines[1] == (
        "Claude stays on claude-sonnet-5: no web search engine is set up, and "
        "the app only moves a seat to a model a search ranks higher.")
    assert _models(chat_id) == {}


def test_the_setting_off_means_no_research_at_all(app, world, monkeypatch):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "test-key")
    world["verdict"] = {"depth": [{"seat": "Claude", "depth": "deep"}]}
    with TestClient(app, base_url="http://127.0.0.1") as c:
        chat_id = _chat(c)
        _scan(app, chat_id, "Claude, think harder", model_step_up=False)
        lines = _system(c, chat_id)
    assert len(lines) == 1 and lines[0].startswith("Claude set to deep thinking")
    assert world["calls"]["list"] == 0
    assert world["calls"]["kinds"] == ["intent_scan"]


def test_a_keyless_install_hears_the_depth_and_nothing_else(app, world,
                                                            monkeypatch):
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    world["verdict"] = {"depth": [{"seat": "all", "depth": "deep"}]}
    with TestClient(app, base_url="http://127.0.0.1") as c:
        chat_id = _chat(c)
        _scan(app, chat_id, "think harder, everyone")
        lines = _system(c, chat_id)
    assert all("thinking" in line for line in lines)
    assert world["calls"]["list"] == 0


# ---------- back to normal ----------

def _step(chat_id, slug="claude", model_from="claude-sonnet-5"):
    con = db.connect()
    try:
        db.set_chat_seat_model(con, chat_id, slug, "claude-opus-5",
                               model_from=model_from, label="Claude Opus 5",
                               from_label="Claude Sonnet 5", set_by="Alex")
    finally:
        con.close()


def test_back_to_normal_returns_the_model_even_with_no_depth_set(app):
    with TestClient(app, base_url="http://127.0.0.1") as c:
        chat_id = _chat(c)
        _step(chat_id)
        out = depth.apply_depth(chat_id, [{"seat": "all", "depth": "normal"}],
                                {"user_name": "Alex"})
        lines = _system(c, chat_id)
    assert out == "depth_cleared"
    assert lines == ["Claude is back on its configured model, Claude Sonnet 5."]
    assert _models(chat_id) == {}


def test_back_to_normal_clears_depth_and_model_with_a_line_each(app):
    with TestClient(app, base_url="http://127.0.0.1") as c:
        chat_id = _chat(c)
        depth.apply_depth(chat_id, [{"seat": "Claude", "depth": "deep"}],
                          {"user_name": "Alex"})
        _step(chat_id)
        depth.apply_depth(chat_id, [{"seat": "all", "depth": "normal"}],
                          {"user_name": "Alex"})
        lines = _system(c, chat_id)
    assert lines[1].startswith("Claude is back to its configured thinking depth")
    assert lines[2] == "Claude is back on its configured model, Claude Sonnet 5."
    con = db.connect()
    try:
        assert db.get_chat_seat_rows(con, chat_id) == {}
    finally:
        con.close()


def test_naming_one_seat_returns_that_seat_only(app):
    with TestClient(app, base_url="http://127.0.0.1") as c:
        chat_id = _chat(c)
        _step(chat_id, "claude")
        _step(chat_id, "gpt", model_from="gpt-5.1")
        depth.apply_depth(chat_id, [{"seat": "GPT", "depth": "normal"}],
                          {"user_name": "Alex"})
    assert set(_models(chat_id)) == {"claude"}


def test_after_a_settings_edit_the_line_names_the_real_model(app):
    with TestClient(app, base_url="http://127.0.0.1") as c:
        chat_id = _chat(c)
        _step(chat_id, model_from="claude-haiku-4-5")  # the seat has moved on
        depth.apply_depth(chat_id, [{"seat": "all", "depth": "normal"}],
                          {"user_name": "Alex"})
        lines = _system(c, chat_id)
    assert lines == ["Claude is back on its configured model, claude-sonnet-5."]


# ---------- apply_decisions ----------

def test_the_cost_line_lands_before_the_step_and_a_raced_seat_is_left(app):
    decision = {"slug": "claude", "name": "Claude", "provider": "anthropic",
                "outcome": "step", "key": "step", "from": "claude-sonnet-5",
                "from_label": "Claude Sonnet 5", "model": "claude-opus-5",
                "label": "Claude Opus 5", "source": "example.org",
                "unpriced": [], "cost": {"turns": 0, "rates_now": (2, 10),
                                         "rates_new": (5, 25)}}
    with TestClient(app, base_url="http://127.0.0.1") as c:
        chat_id = _chat(c)
        out = model_step.apply_decisions(chat_id, [decision], {"user_name": "Alex"})
        assert out == "model_stepped"
        con = db.connect()
        try:
            note = con.execute(
                "SELECT created_at FROM messages WHERE chat_id=? AND speaker='system'",
                (chat_id,)).fetchone()["created_at"]
            step_at = db.get_chat_seat_models(con, chat_id)["claude"]["set_at"]
        finally:
            con.close()
        assert note <= step_at
        # another cue got there first: nothing posted twice
        again = model_step.apply_decisions(chat_id, [decision],
                                           {"user_name": "Alex"})
        assert again == "no_change"
        assert len(_system(c, chat_id)) == 1


# ---------- the running-cost line ----------

def _reflect(chat_id, cfg):
    con = db.connect()
    try:
        chat = dict(con.execute("SELECT * FROM chats WHERE id=?",
                                (chat_id,)).fetchone())
        return spend_note.maybe_spend_note(
            con, chat, db.get_chat_messages(con, chat_id), cfg)
    finally:
        con.close()


def _fill(chat_id, n):
    con = db.connect()
    try:
        for i in range(n):
            db.insert_message(con, chat_id, "claude", f"reply {i}",
                              usage_json=json.dumps({
                                  "model": "claude-opus-5", "input": 1000,
                                  "output": 100, "cost": 0.01,
                                  "stepped_from": "claude-sonnet-5"}))
    finally:
        con.close()


def _notes(c, chat_id):
    return [m for m in _system(c, chat_id) if m.startswith("Running cost:")]


def test_the_running_cost_line_names_a_stepped_up_seat(app):
    cfg = {"user_name": "Alex", "spend_note_every": 3}
    with TestClient(app, base_url="http://127.0.0.1") as c:
        chat_id = _chat(c)
        _step(chat_id)
        _fill(chat_id, 3)
        assert _reflect(chat_id, {**app.state.settings.as_cfg(), **cfg}) is True
        (note,) = _notes(c, chat_id)
    assert note.startswith("Running cost: Claude on Claude Opus 5 since ")
    assert "$0.03 metered since then" in note


def test_the_running_cost_line_puts_the_model_beside_the_depth(app):
    cfg = {"user_name": "Alex", "spend_note_every": 3}
    with TestClient(app, base_url="http://127.0.0.1") as c:
        chat_id = _chat(c)
        depth.apply_depth(chat_id, [{"seat": "Claude", "depth": "deep"}], cfg)
        _step(chat_id)
        _fill(chat_id, 3)
        assert _reflect(chat_id, {**app.state.settings.as_cfg(), **cfg}) is True
        (note,) = _notes(c, chat_id)
    assert note.startswith(
        "Running cost: Claude at deep thinking on Claude Opus 5 since ")


def test_a_step_up_a_settings_edit_ended_raises_nothing(app):
    cfg = {"user_name": "Alex", "spend_note_every": 3}
    with TestClient(app, base_url="http://127.0.0.1") as c:
        chat_id = _chat(c)
        _step(chat_id, model_from="claude-haiku-4-5")
        _fill(chat_id, 3)
        assert _reflect(chat_id, {**app.state.settings.as_cfg(), **cfg}) is False


# ---------- a provider that says nothing about images ----------

def test_a_chat_with_images_and_a_silent_provider_says_so(app, world,
                                                          monkeypatch):
    """OpenAI's model list says nothing about image input. A chat carrying
    an image can't safely move a GPT seat, and the line names that reason
    instead of a vague "nothing fits"."""
    monkeypatch.setenv("OPENAI_API_KEY", "test-key")
    monkeypatch.setattr(model_step.providers, "list_models",
                        lambda provider, api_key_env=None, base_url=None: [
                            {"id": "gpt-5.1", "label": "gpt-5.1",
                             "max_input_tokens": None, "max_tokens": None,
                             "capabilities": None},
                            {"id": "gpt-5.5", "label": "gpt-5.5",
                             "max_input_tokens": None, "max_tokens": None,
                             "capabilities": None}])
    with TestClient(app, base_url="http://127.0.0.1") as c:
        chat_id = _chat(c)
        con = db.connect()
        try:
            msg = db.insert_message(con, chat_id, "user", "what is this?")
            con.execute(
                "INSERT INTO attachments(message_id, filename, stored_name, mime, "
                "size, created_at) VALUES(?,?,?,?,?,?)",
                (msg["id"], "a.png", "a.png", "image/png", 1000, 0))
            con.commit()
        finally:
            con.close()
        gpt = next(p for p in c.get("/api/state").json()["participants"]
                   if p["slug"] == "gpt")
        decisions = asyncio.run(model_step.find_step_ups(
            chat_id, [gpt], _cfg(app)))
    assert [d["key"] for d in decisions] == ["unknown_media"]
    assert model_step.stay_lines(decisions) == [
        "GPT stays on gpt-5.1: this chat carries images or PDF files, and "
        "OpenAI doesn't say which of its models take them."]


def test_the_same_price_per_token_is_said_plainly():
    d = {"name": "Claude", "provider": "anthropic", "label": "Claude Opus 5",
         "from_label": "Claude Opus 4.8", "source": "example.org",
         "unpriced": [], "cost": {"turns": 0, "rates_now": (5.0, 25.0),
                                  "rates_new": (5.0, 25.0)}}
    assert ("Claude Opus 5 costs the same per token as Claude Opus 4.8, $5 in "
            "and $25 out per million tokens, rate-card prices.") \
        in model_step.step_line(d)
