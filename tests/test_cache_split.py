"""Claude-chat prompt-cache layout.

Two things, in the order they had to be built (instrumentation
BEFORE the reorder, so the fix is provable rather than assumed):

  1. Instrumentation: every real Claude-chat request logs a content-free proof
     of what was cacheable and what actually happened -- hashed stable-system
     prefix, hashed volatile-system block, a transcript-prefix hash, real
     token counts, and the ACTUAL cache read/write result (including which
     TTL bucket -- 5m vs 1h -- the write landed in, straight from the API's
     own `cache_creation` breakdown). Never the prompt/transcript text itself.
  2. The reorder, COMPLETED: `providers.split_system_prompt` separates the
     stable persona/project/shared-instructions content from the volatile
     memory_ambient/round_predecessors block, and the adapters keep the
     volatile block OUT of the cacheable prefix entirely -- system carries
     only the stable block (its own cache_control breakpoint); the volatile
     block rides at the very end, INSIDE the final user turn on Anthropic
     (after the transcript breakpoint) and as a trailing `developer` input
     item on OpenAI (instructions = stable only). The first cut of this work
     appended volatile as a second system block -- still upstream of the
     transcript breakpoint, so the conversation cache missed on every call
     (the whole cached prefix is re-written). The prefix-stability
     tests below are the regression guard for that mistake.

No TTL change ships here: cache_control stays a bare {"type": "ephemeral"}
(the 5-minute default) on every block -- asserted explicitly below.
"""

import asyncio
import logging

import pytest

from backend import providers

PARTICIPANT = {"name": "Claude", "slug": "claude", "model": "claude-opus-4-8",
               "provider": "anthropic", "system_prompt": ""}
ROSTER = [{"name": "Claude", "slug": "claude"}, {"name": "GPT", "slug": "gpt"}]


def _cfg(base, **extra):
    c = dict(base)
    c.update(extra)
    return c


# ---------- the split itself ----------

def test_stable_block_excludes_ambient_and_round_predecessors(cfg):
    live_cfg = _cfg(cfg, memory_ambient="Alex likes flat whites.",
                    round_predecessors=["gpt"])
    stable, volatile = providers.split_system_prompt(
        PARTICIPANT, ROSTER, live_cfg, None, "", False)
    assert "flat whites" not in stable
    assert "already replied to this" not in stable
    assert "flat whites" in volatile
    assert "already replied to this" in volatile
    # the stable block still carries the persona/rules content
    assert "Reply as yourself only" in stable


def test_volatile_empty_when_no_ambient_or_predecessors(cfg):
    stable, volatile = providers.split_system_prompt(
        PARTICIPANT, ROSTER, dict(cfg), None, "", False)
    assert volatile == ""
    assert stable  # the rules/persona block is never empty


def test_voice_mode_tail_lands_in_volatile_and_stays_last(cfg):
    live_cfg = _cfg(cfg, memory_ambient="fact", round_predecessors=["gpt"])
    stable, volatile = providers.split_system_prompt(
        PARTICIPANT, ROSTER, live_cfg, None, "", True)
    assert "VOICE MODE" not in stable
    assert volatile.rstrip().endswith(providers.VOICE_INSTRUCTION.rstrip())


def test_group_chat_system_is_the_stable_and_volatile_concatenation(cfg):
    """The monolithic accessor (tests/docs convenience - both live adapters
    assemble from the split now) must never drift from the split path --
    same content, stable then volatile, voice tail last."""
    live_cfg = _cfg(cfg, memory_ambient="fact", round_predecessors=["gpt"])
    combined = providers.group_chat_system(PARTICIPANT, ROSTER, live_cfg, None, "", True)
    stable, volatile = providers.split_system_prompt(
        PARTICIPANT, ROSTER, live_cfg, None, "", True)
    assert combined == "\n".join(t for t in (stable, volatile) if t)


# ---------- fakes for the streaming path ----------

