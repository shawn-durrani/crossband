"""The round's own search of the saved chats (membro#136).

In membro#136's benchmark the seats said memory held nothing without
calling search_history in 11 of 15 misses, and a change of wording didn't
help. The app now runs that first search itself. These tests pin:

- the rule that decides a turn asks about the person or their past, with
  examples both ways, and the search words it takes;
- the parallel start, beside the recall, at the voice commit or the
  round's start, and the prewarm's adoption terms;
- the handover when recall comes back thin, and the search dropped when
  it doesn't;
- the short spoken line when the search is late in a voice chat, and the
  status line in a typed one;
- the tool's rules: the same client call and owner token, only where the
  tool works, and a guest in the room changes nothing;
- a normal turn: no search, no wait, no new event.

Every membro call is faked. Nothing reaches a live service."""

import asyncio
import json
import time

import httpx
import pytest
from fastapi.testclient import TestClient

from backend import db, engine, history_prefetch, providers, tools, work_status
from backend.app import create_app
from backend.config import Settings
from backend.memory_client import MemoryClient, MemorySearchError
from roomkit import sse_events

ASKS = [
    "Can you suggest some useful accessories for my phone?",
    "I've been thinking about making a cocktail for a get-together, any ideas?",
    "What is the total number of siblings I have?",
    "Can you recommend a show or movie for me to watch tonight?",
    "What percentage of packed shoes did I wear on my last trip?",
    "How many different doctors did I visit?",
    "Do you remember what we said about the renovation?",
    "Remind me what the plumber quoted.",
    "What did Sam say about the camping trip last week?",
    "How old was I at Dave's wedding?",
    "Any cultural events happening around me this weekend?",
    "Hey Claude, what's the name of the cafe we went to?",
    "Did I tell you about the new kitchen benchtops?",
    "what did we decide about the bench top oil",
]

NOT_ASKS = [
    "What's the capital of France?",
    "How do I boil an egg?",
    "Tell me a joke.",
    "Can you write a haiku about autumn?",
    "Thanks, that's great.",
    "What do you think?",
    "Okay, let's move on to the next topic.",
    "Is it going to rain today?",
    "What did you just say?",
    "What did we talk about yesterday?",
    "Hey Claude, how are you?",
    "Explain how transformers work.",
    "I'm heading out to the shops now.",
    "/remember my phone",
    "Sand the bench top first, then oil it.",
    "Solo mode.",
    "Who won the cricket?",
    "",
]


# ---------- the rule ----------

@pytest.mark.parametrize("text", ASKS)
def test_a_question_about_you_or_your_past_wants_history(text):
    assert history_prefetch.wants_history(text)


@pytest.mark.parametrize("text", NOT_ASKS)
def test_other_turns_never_search(text):
    assert not history_prefetch.wants_history(text)


def test_search_words_drop_the_filler_and_the_seat_names():
    kw = history_prefetch.keywords
    assert kw("What is the total number of siblings I have?") == ["siblings"]
    assert kw("Can you suggest some useful accessories for my phone?") == [
        "accessories", "phone"]
    seats = history_prefetch.name_words(["claude", "Claude", "gpt-oss", "GPT OSS"])
    assert kw("Hey Claude, what's the name of the cafe we went to?",
              seats) == ["cafe"]
    # curly apostrophes read as straight ones
    assert history_prefetch.wants_history("What’s on my shopping list?")
    many = "my alpha bravo charlie delta echo foxtrot golf hotel?"
    assert len(kw(many)) == history_prefetch.MAX_QUERY_WORDS


def test_recall_is_thin_when_few_facts_share_a_word_with_the_question():
    words = history_prefetch.keywords("How many sisters do I have?")
    on_topic = [{"content": "Alex has a sister called Sam."},
                {"content": "Alex's sisters live interstate."}]
    off_topic = [{"content": "Alex likes flat whites."},
                 {"content": "Alex works on the bench most weekends."}]
    assert history_prefetch.recall_is_thin([], words)
    assert history_prefetch.recall_is_thin(off_topic, words)
    assert history_prefetch.recall_is_thin(off_topic + on_topic[:1], words)
    assert not history_prefetch.recall_is_thin(off_topic + on_topic, words)
    # "phone" meets "iPhone", "watched" meets "watch"
    assert not history_prefetch.recall_is_thin(
        [{"content": "Alex has an iPhone."}, {"content": "Alex's phone case is blue."}],
        ["phone"])


