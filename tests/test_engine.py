"""Round-loop characterization: SSE event shape, per-speaker persistence with
usage/cost, and the persist-on-disconnect ("[cut off by User]") semantics that
replaced the old threadpool relay. Providers are mocked — no network."""

import asyncio
import json

import pytest
from fastapi.testclient import TestClient

from backend import db, engine
from backend.app import create_app
from backend.config import Settings


@pytest.fixture
def app(tmp_path):
    settings = Settings(data_dir=str(tmp_path / "data"),
                        memory_url="http://127.0.0.1:1")
    return create_app(settings)


def fake_stream(text_chunks):
    async def stream_reply(participant, roster, transcript, names, cfg, project,
                           chat_summary, voice_mode, tools=None, memory=None):
        for ch in text_chunks:
            yield ("text", ch)
        yield ("usage", {"input": 10, "cache_read": 2, "cache_creation": 1, "output": 5})
    return stream_reply


def sse_events(body):
    return [json.loads(line[6:]) for line in body.splitlines()
            if line.startswith("data: ")]


def test_send_streams_full_round_and_persists(app, monkeypatch):
    monkeypatch.setattr(engine.providers, "stream_reply",
                        fake_stream(["Hello ", "world"]))
    with TestClient(app, base_url="http://127.0.0.1") as c:
        chat = c.post("/api/chats", json={}).json()
        with c.stream("POST", f"/api/chats/{chat['id']}/send",
                      json={"text": "hi both"}) as r:
            assert r.status_code == 200
            body = "".join(r.iter_text())
        events = sse_events(body)
        types = [e["type"] for e in events]
        assert types[0] == "round_start"  # carries the re-attach round id
        assert types[1] == "user_saved"
        assert types.count("speaker_start") == 2  # full round: both reply
        assert types.count("speaker_end") == 2
        assert types[-1] == "done"

        got = c.get(f"/api/chats/{chat['id']}").json()
        speakers = [m["speaker"] for m in got["messages"]]
        assert speakers == ["user", "claude", "gpt"]
        reply = got["messages"][1]
        assert reply["content"] == "Hello world"
        usage = json.loads(reply["usage_json"])
        assert usage["output"] == 5 and "cost" in usage and "model" in usage

        # next_first rotated so the other model opens the next round
        assert got["chat"]["next_first"] == "gpt"


def test_mention_selects_single_responder(app, monkeypatch):
    monkeypatch.setattr(engine.providers, "stream_reply", fake_stream(["yo"]))
    with TestClient(app, base_url="http://127.0.0.1") as c:
        chat = c.post("/api/chats", json={}).json()
        with c.stream("POST", f"/api/chats/{chat['id']}/send",
                      json={"text": "@gpt only you"}) as r:
            body = "".join(r.iter_text())
        events = sse_events(body)
        starts = [e["speaker"] for e in events if e["type"] == "speaker_start"]
        assert starts == ["gpt"]


def test_provider_error_is_reported_and_round_continues(app, monkeypatch):
    async def erratic(participant, roster, transcript, names, cfg, project,
                      chat_summary, voice_mode, tools=None, memory=None):
        if participant["slug"] == "claude":
            raise RuntimeError("ANTHROPIC_API_KEY is not set")
        yield ("text", "gpt still answers")
        yield ("usage", {"input": 1, "cache_read": 0, "cache_creation": 0, "output": 1})

    monkeypatch.setattr(engine.providers, "stream_reply", erratic)
    with TestClient(app, base_url="http://127.0.0.1") as c:
        chat = c.post("/api/chats", json={}).json()
        with c.stream("POST", f"/api/chats/{chat['id']}/send",
                      json={"text": "go"}) as r:
            body = "".join(r.iter_text())
        events = sse_events(body)
        errors = [e for e in events if e["type"] == "error"]
        assert errors and "ANTHROPIC_API_KEY" in errors[0]["message"]
        got = c.get(f"/api/chats/{chat['id']}").json()
        assert [m["speaker"] for m in got["messages"]] == ["user", "gpt"]