class _FakeCacheCreation:
    def __init__(self, ephemeral_5m=0, ephemeral_1h=0):
        self.ephemeral_5m_input_tokens = ephemeral_5m
        self.ephemeral_1h_input_tokens = ephemeral_1h


class _FakeUsage:
    def __init__(self, input_tokens=100, output_tokens=20, cache_read=0,
                cache_creation_tokens=0, cache_creation=None):
        self.input_tokens = input_tokens
        self.output_tokens = output_tokens
        self.cache_read_input_tokens = cache_read
        self.cache_creation_input_tokens = cache_creation_tokens
        self.cache_creation = cache_creation


class _FakeFinalMessage:
    def __init__(self, usage, stop_reason="end_turn", content=None):
        self.usage = usage
        self.stop_reason = stop_reason
        self.content = content or []


class _FakeStreamCtx:
    def __init__(self, final, text="hi"):
        self._final = final
        self._text = text

    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc, tb):
        return False

    @property
    def text_stream(self):
        async def gen():
            yield self._text
        return gen()

    async def get_final_message(self):
        return self._final


class _FakeMessagesAPI:
    def __init__(self, final):
        self._final = final
        self.captured_kwargs = None

    def stream(self, **kwargs):
        self.captured_kwargs = kwargs
        return _FakeStreamCtx(self._final)


class _FakeAnthropicClient:
    def __init__(self, final):
        self.messages = _FakeMessagesAPI(final)


def _drive(monkeypatch, cfg_dict, *, ambient="", preds=None, cache_read=0,
          cache_5m=0, cache_1h=0, transcript=None, names_map=None,
          participant=None):
    live_cfg = _cfg(cfg_dict, memory_ambient=ambient, round_predecessors=preds or [])
    usage = _FakeUsage(input_tokens=50, output_tokens=10, cache_read=cache_read,
                       cache_creation=_FakeCacheCreation(cache_5m, cache_1h))
    final = _FakeFinalMessage(usage)
    fake_client = _FakeAnthropicClient(final)
    monkeypatch.setattr(providers, "_anthropic_client", lambda p: fake_client)

    events = []

    async def go():
        async for ev in providers.stream_reply(
            participant or PARTICIPANT, ROSTER,
            transcript if transcript is not None else [],
            names_map or {}, live_cfg, None, "", False):
            events.append(ev)

    asyncio.run(go())
    return fake_client, events


def test_system_is_stable_only_volatile_rides_after_the_breakpoint(cfg, monkeypatch):
    # pinned to a model with no mid-conversation system role, so this exercises
    # the IN-TURN shape specifically; the system-turn shape has its own
    # test below, and the prefix-stability guarantee covers both.
    """Completed: system carries ONLY the stable block (one breakpoint,
    bare ephemeral, no ttl field ever); the volatile block is the LAST
    content block of the final user turn, framed as app-assembled context,
    strictly AFTER the transcript breakpoint so it can never sit upstream of
    it in the cache prefix."""
    fake_client, _ = _drive(monkeypatch, cfg, ambient="a memory fact",
                            preds=["gpt"], participant=SONNET)
    kwargs = fake_client.messages.captured_kwargs
    system = kwargs["system"]
    assert len(system) == 1  # stable only - volatile never in system again
    assert system[0]["cache_control"] == {"type": "ephemeral"}
    assert "ttl" not in system[0]["cache_control"]
    assert "a memory fact" not in system[0]["text"]

    last = kwargs["messages"][-1]
    assert last["role"] == "user"
    tail = last["content"][-1]
    assert "a memory fact" in tail["text"]
    assert "already replied to this" in tail["text"]
    assert tail["text"].startswith("[Context refresh")
    assert "cache_control" not in tail  # never cached, never upstream
    # the transcript breakpoint sits on the block BEFORE the volatile tail
    assert last["content"][-2]["cache_control"] == {"type": "ephemeral"}
    assert "ttl" not in last["content"][-2]["cache_control"]