# ---------- fakes ----------

HIT = {"conversation_id": "c9", "speaker": "user", "created_at": 1783500000.0,
       "content": "I got the >>scarf<< for my sister, about forty dollars.",
       "web_sources": []}
WEB_HIT = {"conversation_id": "c8", "speaker": "claude", "created_at": 1783400000.0,
           "content": "The shop page listed the >>scarf<< at forty dollars.",
           "web_sources": ["shop.example"]}


class Memory:
    """A reachable membro. `search_gate`, when set, holds every search until
    the test releases it, and `search_error` makes the search raise."""

    def __init__(self, facts=(), hits=(HIT,), search_error=None):
        self.facts = list(facts)
        self.hits = list(hits)
        self.search_error = search_error
        self.search_gate = None
        self.search_calls = []
        self.search_cancelled = 0
        self.recall_calls = []
        # "recall", "recall done" and "search", in the order they happened
        self.events = []

    async def probe(self, force=False):
        return True

    def any_write_failed(self):
        return False

    async def get_summary(self):
        return "Alex builds things."

    async def recall(self, query, limit=10, include_superseded=False,
                     origin="http", chat_id=None):
        self.events.append("recall")
        self.recall_calls.append(query)
        await asyncio.sleep(0.02)
        self.events.append("recall done")
        return self.facts

    async def search(self, query, limit=20):
        self.events.append("search")
        self.search_calls.append({"query": query, "limit": limit})
        try:
            if self.search_gate is not None:
                await self.search_gate.wait()
        except asyncio.CancelledError:
            self.search_cancelled += 1
            raise
        if self.search_error:
            raise self.search_error
        return self.hits


def capture(seen, reply="Forty dollars, for the scarf."):
    async def stream_reply(participant, roster, transcript, names, cfg, project,
                           chat_summary, voice_mode, tools=None, memory=None):
        seen.append({"slug": participant["slug"],
                     "history": cfg.get("memory_history") or "",
                     "ambient": cfg.get("memory_ambient") or "",
                     "at": time.monotonic()})
        yield ("text", reply)
        yield ("usage", {"input": 1, "cache_read": 0, "cache_creation": 0,
                         "output": 1})
    return stream_reply


@pytest.fixture
def app(tmp_path):
    return create_app(Settings(data_dir=str(tmp_path / "data"),
                               memory_url="http://127.0.0.1:1"))


@pytest.fixture(autouse=True)
def _clean_stores():
    engine._recall_prewarm.clear()
    history_prefetch._prewarmed.clear()
    yield
    engine._recall_prewarm.clear()
    history_prefetch._prewarmed.clear()


def new_chat(app, *, voice=False, room=False, memory=True):
    with TestClient(app, base_url="http://127.0.0.1") as c:
        chat_id = c.post("/api/chats", json={}).json()["id"]
    con = db.connect()
    con.execute("UPDATE chats SET voice_mode=?, room_mode=?, memory_enabled=? "
                "WHERE id=?", (int(voice), int(room), int(memory), chat_id))
    con.commit()
    roster = db.get_chat_participants(con, chat_id)
    con.close()
    return chat_id, roster


def say(chat_id, text, voice_labels=None):
    con = db.connect()
    cur = con.execute(
        "INSERT INTO messages(chat_id, speaker, content, created_at, voice_labels) "
        "VALUES(?, 'user', ?, ?, ?)", (chat_id, text, db.now(), voice_labels or ""))
    con.commit()
    con.close()
    return cur.lastrowid


def run_round(app, chat_id, roster, memory, *, turn_id=None, settings=None,
              is_handback=False):
    """Drive one round to the end; return its SSE events in order."""
    settings = settings or app.state.settings

    async def go():
        out = []
        async for chunk in engine.run_round(chat_id, roster, roster[-1]["slug"],
                                            settings, memory=memory,
                                            turn_id=turn_id,
                                            is_handback=is_handback):
            out.append(chunk)
        return out
    return sse_events("".join(asyncio.run(go())))


def saved(chat_id):
    con = db.connect()
    rows = db.get_chat_messages(con, chat_id)
    con.close()
    return rows


ASK = "What did I buy my sister for her birthday?"


# ---------- a normal turn ----------

