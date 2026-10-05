"""Long MCP work watched in the background (#604).

A tool on an outside MCP server can start work that runs for minutes. Its
result says so in a `background` block, and backend/mcpjobs.py watches the
work from a detached task, the way a Claude Code guest job runs, so no
round waits on it. These tests drive the watcher against a fake manager
whose progress tool answers from a script: the start, the spoken progress
rule and its rate limit, the question and result hand-backs, a barge-in, a
failed poll, a follow-up job, a clean stop and the note every seat reads.
One test runs the whole path against the real fake stdio server, so the
SDK's own result field is what's read. Keyless, no network."""

import asyncio
import json
import sys
from pathlib import Path

import pytest

from backend import db, engine, events, guestjobs, mcpjobs, rounds
from backend import tools as tools_mod
from backend.mcp_client import CallOutcome, McpManager
from backend.providers import _volatile_system_parts

FAKE = str(Path(__file__).parent / "fake_mcp.py")

ASK = "mcp__dovetail__dovetail_ask"
PROGRESS = "mcp__dovetail__dovetail_progress"


def block(state="running", job="job-1", stage="cutting the legs", steps=3,
          elapsed_s=20, waiting_for=None, ask="", reply="", **extra):
    """A block as an older server sends it. `extra` adds the optional
    fields a newer one sends (#607): outcome, error, parts, edits and
    answered."""
    return {"background": {
        "job": job, "state": state, "title": "Dovetail",
        "progress_tool": "dovetail_progress", "stage": stage,
        "steps": steps, "elapsed_s": elapsed_s, "waiting_for": waiting_for,
        "ask": ask, "reply": reply, **extra}}


class FakeManager:
    """The McpManager surface the watcher and the dispatcher use. `script`
    is what successive polls return: a structured dict, None for a failed
    poll, or an exception to raise."""

    def __init__(self, script=(), first=None):
        self.script = list(script)
        self.first = first if first is not None else block()
        self.polls = 0

    def server_of(self, qualified):
        if qualified.startswith("mcp__dovetail__"):
            return ("dovetail", qualified.split("__", 2)[2])
        return None

    def qualified(self, server, tool):
        if server == "dovetail" and tool in ("dovetail_ask",
                                              "dovetail_progress"):
            return f"mcp__dovetail__{tool}"
        return None

    async def call_result(self, qualified, args, cap=8000):
        return CallOutcome("Dovetail is on it.", self.first)

    async def poll(self, server, tool, timeout=10.0):
        self.polls += 1
        if not self.script:
            return block(stage=f"poll {self.polls}", steps=self.polls)
        item = self.script.pop(0)
        if isinstance(item, Exception):
            raise item
        return item


@pytest.fixture
def chat(tmp_path, monkeypatch):
    db.configure(tmp_path / "data")
    db.init()
    con = db.connect()
    cid = con.execute("INSERT INTO chats(created_at, updated_at) VALUES(?,?)",
                      (db.now(), db.now())).lastrowid
    con.commit()
    con.close()
    mcpjobs.reset()
    # Fast clocks: a tick per poll, no settle before a hand-back.
    monkeypatch.setattr(mcpjobs, "POLL_S", 0.01)
    monkeypatch.setattr(mcpjobs, "_SETTLE",
                        {"progress": 0.0, "blocker": 0.0, "result": 0.0})
    monkeypatch.setattr(guestjobs, "_PAUSE_POLL_S", 0.005)
    yield cid
    mcpjobs.reset()


class Handbacks:
    def __init__(self):
        self.calls = []

    async def __call__(self, chat_id, kind, note=""):
        self.calls.append((chat_id, kind, note))

    def kinds(self):
        return [k for _, k, _ in self.calls]


def cfg_for(chat_id, mgr, handback=None, every=120.0):
    return {"chat_id": chat_id, "_mcp": mgr, "_handback": handback,
            "max_tool_output": 8000, "mcp_progress_every_s": every}


