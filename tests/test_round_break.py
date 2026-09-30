"""Text from two tool rounds of one reply is joined with a break (#589).

A seat that writes "Let me check.", calls a tool, then writes "Found it."
streamed the two rounds' text back to back, so the chat, the saved reply and
the voice all got "Let me check.Found it." These tests pin the join rule,
each adapter's use of it, and that the saved reply, the live text and the
voice all get the same words. Fakes only: no provider, no service.
"""

import asyncio
import json
import time
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

from backend import providers, rounds, tts_models, voice
from backend.app import create_app
from backend.config import Settings
from backend.providers import round_break
from tests.test_openai_compat_fallback import (BASE_URL, FakeClient,
                                               _finish_chunk, _text_chunk,
                                               _tool_call_chunk)

CLAUDE = {"name": "Claude", "slug": "claude", "model": "claude-opus-4-8",
          "provider": "anthropic", "system_prompt": ""}
GPT = {"name": "GPT", "slug": "gpt", "model": "gpt-5.1",
       "provider": "openai", "system_prompt": ""}
ROSTER = [{"name": "Claude", "slug": "claude"}, {"name": "GPT", "slug": "gpt"}]
TOOLS = [{"name": "web_search", "description": "search",
          "input_schema": {"type": "object"}}]


# ---------- the rule ----------

@pytest.mark.parametrize("before, after, sep", [
    # a finished sentence: what follows the tool is a new paragraph
    ("Let me check.", "Found it.", "\n\n"),
    ("Is it open?", "Yes.", "\n\n"),
    ("Here's what I found:", "## Results", "\n\n"),
    ('He wrote "done."', "Then", "\n\n"),
    ("**Result:**", "Two", "\n\n"),
    ("| a | b |", "Next", "\n\n"),
    ("Let me think...", "Right", "\n\n"),
    # stopped mid sentence: a space keeps the sentence whole
    ("The tower is", "300 metres tall", " "),
    ("It's about 42", "metres", " "),
    ("First the logs,", "then the rest", " "),
    ("Have a look at the **logs**", "They show", " "),
    # the model's own whitespace stays as it wrote it, never doubled
    ("Let me check. ", "Found it.", ""),
    ("Let me check.\n", "Found it.", ""),
    ("Let me check.", " Found it.", ""),
    ("Let me check.", "\n\nFound it.", ""),
    # nothing written before the tool: nothing to join
    ("", "Found it.", ""),
    ("Let me check.", "", ""),
])
def test_the_break_between_two_rounds(before, after, sep):
    assert round_break(before, after) == sep


# ---------- each adapter ----------

class _Usage:
    input_tokens = 1
    output_tokens = 1
    cache_read_input_tokens = 0
    cache_creation_input_tokens = 0
    cache_creation = None


class _ToolUse:
    type = "tool_use"
    name = "web_search"
    input = {"query": "tower height"}
    id = "t1"


class _Final:
    def __init__(self, stop_reason, content):
        self.usage = _Usage()
        self.stop_reason = stop_reason
        self.content = content


class _Round:
    def __init__(self, pieces, final):
        self._pieces, self._final = pieces, final

    async def __aenter__(self):
        return self

    async def __aexit__(self, *a):
        return False

    @property
    def text_stream(self):
        async def gen():
            for piece in self._pieces:
                yield piece
        return gen()

    async def get_final_message(self):
        return self._final


def _anthropic(*rounds_text):
    """A fake Anthropic client: every round but the last calls a tool."""
    rounds_ = [_Round(pieces, _Final("tool_use", [_ToolUse()]))
               for pieces in rounds_text[:-1]]
    rounds_.append(_Round(rounds_text[-1], _Final("end_turn", [])))

    class _Messages:
        @staticmethod
        def stream(**kwargs):
            return rounds_.pop(0)

    return SimpleNamespace(messages=_Messages())


