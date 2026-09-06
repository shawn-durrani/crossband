"""Tool dispatch: origin stamping on memory writes, graceful unknown-tool and
exception handling, memory tools refusing without a service."""

import asyncio

from backend.memory_client import MemorySearchError
from backend.tools import _clean_event_date, run_tool


def run(coro):
    return asyncio.run(coro)


class FakeMemory:
    def __init__(self, quarantined=False, fail=False, search_fails=False,
                 hits=None):
        self.quarantined = quarantined
        self.fail = fail
        self.search_fails = search_fails
        self.hits = hits
        self.saved = []
        self.recalled = []
        self.searched = []

    async def save_fact(self, content, origin_agent, event_date=None,
                        confidence="medium", web_sources=None,
                        guest_speakers=None):
        if self.fail:
            return None
        self.saved.append({"content": content, "origin_agent": origin_agent,
                           "event_date": event_date, "confidence": confidence,
                           "web_sources": web_sources,
                           "guest_speakers": guest_speakers})
        return {"id": 1, "quarantined": self.quarantined}

    async def recall(self, query, limit=10, include_superseded=False):
        self.recalled.append(query)
        return [{"content": "Alex lives in Fairhaven", "event_date": "2026-01-01",
                 "origin_agent": "user", "confidence": "high", "score": 0.9}]

    async def search(self, query, limit=20):
        self.searched.append(query)
        if self.search_fails:
            raise MemorySearchError("memory /search request failed: HTTPStatusError")
        if self.hits is not None:
            return self.hits
        return [{"conversation_id": "c1", "speaker": "user",
                 "content": "we discussed espresso", "created_at": "2026-05-01T10:00:00+10:00"}]


def test_unknown_tool_returns_error_string(cfg):
    out = run(run_tool("frobnicate", {}, cfg))
    assert out == "Error: unknown tool frobnicate"


def test_research_tool_exception_is_wrapped(cfg):
    # ftp:// trips the SSRF guard inside fetch_page; dispatch wraps it
    out = run(run_tool("fetch_page", {"url": "ftp://example.com"}, cfg))
    assert out.startswith("Error running fetch_page:")


def test_save_memory_stamps_origin_agent(cfg):
    mem = FakeMemory()
    out = run(run_tool("save_memory", {"content": "Alex prefers flat whites"},
                       cfg, origin_agent="claude", memory=mem))
    assert out == "Saved to memory ledger: Alex prefers flat whites"
    assert mem.saved[0]["origin_agent"] == "claude"
    assert mem.saved[0]["event_date"] is None  # service defaults to today


def test_save_memory_passes_valid_event_date(cfg):
    mem = FakeMemory()
    run(run_tool("save_memory", {"content": "Alex moved house last spring",
                                 "event_date": "2025-10-01"},
                 cfg, origin_agent="gpt", memory=mem))
    assert mem.saved[0]["origin_agent"] == "gpt"
    assert mem.saved[0]["event_date"] == "2025-10-01"


def test_save_memory_drops_junk_event_date(cfg):
    mem = FakeMemory()
    run(run_tool("save_memory", {"content": "A fact worth keeping around",
                                 "event_date": "sometime last week"},
                 cfg, origin_agent="claude", memory=mem))
    assert mem.saved[0]["event_date"] is None


def test_save_memory_carries_the_rounds_guest_stamp(cfg):
    """Contract 1.5: the guests present in the round ride the direct save,
    the way the web stamp does, so membro can hold it for review."""
    mem = FakeMemory(quarantined=True)
    cfg = dict(cfg, _round_guest_speakers=["guest:Sam", "guest:unknown"])
    out = run(run_tool("save_memory", {"content": "Sam is allergic to nuts"},
                       cfg, origin_agent="claude", memory=mem))
    assert mem.saved[0]["guest_speakers"] == ["guest:Sam", "guest:unknown"]
    assert "held for the user's review" in out


def test_save_memory_sends_no_guest_stamp_when_nobody_is_present(cfg):
    mem = FakeMemory()
    for absent in ({}, {"_round_guest_speakers": []}):
        mem.saved.clear()
        run(run_tool("save_memory", {"content": "A fact from the owner alone"},
                     dict(cfg, **absent), origin_agent="claude", memory=mem))
        assert mem.saved[0]["guest_speakers"] == []


def test_clean_event_date():
    assert _clean_event_date("2026-07-04") == "2026-07-04"
    assert _clean_event_date("2026-07-04T10:00:00+10:00") == "2026-07-04"
    assert _clean_event_date("last tuesday") is None
    assert _clean_event_date(None) is None


def test_save_memory_quarantine_reported(cfg):
    mem = FakeMemory(quarantined=True)
    out = run(run_tool("save_memory", {"content": "An unverified assertion here"},
                       cfg, origin_agent="claude", memory=mem))
    assert "held for the user's review" in out


def test_save_memory_short_content_rejected(cfg):
    mem = FakeMemory()
    out = run(run_tool("save_memory", {"content": "hi"}, cfg,
                       origin_agent="claude", memory=mem))
    assert out.startswith("Error:")
    assert mem.saved == []


def test_save_memory_service_write_failure_is_loud(cfg):
    mem = FakeMemory(fail=True)
    out = run(run_tool("save_memory", {"content": "A fact that will not stick"},
                       cfg, origin_agent="claude", memory=mem))
    assert "NOT saved" in out


def test_memory_tools_without_service(cfg):
    for name in ("save_memory", "recall_memory", "search_history"):
        out = run(run_tool(name, {"query": "x", "content": "long enough fact"}, cfg,
                           origin_agent="claude", memory=None))
        assert out == "Error: memory service unavailable"