def test_disconnect_persists_partial_with_cutoff_marker(app, monkeypatch):
    """Closing the stream mid-reply (voice barge-in / page close) must persist
    the partial content with the cut-off marker — the native-async port of the
    predecessor's stream_with_disconnect guarantee."""
    async def hanging(participant, roster, transcript, names, cfg, project,
                      chat_summary, voice_mode, tools=None, memory=None):
        yield ("text", "partial answer")
        await asyncio.sleep(3600)  # never finishes on its own

    monkeypatch.setattr(engine.providers, "stream_reply", hanging)
    with TestClient(app, base_url="http://127.0.0.1") as c:
        chat = c.post("/api/chats", json={}).json()
        chat_id = chat["id"]
    settings = app.state.settings

    con = db.connect()
    roster = db.get_chat_participants(con, chat_id)
    con.close()

    async def go():
        gen = engine.run_round(chat_id, roster[:1], "gpt", settings, memory=None)
        async for chunk in gen:
            if '"delta"' in chunk:
                break  # client walks away mid-delta
        await gen.aclose()  # GeneratorExit inside run_round

    asyncio.run(go())

    con = db.connect()
    msgs = db.get_chat_messages(con, chat_id)
    con.close()
    assert len(msgs) == 1
    assert msgs[0]["speaker"] == "claude"
    assert msgs[0]["content"] == "partial answer\n\n[cut off by User]"


def test_pick_responders_rotation():
    roster = [{"slug": "claude"}, {"slug": "gpt"}]
    chat = {"next_first": "gpt"}
    responders, next_first = engine.pick_responders("hello all", chat, roster)
    assert [p["slug"] for p in responders] == ["gpt", "claude"]
    assert next_first == "claude"


def test_pick_responders_mention_does_not_rotate():
    roster = [{"slug": "claude"}, {"slug": "gpt"}]
    chat = {"next_first": "claude"}
    responders, next_first = engine.pick_responders("@claude what say you", chat, roster)
    assert [p["slug"] for p in responders] == ["claude"]
    assert next_first == "claude"


def test_pick_responders_spoken_vocative():
    """Voice can't type @ — a leading 'Claude and GPT, …' addresses them."""
    roster = [{"slug": "claude", "name": "Claude"}, {"slug": "gpt", "name": "GPT"},
              {"slug": "gpt-oss", "name": "GPT-OSS"}]
    chat = {"next_first": "claude"}
    responders, _ = engine.pick_responders(
        "Claude and GPT, um, can you recall where we're up to?", chat, roster)
    assert [p["slug"] for p in responders] == ["claude", "gpt"]
    responders, _ = engine.pick_responders(
        "GPT-OSS, you can sit this one out.", chat, roster)
    assert [p["slug"] for p in responders] == ["gpt-oss"]


def test_pick_responders_vocative_conservative():
    """Ordinary sentences never match: any non-name leading token → full round."""
    roster = [{"slug": "claude", "name": "Claude"}, {"slug": "gpt", "name": "GPT"}]
    chat = {"next_first": "claude"}
    for text in ("Hey guys, are you there?", "Okay, let's try that.",
                 "Claude said something wrong earlier, let's revisit",
                 "Yeah. Okay, let's try that."):
        responders, _ = engine.pick_responders(text, chat, roster)
        assert len(responders) == 2, text


def test_pick_responders_vocative_spoken_compound_name():
    """Transcription splits compound names ('GPT-OSS' → 'GPT OSS') — still matches."""
    roster = [{"slug": "claude", "name": "Claude"}, {"slug": "gpt", "name": "GPT"},
              {"slug": "gpt-oss", "name": "GPT-OSS"}]
    chat = {"next_first": "claude"}
    responders, _ = engine.pick_responders("GPT OSS, sit this one out", chat, roster)
    assert [p["slug"] for p in responders] == ["gpt-oss"]
    # renamed participant: matching follows the display name too
    roster[2] = {"slug": "gpt-oss", "name": "Ozzy"}
    responders, _ = engine.pick_responders("Ozzy, what do you think?", chat, roster)
    assert [p["slug"] for p in responders] == ["gpt-oss"]