async def until(cond, tries=400, delay=0.01):
    for _ in range(tries):
        if cond():
            return True
        await asyncio.sleep(delay)
    return False


def ext_messages(chat_id):
    con = db.connect()
    msgs = [m for m in db.get_chat_messages(con, chat_id)
            if m["speaker"].startswith("ext:")]
    con.close()
    return msgs


# ---------- the block ----------

def test_the_block_is_typed_and_capped():
    b = mcpjobs.background_block(block(stage="x" * 500, steps=48,
                                       elapsed_s=312))
    assert b["state"] == "running" and b["job"] == "job-1"
    assert len(b["stage"]) == mcpjobs.STAGE_CHARS
    assert b["steps"] == 48 and b["elapsed_s"] == 312
    # no block, a bad state, no job, or a junk field degrade safely
    assert mcpjobs.background_block(None) is None
    assert mcpjobs.background_block({"other": 1}) is None
    assert mcpjobs.background_block(block(state="exploded")) is None
    assert mcpjobs.background_block({"background": {"state": "running"}}) \
        is None
    odd = block(steps="lots", waiting_for="pizza")
    odd["background"]["stage"] = "two\nlines"
    b = mcpjobs.background_block(odd)
    assert b["steps"] is None and b["waiting_for"] is None
    assert b["stage"] == "two lines"


def test_the_newer_fields_are_optional_and_checked():
    # an older server's done block reads as finished, with no counts
    old = mcpjobs.background_block(block(state="done"))
    assert old["outcome"] == "finished"
    assert old["parts"] is None and old["edits"] is None
    assert old["error"] == "" and old["answered"] == ""
    # running work has no outcome, whatever the server says
    assert b(outcome="failed")["outcome"] is None
    failed = b(state="done", outcome="failed", error="The disk\nis full",
               parts=18, edits=42, answered="Taken as the reply.")
    assert failed["outcome"] == "failed" and failed["error"] == \
        "The disk is full"
    assert failed["parts"] == 18 and failed["edits"] == 42
    assert failed["answered"] == "Taken as the reply."
    # an error only counts when the work failed, and junk degrades
    assert b(state="done", outcome="stopped", error="x")["error"] == ""
    junk = b(state="done", outcome="exploded", parts="lots", edits=-1,
             answered=7)
    assert junk["outcome"] == "finished"
    assert junk["parts"] is None and junk["edits"] is None
    assert junk["answered"] == ""


# ---------- starting ----------

def test_a_running_result_starts_a_detached_watch(chat):
    """The seat's call comes back running: a watcher starts for this chat
    and server, the seat still gets the server's text, and is told not to
    wait. The watch polls on its own."""
    mgr = FakeManager()

    async def go():
        out = await tools_mod.run_tool(ASK, {"message": "add a drawer"},
                                       cfg_for(chat, mgr))
        [w] = mcpjobs.active_for_chat(chat)
        assert await until(lambda: mgr.polls >= 2)
        alive = not w.task.done()
        await mcpjobs.stop_all()
        return out, w, alive

    out, w, alive = asyncio.run(go())
    assert out.startswith("Dovetail is on it.")
    assert "carries on in the background" in out and "Don't wait" in out
    assert alive and w.server == "dovetail" and w.title == "Dovetail"
    assert w.ask_tool == ASK and w.progress_tool == "dovetail_progress"
    snap = mcpjobs.snapshots(chat)[-1]
    assert snap["type"] == "mcp_job" and snap["server"] == "dovetail"


