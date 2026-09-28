"""The context marker survives a restart and never leaks (#562).

The marker vouches for the app's own context block, and the cached system
prompt names it. It used to be random per process, so every restart
changed that cached prompt for every seat and the next call in each chat
wrote it all again. It is now derived per chat from a key in the data
folder.

The contract under test:

- two builds of a seat's stable block either side of a restart are byte
  for byte the same;
- the key is owner-only, made once, and never overwritten;
- each chat has its own marker, and another install's key gives another;
- a marker a model repeats never reaches the chat, a tool event or a tool.
"""

import asyncio
import json
import logging
import os
import secrets
import stat
import time

import pytest
from fastapi.testclient import TestClient

from backend import context_marker, engine, providers, rounds, tools
from backend.app import create_app
from backend.config import Settings

PARTICIPANT = {"name": "Claude", "slug": "claude", "model": "claude-sonnet-5",
               "provider": "anthropic", "system_prompt": ""}
ROSTER = [{"name": "Claude", "slug": "claude"}, {"name": "GPT", "slug": "gpt"}]


@pytest.fixture(autouse=True)
def _fresh_key(monkeypatch):
    """Every test starts as a bare import would: no key loaded."""
    monkeypatch.setattr(context_marker, "_key", None)


def _restart(monkeypatch):
    """What a restart does to the module: the loaded key is gone and the
    fallback key is new."""
    monkeypatch.setattr(context_marker, "_key", None)
    monkeypatch.setattr(context_marker, "_process_key", secrets.token_bytes(32))


def _stable(cfg, chat_id):
    stable, _ = providers.split_system_prompt(
        PARTICIPANT, ROSTER, dict(cfg, chat_id=chat_id), None, "", False)
    return stable


def test_two_builds_across_a_restart_are_byte_identical(tmp_path, cfg,
                                                        monkeypatch):
    context_marker.load_key(tmp_path)
    before = _stable(cfg, 7)
    frame_before = providers.volatile_note_frame(dict(cfg, chat_id=7))
    _restart(monkeypatch)
    context_marker.load_key(tmp_path)
    after = _stable(cfg, 7)
    assert before.encode() == after.encode()
    assert providers.volatile_note_frame(dict(cfg, chat_id=7)) == frame_before
    # and the block really does carry the marker the frame opens with
    assert context_marker.opening(7) in after
    assert frame_before.startswith(context_marker.opening(7))


def test_the_whole_cached_request_holds_across_a_restart(tmp_path, cfg,
                                                         monkeypatch):
    """The bytes Anthropic caches (tools, then the system block) are the
    same on the first call after a restart as on the last call before it."""
    sent = []

    class _Stream:
        def __init__(self, kwargs):
            sent.append(kwargs)

        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return False

        @property
        def text_stream(self):
            async def gen():
                yield "ok"
            return gen()

        async def get_final_message(self):
            class U:
                input_tokens = 1
                output_tokens = 1
                cache_read_input_tokens = 0
                cache_creation_input_tokens = 0
                cache_creation = None

            class M:
                usage = U()
                stop_reason = "end_turn"
                content = []
            return M()

    class _Client:
        class messages:
            @staticmethod
            def stream(**kwargs):
                return _Stream(kwargs)

    monkeypatch.setattr(providers, "_anthropic_client", lambda p: _Client())
    live = dict(cfg, chat_id=3, memory_ambient="a fact",
                round_predecessors=[])

    async def call():
        async for _ in providers.stream_reply(PARTICIPANT, ROSTER, [], {},
                                              live, None, "", False):
            pass

    context_marker.load_key(tmp_path)
    asyncio.run(call())
    _restart(monkeypatch)
    context_marker.load_key(tmp_path)
    asyncio.run(call())
    first, second = sent
    assert json.dumps(first["system"], sort_keys=True).encode() == \
        json.dumps(second["system"], sort_keys=True).encode()


def test_the_key_file_is_owner_only_and_made_once(tmp_path):
    context_marker.load_key(tmp_path)
    path = tmp_path / context_marker.KEY_FILE
    assert stat.S_IMODE(os.stat(path).st_mode) == 0o600
    key = path.read_text()
    assert len(bytes.fromhex(key)) == 32
    context_marker.load_key(tmp_path)
    assert path.read_text() == key