def test_transcript_breakpoint_is_second_to_last_message_not_last(cfg, monkeypatch,
                                                                  transcript, names):
    """With more than one transcript message, the cache_control
    breakpoint must land on messages[-2] (the large/stable history), NEVER on
    messages[-1] (the just-added, freshest turn) - that exact message never
    recurs verbatim on a later call, so breakpointing it means every call
    writes a fresh cache entry that's never read back. The volatile tail
    still rides at the very end of messages[-1], after (never as) the
    breakpoint."""
    fake_client, _ = _drive(monkeypatch, cfg, ambient="a memory fact",
                            preds=["gpt"], transcript=transcript, names_map=names)
    messages = fake_client.messages.captured_kwargs["messages"]
    # On a model with a mid-conversation system role the context rides a
    # trailing system turn, which shifts these indices by one. Drop it first -
    # the guarantee is about where the breakpoint sits in the TRANSCRIPT,
    # and must hold under either delivery shape.
    if messages[-1]["role"] == "system":
        messages = messages[:-1]
    assert len(messages) >= 2
    last = messages[-1]
    second_last = messages[-2]

    def _blocks(msg):
        c = msg["content"]
        return c if isinstance(c, list) else [{"text": c}]

    last_blocks = _blocks(last)
    # no block of the last (freshest) message may carry the breakpoint -
    # including the volatile tail when this shape appends one
    for b in last_blocks:
        assert "cache_control" not in b
    second_last_blocks = _blocks(second_last)
    assert second_last_blocks[-1]["cache_control"] == {"type": "ephemeral"}


def test_transcript_breakpoint_falls_back_to_only_message_when_len_one(cfg, monkeypatch):
    """With a single transcript message (e.g. the very first turn) there is
    no second-to-last message yet - the sole message is used as the
    breakpoint, matching the existing empty-transcript tests above."""
    fake_client, _ = _drive(monkeypatch, cfg)
    messages = fake_client.messages.captured_kwargs["messages"]
    assert len(messages) == 1


def test_prefix_is_byte_stable_when_only_volatile_changes(cfg, monkeypatch):
    """THE regression guard, and it must hold for BOTH delivery shapes: two
    calls differing only in ambient memory / round predecessors must produce a
    byte-identical system block and byte-identical messages up to the
    breakpoint - everything the provider hashes for the cache prefix. (The
    first cut failed exactly this.)"""
    for seat in (SONNET, OPUS5):
        a_client, _ = _drive(monkeypatch, cfg, ambient="fact about flat whites",
                             preds=["gpt"], participant=seat)
        b_client, _ = _drive(monkeypatch, cfg, ambient="entirely different fact",
                             preds=["gpt", "claude"], participant=seat)
        a, b = a_client.messages.captured_kwargs, b_client.messages.captured_kwargs
        assert a["system"] == b["system"], seat["model"]

        def prefix(kw):
            """Everything up to and including the breakpoint turn, with any
            volatile tail stripped - i.e. exactly what the cache keys on."""
            msgs = [dict(m) for m in kw["messages"]]
            if msgs[-1]["role"] == "system":      # system-turn shape
                return msgs[:-1]
            msgs[-1] = {**msgs[-1], "content": msgs[-1]["content"][:-1]}
            return msgs                            # in-turn shape

        assert prefix(a) == prefix(b), seat["model"]
        # and the volatile parts DID differ (the test isn't vacuous)
        assert a["messages"][-1] != b["messages"][-1], seat["model"]


# ---------- the two fields misclassified as stable ----------
#
# The reorder above moved memory_ambient and round_predecessors out of the
# cached block and stopped there. `memory_summary` is re-fetched from the
# memory service EVERY round (backend/engine.py) and rebuilt whenever
# save_memory runs; `delegation_note` flips when a Claude Code summons is
# claimed and again when it clears. Both stayed in the stable block, so either
# one changing invalidated the system block, and with it the whole transcript
# behind it.
# Measured against real traffic: the great majority of the Claude token bill
# was cache WRITE tokens, and the read:write ratio tracked summon count
# inversely. With no summons the cache was read back several times per write;
# with many summons it was re-written more than it was ever read back.