def test_trial_seat_excluded_from_unaddressed_round():
    """The real lifecycle gate: a `trial` seat is manual-invoke-only and must NOT
    auto-speak in a normal, unaddressed round — while onboarded seats behave
    exactly as before."""
    roster = [{"slug": "claude", "name": "Claude", "lifecycle": "onboarded"},
              {"slug": "gpt", "name": "GPT", "lifecycle": "onboarded"},
              {"slug": "trialbot", "name": "TrialBot", "lifecycle": "trial"}]
    chat = {"next_first": "claude"}
    responders, next_first = engine.pick_responders("hello all", chat, roster)
    slugs = [p["slug"] for p in responders]
    assert "trialbot" not in slugs           # gated out of the auto round
    assert slugs == ["claude", "gpt"]         # onboarded seats unchanged
    assert next_first == "gpt"                # rotation runs over auto seats only


def test_trial_seat_speaks_when_mentioned():
    """The manual path: an explicit @mention invokes a trial seat for that turn."""
    roster = [{"slug": "claude", "name": "Claude", "lifecycle": "onboarded"},
              {"slug": "trialbot", "name": "TrialBot", "lifecycle": "trial"}]
    chat = {"next_first": "claude"}
    responders, _ = engine.pick_responders("@trialbot what do you think?", chat, roster)
    assert [p["slug"] for p in responders] == ["trialbot"]


def test_trial_seat_speaks_when_addressed_by_name():
    """Voice can't type @ — addressing a trial seat by name also invokes it."""
    roster = [{"slug": "claude", "name": "Claude", "lifecycle": "onboarded"},
              {"slug": "trialbot", "name": "TrialBot", "lifecycle": "trial"}]
    chat = {"next_first": "claude"}
    responders, _ = engine.pick_responders("TrialBot, can you weigh in?", chat, roster)
    assert [p["slug"] for p in responders] == ["trialbot"]


def test_all_trial_roster_selects_nobody_when_unaddressed():
    """A chat of only trial seats has no auto-responders until one is addressed
    or onboarded — the gate is real, not cosmetic."""
    roster = [{"slug": "a", "name": "A", "lifecycle": "trial"},
              {"slug": "b", "name": "B", "lifecycle": "trial"}]
    chat = {"next_first": "a"}
    responders, next_first = engine.pick_responders("hello all", chat, roster)
    assert responders == []
    assert next_first == "a"
    # …but addressing one still works.
    responders, _ = engine.pick_responders("@a hi", chat, roster)
    assert [p["slug"] for p in responders] == ["a"]


def test_missing_lifecycle_participates_no_regression():
    """A roster row with no lifecycle key at all (older callers) participates —
    only an EXPLICIT trial is gated, so nothing pre-existing regresses."""
    roster = [{"slug": "claude"}, {"slug": "gpt"}]
    chat = {"next_first": "claude"}
    responders, _ = engine.pick_responders("hello all", chat, roster)
    assert [p["slug"] for p in responders] == ["claude", "gpt"]


def test_sweep_candidates_watermarks(app):
    """The periodic sweep picks settled chats with unreflected content and
    skips fresh, reflected, or too-small ones."""
    con = engine.db.connect()
    now = engine.db.now()

    def mk(title_upto, ingested_upto, msgs, age_s=600, memory_enabled=1):
        cur = con.execute(
            "INSERT INTO chats(title, title_upto, ingested_upto, memory_enabled, "
            "created_at, updated_at) VALUES('t', ?, ?, ?, ?, ?)",
            (title_upto, ingested_upto, memory_enabled, now - age_s, now - age_s))
        cid = cur.lastrowid
        for i in range(msgs):
            con.execute("INSERT INTO messages(chat_id, speaker, content, created_at) "
                        "VALUES(?, 'user', ?, ?)", (cid, f"m{i}", now - age_s + i))
        con.commit()
        return cid

    untitled = mk(0, 10**9, 3)               # placeholder title, enough messages
    fresh = mk(0, 10**9, 3, age_s=10)        # same but still active — skipped
    locked_ingested = mk(-1, 10**9, 3)       # user-titled, nothing to ingest
    uningested = mk(-1, 0, 3)                # user-titled but memory behind
    memoff_behind = mk(-1, 0, 3, memory_enabled=0)  # memory off — ingest doesn't count
    tiny = mk(0, 10**9, 1)                   # one message — too small to title

    ids = set(engine.sweep_candidates(con, idle_s=120))
    con.close()
    assert untitled in ids
    assert uningested in ids
    assert fresh not in ids
    assert locked_ingested not in ids
    assert memoff_behind not in ids
    assert tiny not in ids