def test_a_normal_turn_never_searches_or_waits(app, monkeypatch):
    seen = []
    monkeypatch.setattr(engine.providers, "stream_reply", capture(seen, "Paris."))
    chat_id, roster = new_chat(app, voice=True)
    say(chat_id, "What's the capital of France?")
    mem = Memory(facts=[])
    mem.search_gate = asyncio.Event()  # a search would hang the round
    events = run_round(app, chat_id, roster, mem, turn_id="turn-normal")
    assert mem.search_calls == []
    assert [s["history"] for s in seen] == ["", ""]
    assert not any(e.get(history_prefetch.SPEAK_NOW_FLAG) for e in events)
    assert not any(e["type"] == "work_status" for e in events)
    # the first event after speaker_start is the model's own first word
    first = events.index(next(e for e in events if e["type"] == "speaker_start"))
    assert events[first + 1] == {"type": "delta", "speaker": roster[0]["slug"],
                                 "text": "Paris."}
    con = db.connect()
    stages = {r["stage"] for r in db.get_voice_traces(con)}
    con.close()
    assert "server_memory_history_wait" not in stages
    assert "server_context_assembly" in stages


def test_a_normal_turn_streams_the_same_events_with_the_search_off(
        app, monkeypatch):
    monkeypatch.setattr(engine.providers, "stream_reply", capture([], "Paris."))
    off = Settings(data_dir=app.state.settings.data_dir,
                   memory_url="http://127.0.0.1:1", history_prefetch=False)
    shapes = []
    for settings in (app.state.settings, off):
        chat_id, roster = new_chat(app, voice=True)
        say(chat_id, "What's the capital of France?")
        events = run_round(app, chat_id, roster, Memory(facts=[]),
                           settings=settings)
        shapes.append([(e["type"], e.get("speaker"), e.get("text"))
                       for e in events])
    assert shapes[0] == shapes[1]


def test_rich_recall_drops_the_search_without_waiting(app, monkeypatch):
    seen = []
    monkeypatch.setattr(engine.providers, "stream_reply", capture(seen))
    chat_id, roster = new_chat(app, voice=True)
    say(chat_id, ASK)
    mem = Memory(facts=[{"content": "Alex bought his sister a scarf."},
                        {"content": "Alex's sister turned 30 in May."}])
    mem.search_gate = asyncio.Event()  # never released
    t0 = time.monotonic()
    events = run_round(app, chat_id, roster, mem)
    assert time.monotonic() - t0 < history_prefetch.GRACE_S + 1.0
    assert len(mem.search_calls) == 1
    assert mem.search_cancelled == 1
    assert [s["history"] for s in seen] == ["", ""]
    assert not any(e.get(history_prefetch.SPEAK_NOW_FLAG) for e in events)


def test_the_setting_off_leaves_searching_to_the_seats(app, monkeypatch):
    seen = []
    monkeypatch.setattr(engine.providers, "stream_reply", capture(seen))
    chat_id, roster = new_chat(app)
    say(chat_id, ASK)
    mem = Memory(facts=[])
    off = Settings(data_dir=app.state.settings.data_dir,
                   memory_url="http://127.0.0.1:1", history_prefetch=False)
    run_round(app, chat_id, roster, mem, settings=off)
    assert mem.search_calls == []
    assert [s["history"] for s in seen] == ["", ""]


# ---------- the parallel start and the handover ----------

def test_the_search_starts_beside_the_recall_and_every_seat_gets_the_hits(
        app, monkeypatch):
    seen = []
    monkeypatch.setattr(engine.providers, "stream_reply", capture(seen))
    chat_id, roster = new_chat(app)
    say(chat_id, ASK)
    mem = Memory(facts=[{"content": "Alex likes flat whites."}],
                 hits=[HIT, WEB_HIT])
    events = run_round(app, chat_id, roster, mem)
    # started while the recall was still out, not after it came back
    assert mem.events.index("search") < mem.events.index("recall done")
    assert mem.search_calls == [{"query": "buy sister birthday",
                                 "limit": history_prefetch.SEARCH_LIMIT}]
    assert len(seen) == 2
    for s in seen:
        assert "scarf" in s["history"]
        assert '"buy sister birthday"' in s["history"]
        assert "search_history" in s["history"]
        assert "Your reply has already begun" not in s["history"]
    # a hit from a round that read the web keeps the tool's marker
    assert "[Untrusted web-derived content from shop.example" in seen[0]["history"]
    # finished alongside the recall, so no status line and no spoken line
    assert not any(e["type"] == "work_status" for e in events)
    assert not any(e.get(history_prefetch.SPEAK_NOW_FLAG) for e in events)