def test_nothing_starts_without_running_work_or_a_progress_tool(chat):
    async def go(first, chat_id=chat):
        mgr = FakeManager(first=first)
        out = await tools_mod.run_tool(ASK, {}, cfg_for(chat_id, mgr))
        started = bool(mcpjobs.active_for_chat(chat_id))
        await mcpjobs.stop_all()
        return out, started

    # finished at once: the seat has the reply in front of it
    assert asyncio.run(go(block(state="done", reply="Done."))) == \
        ("Dovetail is on it.", False)
    assert asyncio.run(go(block(state="idle")))[1] is False
    # no structured content at all: an ordinary tool
    assert asyncio.run(go({}))[1] is False
    # a progress tool the server never listed is not called
    unknown = block()
    unknown["background"]["progress_tool"] = "rm_rf"
    assert asyncio.run(go(unknown))[1] is False
    # no chat: nothing to hand back to
    assert asyncio.run(go(block(), chat_id=None))[1] is False


# ---------- spoken progress ----------

def _watcher(now=0.0, every=120.0):
    w = mcpjobs.Watcher(1, "dovetail", FakeManager(),
                        progress_tool="dovetail_progress", now=now,
                        progress_every=every)
    w.apply(mcpjobs.background_block(block(stage="cutting the legs",
                                           elapsed_s=0)),
            now, from_seat=True, tool=ASK)
    return w


def b(**kw):
    return mcpjobs.background_block(block(**kw))


def test_progress_is_spoken_only_on_a_new_stage_and_at_most_every_two_minutes():
    w = _watcher()
    # the stage moved on, but the work is in its first minute
    assert w.apply(b(stage="fitting the top", elapsed_s=40), 40) is None
    # past a minute, but the seat spoke about it under two minutes ago
    assert w.apply(b(stage="fitting the top", elapsed_s=90), 90) is None
    # two minutes since anyone spoke, and the stage is new: due
    assert w.apply(b(stage="fitting the top", elapsed_s=125), 125) == \
        "progress"
    # spoken (the relay marks it), then the same stage is never repeated
    w.last_spoken_at, w.last_spoken_stage = 126, w.stage
    assert w.apply(b(stage="fitting the top", elapsed_s=400), 400) is None
    # a new stage inside the two minutes waits
    assert w.apply(b(stage="adding the runners", elapsed_s=200), 200) is None
    # and is due once they've passed
    assert w.apply(b(stage="adding the runners", elapsed_s=250), 250) == \
        "progress"


def test_progress_counts_the_servers_own_elapsed_time():
    """Work begun elsewhere, say in the server's own window, is already
    past its first minute when the watch starts."""
    w = _watcher()
    w.last_spoken_at = -1000  # nobody has spoken about it in a long while
    assert w.apply(b(stage="sanding", elapsed_s=10), 5) is None
    assert w.apply(b(stage="sanding", elapsed_s=600), 6) == "progress"


def test_a_seats_own_call_resets_the_clock_and_the_stage():
    w = _watcher()
    w.apply(b(stage="fitting the top", elapsed_s=300), 300, from_seat=True,
            tool=PROGRESS)
    assert w.last_spoken_at == 300 and w.last_spoken_stage == "fitting the top"
    assert w.apply(b(stage="fitting the top", elapsed_s=500), 500) is None
    # the progress tool never becomes the tool a change goes through
    assert w.ask_tool == ASK


def test_progress_setting_zero_keeps_progress_quiet():
    w = _watcher(every=0)
    assert w.apply(b(stage="fitting the top", elapsed_s=900), 900) is None


def test_the_watcher_speaks_progress_through_the_narrator(chat):
    mgr = FakeManager(script=[block(stage="fitting the top", elapsed_s=200)])
    hb = Handbacks()

    async def go():
        await tools_mod.run_tool(ASK, {}, cfg_for(chat, mgr, hb, every=0.001))
        assert await until(lambda: hb.calls)
        await mcpjobs.stop_all()

    asyncio.run(go())
    chat_id, kind, note = hb.calls[0]
    assert (chat_id, kind) == (chat, "progress")
    assert "one short progress line" in note and "Don't call any tool" in note
    # a progress note posts nothing in the chat
    assert ext_messages(chat) == []


# ---------- hand-backs ----------