def test_stable_block_excludes_delegation_note_and_memory_summary(cfg):
    live_cfg = _cfg(cfg, memory_summary="Alex ships via issue then PR then merge.",
                    delegation_note="Claude Code has ALREADY been summoned this round.")
    stable, volatile = providers.split_system_prompt(
        PARTICIPANT, ROSTER, live_cfg, None, "", False)
    assert "issue then PR then merge" not in stable
    assert "ALREADY been summoned" not in stable
    assert "issue then PR then merge" in volatile
    assert "ALREADY been summoned" in volatile
    # the stable block still carries the persona/rules content
    assert "Reply as yourself only" in stable


def test_memory_summary_precedes_ambient_detail_in_volatile(cfg):
    """Reading order survives the move: the summary is the index, the
    auto-retrieved facts are the detail, and delegation state sits with this
    round's position. Only the position relative to the TRANSCRIPT changed."""
    _, volatile = providers.split_system_prompt(
        PARTICIPANT, ROSTER,
        _cfg(cfg, memory_summary="INDEXTOKEN", memory_ambient="DETAILTOKEN",
             delegation_note="CLAIMTOKEN", round_predecessors=["gpt"]),
        None, "", False)
    assert (volatile.index("INDEXTOKEN") < volatile.index("DETAILTOKEN")
            < volatile.index("CLAIMTOKEN") < volatile.index("already replied to this"))


def test_prefix_is_byte_stable_when_delegation_or_memory_summary_changes(cfg, monkeypatch):
    """THE regression guard, for both delivery shapes. A Claude Code
    summons (delegation_note set, then cleared) or a save_memory-triggered
    summary rebuild must not move a single byte of the cached prefix. Before
    the fix each flip re-wrote the entire 110-184k-token prefix at Anthropic's
    1.25x cache-WRITE rate - 12.5x what reading it back costs."""
    for seat in (SONNET, OPUS5):
        a_client, _ = _drive(
            monkeypatch,
            _cfg(cfg, delegation_note="", memory_summary="the summary as it was"),
            ambient="same fact", preds=["gpt"], participant=seat)
        b_client, _ = _drive(
            monkeypatch,
            _cfg(cfg, delegation_note="Claude Code has ALREADY been summoned this round.",
                 memory_summary="a rebuilt, entirely different summary"),
            ambient="same fact", preds=["gpt"], participant=seat)
        a, b = a_client.messages.captured_kwargs, b_client.messages.captured_kwargs
        assert a["system"] == b["system"], seat["model"]

        def prefix(kw):
            msgs = [dict(m) for m in kw["messages"]]
            if msgs[-1]["role"] == "system":      # system-turn shape
                return msgs[:-1]
            msgs[-1] = {**msgs[-1], "content": msgs[-1]["content"][:-1]}
            return msgs                            # in-turn shape

        assert prefix(a) == prefix(b), seat["model"]
        # the delegation/summary change DID land somewhere (not vacuous)
        assert a["messages"][-1] != b["messages"][-1], seat["model"]


def test_openai_prefix_byte_stable_when_delegation_or_memory_summary_changes(cfg, monkeypatch):
    """Same guarantee on the OpenAI seat: `instructions` leads that provider's
    cache prefix, so neither field may reach it."""
    fake_a = _drive_openai(monkeypatch,
                           _cfg(cfg, delegation_note="", memory_summary="before"),
                           ambient="same fact", preds=["claude"])
    ka = dict(fake_a.responses.captured_kwargs)
    fake_b = _drive_openai(monkeypatch,
                           _cfg(cfg, delegation_note="CLAIMTOKEN", memory_summary="after"),
                           ambient="same fact", preds=["claude"])
    kb = fake_b.responses.captured_kwargs
    assert ka["instructions"] == kb["instructions"]
    assert ka["input"][:-1] == kb["input"][:-1]
    assert ka["input"][-1] != kb["input"][-1]


# ---------- the write-failure note, and why chat_summary stays put ----------