def _responses(*rounds_text):
    """A fake OpenAI Responses client: every round but the last calls a
    tool."""
    rounds_ = list(rounds_text)

    async def create(**kwargs):
        pieces = rounds_.pop(0)
        call = SimpleNamespace(type="function_call", name="web_search",
                               call_id="c1", arguments="{}")
        final = SimpleNamespace(
            usage=None, output=[call] if rounds_ else [])

        async def gen():
            for piece in pieces:
                yield SimpleNamespace(type="response.output_text.delta",
                                      delta=piece)
            yield SimpleNamespace(type="response.completed", response=final)
        return gen()

    return SimpleNamespace(responses=SimpleNamespace(create=create))


async def _fake_tool(name, args, cfg, origin_agent=None, memory=None):
    return "three results"


def _drain(participant, cfg):
    async def go():
        return [ev async for ev in providers.stream_reply(
            participant, ROSTER, [], {}, dict(cfg), None, "", False,
            tools=TOOLS)]
    return asyncio.run(go())


def _texts(events):
    return [v for k, v in events if k == "text"]


@pytest.fixture
def audited(monkeypatch):
    """The text the attribution audit was handed, one entry per reply."""
    seen = []

    def check(text, *a, **k):
        seen.append(text)
        return []

    monkeypatch.setattr(providers, "_check_attribution", check)
    monkeypatch.setattr(providers, "run_tool", _fake_tool)
    return seen


def test_anthropic_joins_its_rounds_with_a_break(cfg, monkeypatch, audited):
    monkeypatch.setattr(providers, "_anthropic_client", lambda p: _anthropic(
        ["Let me ", "check."], ["Found ", "it."]))
    texts = _texts(_drain(CLAUDE, cfg))
    # one round's pieces are untouched; the break rides the next round's first
    assert texts == ["Let me ", "check.", "\n\nFound ", "it."]
    assert audited == ["Let me check.\n\nFound it."]


def test_anthropic_mid_sentence_gets_a_space(cfg, monkeypatch, audited):
    monkeypatch.setattr(providers, "_anthropic_client", lambda p: _anthropic(
        ["The tower is"], ["300 metres tall."]))
    assert "".join(_texts(_drain(CLAUDE, cfg))) == \
        "The tower is 300 metres tall."


def test_anthropic_a_silent_first_round_adds_nothing(cfg, monkeypatch,
                                                      audited):
    monkeypatch.setattr(providers, "_anthropic_client", lambda p: _anthropic(
        [], ["Found it."]))
    assert _texts(_drain(CLAUDE, cfg)) == ["Found it."]


def test_anthropic_joins_across_a_silent_middle_round(cfg, monkeypatch,
                                                       audited):
    monkeypatch.setattr(providers, "_anthropic_client", lambda p: _anthropic(
        ["Let me check."], [], ["Found it."]))
    assert "".join(_texts(_drain(CLAUDE, cfg))) == \
        "Let me check.\n\nFound it."


def test_openai_responses_joins_its_rounds_with_a_break(cfg, monkeypatch,
                                                        audited):
    monkeypatch.setattr(providers, "_openai_client", lambda p: _responses(
        ["Let me ", "check."], ["Found ", "it."]))
    texts = _texts(_drain(GPT, cfg))
    assert texts == ["Let me ", "check.", "\n\nFound ", "it."]
    assert audited == ["Let me check.\n\nFound it."]


def test_chat_completions_joins_its_rounds_with_a_break(cfg, monkeypatch,
                                                        audited):
    monkeypatch.setattr(providers, "_chat_completions_only", {BASE_URL})
    client = FakeClient([
        [_text_chunk("Let me "), _text_chunk("check."),
         _tool_call_chunk(0, "c1", "web_search", "{}"),
         _finish_chunk("tool_calls")],
        [_text_chunk("Found "), _text_chunk("it."), _finish_chunk("stop")],
    ])
    monkeypatch.setattr(providers, "_openai_client", lambda p: client)
    seat = dict(GPT, slug="qwen", name="Qwen", base_url=BASE_URL,
                model="test-model", api_key_env="")
    texts = _texts(_drain(seat, cfg))
    assert texts == ["Let me ", "check.", "\n\nFound ", "it."]
    assert audited == ["Let me check.\n\nFound it."]