def test_a_question_is_posted_and_handed_back_at_once(chat):
    mgr = FakeManager(script=[
        block(state="waiting", waiting_for="question",
              ask="Which runner length, 450 or 500?"),
        # asked again on the next poll: handed back once
        block(state="waiting", waiting_for="question",
              ask="Which runner length, 450 or 500?"),
    ])
    hb = Handbacks()

    async def go():
        await tools_mod.run_tool(ASK, {}, cfg_for(chat, mgr, hb))
        assert await until(lambda: mgr.polls >= 4)
        await mcpjobs.stop_all()

    asyncio.run(go())
    assert hb.kinds() == ["blocker"]
    note = hb.calls[0][2]
    assert "waiting on the room" in note and ASK in note
    assert "don't answer it yourself" in note
    [msg] = ext_messages(chat)
    assert msg["speaker"] == "ext:dovetail"
    assert msg["content"] == ("**Dovetail is asking**\n"
                              "Which runner length, 450 or 500?")


def test_a_question_the_seat_already_has_is_not_handed_back(chat):
    first = block(state="waiting", waiting_for="question", ask="Oak or ash?")
    mgr = FakeManager(first=first, script=[first, first])
    hb = Handbacks()

    async def go():
        await tools_mod.run_tool(ASK, {}, cfg_for(chat, mgr, hb))
        assert await until(lambda: mgr.polls >= 3)
        await mcpjobs.stop_all()

    asyncio.run(go())
    assert hb.calls == [] and ext_messages(chat) == []


def test_the_result_is_relayed_and_then_the_watch_ends(chat):
    long_reply = "Built the chest. " + "It has four drawers. " * 200
    mgr = FakeManager(script=[block(state="done", reply=long_reply)])
    hb = Handbacks()

    async def go():
        await tools_mod.run_tool(ASK, {}, cfg_for(chat, mgr, hb))
        [w] = mcpjobs.active_for_chat(chat)
        assert await until(lambda: w.task.done())
        return w

    w = asyncio.run(go())
    assert hb.kinds() == ["result"]
    assert "has finished" in hb.calls[0][2]
    assert mcpjobs.active_for_chat(chat) == []
    assert w.ended == "done"
    [msg] = ext_messages(chat)
    assert msg["content"].startswith("**Dovetail finished**\nBuilt the chest.")
    assert len(msg["content"]) <= mcpjobs.RESULT_POST_CHARS + 30
    snap = mcpjobs.snapshots(chat)[-1]
    assert snap["watching"] is False and snap["state"] == "done"


def _ended(chat, done_block):
    mgr = FakeManager(script=[done_block])
    hb = Handbacks()

    async def go():
        await tools_mod.run_tool(ASK, {}, cfg_for(chat, mgr, hb))
        [w] = mcpjobs.active_for_chat(chat)
        assert await until(lambda: w.task.done())

    asyncio.run(go())
    [msg] = ext_messages(chat)
    [(_, kind, note)] = hb.calls
    assert kind == "result"
    return msg["content"], note, mcpjobs.snapshots(chat)[-1]


def test_work_that_failed_says_so_with_its_error(chat):
    post, note, snap = _ended(chat, block(
        state="done", outcome="failed", error="The disk is full",
        reply="Cut the legs and the top."))
    assert post == ("**Dovetail stopped with an error**\nThe disk is full"
                    "\n\nCut the legs and the top.")
    assert "stopped with an error" in note and "what the error was" in note
    assert "has finished" not in note
    assert snap["outcome"] == "failed" and snap["ended"] == "done"


def test_an_error_the_reply_already_gives_is_not_said_twice(chat):
    post, _, _ = _ended(chat, block(
        state="done", outcome="failed", error="The disk is full",
        reply="It stopped. The error: The disk is full. Ask it to carry on."))
    assert post == ("**Dovetail stopped with an error**\nIt stopped. The "
                    "error: The disk is full. Ask it to carry on.")