def test_prefix_is_byte_stable_when_room_state_changes(cfg, monkeypatch):
    """The volatile room-state line (#28, room commands): arming room mode,
    disarming it, or the roster changing between rounds must not move a
    single byte of the cached prefix - the line rides the volatile tail
    only, for both delivery shapes."""
    for seat in (SONNET, OPUS5):
        a_client, _ = _drive(
            monkeypatch, _cfg(cfg, room_mode=False, room_roster_names=[]),
            ambient="same fact", preds=["gpt"], participant=seat)
        b_client, _ = _drive(
            monkeypatch, _cfg(cfg, room_mode=True,
                              room_roster_names=["Alex", "Sam"]),
            ambient="same fact", preds=["gpt"], participant=seat)
        a, b = a_client.messages.captured_kwargs, b_client.messages.captured_kwargs
        assert a["system"] == b["system"], seat["model"]

        def prefix(kw):
            msgs = [dict(m) for m in kw["messages"]]
            if msgs[-1]["role"] == "system":      # system-turn shape
                return msgs[:-1]
            msgs[-1] = {**msgs[-1], "content": msgs[-1]["content"][:-1]}
            return msgs                            # in-turn shape

        assert prefix(a) == prefix(b), seat["model"]
        # the room state DID land somewhere (not vacuous)
        assert a["messages"][-1] != b["messages"][-1], seat["model"]


def test_memory_write_warning_rides_the_volatile_tail(cfg):
    stable, volatile = providers.split_system_prompt(
        PARTICIPANT, ROSTER,
        _cfg(cfg, memory_write_warning="a recent memory save failed"),
        None, "", False)
    assert "a recent memory save failed" not in stable
    assert "a recent memory save failed" in volatile
    assert "System note" in volatile


def test_prefix_is_byte_stable_when_the_write_warning_flips(cfg, monkeypatch):
    """This warning flips with memory-service health while `summary_upto`
    stays put - so unlike a real summary fold the transcript does NOT change,
    and the cached block is the only thing that could differ. It must not.
    Concatenated into chat_summary (as it was), a memory outage doubled as a
    prompt-cache outage."""
    for seat in (SONNET, OPUS5):
        a_client, _ = _drive(monkeypatch, _cfg(cfg, memory_write_warning=""),
                             ambient="same fact", preds=["gpt"], participant=seat)
        b_client, _ = _drive(monkeypatch,
                             _cfg(cfg, memory_write_warning="a recent memory save failed"),
                             ambient="same fact", preds=["gpt"], participant=seat)
        a, b = a_client.messages.captured_kwargs, b_client.messages.captured_kwargs
        assert a["system"] == b["system"], seat["model"]
        # the warning DID land somewhere (not vacuous)
        assert a["messages"][-1] != b["messages"][-1], seat["model"]


def test_chat_summary_stays_in_the_cached_block_on_purpose(cfg):
    """Deliberate - NOT an oversight left over from the reorder, so do not
    "finish the job" here. `chats.summary` has exactly one writer
    (backend/chat_memory.py) and it always moves `summary_upto` in the same
    statement; the transcript is gated on that same watermark
    (backend/engine.py). A summary rebuild therefore ALREADY re-writes the
    transcript, so moving the summary out could not prevent that bust - while
    costing 900-1900 tokens of uncached input on every turn (~$0.4-0.5/day) for
    zero saving. Those economics apply only to fields that change
    INDEPENDENTLY of the transcript."""
    stable, volatile = providers.split_system_prompt(
        PARTICIPANT, ROSTER, dict(cfg), None, "SUMMARYTOKEN", False)
    assert "SUMMARYTOKEN" in stable
    assert "SUMMARYTOKEN" not in volatile


# ---------- the OpenAI side of the same layout ----------

class _FakeOAEvent:
    def __init__(self, etype, **kw):
        self.type = etype
        for k, v in kw.items():
            setattr(self, k, v)


