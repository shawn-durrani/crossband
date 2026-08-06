"""Tool dispatch: origin stamping on memory writes, graceful unknown-tool and
exception handling, memory tools refusing without a service."""

import asyncio

from backend.tools import _clean_event_date, run_tool


def run(coro):
    return asyncio.run(coro)


class FakeMemory:
    def __init__(self, quarantined=False, fail=False):
        self.quarantined = quarantined
        self.fail = fail
        self.saved = []
        self.recalled = []
        self.searched = []

    async def save_fact(self, content, origin_agent, event_date=None, confidence="medium"):
        if self.fail:
            return None
        self.saved.append({"content": content, "origin_agent": origin_agent,
                           "event_date": event_date, "confidence": confidence})
        return {"id": 1, "quarantined": self.quarantined}

    async def recall(self, query, limit=10, include_superseded=False):
        self.recalled.append(query)
        return [{"content": "Alex lives in Fairhaven", "event_date": "2026-01-01",
                 "origin_agent": "user", "confidence": "high", "score": 0.9}]

    async def search(self, query, limit=20):
        self.searched.append(query)
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