def test_work_that_was_stopped_says_so(chat):
    post, note, _ = _ended(chat, block(state="done", outcome="stopped",
                                       reply="Cut the legs."))
    assert post == "**Dovetail was stopped**\nCut the legs."
    assert "was stopped before it finished" in note


def test_an_answered_plan_is_said_so_nobody_says_it_still_waits(chat):
    line = "A message sent mid-build was taken as the reply to its plan."
    post, note, _ = _ended(chat, block(state="done", outcome="finished",
                                       answered=line, reply="Oak it is."))
    assert post == f"**Dovetail finished**\n{line}\n\nOak it is."
    assert line in note and "isn't waiting on that any more" in note


def test_idle_ends_the_watch_quietly(chat):
    mgr = FakeManager(script=[block(state="idle")])
    hb = Handbacks()

    async def go():
        await tools_mod.run_tool(ASK, {}, cfg_for(chat, mgr, hb))
        [w] = mcpjobs.active_for_chat(chat)
        assert await until(lambda: w.task.done())

    asyncio.run(go())
    assert hb.calls == [] and ext_messages(chat) == []


def test_a_new_job_id_keeps_the_same_watcher(chat):
    """A follow-up request gets a new job id. The watch carries on, and the
    result that comes back is the follow-up's."""
    mgr = FakeManager(script=[block(job="job-1")] * 2)
    hb = Handbacks()

    async def go():
        await tools_mod.run_tool(ASK, {}, cfg_for(chat, mgr, hb))
        [w] = mcpjobs.active_for_chat(chat)
        assert await until(lambda: mgr.polls >= 2)
        mgr.first = block(job="job-2", stage="adding a shelf")
        out = await tools_mod.run_tool(ASK, {"message": "add a shelf"},
                                       cfg_for(chat, mgr, hb))
        assert mcpjobs.active_for_chat(chat) == [w]
        assert w.job == "job-2" and "carries on" in out
        mgr.script = [block(job="job-2", state="done", reply="Shelf added.")]
        assert await until(lambda: w.task.done())
        return w

    w = asyncio.run(go())
    assert hb.kinds() == ["result"]
    assert ext_messages(chat)[-1]["content"].endswith("Shelf added.")


# ---------- what nothing in a round can touch ----------

def test_a_barge_in_never_cancels_the_watch(chat):
    """The watch starts inside a round, as a seat's tool call does, and the
    round is then aborted the way a barge-in aborts it. The watch keeps
    polling."""
    mgr = FakeManager()

    async def go():
        started = asyncio.Event()

        async def round_gen():
            await tools_mod.run_tool(ASK, {}, cfg_for(chat, mgr))
            started.set()
            yield 'data: {"type": "delta"}\n\n'
            await asyncio.sleep(3600)
            yield 'data: {"type": "done"}\n\n'

        rounds.start(chat, round_gen())
        await started.wait()
        [w] = mcpjobs.active_for_chat(chat)
        assert await rounds.abort(chat)
        before = mgr.polls
        assert await until(lambda: mgr.polls >= before + 3)
        alive = not w.task.done()
        await mcpjobs.stop_all()
        return alive

    assert asyncio.run(go())


def test_a_failed_poll_keeps_watching(chat):
    mgr = FakeManager(script=[None, RuntimeError("pipe hiccup"), None,
                              block(state="done", reply="All done.")])
    hb = Handbacks()

    async def go():
        await tools_mod.run_tool(ASK, {}, cfg_for(chat, mgr, hb))
        [w] = mcpjobs.active_for_chat(chat)
        assert await until(lambda: w.task.done())
        return w

    w = asyncio.run(go())
    assert mgr.polls == 4
    assert hb.kinds() == ["result"] and w.ended == "done"


