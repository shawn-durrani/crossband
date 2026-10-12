"""The instruction check on Jev, live (#659).

Pins: with `intent_check` left at claude, or with no key, only the merged
Claude call reads the turn; with it set to jev, Jev answers first and its
verdict is used; Claude is asked as well only when Jev says the turn names,
loses or renames someone, or switches room mode on, and then the names are
Claude's and everything else Jev's; a failed or empty Jev answer falls back
to Claude; an answered Jev call is an unpriced utility_usage row of kind
intent_check; the room-mode "off" guard for company and back-soon remarks
applies to Jev's answer; and the log carries timings, never words.
"""

import asyncio
import json
import logging

import pytest
from fastapi.testclient import TestClient

from backend import db, introductions, intent, jev
from backend.app import create_app
from backend.config import Settings

ON = {"intent_check": "jev", "user_name": "Alex"}


def _answers(**choices):
    return {k: {"choice": v, "confidence": 0.95} for k, v in choices.items()}


NOTHING = _answers(mode="none", people="no", depth="none", depth_seat="all",
                   depth_once="no", research="none", model="none")


@pytest.fixture
def calls(monkeypatch, tmp_path):
    """Stand-ins for both readers. `state['jev']` is Jev's reply,
    `state['claude']` the merged prompt's."""
    state = {"jev": {"answers": NOTHING, "model": "jev-1.13.0", "ms": 300,
                     "usage": {"input_tokens": 400, "output_tokens": 7}},
             "claude": json.dumps({"mode_command": "none"}), "asked": []}

    async def fake_jev(st, questions, timeout_s=1.0, client=None):
        state["asked"].append("jev")
        return dict(state["jev"])

    async def fake_claude(chat_id, kind, prompt, cfg, max_tokens=2000):
        state["asked"].append("claude")
        return state["claude"]

    monkeypatch.setattr(introductions.jev, "ask", fake_jev)
    monkeypatch.setattr(introductions.llm_util, "utility_complete_logged", fake_claude)
    monkeypatch.setenv("TYPESAFE_API_KEY", "test-key")
    app = create_app(Settings(data_dir=str(tmp_path / "data"),
                              memory_url="http://127.0.0.1:1"))
    with TestClient(app, base_url="http://127.0.0.1") as c:
        state["chat"] = c.post("/api/chats", json={}).json()["id"]
        yield state


def _read(calls, text, cfg):
    return asyncio.run(introductions.read_instructions(
        calls["chat"], text, cfg, ["Claude", "GPT"], [], [], False))


def test_claude_alone_reads_the_turn_by_default_or_without_a_key(calls, monkeypatch):
    calls["claude"] = json.dumps({"research": "more"})
    verdict, reply = _read(calls, "research this properly", {"user_name": "Alex"})
    assert calls["asked"] == ["claude"] and verdict["research"] == "more"
    monkeypatch.delenv("TYPESAFE_API_KEY")
    calls["asked"].clear()
    _read(calls, "research this properly", ON)
    assert calls["asked"] == ["claude"]


def test_jev_alone_reads_a_turn_with_no_names(calls):
    calls["jev"]["answers"] = {**NOTHING, **_answers(depth="deep", depth_seat="Claude")}
    verdict, reply = _read(calls, "Claude, take your time from now on", ON)
    assert calls["asked"] == ["jev"] and reply is None
    assert verdict["depth"] == [{"seat": "Claude", "depth": "deep", "once": False}]
    con = db.connect()
    try:
        rows = [dict(r) for r in con.execute(
            "SELECT kind, model, input_tokens, cost FROM utility_usage")]
    finally:
        con.close()
    assert rows == [{"kind": "intent_check", "model": "jev-1.13.0",
                     "input_tokens": 400, "cost": None}]


@pytest.mark.parametrize("answers", [_answers(people="yes"), _answers(mode="on")])
def test_names_come_from_claude_and_the_rest_from_jev(calls, answers):
    calls["jev"]["answers"] = {**NOTHING, **answers, **_answers(research="more")}
    calls["claude"] = json.dumps({"introductions": ["Sam"], "research": "none"})
    verdict, reply = _read(calls, "Group mode on, and say hi to Sam.", ON)
    assert calls["asked"] == ["jev", "claude"] and reply == calls["claude"]
    assert verdict["introductions"] == ["Sam"]
    assert verdict["research"] == "more"


@pytest.mark.parametrize("reply", [
    {"answers": {}, "model": "jev-latest", "ms": 1500, "usage": {}, "error": "ReadTimeout"},
    {"answers": {}, "model": "jev-latest", "ms": 200, "usage": {}},
])
def test_a_failed_jev_answer_falls_back_to_claude(calls, reply, caplog):
    calls["jev"] = reply
    calls["claude"] = json.dumps({"mode_command": "on"})
    caplog.set_level(logging.INFO, logger="crossband.introductions")
    verdict, _ = _read(calls, "group mode on please", ON)
    assert calls["asked"] == ["jev", "claude"] and verdict["mode_command"] == "on"
    logged = " ".join(r.getMessage() for r in caplog.records)
    assert "asking claude" in logged and "group mode" not in logged


def test_the_company_guard_applies_to_jevs_off():
    off = _answers(mode="off")
    verdict, _ = intent.parse_jev_check(off, "It's just me and Sam tonight.")
    assert verdict["mode_command"] == "none"
    verdict, _ = intent.parse_jev_check(off, "Solo mode, it's just me now.")
    assert verdict["mode_command"] == "off"
    _, names = intent.parse_jev_check(_answers(mode="on"), "Group mode on.")
    assert names


def test_the_live_check_sends_the_questions_the_trial_measured():
    state, q = intent.build_jev_check("Group mode on.", "Alex", ["Claude"], [], [])
    assert set(q) == {"mode", "people", "depth", "depth_seat", "depth_once",
                      "research", "model"}
    assert jev.record_usage and introductions.JEV_TIMEOUT_S <= 2.0
