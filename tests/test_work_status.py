"""Announce pending external work before the chat goes silent,
using a STRUCTURED status event (a trusted activity label) rather than
hardcoded filler text, and NEVER persisting it into a chat message or the
model's own reply content.

Engine-level, not prompt-only (a model can forget to say something; a timer
can't): backend/work_status.py is the shared policy (which tools count as
"may create noticeable delay", the 20s/30s thresholds, the activity-label
taxonomy), consumed by two independent call sites --

- backend/providers.py's `_tool_batch_events` (both provider adapters'
  synchronous tool-await loop) -- an immediate start event plus timed ongoing
  check-ins, all riding the SAME SSE stream as the model's own reply but as a
  DISTINCT event kind ("work_status"), never folded into live["content"];
- backend/guestjobs.py's `_status_pinger` -- the delegated Claude Code guest
  job's own multi-minute run, which continues after the round that summoned
  it has already finished. Its ping is fully ephemeral: a patch to the job
  row (status_label/status_at) broadcast over the existing guest_job events
  channel, never a chat message.

No live API calls; provider clients and the guest runner are faked."""

import asyncio

import pytest

from backend import db, guest, guestjobs, providers, work_status


# ---------- the policy itself ----------

def test_fast_tools_excludes_web_github_and_delegation():
    for slow in ("web_search", "fetch_page", "summon_claude_code",
                 "read_github_issues", "file_github_issue", "an_mcp_tool"):
        assert slow not in work_status.FAST_TOOLS


def test_fast_tools_are_the_instant_local_ones():
    for fast in ("get_diagnostic", "recall_memory", "search_history", "save_memory"):
        assert fast in work_status.FAST_TOOLS


def test_is_announceable_true_for_any_non_fast_tool():
    assert work_status.is_announceable(["recall_memory", "web_search"])
    assert work_status.is_announceable(["summon_claude_code"])


def test_is_announceable_false_when_every_tool_is_fast():
    assert not work_status.is_announceable(["get_diagnostic"])
    assert not work_status.is_announceable(["recall_memory", "save_memory"])


def test_is_announceable_false_for_empty_batch():
    assert not work_status.is_announceable([])


def test_thresholds_match_the_248_acceptance_criteria():
    assert work_status.CHECKIN_THRESHOLD_S == 20.0
    assert work_status.CHECKIN_INTERVAL_S == 30.0


def test_activity_labels_never_claim_completion_or_an_eta_or_leak_the_tool():
    for tool in list(work_status.TOOL_LABELS) + ["an_unmapped_tool"]:
        label = work_status.activity_label(tool)
        low = label.lower()
        for banned in ("done", "finished", "complete", "second", "minute",
                       "soon", "shortly", "almost there"):
            assert banned not in low, label
        # the label must never just echo the raw tool/function name
        assert tool.replace("_", " ") not in low or tool == "an_unmapped_tool"


def test_activity_label_resolves_known_built_in_tools():
    assert work_status.activity_label("web_search") == "Searching the web"
    assert work_status.activity_label("read_github_pr") == "Checking GitHub"
    assert work_status.activity_label("summon_claude_code") == "Delegating to Claude Code"


def test_activity_label_falls_back_generically_for_unknown_tools():
    assert work_status.activity_label("some_future_tool") == work_status.GENERIC_TOOL_LABEL


def test_activity_label_uses_the_mcp_servers_own_trusted_label_when_configured():
    class _FakeMcp:
        def activity_label(self, server):
            return {"build-watcher": "Checking build status"}.get(server)
    mcp = _FakeMcp()
    assert work_status.activity_label("mcp__build-watcher__search", mcp=mcp) == "Checking build status"
    # an MCP server with no configured label still degrades to the generic
    # fallback, never a guess at what it does
    assert work_status.activity_label("mcp__mystery__do_thing", mcp=mcp) == work_status.GENERIC_TOOL_LABEL
    # and with no manager supplied at all (offline dispatch path)
    assert work_status.activity_label("mcp__build-watcher__search") == work_status.GENERIC_TOOL_LABEL


