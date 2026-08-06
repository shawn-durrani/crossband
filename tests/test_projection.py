"""Characterization tests for the transcript projection — the load-bearing
invariants: own messages become assistant turns, everyone else becomes
labelled user turns with real timestamps, tool activity replays as a research
log, and a conversation never starts or ends on an assistant turn."""

import inspect
import json
import re

from backend.providers import (CONTINUE_NUDGE, build_anthropic_messages,
                               build_openai_input, split_system_prompt)
from tests.conftest import make_msg

LABEL_RE = re.compile(
    r"^\[(?P<name>[^\]·]+) · \d{4}-\d{2}-\d{2}T\d{2}:\d{2}[+-]\d{2}:\d{2}\]: ")


# ---------- Anthropic ----------

def test_anthropic_roles(transcript, names, cfg):
    msgs = build_anthropic_messages("claude", transcript, names, cfg)
    roles = [m["role"] for m in msgs]
    # user, assistant(claude), user(gpt), assistant(claude), + trailing-guard user
    assert roles == ["user", "assistant", "user", "assistant", "user"]


def test_anthropic_own_messages_are_plain_assistant(transcript, names, cfg):
    msgs = build_anthropic_messages("claude", transcript, names, cfg)
    assert msgs[1] == {"role": "assistant", "content": "hi, I'm Claude"}


def test_anthropic_other_messages_are_labelled_with_timestamp(transcript, names, cfg):
    msgs = build_anthropic_messages("claude", transcript, names, cfg)
    user_text = msgs[0]["content"][0]["text"]
    m = LABEL_RE.match(user_text)
    assert m and m.group("name") == "User"
    gpt_text = msgs[2]["content"][0]["text"]
    m = LABEL_RE.match(gpt_text)
    assert m and m.group("name") == "GPT"
    assert gpt_text.endswith("and I'm GPT")


def test_anthropic_never_ends_on_assistant(transcript, names, cfg):
    msgs = build_anthropic_messages("claude", transcript, names, cfg)
    assert msgs[-1]["role"] == "user"
    assert msgs[-1]["content"][0]["text"] == CONTINUE_NUDGE


def test_anthropic_never_starts_on_assistant(names, cfg):
    only_own = [make_msg(1, "claude", "monologue")]
    msgs = build_anthropic_messages("claude", only_own, names, cfg)
    assert msgs[0]["role"] == "user"
    assert msgs[0]["content"][0]["text"] == "(The group chat continues.)"
    assert msgs[-1]["role"] == "user"


def test_anthropic_tool_events_replay_as_research_log(names, cfg):
    ev = {"tool": "web_search", "input_json": '{"query": "espresso"}',
          "output_text": "X" * 5000}
    transcript = [
        make_msg(1, "user", "look it up"),
        make_msg(2, "gpt", "done", tool_events=[ev]),
    ]
    msgs = build_anthropic_messages("claude", transcript, names, cfg)
    log_text = msgs[1]["content"][0]["text"]
    assert log_text.startswith("[research log] GPT used these tools")
    assert "web_search" in log_text
    # trimmed to the configured replay cap, not the full 5000 chars
    assert "X" * cfg["tool_log_chars"] in log_text
    assert "X" * (cfg["tool_log_chars"] + 1) not in log_text


def test_anthropic_youtube_transcript_exempt_from_replay_trim(names, cfg):
    ev = {"tool": "fetch_youtube_transcript", "input_json": '{"url": "u"}',
          "output_text": "Y" * 5000}
    transcript = [make_msg(1, "gpt", "watched it", tool_events=[ev])]
    msgs = build_anthropic_messages("claude", transcript, names, cfg)
    assert "Y" * 5000 in msgs[0]["content"][0]["text"]


# ---------- OpenAI (Responses API) ----------

def test_openai_roles(transcript, names, cfg):
    items = build_openai_input("gpt", transcript, names, cfg)
    roles = [m["role"] for m in items]
    # user, user(claude), assistant(gpt), user(claude) — ends on user already
    assert roles == ["user", "user", "assistant", "user"]


def test_openai_own_messages_are_assistant(transcript, names, cfg):
    items = build_openai_input("gpt", transcript, names, cfg)
    assert items[2] == {"role": "assistant", "content": "and I'm GPT"}


def test_openai_labels_use_input_text_parts(transcript, names, cfg):
    items = build_openai_input("gpt", transcript, names, cfg)
    part = items[1]["content"][0]
    assert part["type"] == "input_text"
    m = LABEL_RE.match(part["text"])
    assert m and m.group("name") == "Claude"


def test_openai_never_ends_on_assistant(transcript, names, cfg):
    items = build_openai_input("claude", transcript, names, cfg)
    assert items[-1]["role"] == "user"
    assert items[-1]["content"][0]["text"] == CONTINUE_NUDGE


