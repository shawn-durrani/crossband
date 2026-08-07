"""Explicit shared delegation/claim state for specialist actions.

Coordinate delegation of the summon_claude_code specialist tool BEFORE
narrating it: the first participant that commits to it claims and invokes
immediately (already enforced by guest._pending / guestjobs.active_for_chat -
see test_guest.py / test_guestjobs.py); this file covers the NEW half -
`guest.claimed()`/`guest.delegation_note()` giving the round loop and the
system prompt ONE shared answer to "is this already spoken for?" so:

  - a claimed action is not even OFFERED to another participant as a tool
    this turn (mechanically exactly-once, not just "refused if attempted"),
  - every other participant's system prompt (voice mode included, since it's
    just another system-prompt section) carries an explicit note so nobody
    narrates, offers, or duplicates it,
  - a finished guest job's result is relayed by exactly ONE synthesizing
    narrator on hand-back, not the whole roster restating the same report.

No SDK, no network - providers.stream_reply and guest.run_guest are mocked.
guest.available() is mocked too in every test that exercises the round loop's
tool gate: it probes the host for a real Claude Code CLI, so leaving it live
makes these pass on a dev machine and behave differently on a keyless CI
runner with no CLI - failing outright where the tool must be OFFERED, and
passing vacuously where it must be ABSENT.
"""

import asyncio
import json

import pytest
from fastapi.testclient import TestClient

from backend import db, engine, guest, guestjobs
from backend.app import create_app
from backend.config import Settings
from backend.providers import group_chat_system

PARTICIPANT = {"name": "Claude", "slug": "claude", "system_prompt": ""}
ROSTER = [{"name": "Claude", "slug": "claude"}, {"name": "GPT", "slug": "gpt"}]


@pytest.fixture(autouse=True)
def clean_state():
    guest._pending.clear()
    guestjobs._jobs.clear()
    yield
    guest._pending.clear()
    guestjobs._jobs.clear()


@pytest.fixture
def app(tmp_path):
    settings = Settings(data_dir=str(tmp_path / "data"),
                        memory_url="http://127.0.0.1:1",
                        code_repos={"demo": str(tmp_path)})
    return create_app(settings)


def sse_events(body):
    return [json.loads(line[6:]) for line in body.splitlines()
            if line.startswith("data: ")]


# ---------- guest.claimed() / guest.delegation_note() ----------

def test_claimed_none_when_nothing_in_flight():
    assert guest.claimed(1) is None
    assert guest.delegation_note(None) == ""


def test_claimed_reports_a_summons_queued_this_round():
    guest.request(5, {"task": "look at the round loop", "repo": "demo"},
                  {"chat_id": 5, "code_repos": {"demo": "/tmp"}},
                  requested_by="claude")
    claim = guest.claimed(5)
    assert claim == {"state": "queued", "task": "look at the round loop",
                     "repo": "demo", "mode": "investigate",
                     "requested_by": "claude"}
    note = guest.delegation_note(claim)
    assert "ALREADY been summoned this round" in note
    assert "claude" in note and "look at the round loop" in note
    assert "not offered to you as a tool" in note


def test_claimed_reports_a_running_background_job(tmp_path, monkeypatch):
    db.configure(tmp_path / "data")
    db.init()
    con = db.connect()
    cid = con.execute(
        "INSERT INTO chats(created_at, updated_at, code_enabled) VALUES(?,?,1)",
        (db.now(), db.now())).lastrowid
    con.commit()
    con.close()

    gate = asyncio.Event()

    async def run_guest(task, repo_key, context, cfg, mode="investigate",
                        resume=None, chat_id=0, model="", effort="",
                        session_key=None, ref=""):
        await gate.wait()
        yield ("text", "done")
        yield ("usage", {"cost": 0.0})

    monkeypatch.setattr(guestjobs.guest, "run_guest", run_guest)

    async def go():
        job = guestjobs.start(cid, "investigate the latency issue", "demo",
                              "ctx", {}, handback=None)
        await asyncio.sleep(0.01)
        claim = guest.claimed(cid)
        assert claim == {"state": "running", "task": "investigate the latency issue",
                         "repo": "demo", "mode": "investigate"}
        note = guest.delegation_note(claim)
        assert "ALREADY working in the background" in note
        assert "not offered to you as a tool" in note
        gate.set()
        await job.task

    asyncio.run(go())