def test_batch_activity_picks_the_first_non_fast_tools_label():
    assert work_status.batch_activity(["recall_memory", "web_search"]) == "Searching the web"
    assert work_status.batch_activity(["summon_claude_code"]) == "Delegating to Claude Code"


def test_guest_job_label_keys_on_mode():
    assert work_status.guest_job_label("investigate") == "Investigating the code"
    assert work_status.guest_job_label("implement") == "Building the change"
    assert work_status.guest_job_label("bogus") == work_status.GUEST_JOB_LABEL_DEFAULT


# ---------- _tool_batch_events: the shared timer/announce generator ----------

async def _slow(delay, result="ok"):
    await asyncio.sleep(delay)
    return result


def test_no_label_yields_only_done_events_in_order():
    async def go():
        tasks = [asyncio.create_task(_slow(0.01, "a")), asyncio.create_task(_slow(0.0, "b"))]
        return [ev async for ev in providers._tool_batch_events(tasks, label=None)]
    events = asyncio.run(go())
    assert [e[0] for e in events] == ["done", "done"]
    assert [e[1] for e in events] == [0, 1]  # original order preserved


def test_label_yields_start_work_status_before_any_done():
    async def go():
        tasks = [asyncio.create_task(_slow(0.02))]
        return [ev async for ev in providers._tool_batch_events(tasks, label="Searching the web")]
    events = asyncio.run(go())
    assert events[0] == ("work_status", {"phase": "start", "label": "Searching the web"})
    assert events[-1] == ("done", 0)


def test_ongoing_checkin_fires_after_threshold_and_repeats_at_interval(monkeypatch):
    monkeypatch.setattr(work_status, "CHECKIN_THRESHOLD_S", 0.03)
    monkeypatch.setattr(work_status, "CHECKIN_INTERVAL_S", 0.03)

    async def go():
        tasks = [asyncio.create_task(_slow(0.11))]
        return [ev async for ev in providers._tool_batch_events(tasks, label="Searching the web")]
    events = asyncio.run(go())
    statuses = [e for e in events if e[0] == "work_status"]
    # one "start" immediately, then multiple "ongoing" while the single slow
    # task keeps running past threshold/interval
    assert statuses[0] == ("work_status", {"phase": "start", "label": "Searching the web"})
    assert statuses.count(("work_status", {"phase": "ongoing", "label": "Searching the web"})) >= 2
    assert events[-1] == ("done", 0)


def test_no_trailing_checkin_once_all_tasks_are_done(monkeypatch):
    monkeypatch.setattr(work_status, "CHECKIN_THRESHOLD_S", 0.03)
    monkeypatch.setattr(work_status, "CHECKIN_INTERVAL_S", 100.0)

    async def go():
        tasks = [asyncio.create_task(_slow(0.04))]
        return [ev async for ev in providers._tool_batch_events(tasks, label="Searching the web")]
    events = asyncio.run(go())
    assert events[-1] == ("done", 0)  # nothing announced AFTER completion


# ---------- full stream_reply: both providers, avoiding chatter for fast work ----------

PARTICIPANT = {"name": "Claude", "slug": "claude", "model": "claude-opus-4-8",
               "provider": "anthropic", "system_prompt": ""}
OA_PARTICIPANT = {"name": "GPT", "slug": "gpt", "model": "gpt-5.6-terra",
                  "provider": "openai", "system_prompt": ""}
ROSTER = [{"name": "Claude", "slug": "claude"}, {"name": "GPT", "slug": "gpt"}]


class _Block:
    def __init__(self, name, block_id):
        self.type = "tool_use"
        self.name = name
        self.input = {}
        self.id = block_id


class _Usage:
    input_tokens = 1
    output_tokens = 1
    cache_read_input_tokens = 0
    cache_creation_input_tokens = 0
    cache_creation = None