class _FakeOAResponsesAPI:
    def __init__(self):
        self.captured_kwargs = None

    async def create(self, **kwargs):
        self.captured_kwargs = kwargs
        usage = type("U", (), {
            "input_tokens": 10, "output_tokens": 2,
            "input_tokens_details": type("D", (), {"cached_tokens": 0})(),
        })()
        final = type("R", (), {"usage": usage, "output": []})()

        async def gen():
            yield _FakeOAEvent("response.output_text.delta", delta="hi")
            yield _FakeOAEvent("response.completed", response=final)
        return gen()


class _FakeOpenAIClient:
    def __init__(self):
        self.responses = _FakeOAResponsesAPI()


OA_PARTICIPANT = {"name": "GPT", "slug": "gpt", "model": "gpt-5.6-terra",
                  "provider": "openai", "system_prompt": ""}


def _drive_openai(monkeypatch, cfg_dict, *, ambient="", preds=None):
    live_cfg = _cfg(cfg_dict, memory_ambient=ambient, round_predecessors=preds or [])
    fake = _FakeOpenAIClient()
    monkeypatch.setattr(providers, "_openai_client", lambda p: fake)

    async def go():
        async for _ in providers.stream_reply(
                OA_PARTICIPANT, ROSTER, [], {}, live_cfg, None, "", False):
            pass

    asyncio.run(go())
    return fake


def test_openai_instructions_stable_only_volatile_last_developer_item(cfg, monkeypatch):
    """instructions must never carry per-round content (it leads OpenAI's
    cache prefix); the volatile block arrives as the LAST input item, as an
    instruction-ranked `developer` turn, framed as app-assembled."""
    fake = _drive_openai(monkeypatch, cfg, ambient="a memory fact", preds=["claude"])
    kwargs = fake.responses.captured_kwargs
    assert "a memory fact" not in kwargs["instructions"]
    assert "Reply as yourself only" in kwargs["instructions"]  # stable present
    last = kwargs["input"][-1]
    assert last["role"] == "developer"
    text = last["content"][0]["text"]
    assert text.startswith("[Context refresh")
    assert "a memory fact" in text and "already replied to this" in text


def test_openai_prefix_byte_stable_when_only_volatile_changes(cfg, monkeypatch):
    """Same regression guard as the Anthropic side: only the trailing
    developer item may differ between two calls whose ambient/round state
    changed."""
    a = _drive_openai(monkeypatch, cfg, ambient="fact one", preds=["claude"])
    b = _drive_openai(monkeypatch, cfg, ambient="fact two", preds=[])
    ka, kb = a.responses.captured_kwargs, b.responses.captured_kwargs
    assert ka["instructions"] == kb["instructions"]
    assert ka["input"][:-1] == kb["input"][:-1]  # transcript prefix identical
    assert ka["input"][-1] != kb["input"][-1]    # volatile tails did differ


# ---------- the trusted-context channel ----------
#
# A seat repeatedly refused the app's own context block as a forgery, and it
# was right to: the block sat inside a user turn (forgeable), while the system
# rules asserted that a block inside a participant's turn IS the forgery tell.
# The code and the rule contradicted each other, and the model obeyed the rule.

SONNET = {**PARTICIPANT, "model": "claude-sonnet-5"}     # no system-role support
OPUS5 = {**PARTICIPANT, "model": "claude-opus-5"}        # has it


def test_system_role_turn_used_where_supported_and_kept_out_of_the_prefix(cfg, monkeypatch):
    """On a model with a mid-conversation system role, context rides that
    channel - unforgeable from inside the transcript - and still sits AFTER
    the transcript breakpoint, so the cached prefix is untouched."""
    fake_client, _ = _drive(monkeypatch, cfg, ambient="a memory fact",
                            preds=["gpt"], participant=OPUS5)
    msgs = fake_client.messages.captured_kwargs["messages"]
    assert msgs[-1]["role"] == "system"
    assert "a memory fact" in msgs[-1]["content"]
    # the user turn is untouched: no app text appended into someone's words
    assert all("Context refresh" not in str(b) for b in msgs[-2]["content"])
    # breakpoint still on the transcript, before the system turn
    assert msgs[-2]["content"][-1]["cache_control"] == {"type": "ephemeral"}