# ---------- exactly-once: not even offered once claimed ----------

def test_second_participant_this_round_never_sees_the_tool_or_the_job_offered(app, monkeypatch):
    """The scenario that motivated this: within ONE round, one
    participant decides to summon Claude Code (queues it) - the very next
    participant to speak must not be offered summon_claude_code at all, and
    must see an explicit claim note instead of guessing from the transcript."""
    seen = []

    async def stream_reply(participant, roster, transcript, names, cfg, project,
                           chat_summary, voice_mode, tools=None, memory=None):
        seen.append({
            "speaker": participant["slug"],
            "tools": [t["name"] for t in (tools or [])],
            "delegation_note": cfg.get("delegation_note", ""),
        })
        if participant["slug"] == "claude":
            # claude claims it - exactly what request() does when a model
            # actually invokes the tool mid-turn.
            guest.request(cfg["chat_id"],
                          {"task": "check the live latency endpoint"},
                          cfg, requested_by="claude")
            yield ("tool", {"tool": "summon_claude_code",
                            "input": {"task": "check the live latency endpoint"},
                            "output": guest.claimed(cfg["chat_id"]) and
                                      "queued for the end of this round"})
        yield ("text", "ok")

    async def run_guest(task, repo_key, context, cfg, mode="investigate",
                        resume=None, chat_id=0, model="", effort="",
                        session_key=None, ref=""):
        yield ("text", "done")
        yield ("usage", {"cost": 0.0})

    monkeypatch.setattr(engine.providers, "stream_reply", stream_reply)
    monkeypatch.setattr(engine.guest, "available", lambda cfg: True)
    # This is the one test in the file where guest.request() actually runs
    # end to end, so the round's real end-of-round code (engine.py's
    # guest.take()/_launch_guest_job) really does start a background guest
    # job (backend/guestjobs.py) - unlike every other test here, which only
    # exercises guest.request()'s queuing side or seeds an already-"running"
    # job directly. Without this mock, that background job calls the REAL
    # guest.run_guest - a live Claude Agent SDK/CLI invocation - on any
    # machine that happens to have the SDK and CLI installed (true of a dev
    # box, false of CI), turning this test into a non-deterministic, non-
    # keyless flake instead of the fast, hermetic check it's meant to be.
    # Matches the module docstring's promise ("guest.run_guest are mocked")
    # and the pattern already used a few tests up.
    monkeypatch.setattr(guestjobs.guest, "run_guest", run_guest)
    with TestClient(app, base_url="http://127.0.0.1") as c:
        chat = c.post("/api/chats", json={}).json()
        c.patch(f"/api/chats/{chat['id']}", json={"code_enabled": True})
        with c.stream("POST", f"/api/chats/{chat['id']}/send", json={"text": "hi"}) as r:
            "".join(r.iter_text())

    assert [s["speaker"] for s in seen] == ["claude", "gpt"]
    # claude (first) still had the tool available and no claim yet
    assert "summon_claude_code" in seen[0]["tools"]
    assert seen[0]["delegation_note"] == ""
    # gpt (second) does NOT see the tool, and sees exactly why
    assert "summon_claude_code" not in seen[1]["tools"]
    note = seen[1]["delegation_note"]
    assert "ALREADY been summoned this round" in note
    assert "check the live latency endpoint" in note
    assert "claude" in note