class _Final:
    def __init__(self, stop_reason, content):
        self.usage = _Usage()
        self.stop_reason = stop_reason
        self.content = content


class _StreamCtx:
    def __init__(self, final):
        self._final = final

    async def __aenter__(self):
        return self

    async def __aexit__(self, *a):
        return False

    @property
    def text_stream(self):
        async def gen():
            yield "hi"
        return gen()

    async def get_final_message(self):
        return self._final


class _TwoRoundClient:
    def __init__(self, tool_name):
        self.calls = 0

        class _Msgs:
            def stream(inner, **kwargs):
                final = (_Final("tool_use", [_Block(tool_name, "t1")])
                         if self.calls == 0 else _Final("end_turn", []))
                self.calls += 1
                return _StreamCtx(final)
        self.messages = _Msgs()


async def _run_tool_delay(name, tool_input, cfg_, origin_agent=None, memory=None, delay=0.0):
    await asyncio.sleep(delay)
    return f"out-{name}"


def _drive_anthropic(monkeypatch, tool_name, delay, cfg):
    fake = _TwoRoundClient(tool_name)
    monkeypatch.setattr(providers, "_anthropic_client", lambda p: fake)

    async def fake_run_tool(name, tool_input, cfg_, origin_agent=None, memory=None):
        return await _run_tool_delay(name, tool_input, cfg_, delay=delay)
    monkeypatch.setattr(providers, "run_tool", fake_run_tool)

    events = []

    async def go():
        async for ev in providers.stream_reply(
                PARTICIPANT, ROSTER, [], {}, dict(cfg), None, "", False,
                tools=[{"name": tool_name, "description": "d", "input_schema": {}}]):
            events.append(ev)
    asyncio.run(go())
    return events


def test_slow_tool_gets_start_work_status_before_the_result(monkeypatch, cfg):
    events = _drive_anthropic(monkeypatch, "web_search", delay=0.01, cfg=cfg)
    kinds = [e[0] for e in events]
    status_idx = kinds.index("work_status")
    assert events[status_idx][1] == {"phase": "start", "label": "Searching the web"}
    tool_idx = kinds.index("tool")
    assert status_idx < tool_idx  # acknowledged BEFORE the tool result lands


def test_work_status_never_rides_the_text_channel_or_the_persisted_reply(monkeypatch, cfg):
    """This fixed a real bug: the old implementation folded its
    acknowledgement into a ("text", ...) event, which engine.py appends
    straight into live["content"] -- i.e. it was literally persisted into
    the model's own saved reply. work_status must be a fully separate kind
    that never contaminates the text a client would save/read back."""
    events = _drive_anthropic(monkeypatch, "web_search", delay=0.01, cfg=cfg)
    joined_text = "".join(e[1] for e in events if e[0] == "text")
    assert "Searching the web" not in joined_text
    assert any(e[0] == "work_status" for e in events)


def test_fast_tool_gets_no_acknowledgement_at_all(monkeypatch, cfg):
    events = _drive_anthropic(monkeypatch, "recall_memory", delay=0.0, cfg=cfg)
    assert not any(e[0] == "work_status" for e in events)