def test_openai_never_starts_on_assistant(names, cfg):
    only_own = [make_msg(1, "gpt", "monologue")]
    items = build_openai_input("gpt", only_own, names, cfg)
    assert items[0]["role"] == "user"
    assert items[-1]["role"] == "user"


def test_empty_attachment_message_gets_placeholder(names, cfg, monkeypatch):
    import backend.providers as providers
    monkeypatch.setattr(providers.att_mod, "anthropic_blocks", lambda a: [])
    att = {"filename": "a.png", "mime": "image/png", "size": 10, "stored_name": "x"}
    transcript = [make_msg(1, "user", "", attachments=[att])]
    # projection should note the file even though the text body is empty
    msgs = build_anthropic_messages("claude", transcript, names, cfg)
    assert "(sent the attached file(s))" in msgs[0]["content"][0]["text"]


# ---------- system-only context never leaks into transcript turns ----------
#
# Persona, project instructions, chat/project summaries, and memory text must
# only ever reach a model through the system/instructions channel — never as
# a transcript turn, which is the one place attribution to "the user" (a
# labelled `[User · ts]:` turn) is actually meaningful. These tests pin that
# boundary structurally, independent of any prompt wording, so a future
# refactor that accidentally routes one of these fields through the
# transcript builders fails the build rather than silently reopening the leak.

PARTICIPANT = {"name": "Claude", "slug": "claude", "model": "claude-opus-4-8",
               "provider": "anthropic", "system_prompt": "PERSONA_SENTINEL_ABC"}
ROSTER = [{"name": "Claude", "slug": "claude"}, {"name": "GPT", "slug": "gpt"}]
PROJECT = {"instructions": "PROJECT_INSTRUCTIONS_SENTINEL_DEF",
           "memory": "PROJECT_MEMORY_SENTINEL_GHI"}
CHAT_SUMMARY = "CHAT_SUMMARY_SENTINEL_JKL"

_SYSTEM_ONLY_SENTINELS = (
    "PERSONA_SENTINEL_ABC", "PROJECT_INSTRUCTIONS_SENTINEL_DEF",
    "PROJECT_MEMORY_SENTINEL_GHI", "CHAT_SUMMARY_SENTINEL_JKL",
    "MEMORY_SUMMARY_SENTINEL_MNO", "MEMORY_AMBIENT_SENTINEL_PQR",
    "DELEGATION_SENTINEL_STU", "SHARED_INSTRUCTIONS_SENTINEL_VWX",
)


def _cfg_with_system_only_fields(base_cfg):
    c = dict(base_cfg)
    c.update(
        memory_summary="MEMORY_SUMMARY_SENTINEL_MNO",
        memory_ambient="MEMORY_AMBIENT_SENTINEL_PQR",
        delegation_note="DELEGATION_SENTINEL_STU",
        shared_instructions="SHARED_INSTRUCTIONS_SENTINEL_VWX",
    )
    return c


def test_system_only_fields_absent_from_anthropic_transcript_turns(transcript, names, cfg):
    live_cfg = _cfg_with_system_only_fields(cfg)
    msgs = build_anthropic_messages("claude", transcript, names, live_cfg)
    blob = json.dumps(msgs)
    for sentinel in _SYSTEM_ONLY_SENTINELS:
        assert sentinel not in blob, f"{sentinel} leaked into the transcript turns"


def test_system_only_fields_absent_from_openai_transcript_items(transcript, names, cfg):
    live_cfg = _cfg_with_system_only_fields(cfg)
    items = build_openai_input("gpt", transcript, names, live_cfg)
    blob = json.dumps(items)
    for sentinel in _SYSTEM_ONLY_SENTINELS:
        assert sentinel not in blob, f"{sentinel} leaked into the transcript items"


def test_transcript_builders_have_no_project_or_summary_parameters():
    """Belt-and-suspenders: project instructions/memory and chat_summary can't
    reach the transcript builders even by accident, because the functions
    don't accept them as arguments at all — only split_system_prompt does."""
    for fn in (build_anthropic_messages, build_openai_input):
        params = set(inspect.signature(fn).parameters)
        assert "project" not in params
        assert "chat_summary" not in params


def test_system_only_fields_are_real_and_reach_the_system_channel(cfg):
    """Positive control for the two leak tests above: proves the sentinels
    aren't silently unused anywhere -- they DO reach the model, just only
    through split_system_prompt (persona/project/summary), never through the
    transcript builders (asserted above)."""
    live_cfg = _cfg_with_system_only_fields(cfg)
    stable, volatile = split_system_prompt(
        PARTICIPANT, ROSTER, live_cfg, PROJECT, CHAT_SUMMARY, False)
    combined = stable + volatile
    for sentinel in _SYSTEM_ONLY_SENTINELS:
        assert sentinel in combined, f"{sentinel} never reached the system prompt at all"