def test_only_a_long_run_of_failures_ends_the_watch(chat, monkeypatch):
    monkeypatch.setattr(mcpjobs, "GIVE_UP_S", 0.05)
    mgr = FakeManager(script=[None] * 1000)
    hb = Handbacks()

    async def go():
        await tools_mod.run_tool(ASK, {}, cfg_for(chat, mgr, hb))
        [w] = mcpjobs.active_for_chat(chat)
        assert await until(lambda: w.task.done())
        return w

    w = asyncio.run(go())
    assert w.ended == "lost" and hb.calls == []
    assert mcpjobs.snapshots(chat)[-1]["ended"] == "lost"


def test_stopping_the_app_stops_every_watch(chat):
    mgr = FakeManager()

    async def go():
        await tools_mod.run_tool(ASK, {}, cfg_for(chat, mgr))
        [w] = mcpjobs.active_for_chat(chat)
        await mcpjobs.stop_all()
        return w

    w = asyncio.run(go())
    assert w.task.done() and w.ended == "stopped"
    assert mcpjobs.active_for_chat(chat) == []


# ---------- what the seats and the clients see ----------

def test_every_seat_reads_one_background_line(chat, cfg):
    mgr = FakeManager(first=block(stage="adding the drawer runners",
                                  steps=48, elapsed_s=312))

    async def go():
        await tools_mod.run_tool(ASK, {}, cfg_for(chat, mgr))
        note = mcpjobs.status_note(chat)
        await mcpjobs.stop_all()
        return note

    note = asyncio.run(go())
    assert "Dovetail is working in the background" in note
    assert "whatever happens here" in note and "never wait on it" in note
    assert "\"adding the drawer runners\" (Dovetail's own words)" in note
    assert "step 48" in note and "5 min in" in note
    assert f"call {PROGRESS} once, which answers at once" in note
    assert f"redirect the work, pass the change on with {ASK}" in note
    # it rides the volatile tail of the seat's prompt
    seat_cfg = dict(cfg, background_note=note)
    text = "".join(_volatile_system_parts(seat_cfg))
    assert "## Background work\n" + note in text
    assert mcpjobs.status_note(chat + 1) == ""


def test_the_line_says_how_far_the_work_has_got():
    """At step 24 a seat said nothing was on screen yet, with the cabinet
    already showing. The parts and edits counts say otherwise."""
    w = _watcher()
    w.apply(b(stage="fitting the doors", steps=24, parts=18, edits=42,
              elapsed_s=300), 300, from_seat=True, tool=ASK)
    note = w.note(300)
    assert "step 24, 18 parts so far, 42 edits, 5 min in" in note
    assert "already there to see in Dovetail" in note
    w.apply(b(steps=2, parts=0, edits=1), 310, from_seat=True, tool=ASK)
    note = w.note(310)
    assert "no parts yet, 1 edit" in note and "already there" not in note
    # an older server sends neither, and the line reads as before
    w.apply(b(stage="fitting the doors", steps=24, elapsed_s=300), 320,
            from_seat=True, tool=ASK)
    assert "Right now: \"fitting the doors\" (Dovetail's own words), " \
        "step 24, 5 min in." in w.note(320)
    snap = w.snapshot(320)
    assert snap["parts"] is None and snap["outcome"] is None


def test_a_running_watch_says_when_its_plan_was_answered():
    w = _watcher()
    w.apply(b(answered="A message was taken as the reply to its plan."), 30,
            from_seat=True, tool=ASK)
    note = w.note(31)
    assert "\"A message was taken as the reply to its plan.\" " \
        "(Dovetail's own words) So it isn't waiting on that any more" in note


def test_a_waiting_watch_puts_its_question_in_the_line():
    w = _watcher()
    w.apply(b(state="waiting", waiting_for="question", ask="Oak or ash?"),
            30, from_seat=True, tool=ASK)
    note = w.note(31)
    assert "waiting on the room for an answer" in note
    assert "\"Oak or ash?\"" in note