def test_pick_responders_vocative_after_greeting():
    """'Hey, GPT, …' — the comma after the greeting must not hide the name
    (live failure: Ozzy kept answering questions addressed to GPT)."""
    roster = [{"slug": "claude", "name": "Claude"}, {"slug": "gpt", "name": "GPT"},
              {"slug": "gpt-oss", "name": "Ozzy"}]
    chat = {"next_first": "claude"}
    for text in ("Hey, GPT, um, do you have any recommendations?",
                 "Okay, Claude, take it away",
                 "hey GPT, quick one"):
        responders, _ = engine.pick_responders(text, chat, roster)
        assert len(responders) == 1, text
    # greetings alone still never address anyone
    responders, _ = engine.pick_responders("Hey, guys, are you there?", chat, roster)
    assert len(responders) == 3


class FakeMemory:
    """Reachable memory service double for ambient-recall round tests."""

    def __init__(self, facts):
        self.facts = facts
        self.recall_calls = []

    async def probe(self, force=False):
        return True

    async def get_summary(self):
        return "Alex builds things."

    async def recall(self, query, limit=10, include_superseded=False, origin="http"):
        self.recall_calls.append({"query": query, "limit": limit, "origin": origin})
        return self.facts

    def any_write_failed(self):
        return False


def _capture_cfg(captured):
    async def stream_reply(participant, roster, transcript, names, cfg, project,
                           chat_summary, voice_mode, tools=None, memory=None):
        captured.append(cfg)
        yield ("text", "ok")
    return stream_reply


def test_ambient_recall_prepares_round_context(app, monkeypatch):
    """Memory prepares for the conversation unprompted: one origin=auto recall
    per round, keyed on the latest user message, injected for EVERY speaker —
    models shouldn't have to remember to look things up."""
    captured = []
    monkeypatch.setattr(engine.providers, "stream_reply", _capture_cfg(captured))
    with TestClient(app, base_url="http://127.0.0.1") as c:
        chat_id = c.post("/api/chats", json={}).json()["id"]
    settings = app.state.settings

    con = db.connect()
    con.execute("INSERT INTO messages(chat_id, speaker, content, created_at) "
                "VALUES(?, 'user', 'any update from Oscar?', ?)", (chat_id, db.now()))
    con.commit()
    roster = db.get_chat_participants(con, chat_id)
    con.close()

    mem = FakeMemory([{"id": 1, "content": "Alex sent the follow-up to Oscar on July 8.",
                       "event_date": 1783500000.0, "origin_agent": "chat", "score": 0.9}])

    async def go():
        async for _ in engine.run_round(chat_id, roster, "gpt", settings, memory=mem):
            pass

    asyncio.run(go())

    # one ambient recall for the whole round, tagged auto, keyed on the message
    assert len(mem.recall_calls) == 1
    assert mem.recall_calls[0]["origin"] == "auto"
    assert mem.recall_calls[0]["query"] == "any update from Oscar?"
    # both speakers received the same prepared context + the summary
    assert len(captured) == 2
    for cfg in captured:
        assert "follow-up to Oscar" in cfg["memory_ambient"]
        assert cfg["memory_summary"] == "Alex builds things."


def test_ambient_recall_skipped_without_user_message(app, monkeypatch):
    captured = []
    monkeypatch.setattr(engine.providers, "stream_reply", _capture_cfg(captured))
    with TestClient(app, base_url="http://127.0.0.1") as c:
        chat_id = c.post("/api/chats", json={}).json()["id"]
    settings = app.state.settings
    con = db.connect()
    roster = db.get_chat_participants(con, chat_id)
    con.close()
    mem = FakeMemory([{"id": 1, "content": "x", "event_date": 1.0}])

    async def go():
        async for _ in engine.run_round(chat_id, roster[:1], "gpt", settings, memory=mem):
            pass

    asyncio.run(go())
    assert mem.recall_calls == []  # nothing to key on — no ambient noise
    assert captured[0]["memory_ambient"] == ""