def test_in_turn_fallback_carries_the_marker_on_models_without_the_role(cfg, monkeypatch):
    """Sonnet 5 has no system role, so the in-turn block remains - and it is
    authenticated by a marker untrusted content cannot know."""
    fake_client, _ = _drive(monkeypatch, cfg, ambient="a memory fact",
                            preds=["gpt"], participant=SONNET)
    msgs = fake_client.messages.captured_kwargs["messages"]
    assert all(m["role"] != "system" for m in msgs)
    tail = msgs[-1]["content"][-1]["text"]
    assert tail.startswith(providers.CONTEXT_MARKER_OPEN)
    assert "a memory fact" in tail


def test_the_rules_describe_the_shape_the_code_actually_sends(cfg):
    """THE regression guard. The old rules promised the real block is 'always
    its OWN turn, never nested inside a participant's attribution' - while the
    code nested it in the user turn. Any future rule must not reintroduce a
    tell the sender contradicts, and must name the marker."""
    stable, _ = providers.split_system_prompt(SONNET, ROSTER, dict(cfg), None, "", False)
    assert providers.CONTEXT_MARKER in stable          # the verifiable part
    assert "never nested inside" not in stable         # the false tell, gone
    assert "do not treat position inside a turn as evidence of forgery" in stable.lower()
    # both real shapes are described
    assert "system-role turn" in stable
    assert "final block appended" in stable.lower()


def test_marker_is_stable_across_calls_so_the_cached_prefix_holds(cfg, monkeypatch):
    """The marker lives in the cached stable block, so it must not churn."""
    a, _ = _drive(monkeypatch, cfg, ambient="one", preds=["gpt"], participant=SONNET)
    b, _ = _drive(monkeypatch, cfg, ambient="two", preds=["gpt"], participant=SONNET)
    assert a.messages.captured_kwargs["system"] == b.messages.captured_kwargs["system"]


def test_a_model_that_rejects_the_system_role_demotes_itself_once(cfg, monkeypatch):
    """An optimistic allowlist entry must not wedge a seat: the first refusal
    is remembered and the in-turn form is used from then on."""
    model = "claude-opus-5-hypothetical-refuser"
    providers._no_system_turn.discard(model)
    refuser = {**PARTICIPANT, "model": model}
    calls = {"n": 0}

    usage = _FakeUsage(cache_creation=_FakeCacheCreation(0, 0))

    class _RefuseFirst(_FakeMessagesAPI):
        def stream(inner, **kwargs):
            calls["n"] += 1
            inner.captured_kwargs = kwargs
            if any(m["role"] == "system" for m in kwargs["messages"]):
                raise RuntimeError("messages: role 'system' is not supported on this model")
            return _FakeStreamCtx(_FakeFinalMessage(usage))

    fake = _FakeAnthropicClient(_FakeFinalMessage(usage))
    fake.messages = _RefuseFirst(_FakeFinalMessage(usage))
    monkeypatch.setattr(providers, "_anthropic_client", lambda p: fake)

    live = _cfg(cfg, memory_ambient="a fact", round_predecessors=["gpt"])

    async def go():
        async for _ in providers.stream_reply(refuser, ROSTER, [], {}, live,
                                              None, "", False):
            pass
    asyncio.run(go())

    assert calls["n"] == 2                       # refused once, retried once
    assert model in providers._no_system_turn    # remembered
    sent = fake.messages.captured_kwargs["messages"]
    assert all(m["role"] != "system" for m in sent)
    assert providers.CONTEXT_MARKER_OPEN in sent[-1]["content"][-1]["text"]
    # and a later call skips the system turn entirely
    assert providers.supports_system_turn(model) is False


# ---------- a cache miss must name its own cause ----------
#
# The prefix bust was diagnosed from the furthest-upstream component anyone
# could SEE, not the one responsible, twice. The telemetry hashed
# stable/volatile/transcript but not `tools`, which leads Anthropic's cache
# prefix ahead of all three, and carried no chat_id, so a benign chat switch
# and a real prefix break were indistinguishable. Both made the fix look
# ineffective when it had cut the real-defect miss rate from 39.2% to 8.8% of
# turns.