def test_new_round_while_job_running_neither_offers_nor_narrates(app, monkeypatch, tmp_path):
    """A guest job started in an EARLIER round is still running when a fresh
    round begins (e.g. the next voice turn) - every participant in the new
    round must see the claim too, not just participants in the summoning
    round. This is the cross-round gap (redundant
    coordination chatter over several separate turns). The job is seeded
    directly as a live 'running' registry entry - no real background task -
    so this doesn't depend on which event loop TestClient happens to drive."""
    seen = []

    async def stream_reply(participant, roster, transcript, names, cfg, project,
                           chat_summary, voice_mode, tools=None, memory=None):
        seen.append({
            "speaker": participant["slug"],
            "tools": [t["name"] for t in (tools or [])],
            "delegation_note": cfg.get("delegation_note", ""),
        })
        yield ("text", "ok")

    monkeypatch.setattr(engine.providers, "stream_reply", stream_reply)
    monkeypatch.setattr(engine.guest, "available", lambda cfg: True)

    with TestClient(app, base_url="http://127.0.0.1") as c:
        chat = c.post("/api/chats", json={}).json()
        cid = chat["id"]
        c.patch(f"/api/chats/{cid}", json={"code_enabled": True})

        # A job left running from an earlier round (as if summoned last turn).
        con = db.connect()
        row = db.insert_guest_job(con, cid, "dig into the latency numbers",
                                  "demo", "investigate", "claude")
        con.close()
        job = guestjobs.GuestJob(row)
        guestjobs._jobs[job.id] = job
        assert guestjobs.active_for_chat(cid) is not None

        with c.stream("POST", f"/api/chats/{cid}/send", json={"text": "any update?"}) as r:
            "".join(r.iter_text())

    assert [s["speaker"] for s in seen] == ["claude", "gpt"]
    for s in seen:
        assert "summon_claude_code" not in s["tools"]
        assert "ALREADY working in the background" in s["delegation_note"]
        assert "dig into the latency numbers" in s["delegation_note"]


# ---------- voice mode specifically ----------

def test_delegation_note_reaches_voice_mode_system_prompt(cfg):
    """The note is just another system-prompt section, so it must survive
    into voice mode alongside the brevity/VOICE-MODE override - voice is
    where dead air and redundant turns are the most expensive."""
    note = guest.delegation_note({"state": "running", "task": "check the trace endpoint",
                                  "repo": "sideband", "mode": "investigate"})
    voice_cfg = dict(cfg)
    voice_cfg["delegation_note"] = note
    text = group_chat_system(PARTICIPANT, ROSTER, voice_cfg, None, "", True)
    assert "ALREADY working in the background" in text
    assert "check the trace endpoint" in text
    # the brevity override still applies LAST, on top of the delegation note
    assert text.index("ALREADY working in the background") < text.index("VOICE MODE")


def test_no_delegation_note_section_when_nothing_claimed(cfg):
    text = group_chat_system(PARTICIPANT, ROSTER, dict(cfg), None, "", False)
    assert "already claimed" not in text.lower()


# ---------- hand-back: one synthesized reply, not a chorus ----------

def test_handback_relays_through_exactly_one_narrator(app, monkeypatch):
    monkeypatch.setattr(guestjobs, "RESULT_SETTLE_S", 0.0)

    async def stream_reply(participant, roster, transcript, names, cfg, project,
                           chat_summary, voice_mode, tools=None, memory=None):
        yield ("text", f"{participant['slug']} acknowledges")

    monkeypatch.setattr(engine.providers, "stream_reply", stream_reply)
    monkeypatch.setattr(engine.guest, "run_guest", lambda *a, **k: _fake_result())

    with TestClient(app, base_url="http://127.0.0.1") as c:
        chat = c.post("/api/chats", json={}).json()
        cid = chat["id"]
        c.patch(f"/api/chats/{cid}", json={"code_enabled": True})
        guest.request(cid, {"task": "look into the fix", "mode": "investigate"},
                      {"code_repos": {"demo": "/tmp"}, "chat_id": cid})
        with c.stream("POST", f"/api/chats/{cid}/send", json={"text": "go"}) as r:
            "".join(r.iter_text())

        job = _poll(lambda: next(
            (j for j in c.get(f"/api/chats/{cid}/guest_jobs").json()["guest_jobs"]
             if j["status"] == "completed"), None))
        assert job

        messages = _poll(lambda: (
            (m := c.get(f"/api/chats/{cid}").json()["messages"])
            and m[-1]["speaker"] != "claude-code" and m) or None)
        assert messages
        narrators = [m["speaker"] for m in messages
                    if m["speaker"] not in ("user", "claude-code")
                    and m["created_at"] >= next(
                        mm["created_at"] for mm in messages if mm["speaker"] == "claude-code")]
        # exactly one seat relays the finished job - not the whole roster
        assert len(narrators) == 1


async def _fake_result():
    yield ("text", "Looked into it — all clear.")
    yield ("usage", {"input": 1, "output": 1, "cache_read": 0,
                     "cache_creation": 0, "cost": 0.01, "session_id": "s"})


def _poll(fn, tries=200, delay=0.03):
    import time
    for _ in range(tries):
        val = fn()
        if val:
            return val
        time.sleep(delay)
    return None