def test_handoff_ships_attachment_bytes(app, monkeypatch):
    """Nothing that traveled with a chat is lost to memory: the leave-hook
    handoff encodes each message's attachment files (bytes from disk) into
    the ingest payload."""
    with TestClient(app, base_url="http://127.0.0.1") as c:
        chat_id = c.post("/api/chats", json={}).json()["id"]

    con = db.connect()
    cur = con.execute(
        "INSERT INTO messages(chat_id, speaker, content, created_at) "
        "VALUES(?, 'user', 'see attached', ?)", (chat_id, db.now()))
    msg_id = cur.lastrowid
    import os
    os.makedirs(db.ATTACH_DIR, exist_ok=True)
    (db.ATTACH_DIR / "stored-1.txt").write_bytes(b"the pasted document body")
    con.execute(
        "INSERT INTO attachments(message_id, filename, stored_name, mime, size, "
        "created_at) VALUES(?, 'pasted.txt', 'stored-1.txt', 'text/plain', ?, ?)",
        (msg_id, 24, db.now()))
    con.commit()
    con.close()

    class FakeHandoffMemory:
        msgs = None

        async def handoff_chat(self, cid, get_new, advance):
            FakeHandoffMemory.msgs = await asyncio.to_thread(get_new)

    mem = FakeHandoffMemory()
    asyncio.run(engine.leave_chat_job(chat_id, app.state.settings.as_cfg(), mem))

    import base64
    msgs = FakeHandoffMemory.msgs
    assert msgs and msgs[0]["attachments"][0]["filename"] == "pasted.txt"
    raw = base64.b64decode(msgs[0]["attachments"][0]["data_b64"])
    assert raw == b"the pasted document body"


# ---------- server-side voice-latency stage split ----------

def test_turn_id_records_server_context_and_ttft_stages(app, monkeypatch):
    """A /send carrying turn_id (the live-voice path) gets the round's first
    responder's context-assembly and provider-TTFT split recorded, correlated
    by that same turn_id — the server-side half of final_to_first_token."""
    monkeypatch.setattr(engine.providers, "stream_reply", fake_stream(["hi"]))
    with TestClient(app, base_url="http://127.0.0.1") as c:
        chat = c.post("/api/chats", json={}).json()
        with c.stream("POST", f"/api/chats/{chat['id']}/send",
                      json={"text": "hi both", "turn_id": "turn-abc"}) as r:
            "".join(r.iter_text())

    con = db.connect()
    rows = db.get_voice_traces(con)
    con.close()
    stages = {r["stage"] for r in rows if r["turn_id"] == "turn-abc"}
    assert "server_context_assembly" in stages
    assert "server_provider_first_token" in stages
    # only the FIRST responder's split is recorded — a full round replies
    # with both seats, but only one context-assembly/TTFT pair is written
    assert sum(1 for r in rows if r["stage"] == "server_context_assembly") == 1


def test_no_turn_id_records_no_server_stages(app, monkeypatch):
    """A normal text send (no turn_id — not a voice turn) writes nothing to
    the trace table: there's no client-side stopwatch to correlate against."""
    monkeypatch.setattr(engine.providers, "stream_reply", fake_stream(["hi"]))
    with TestClient(app, base_url="http://127.0.0.1") as c:
        chat = c.post("/api/chats", json={}).json()
        with c.stream("POST", f"/api/chats/{chat['id']}/send",
                      json={"text": "hi both"}) as r:
            "".join(r.iter_text())

    con = db.connect()
    rows = db.get_voice_traces(con)
    con.close()
    assert rows == []


def test_trace_recording_failure_never_breaks_the_round(app, monkeypatch, caplog):
    """Best-effort by design: if writing the trace stage blows up, the round
    still completes and the reply still persists."""
    import logging
    from backend import voice_trace as voice_trace_mod
    monkeypatch.setattr(engine.providers, "stream_reply", fake_stream(["hi"]))

    def boom(*a, **k):
        raise RuntimeError("db is on fire")
    monkeypatch.setattr(voice_trace_mod, "record_server_stage", boom)

    caplog.set_level(logging.WARNING, logger="mmc.engine")
    with TestClient(app, base_url="http://127.0.0.1") as c:
        chat = c.post("/api/chats", json={}).json()
        with c.stream("POST", f"/api/chats/{chat['id']}/send",
                      json={"text": "hi both", "turn_id": "turn-boom"}) as r:
            body = "".join(r.iter_text())
        events = sse_events(body)
        assert events[-1]["type"] == "done"  # round completed despite the boom
        got = c.get(f"/api/chats/{chat['id']}").json()
        assert [m["speaker"] for m in got["messages"]] == ["user", "claude", "gpt"]