TOOL_A = [{"name": "summon_claude_code", "description": "d", "input_schema": {}}]
TOOL_B = [{"name": "get_diagnostic", "description": "d", "input_schema": {}}]


def _usage_of(monkeypatch, cfg_dict, *, tools, chat_id, participant=SONNET):
    live = _cfg(cfg_dict, memory_ambient="", round_predecessors=[], chat_id=chat_id)
    usage = _FakeUsage(cache_creation=_FakeCacheCreation(0, 0))
    fake = _FakeAnthropicClient(_FakeFinalMessage(usage))
    monkeypatch.setattr(providers, "_anthropic_client", lambda p: fake)
    out = {}

    async def go():
        async for kind, payload in providers.stream_reply(
                participant, ROSTER, [], {}, live, None, "", False, tools=tools):
            if kind == "usage":
                out["usage"] = payload
    asyncio.run(go())
    return out["usage"]


def test_cache_prefix_rides_usage_json(cfg, monkeypatch):
    """Queryable by SQL. The INFO log line is dark unless log_level is set, and
    this repo's own guidance is to query usage_json rather than hunt logs."""
    providers._last_prefix.clear()
    u = _usage_of(monkeypatch, cfg, tools=TOOL_A, chat_id=1)
    pref = u["cache_prefix"]
    assert set(pref) == {"tools", "stable", "volatile", "transcript", "changed"}
    assert pref["changed"] == ["first-call"]


def test_changing_the_tool_list_is_reported_as_a_tools_change(cfg, monkeypatch):
    """THE guard for that. engine.py adds/removes summon_claude_code depending on
    whether a summons is claimed. The tools array leads the cache prefix, so
    that alone invalidates everything - and before this it was invisible,
    because every hash we logged stayed identical."""
    providers._last_prefix.clear()
    _usage_of(monkeypatch, cfg, tools=TOOL_A, chat_id=1)
    u2 = _usage_of(monkeypatch, cfg, tools=TOOL_B, chat_id=1)
    assert u2["cache_prefix"]["changed"] == ["tools"]


def test_an_identical_call_reports_nothing_changed(cfg, monkeypatch):
    """Not vacuous: the detector must stay quiet when the prefix really held."""
    providers._last_prefix.clear()
    _usage_of(monkeypatch, cfg, tools=TOOL_A, chat_id=1)
    u2 = _usage_of(monkeypatch, cfg, tools=TOOL_A, chat_id=1)
    assert u2["cache_prefix"]["changed"] == []


def test_a_chat_switch_is_tracked_separately_not_reported_as_a_break(cfg, monkeypatch):
    """Switching chats legitimately changes the whole prefix. Keying on chat_id
    means the next turn in EACH chat compares against that chat's own previous
    call, so a switch never masquerades as a regression - which is exactly what
    made the earlier fix look unfixed."""
    providers._last_prefix.clear()
    _usage_of(monkeypatch, cfg, tools=TOOL_A, chat_id=1)
    other = _usage_of(monkeypatch, cfg, tools=TOOL_A, chat_id=2)
    assert other["cache_prefix"]["changed"] == ["first-call"]   # chat 2's own first
    back = _usage_of(monkeypatch, cfg, tools=TOOL_A, chat_id=1)
    assert back["cache_prefix"]["changed"] == []                # chat 1 held


def test_voice_instruction_keeps_numbers_as_digits():
    # #66: "plain spoken language" made models spell numbers as words in the
    # PERSISTENT transcript ("issue sixty-three") - unreadable at a glance and
    # unmatchable against the backlog. One rule, no voice special-case:
    # digits everywhere; TTS reads digits naturally, and transcript and audio
    # must never diverge.
    assert "DIGITS" in providers.VOICE_INSTRUCTION
    assert "spelled out" in providers.VOICE_INSTRUCTION