def test_the_hits_read_exactly_as_the_tool_shows_them():
    cfg = Settings().as_cfg()
    text = history_prefetch.handover("found", [HIT, WEB_HIT], cfg, "scarf")
    assert text.startswith(tools.format_search_hits(
        [HIT, WEB_HIT], cfg, history_prefetch.HANDOVER_CHARS))
    tool = asyncio.run(tools.search_history({"query": "scarf"}, cfg,
                                            Memory(hits=[HIT, WEB_HIT])))
    assert tool == tools.format_search_hits([HIT, WEB_HIT], cfg,
                                            cfg["max_tool_output"])


def test_the_handover_lands_in_the_uncached_block(cfg):
    participant = {"name": "Claude", "slug": "claude", "model": "claude-opus-4-8",
                   "provider": "anthropic", "system_prompt": ""}
    roster = [{"name": "Claude", "slug": "claude"}]
    live = dict(cfg, memory_history="HISTORYTOKEN")
    stable, volatile = providers.split_system_prompt(
        participant, roster, live, None, "", False)
    assert "HISTORYTOKEN" not in stable
    assert "## From past chats" in volatile and "HISTORYTOKEN" in volatile
    bare_stable, _ = providers.split_system_prompt(
        participant, roster, dict(cfg), None, "", False)
    assert stable == bare_stable


def test_a_search_that_finds_nothing_or_fails_says_so(app, monkeypatch):
    for mem, words in ((Memory(facts=[], hits=[]), "found nothing"),
                       (Memory(facts=[], search_error=MemorySearchError("401")),
                        "search failed")):
        seen = []
        monkeypatch.setattr(engine.providers, "stream_reply", capture(seen))
        chat_id, roster = new_chat(app)
        say(chat_id, ASK)
        run_round(app, chat_id, roster, mem)
        assert all(words in s["history"] for s in seen)
        assert all("search_history" in s["history"] for s in seen)


def test_a_hand_back_round_never_searches(app, monkeypatch):
    seen = []
    monkeypatch.setattr(engine.providers, "stream_reply", capture(seen))
    chat_id, roster = new_chat(app)
    say(chat_id, ASK)
    mem = Memory(facts=[])
    run_round(app, chat_id, roster[:1], mem, is_handback=True)
    assert mem.search_calls == []


# ---------- a late search ----------

def late(mem, after):
    """Release the held search `after` seconds into the round."""
    async def release():
        await asyncio.sleep(after)
        mem.search_gate.set()
    return release


def run_late_round(app, monkeypatch, *, voice, release_after=0.6, wait_s=None):
    seen = []
    monkeypatch.setattr(engine.providers, "stream_reply", capture(seen))
    if wait_s is not None:
        monkeypatch.setattr(history_prefetch, "WAIT_S", wait_s)
    chat_id, roster = new_chat(app, voice=voice)
    asked = say(chat_id, ASK)
    mem = Memory(facts=[])
    releaser = late(mem, release_after)

    async def go():
        mem.search_gate = asyncio.Event()
        task = asyncio.create_task(releaser())
        out = []
        async for chunk in engine.run_round(chat_id, roster, roster[-1]["slug"],
                                            app.state.settings, memory=mem,
                                            turn_id="turn-late"):
            out.append((time.monotonic(), chunk))
        task.cancel()
        return out
    stamped = asyncio.run(go())
    events = [(t, e) for t, chunk in stamped for e in sse_events(chunk)]
    return chat_id, roster, asked, mem, seen, events


