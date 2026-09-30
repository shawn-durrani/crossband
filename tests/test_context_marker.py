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
- a marker a model repeats never reaches the chat, a tool event or a tool;
- nor the live stream to the browser, even split across pieces, and the
  stream holds back nothing that couldn't be the start of one (#574).
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


def _redacted_stream(chat_id, pieces):
    r = context_marker.StreamRedactor(chat_id)
    out = [r.feed(p) for p in pieces]
    return out, r.flush()


def test_a_marker_split_anywhere_never_streams():
    m = context_marker.marker(8)
    reply = f"It opened with [Context refresh · {m}] and then the facts."
    for cut in range(1, len(reply)):
        for cut2 in range(cut, len(reply), 7):
            pieces = [reply[:cut], reply[cut:cut2], reply[cut2:]]
            out, rest = _redacted_stream(8, pieces)
            shown = "".join(out) + rest
            assert m not in shown
            assert shown == context_marker.redact(reply, 8)


def test_text_that_cannot_start_a_marker_goes_out_at_once():
    m = context_marker.marker(8)
    last = next(c for c in "ghijklmnopqrstuvwxyz ." if c != m[0])
    # Each piece ends on a letter a hex marker can't start with, so nothing
    # is ever held back, whatever marker the chat drew.
    out, rest = _redacted_stream(8, ["Hello th", "ere, all well" + last])
    assert out == ["Hello th", "ere, all well" + last]
    assert rest == ""


def test_only_a_possible_start_is_held_and_never_more():
    m = context_marker.marker(8)
    r = context_marker.StreamRedactor(8)
    assert r.feed("see " + m[:5]) == "see "
    assert r.feed(m[5:11]) == ""                  # 11 held, still a start
    assert r.feed("!") == m[:11] + "!"            # not the marker after all
    assert r.feed("x" + m[:1]) == "x"
    assert r.flush() == m[:1]                     # the reply ended there
    assert r.flush() == ""


def test_the_live_stream_never_carries_the_marker(tmp_path, monkeypatch):
    """The saved reply was already clean. What streamed to the browser and
    the voice while it was written was not, and a marker split across two
    pieces is the shape a stream really delivers."""
    app = create_app(Settings(data_dir=str(tmp_path / "data"),
                              memory_url="http://127.0.0.1:1"))

    async def stream_reply(participant, roster, transcript, names, cfg,
                           project, chat_summary, voice_mode, tools=None,
                           memory=None):
        m = context_marker.marker(cfg["chat_id"])
        yield ("tool", {"tool": "web_search", "input": {"query": m},
                        "output": f"no results for {m}"})
        yield ("text", f"The block opened with [Context refresh · {m[:4]}")
        yield ("text", f"{m[4:]}]. That's all")
        yield ("text", f" I know, {m[:3]}")

    monkeypatch.setattr(engine.providers, "stream_reply", stream_reply)
    with TestClient(app, base_url="http://127.0.0.1") as c:
        chat_id = c.post("/api/chats", json={}).json()["id"]
        m = context_marker.marker(chat_id)
        events = []
        with c.stream("POST", f"/api/chats/{chat_id}/send",
                      json={"text": "what did the context say?"}) as r:
            for line in r.iter_lines():
                if line.startswith("data: "):
                    events.append(json.loads(line[len("data: "):]))
        assert events
        assert m not in json.dumps(events)
        first = next(e["speaker"] for e in events
                     if e.get("type") == "speaker_start")
        deltas = "".join(e["text"] for e in events
                         if e.get("type") == "delta" and e["speaker"] == first)
        assert deltas == (f"The block opened with [Context refresh · "
                          f"{context_marker.REDACTED}]. That's all I know, "
                          f"{m[:3]}")
        tool = next(e for e in events if e.get("type") == "tool_activity")
        assert context_marker.REDACTED in tool["input_json"]
        assert context_marker.REDACTED in tool["output_text"]
        deadline = time.time() + 5
        while rounds.active(chat_id) is not None and time.time() < deadline:
            time.sleep(0.05)
        saved = [msg for msg in c.get(f"/api/chats/{chat_id}").json()["messages"]
                 if msg["speaker"] == first]
        assert saved[0]["content"] == deltas