def test_a_loose_key_file_is_tightened(tmp_path):
    path = tmp_path / context_marker.KEY_FILE
    path.write_text(secrets.token_bytes(32).hex())
    os.chmod(path, 0o644)
    context_marker.load_key(tmp_path)
    assert stat.S_IMODE(os.stat(path).st_mode) == 0o600


def test_each_chat_and_each_install_has_its_own_marker(tmp_path):
    (tmp_path / "a").mkdir()
    (tmp_path / "b").mkdir()
    context_marker.load_key(tmp_path / "a")
    one, two = context_marker.marker(1), context_marker.marker(2)
    assert one != two
    assert len(one) == 12 and int(one, 16) >= 0
    context_marker.load_key(tmp_path / "b")
    assert context_marker.marker(1) != one


def test_a_bare_import_uses_a_key_for_this_run(tmp_path):
    """No data folder was loaded: the marker still works and holds for the
    life of the process, as the per-process marker always did."""
    assert context_marker._key is None
    assert context_marker.marker(5) == context_marker.marker(5)
    assert not (tmp_path / context_marker.KEY_FILE).exists()


def test_an_unusable_key_file_is_left_alone(tmp_path, caplog):
    path = tmp_path / context_marker.KEY_FILE
    path.write_text("not a key")
    with caplog.at_level(logging.WARNING, logger="crossband.context_marker"):
        context_marker.load_key(tmp_path)
    assert context_marker._key is None
    assert path.read_text() == "not a key"
    assert "unusable" in caplog.text
    assert context_marker.marker(1) == context_marker.marker(1)


def test_the_app_keeps_its_key_in_the_data_folder(tmp_path):
    create_app(Settings(data_dir=str(tmp_path / "data"),
                        memory_url="http://127.0.0.1:1"))
    path = tmp_path / "data" / context_marker.KEY_FILE
    assert path.exists()
    assert context_marker._key == bytes.fromhex(path.read_text())


def test_redact_reaches_every_string():
    m = context_marker.marker(9)
    got = context_marker.redact(
        {"a": f"x {m} y", "b": [m, {"c": f"[Context refresh · {m}]"}], "n": 3},
        9)
    assert m not in json.dumps(got)
    assert got["a"] == f"x {context_marker.REDACTED} y"
    assert got["n"] == 3
    # another chat's marker is not this chat's, and is left as it is
    assert context_marker.redact(m, 10) == m


def test_a_tool_never_sees_the_marker(cfg, monkeypatch):
    seen = []
    monkeypatch.setitem(tools._RESEARCH_TOOLS, "web_search",
                        lambda args, cfg: seen.append(args) or "results")
    m = context_marker.marker(4)
    out = asyncio.run(tools.run_tool(
        "web_search", {"query": f"what is {m}"}, dict(cfg, chat_id=4)))
    assert out == "results"
    assert seen == [{"query": f"what is {context_marker.REDACTED}"}]


def test_a_reply_that_repeats_the_marker_is_saved_without_it(tmp_path,
                                                             monkeypatch):
    app = create_app(Settings(data_dir=str(tmp_path / "data"),
                              memory_url="http://127.0.0.1:1"))

    async def stream_reply(participant, roster, transcript, names, cfg,
                           project, chat_summary, voice_mode, tools=None,
                           memory=None):
        m = context_marker.marker(cfg["chat_id"])
        yield ("tool", {"tool": "web_search", "input": {"query": m},
                        "output": f"no results for {m}"})
        yield ("text", f"The block opened with [Context refresh · {m}].")

    monkeypatch.setattr(engine.providers, "stream_reply", stream_reply)
    with TestClient(app, base_url="http://127.0.0.1") as c:
        chat_id = c.post("/api/chats", json={}).json()["id"]
        with c.stream("POST", f"/api/chats/{chat_id}/send",
                      json={"text": "what did the context say?"}) as r:
            for _ in r.iter_lines():
                pass
        deadline = time.time() + 5
        while rounds.active(chat_id) is not None and time.time() < deadline:
            time.sleep(0.05)
        payload = c.get(f"/api/chats/{chat_id}").json()
        replies = [m for m in payload["messages"] if m["speaker"] != "user"]
        assert replies
        m = context_marker.marker(chat_id)
        assert m not in json.dumps(payload)
        assert context_marker.REDACTED in replies[0]["content"]
        assert context_marker.REDACTED in json.dumps(replies[0]["tool_events"])