def test_a_late_search_in_a_voice_chat_opens_with_a_spoken_line(app, monkeypatch):
    chat_id, roster, asked, mem, seen, events = run_late_round(
        app, monkeypatch, voice=True)
    first = roster[0]["slug"]
    kinds = [e["type"] for _, e in events]
    start = kinds.index("speaker_start")
    t_line, line_ev = events[start + 1]
    line = history_prefetch.spoken_line(asked)
    assert line_ev == {"type": "delta", "speaker": first, "text": line + " ",
                       history_prefetch.SPEAK_NOW_FLAG: True}
    # the line went out at once, and the model's call waited for the hits
    t_start = events[start][0]
    assert t_line - t_start < history_prefetch.GRACE_S + 0.2
    assert seen[0]["at"] - t_start >= 0.5
    assert "scarf" in seen[0]["history"]
    assert f'you said "{line}" out loud' in seen[0]["history"]
    # the second seat reads the hits, and was never told it said the line
    assert "scarf" in seen[1]["history"]
    assert "Your reply has already begun" not in seen[1]["history"]
    assert sum(1 for _, e in events if e.get(history_prefetch.SPEAK_NOW_FLAG)) == 1
    # the saved reply holds what was spoken, line first
    rows = saved(chat_id)
    assert rows[1]["speaker"] == first
    assert rows[1]["content"] == f"{line} Forty dollars, for the scarf."
    assert rows[2]["content"] == "Forty dollars, for the scarf."
    # the wait is in the voice trace
    con = db.connect()
    waits = [r["ms"] for r in db.get_voice_traces(con)
             if r["stage"] == "server_memory_history_wait"]
    con.close()
    assert len(waits) == 1 and waits[0] >= 500


def test_a_late_search_in_a_typed_chat_shows_a_status_line(app, monkeypatch):
    chat_id, roster, asked, mem, seen, events = run_late_round(
        app, monkeypatch, voice=False)
    kinds = [e["type"] for _, e in events]
    start = kinds.index("speaker_start")
    assert events[start + 1][1] == {"type": "work_status",
                                    "speaker": roster[0]["slug"],
                                    "phase": "start",
                                    "label": work_status.PAST_CHATS_LABEL}
    assert not any(e.get(history_prefetch.SPEAK_NOW_FLAG) for _, e in events)
    assert "scarf" in seen[0]["history"]
    assert "Your reply has already begun" not in seen[0]["history"]
    assert saved(chat_id)[1]["content"] == "Forty dollars, for the scarf."


def test_a_search_that_never_lands_is_given_up_on(app, monkeypatch):
    chat_id, roster, asked, mem, seen, events = run_late_round(
        app, monkeypatch, voice=True, release_after=30, wait_s=0.3)
    assert mem.search_cancelled == 1
    assert "didn't come back in time" in seen[0]["history"]
    assert "search_history" in seen[0]["history"]
    assert len(seen) == 2  # the round went on


def test_a_quick_search_needs_no_line(app, monkeypatch):
    chat_id, roster, asked, mem, seen, events = run_late_round(
        app, monkeypatch, voice=True, release_after=0.05)
    assert not any(e.get(history_prefetch.SPEAK_NOW_FLAG) for _, e in events)
    assert "scarf" in seen[0]["history"]
    assert "Your reply has already begun" not in seen[0]["history"]


def test_stopping_the_round_mid_wait_cancels_the_search(app, monkeypatch):
    monkeypatch.setattr(engine.providers, "stream_reply", capture([]))
    chat_id, roster = new_chat(app, voice=True)
    say(chat_id, ASK)
    mem = Memory(facts=[])

    async def go():
        mem.search_gate = asyncio.Event()
        gen = engine.run_round(chat_id, roster, roster[-1]["slug"],
                               app.state.settings, memory=mem)
        async for chunk in gen:
            if history_prefetch.SPEAK_NOW_FLAG in chunk:
                break  # the owner talks over the line
        await gen.aclose()
        await asyncio.sleep(0)
    asyncio.run(go())
    assert mem.search_cancelled == 1
    # nothing but the question is saved: the seat never got to answer
    assert [m["speaker"] for m in saved(chat_id)] == ["user"]


# ---------- the voice commit ----------

def prewarm_then_round(app, monkeypatch, partial, final, *, memory=True, history=True):
    seen = []
    monkeypatch.setattr(engine.providers, "stream_reply", capture(seen))
    chat_id, roster = new_chat(app, voice=True, memory=memory)
    mem = Memory(facts=[])

    async def go():
        engine.prewarm_recall(chat_id, partial, mem, history=history)
        await asyncio.sleep(0.05)  # transcription finishing
        mem.events.append("round")
        say(chat_id, final)
        async for _ in engine.run_round(chat_id, roster, roster[-1]["slug"],
                                        app.state.settings, memory=mem):
            pass
    asyncio.run(go())
    return mem, seen


def test_the_voice_commit_starts_the_search_and_the_round_adopts_it(
        app, monkeypatch):
    mem, seen = prewarm_then_round(
        app, monkeypatch, "what did i buy my sister for her",
        "What did I buy my sister for her birthday?")
    assert len(mem.search_calls) == 1  # adopted: no second search
    assert mem.search_calls[0]["query"] == "buy sister"
    # it ran while the words were still being transcribed
    assert mem.events.index("search") < mem.events.index("round")
    assert "scarf" in seen[0]["history"]