def test_the_round_hands_each_seat_the_note_and_the_reason(tmp_path,
                                                           monkeypatch):
    """Through the engine: a round's seats get the background line, and a
    hand-back round's seat also gets why it's on."""
    from fastapi.testclient import TestClient

    from backend.app import create_app
    from backend.config import Settings

    seen = []

    async def stream_reply(participant, roster, transcript, names, cfg,
                           project, chat_summary, voice_mode, tools=None,
                           memory=None):
        seen.append((cfg.get("background_note"), cfg.get("handback_note")))
        yield ("text", "ok")

    monkeypatch.setattr(engine.providers, "stream_reply", stream_reply)
    monkeypatch.setattr(mcpjobs, "POLL_S", 0.01)
    mcpjobs.reset()
    app = create_app(Settings(data_dir=str(tmp_path / "data"),
                              memory_url="http://127.0.0.1:1"))
    with TestClient(app, base_url="http://127.0.0.1") as c:
        chat_id = c.post("/api/chats", json={}).json()["id"]
    settings = app.state.settings
    con = db.connect()
    seat = db.get_chat_participants(con, chat_id)[:1]
    con.close()
    mgr = FakeManager()

    async def go():
        await tools_mod.run_tool(ASK, {}, cfg_for(chat_id, mgr))
        async for _ in engine.run_round(chat_id, seat, "gpt", settings,
                                        memory=None):
            pass
        async for _ in engine.run_round(chat_id, seat, "gpt", settings,
                                        memory=None, is_handback=True,
                                        handback_note="Say how it's going."):
            pass
        await mcpjobs.stop_all()

    asyncio.run(go())
    mcpjobs.reset()
    assert len(seen) == 2
    assert "working in the background" in seen[0][0] and seen[0][1] == ""
    assert "working in the background" in seen[1][0]
    assert seen[1][1] == "Say how it's going."


def test_status_rides_the_events_stream(chat):
    """An mcp_job event goes out for a change after the client connected,
    and the snapshot route's data is the same status."""
    mgr = FakeManager()

    async def go():
        events.bind_loop(asyncio.get_running_loop())
        stream = events.stream(since=10**9, heartbeat_secs=0.05)
        first = asyncio.ensure_future(stream.__anext__())
        await asyncio.sleep(0.01)
        await tools_mod.run_tool(ASK, {}, cfg_for(chat, mgr))
        got = []
        while not got:
            chunk = await asyncio.wait_for(first, 2)
            if chunk.startswith("data: "):
                got.append(json.loads(chunk[6:]))
            else:
                first = asyncio.ensure_future(stream.__anext__())
        await stream.aclose()
        await mcpjobs.stop_all()
        events.unbind_loop()
        return got[0]

    ev = asyncio.run(go())
    assert ev["type"] == "mcp_job" and ev["chat_id"] == chat
    assert ev["title"] == "Dovetail" and ev["stage"] == "cutting the legs"
    assert ev["watching"] is True and ev["state"] == "running"


# ---------- the real SDK, end to end ----------

def test_a_real_servers_structured_result_starts_a_watch(chat, monkeypatch):
    """Against the real fake stdio server: the SDK's own result field
    carries the block, the watcher starts, and its polls reach the
    server's progress tool."""
    async def go():
        mgr = McpManager({"fake": {"command": sys.executable,
                                   "args": [FAKE]}})
        task = asyncio.get_running_loop().create_task(mgr.run())
        try:
            for _ in range(100):
                if mgr.tool_definitions():
                    break
                await asyncio.sleep(0.1)
            out = await tools_mod.run_tool(
                "mcp__fake__build", {"text": "a bench"},
                cfg_for(chat, mgr))
            [w] = mcpjobs.active_for_chat(chat)
            assert await until(lambda: (w.steps or 0) >= 2, tries=500)
            note = mcpjobs.status_note(chat)
            await mcpjobs.stop_all()
            return out, w, note
        finally:
            await mgr.stop()
            await task

    out, w, note = asyncio.run(go())
    assert out.startswith("Started building: a bench")
    assert w.title == "Fake bench" and w.stage == "fitting the top"
    assert "mcp__fake__build_progress" in note
    assert "mcp__fake__build" in note