def test_openai_slow_tool_gets_start_work_status_before_the_result(monkeypatch, cfg):
    class _FakeOAResponsesAPI:
        def __init__(self):
            self.calls = 0

        async def create(self, **kwargs):
            calls = self.calls
            self.calls += 1

            async def gen():
                yield type("E", (), {"type": "response.output_text.delta", "delta": "hi"})()
                if calls == 0:
                    call = type("C", (), {"type": "function_call", "call_id": "c1",
                                          "name": "read_github_issues", "arguments": "{}"})()
                    resp = type("R", (), {"usage": None, "output": [call]})()
                else:
                    resp = type("R", (), {"usage": None, "output": []})()
                yield type("E", (), {"type": "response.completed", "response": resp})()
            return gen()

    class _FakeOpenAIClient:
        def __init__(self):
            self.responses = _FakeOAResponsesAPI()

    monkeypatch.setattr(providers, "_openai_client", lambda p: _FakeOpenAIClient())

    async def fake_run_tool(name, tool_input, cfg_, origin_agent=None, memory=None):
        await asyncio.sleep(0.01)
        return "out"
    monkeypatch.setattr(providers, "run_tool", fake_run_tool)

    events = []

    async def go():
        async for ev in providers.stream_reply(
                OA_PARTICIPANT, ROSTER, [], {}, dict(cfg), None, "", False,
                tools=[{"name": "read_github_issues", "description": "d", "input_schema": {}}]):
            events.append(ev)
    asyncio.run(go())

    kinds = [e[0] for e in events]
    status_idx = kinds.index("work_status")
    assert events[status_idx][1] == {"phase": "start", "label": "Checking GitHub"}
    assert status_idx < kinds.index("tool")


def test_barge_in_still_cancels_the_sibling_with_a_label_active(monkeypatch, cfg):
    """Work status must never regress barge-in: closing the generator mid-wait
    still cancels an in-flight tool call, announced or not."""
    fake = _TwoRoundClient("web_search")
    monkeypatch.setattr(providers, "_anthropic_client", lambda p: fake)
    state = {}

    async def fake_run_tool(name, tool_input, cfg_, origin_agent=None, memory=None):
        try:
            await asyncio.sleep(30)
            state["done"] = True
            return "ok"
        except asyncio.CancelledError:
            state["cancelled"] = True
            raise
    monkeypatch.setattr(providers, "run_tool", fake_run_tool)

    async def go():
        agen = providers.stream_reply(
            PARTICIPANT, ROSTER, [], {}, dict(cfg), None, "", False,
            tools=[{"name": "web_search", "description": "d", "input_schema": {}}])
        async for ev in agen:
            if ev[0] == "work_status" and ev[1]["phase"] == "start":
                await agen.aclose()
                break
        await asyncio.sleep(0.01)
    asyncio.run(go())
    assert state.get("cancelled") is True
    assert "done" not in state


# ---------- the delegated-job side: backend/guestjobs.py's periodic pinger ----------

@pytest.fixture
def chat(tmp_path):
    db.configure(tmp_path / "data")
    db.init()
    con = db.connect()
    cur = con.execute(
        "INSERT INTO chats(created_at, updated_at, code_enabled) VALUES(?,?,1)",
        (db.now(), db.now()))
    cid = cur.lastrowid
    con.commit()
    con.close()
    guestjobs._jobs.clear()
    yield cid
    guestjobs._jobs.clear()
    guest._pending.clear()


def _guest_messages(chat_id):
    con = db.connect()
    msgs = [m for m in db.get_chat_messages(con, chat_id) if m["speaker"] == guest.SLUG]
    con.close()
    return msgs


def fake_guest_slow(delay, events):
    async def run_guest(task, repo_key, context, cfg, mode="investigate",
                        resume=None, chat_id=0, model="", effort="",
                        session_key=None, ref=""):
        await asyncio.sleep(delay)
        for ev in events:
            yield ev
    return run_guest


def test_no_ping_when_job_finishes_before_the_threshold(chat, monkeypatch):
    monkeypatch.setattr(work_status, "CHECKIN_THRESHOLD_S", 5.0)
    monkeypatch.setattr(guest, "run_guest", fake_guest_slow(0.01, [
        ("text", "fast result"), ("usage", {"cost": 0.0, "session_id": "s"}),
    ]))

    async def go():
        job = guestjobs.start(chat, "a quick task here", "demo", "ctx", {}, handback=None)
        await job.task
        return job.id
    job_id = asyncio.run(go())
    msgs = _guest_messages(chat)
    assert [m["content"] for m in msgs] == ["fast result"]  # no check-in text mixed in
    con = db.connect()
    row = db.get_guest_job(con, job_id)
    con.close()
    assert row["status_label"] == ""  # pinger never fired