def test_a_mismatched_commit_search_is_replaced(app, monkeypatch):
    mem, seen = prewarm_then_round(
        app, monkeypatch, "what did i order for my dinner",
        "What did I buy my sister for her birthday?")
    assert [c["query"] for c in mem.search_calls] == ["order dinner",
                                                      "buy sister birthday"]
    assert "scarf" in seen[0]["history"]


def test_the_commit_never_searches_a_memory_off_chat(app, monkeypatch):
    mem, seen = prewarm_then_round(
        app, monkeypatch, "what did i buy my sister for her birthday",
        "What did I buy my sister for her birthday?", memory=False)
    assert mem.search_calls == []
    assert [s["history"] for s in seen] == ["", ""]


def test_the_commit_leaves_a_normal_turn_and_the_setting_off_alone(
        app, monkeypatch):
    mem, _ = prewarm_then_round(app, monkeypatch, "what's the capital of",
                                "What's the capital of France?")
    assert mem.search_calls == []
    assert mem.recall_calls  # the recall prewarm itself is unchanged
    chat_id, _ = new_chat(app)
    mem = Memory()

    async def go():
        engine.prewarm_recall(chat_id, ASK, mem, history=False)
        await asyncio.sleep(0.05)
    asyncio.run(go())
    assert mem.search_calls == [] and chat_id not in history_prefetch._prewarmed


# ---------- the tool's rules ----------

def test_the_search_goes_out_with_the_owner_token(monkeypatch):
    """The same client call search_history makes, so the same bearer."""
    monkeypatch.setenv("MEMORY_AUTH_TOKEN", "s3cr3t-owner-token")
    sent = {}

    async def go():
        c = MemoryClient("http://127.0.0.1:1")

        async def fake_get(url, **kw):
            return httpx.Response(200, json={"status": "ok",
                                             "contract_version": "1.8"},
                                  request=httpx.Request("GET", url))

        async def fake_post(url, json=None, headers=None):
            sent.update(url=url, body=json, headers=headers)
            return httpx.Response(200, json={"hits": [HIT]},
                                  request=httpx.Request("POST", url))

        c._client.get = fake_get
        c._client.post = fake_post
        pre = history_prefetch.start(ASK, c)
        state, hits = await pre.result(1.0)
        await c.aclose()
        return state, hits
    state, hits = asyncio.run(go())
    assert (state, hits) == ("found", [HIT])
    assert sent["url"].endswith("/v1/search")
    assert sent["body"] == {"query": "buy sister birthday",
                            "limit": history_prefetch.SEARCH_LIMIT}
    assert sent["headers"]["Authorization"] == "Bearer s3cr3t-owner-token"


def test_a_room_with_a_guest_follows_the_tools_rules(app, monkeypatch):
    """search_history reads every saved chat in a round with a guest
    present, and the owner accepted that while they're in the room. The
    round's own search does the same, no more: the same call, the same
    hits, the same web marker, whoever asked."""
    seen = []
    monkeypatch.setattr(engine.providers, "stream_reply", capture(seen))
    chat_id, roster = new_chat(app, voice=True, room=True)
    say(chat_id, ASK, voice_labels=json.dumps({"labels": ["Sam"]}))
    mem = Memory(facts=[], hits=[HIT, WEB_HIT])
    run_round(app, chat_id, roster, mem)
    assert mem.search_calls == [{"query": "buy sister birthday",
                                 "limit": history_prefetch.SEARCH_LIMIT}]
    cfg = app.state.settings.as_cfg()
    tool = asyncio.run(tools.search_history({"query": "buy sister birthday"},
                                            cfg, mem))
    assert tool in seen[0]["history"]


def test_no_search_where_the_tool_cannot_reach_memory(app, monkeypatch):
    class Down(Memory):
        async def probe(self, force=False):
            return False

    for mem, memory_on in ((Down(facts=[]), True), (Memory(facts=[]), False)):
        seen = []
        monkeypatch.setattr(engine.providers, "stream_reply", capture(seen))
        chat_id, roster = new_chat(app, memory=memory_on)
        say(chat_id, ASK)
        run_round(app, chat_id, roster, mem)
        assert mem.search_calls == []
        assert [s["history"] for s in seen] == ["", ""]