def test_recall_and_search_formatting(cfg):
    mem = FakeMemory()
    out = run(run_tool("recall_memory", {"query": "where does he live"}, cfg,
                       origin_agent="claude", memory=mem))
    assert "Alex lives in Fairhaven" in out
    # provenance is preserved end to end: date, authoring agent, and confidence
    assert "[2026-01-01 ·user ·conf:high]" in out
    out = run(run_tool("search_history", {"query": "espresso"}, cfg,
                       origin_agent="claude", memory=mem))
    assert "we discussed espresso" in out
    assert out.startswith("[2026-05-01]")


def test_search_history_failure_is_not_reported_as_empty(cfg):
    """Regression for #63: a broken/unreachable-post-probe search must read
    as an explicit error, never as "No matching messages" - the two are not
    interchangeable to a model deciding what it does or doesn't know."""
    mem = FakeMemory(search_fails=True)
    out = run(run_tool("search_history", {"query": "espresso"}, cfg,
                       origin_agent="claude", memory=mem))
    assert out.startswith("Error:")
    assert "No matching messages" not in out
    # the failure detail Membro/httpx produced never leaks into the reply
    assert "espresso" not in out


def test_search_history_marks_web_derived_hits(cfg):
    """Contract 1.4: a hit whose authoring round read the web carries
    `web_sources`, and gets the untrusted marker a live fetch gets, naming
    the domains. A hit without the field, or with an empty list, renders
    exactly as before, so an older membro changes nothing."""
    mem = FakeMemory(hits=[
        {"speaker": "claude", "content": "the page said buy now",
         "created_at": "2026-05-01T10:00:00+10:00",
         "web_sources": ["example.com", "other.org"]},
        {"speaker": "user", "content": "we discussed espresso",
         "created_at": "2026-05-02T10:00:00+10:00", "web_sources": []},
        {"speaker": "gpt", "content": "older service, no field",
         "created_at": "2026-05-03T10:00:00+10:00"},
    ])
    out = run(run_tool("search_history", {"query": "espresso"}, cfg,
                       origin_agent="claude", memory=mem))
    lines = out.split("\n")
    assert lines[0].startswith("[Untrusted web-derived content from example.com, other.org, read in a past chat.")
    assert "quoted page data" in lines[0]
    assert lines[1] == "[2026-05-01] claude: the page said buy now"
    assert lines[2] == "[2026-05-02] user: we discussed espresso"
    assert lines[3] == "[2026-05-03] gpt: older service, no field"
    assert out.count("Untrusted") == 1


def test_search_history_marker_caps_the_domains_shown(cfg):
    mem = FakeMemory(hits=[
        {"speaker": "claude", "content": "x", "created_at": "2026-05-01",
         "web_sources": [f"d{i}.example" for i in range(9)]},
    ])
    out = run(run_tool("search_history", {"query": "x"}, cfg,
                       origin_agent="claude", memory=mem))
    assert "d4.example, read in a past chat" in out
    assert "d5.example" not in out


def test_clean_event_date_is_the_local_calendar_day(monkeypatch):
    """Contract 1.4: event_date is a calendar day at the owner's local
    midnight. An offset-carrying value is converted to local time first,
    so the day membro stores is the day the owner would name."""
    import time
    monkeypatch.setenv("TZ", "Australia/Sydney")
    time.tzset()
    try:
        # 23:30 in Los Angeles on the 4th is the afternoon of the 5th in Sydney
        assert _clean_event_date("2026-07-04T23:30:00-07:00") == "2026-07-05"
        # a naive value is already the owner's local time
        assert _clean_event_date("2026-07-04T23:30:00") == "2026-07-04"
        assert _clean_event_date("2026-07-04") == "2026-07-04"
    finally:
        monkeypatch.delenv("TZ")
        time.tzset()


def test_format_facts_handles_numeric_timestamps():
    """Regression: the memory service returns unix-float event_date/created_at;
    formatting them as strings crashed recall ('float' object is not subscriptable)."""
    from backend.tools import _day, _format_facts
    facts = [{"event_date": 1783150000.5, "origin_agent": "gpt", "content": "numeric ts"},
             {"event_date": "2026-07-04T10:00:00", "origin_agent": "", "content": "iso ts"},
             {"event_date": None, "content": "missing ts"}]
    out = _format_facts(facts, 10_000)
    assert "numeric ts" in out and "[2026-07-04] iso ts" in out and "missing ts" in out
    assert _day(1783150000.5).startswith("2026-")


def test_format_facts_includes_confidence_when_present():
    """Confidence is optional in the Membro contract: render it when supplied,
    omit the tag entirely when it is absent (clean degrade, no empty markers)."""
    from backend.tools import _format_facts
    facts = [
        {"event_date": "2026-01-01", "origin_agent": "user",
         "confidence": "high", "content": "with confidence"},
        {"event_date": "2026-02-02", "origin_agent": "gpt",
         "content": "no confidence field"},
        {"event_date": "2026-03-03", "origin_agent": "claude",
         "confidence": "", "content": "empty confidence"},
    ]
    out = _format_facts(facts, 10_000)
    assert "[2026-01-01 ·user ·conf:high] with confidence" in out
    assert "[2026-02-02 ·gpt] no confidence field" in out
    # empty confidence degrades to no tag, exactly like a missing field
    assert "[2026-03-03 ·claude] empty confidence" in out
    assert "·conf:" not in out.split("no confidence field")[1]