# ---------- the saved reply, the live text and the voice agree ----------

def test_saved_reply_live_text_and_voice_all_get_the_break(tmp_path,
                                                           monkeypatch):
    """Both default seats run through their real adapters with fake
    clients. What streams to the browser, what's saved, and what the voice
    relay sends on are the same words, with the break between rounds."""
    monkeypatch.setattr(providers, "run_tool", _fake_tool)
    monkeypatch.setattr(providers, "_anthropic_client", lambda p: _anthropic(
        ["Let me ", "check."], ["Found ", "it."]))
    monkeypatch.setattr(providers, "_openai_client", lambda p: _responses(
        ["Let me ", "check."], ["Found ", "it."]))
    app = create_app(Settings(data_dir=str(tmp_path / "data"),
                              memory_url="http://127.0.0.1:1"))
    with TestClient(app, base_url="http://127.0.0.1") as c:
        chat_id = c.post("/api/chats", json={}).json()["id"]
        events = []
        with c.stream("POST", f"/api/chats/{chat_id}/send",
                      json={"text": "how tall is the tower?"}) as r:
            for line in r.iter_lines():
                if line.startswith("data: "):
                    events.append(json.loads(line[len("data: "):]))
        deadline = time.time() + 5
        while rounds.active(chat_id) is not None and time.time() < deadline:
            time.sleep(0.05)
        saved = {m["speaker"]: m["content"]
                 for m in c.get(f"/api/chats/{chat_id}").json()["messages"]
                 if m["speaker"] != "user"}

    assert set(saved) == {"claude", "gpt"}, [e.get("type") for e in events]
    for speaker, content in saved.items():
        deltas = [e["text"] for e in events
                  if e.get("type") == "delta" and e["speaker"] == speaker]
        assert "".join(deltas) == content == "Let me check.\n\nFound it."
        # The voice speaks the deltas as they arrive. The relay holds text
        # until a sentence ends, and the break reaches TTS in front of the
        # second sentence, so the two are never read as one word.
        carry = {"chunks": True}
        spoken = []
        for i, piece in enumerate(deltas):
            msg = {"text": piece}
            if i == len(deltas) - 1:
                msg.update(flush=True, done=True)
            for frame in voice.tts_upstream_frames(
                    tts_models.ROUTE_DIALOGUE, msg, "v1", carry):
                sent = json.loads(frame).get("inputs")
                if sent:
                    spoken.append(sent[0]["text"])
        assert spoken == ["Let me check.", "\n\nFound it."]


def test_engine_keeps_a_single_round_as_it_streamed(tmp_path, monkeypatch):
    """No tool call, no break: one round's text is saved as it streamed."""
    monkeypatch.setattr(providers, "_anthropic_client", lambda p: _anthropic(
        ["Three ", "hundred", " metres."]))
    monkeypatch.setattr(providers, "_openai_client", lambda p: _responses(
        ["Three ", "hundred", " metres."]))
    app = create_app(Settings(data_dir=str(tmp_path / "data"),
                              memory_url="http://127.0.0.1:1"))
    with TestClient(app, base_url="http://127.0.0.1") as c:
        chat_id = c.post("/api/chats", json={}).json()["id"]
        with c.stream("POST", f"/api/chats/{chat_id}/send",
                      json={"text": "how tall is the tower?"}) as r:
            for _ in r.iter_lines():
                pass
        deadline = time.time() + 5
        while rounds.active(chat_id) is not None and time.time() < deadline:
            time.sleep(0.05)
        saved = [m["content"]
                 for m in c.get(f"/api/chats/{chat_id}").json()["messages"]
                 if m["speaker"] != "user"]
    assert saved and set(saved) == {"Three hundred metres."}