def test_timed_ms_reports_each_tasks_own_duration():
    """Two concurrent reads awaited in sequence must each report their
    OWN runtime — the old outside-the-await clocking charged the second task
    only for the tail that outlived the first, reading ~0 when it was in
    fact the slower call's equal-length twin."""
    async def go():
        async def sleeper(s):
            await asyncio.sleep(s)
            return s
        # EQUAL sleeps make the failure mode unmistakable: they run
        # concurrently, so tail-clocking would charge the second one ~0ms
        # (it finished in the same instant the first await returned), while
        # self-clocking reports ~the full sleep for both.
        a = asyncio.create_task(engine._timed_ms(sleeper(0.05)))
        b = asyncio.create_task(engine._timed_ms(sleeper(0.05)))
        ra, a_ms = await a
        rb, b_ms = await b
        assert (ra, rb) == (0.05, 0.05)  # results pass through untouched
        assert a_ms >= 40
        assert b_ms >= 40  # its own duration, not the post-first-await tail
    asyncio.run(go())


def test_round_reads_transcript_once_then_deltas_nothing_missed(app, monkeypatch):
    """ONE full get_chat_messages per round; each later speaker fetches
    only the delta — and still sees both the previous speaker's reply and a
    mid-round external insert, exactly as the old full re-read did."""
    counts = {"full": 0, "delta": 0}
    real_full, real_after = db.get_chat_messages, db.get_messages_after

    def counting_full(con, chat_id):
        counts["full"] += 1
        return real_full(con, chat_id)

    def counting_after(con, since, chat_id=None):
        counts["delta"] += 1
        return real_after(con, since, chat_id=chat_id)

    monkeypatch.setattr(engine.db, "get_chat_messages", counting_full)
    monkeypatch.setattr(engine.db, "get_messages_after", counting_after)

    async def no_reflect(chat_id, cfg):
        return None  # the post-round title/summary job does its own full
                     # read — silence it so the count below is the ROUND's

    monkeypatch.setattr(engine, "post_round_reflect_job", no_reflect)

    transcripts = {}

    async def capturing_stream(participant, roster, transcript, names, cfg,
                               project, chat_summary, voice_mode,
                               tools=None, memory=None):
        transcripts[participant["slug"]] = [
            (m["speaker"], m["content"]) for m in transcript]
        if not transcripts.get("_ext_planted"):
            # mid-round external insert, while speaker 1 is "generating"
            transcripts["_ext_planted"] = True
            con = db.connect()
            db.insert_message(con, cfg["chat_id"], "ext:radar", "external ping")
            con.close()
        yield ("text", f"reply from {participant['slug']}")
        yield ("usage", {"input": 1, "cache_read": 0, "cache_creation": 0, "output": 1})

    monkeypatch.setattr(engine.providers, "stream_reply", capturing_stream)
    with TestClient(app, base_url="http://127.0.0.1") as c:
        chat = c.post("/api/chats", json={}).json()
        with c.stream("POST", f"/api/chats/{chat['id']}/send",
                      json={"text": "hi both"}) as r:
            "".join(r.iter_text())

    assert counts["full"] == 1                      # one full read per round
    assert counts["delta"] == 1                     # speaker 2 appended the delta
    second = transcripts["gpt"]
    assert ("claude", "reply from claude") in second  # sees speaker 1's reply
    assert ("ext:radar", "external ping") in second   # sees the mid-round insert
    first = transcripts["claude"]
    assert ("claude", "reply from claude") not in first  # ordering sanity


def test_guest_availability_checked_once_per_round(app, monkeypatch):
    """guest.available (a filesystem PATH scan) runs once per round,
    not once per speaker."""
    calls = {"n": 0}
    real = engine.guest.available

    def counting(cfg):
        calls["n"] += 1
        return real(cfg)

    monkeypatch.setattr(engine.guest, "available", counting)
    monkeypatch.setattr(engine.providers, "stream_reply", fake_stream(["hi"]))
    with TestClient(app, base_url="http://127.0.0.1") as c:
        chat = c.post("/api/chats", json={}).json()
        with c.stream("POST", f"/api/chats/{chat['id']}/send",
                      json={"text": "hi both"}) as r:
            "".join(r.iter_text())
    assert calls["n"] == 1