def test_ping_fires_after_threshold_and_repeats_ephemerally(chat, monkeypatch):
    """The periodic ping is NEVER a chat message -- it patches the job
    row and broadcasts over the guest_job events channel. A reconnecting
    client re-reads db.get_guest_job (or GET .../guest_jobs), not a replayed
    message that never happened in the conversation."""
    monkeypatch.setattr(work_status, "CHECKIN_THRESHOLD_S", 0.03)
    monkeypatch.setattr(work_status, "CHECKIN_INTERVAL_S", 0.03)
    monkeypatch.setattr(guest, "run_guest", fake_guest_slow(0.15, [
        ("text", "the real result"), ("usage", {"cost": 0.0, "session_id": "s"}),
    ]))
    broadcasts = []
    from backend import events as events_mod
    monkeypatch.setattr(events_mod, "notify_guest_job", lambda: broadcasts.append(1))

    async def go():
        job = guestjobs.start(chat, "a slow investigation here", "demo", "ctx", {},
                              handback=None, mode="investigate")
        await asyncio.sleep(0.11)
        con = db.connect()
        mid_run_label = db.get_guest_job(con, job.id)["status_label"]
        con.close()
        await job.task
        return job.id, mid_run_label
    job_id, mid_run_label = asyncio.run(go())
    assert mid_run_label == "Investigating the code"
    assert len(broadcasts) >= 2  # at least two ephemeral pings broadcast while running

    msgs = _guest_messages(chat)
    assert [m["content"] for m in msgs] == ["the real result"]  # NO check-in messages, ever


def test_pinger_stops_the_moment_the_job_settles(chat, monkeypatch):
    monkeypatch.setattr(work_status, "CHECKIN_THRESHOLD_S", 0.03)
    monkeypatch.setattr(work_status, "CHECKIN_INTERVAL_S", 0.03)
    monkeypatch.setattr(guest, "run_guest", fake_guest_slow(0.05, [
        ("text", "result"), ("usage", {"cost": 0.0, "session_id": "s"}),
    ]))

    async def go():
        job = guestjobs.start(chat, "another slow task here", "demo", "ctx", {},
                              handback=None)
        await job.task
        con = db.connect()
        label_at_done = db.get_guest_job(con, job.id)["status_at"]
        con.close()
        await asyncio.sleep(0.15)  # long enough for 2+ more intervals, if it kept pinging
        con = db.connect()
        label_later = db.get_guest_job(con, job.id)["status_at"]
        con.close()
        return label_at_done, label_later
    at_done, later = asyncio.run(go())
    assert later == at_done  # nothing more arrived after settlement


def test_ping_still_fires_when_another_round_is_active(chat, monkeypatch):
    """A real round in progress is no longer relevant to the ping at all
    (the round-competing-for-voice special case was dropped along with the
    fake message) -- the ephemeral broadcast always fires."""
    monkeypatch.setattr(work_status, "CHECKIN_THRESHOLD_S", 0.03)
    monkeypatch.setattr(guest, "run_guest", fake_guest_slow(0.08, [
        ("text", "result"), ("usage", {"cost": 0.0, "session_id": "s"}),
    ]))
    from backend import rounds
    monkeypatch.setattr(rounds, "active", lambda chat_id: object())  # "a round is live"

    async def go():
        job = guestjobs.start(chat, "yet another slow task", "demo", "ctx", {},
                              handback=None)
        await job.task
        return job.id
    job_id = asyncio.run(go())
    con = db.connect()
    row = db.get_guest_job(con, job_id)
    con.close()
    assert row["status_label"] != ""  # the ping still landed, ephemerally
    msgs = _guest_messages(chat)
    assert [m["content"] for m in msgs] == ["result"]  # never a check-in message
