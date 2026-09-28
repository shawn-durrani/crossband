"""How often the memory summary repeats between Claude seat calls (#565).

Every Claude seat call sends the memory summary in the uncached tail of its
prompt, about 3,000 tokens at full price. A cached block of its own, after
the stable system block, would let a call read it back cheaply instead. The
cache only serves that block when an earlier call sent the same bytes ahead
of it and the same summary less than five minutes before, on the same
model. Each call's usage block records the summary's fingerprint and length,
never its text, under usage_json.cache_prefix. This report reads those
records and says what caching the summary would have saved.

Read-only: it opens chat.db read-only and writes nothing.

What it counts, per model and in total:

- calls: Claude seat calls in the period that carry a summary fingerprint,
  replies and calls that left no message alike, and the requests they made
  (one per tool round, and each sends the summary again);
- distinct summaries, and how often a seat's summary was new since its last
  call in that chat;
- repeats: calls whose summary an earlier call sent less than five minutes
  before, on the same model with the same tools and stable block. Those are
  the calls a cached summary block would have served. A tool round after
  the first is always served, since it follows seconds later;
- the estimate, at each model's rate card:
    saved    each served send read from cache instead of sent at full price;
    premium  each first send written to cache at the write rate instead;
    rewrite  a new summary while the seat's cache in that chat was warm
             would throw away the conversation behind it, so what the call
             read from cache would be written again. An upper bound: the
             tools and the stable block are in that count and stay cached;
    net      saved less premium and rewrite, and its share of the spend
             those calls recorded.

Tokens are estimated at four characters each. Gaps are measured between the
times the calls were recorded, which is when each one finished, so a gap
reads a little long and the repeats are a floor.

Run:  .venv/bin/python scripts/summary_reuse_report.py [--days 7]
      .venv/bin/python scripts/summary_reuse_report.py --since 2026-09-29 --until 2026-10-06
"""

import argparse
import datetime as dt
import json
import os
import sqlite3
import statistics
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from backend.config import ANTHROPIC_CACHE, load_settings, price_for  # noqa: E402

# Anthropic keeps a cached block for five minutes when a request sends no
# ttl, and crossband sends none (providers.CACHE_TTL_LABEL).
TTL_SECONDS = 300
CHARS_PER_TOKEN = 4
NO_SUMMARY = "none"  # what providers records when a call sent no summary


@dataclass
class Call:
    ts: float
    chat_id: int
    speaker: str
    model: str
    tools: str
    stable: str
    summary: str
    chars: int
    requests: int
    cache_read: int
    cost: float


@dataclass
class ModelTally:
    model: str
    calls: int = 0
    requests: int = 0
    no_summary: int = 0
    summaries: set = field(default_factory=set)
    chars: list = field(default_factory=list)
    changes: int = 0
    repeats: int = 0
    served: int = 0
    written: int = 0
    rewrites: int = 0
    spend: float = 0.0
    priced: bool = True
    saved: float = 0.0
    premium: float = 0.0
    rewrite: float = 0.0

    @property
    def net(self):
        return self.saved - self.premium - self.rewrite


def load_calls(con, since, until):
    """(calls oldest first, count of records made before the fingerprint
    existed). Reads messages and seat_usage, whose usage blocks have the
    same shape; a record with no cache_prefix isn't a Claude seat call."""
    calls, before = [], 0
    for table in ("messages", "seat_usage"):
        rows = con.execute(
            f"SELECT chat_id, speaker, usage_json, created_at FROM {table} "
            "WHERE usage_json IS NOT NULL AND created_at >= ? "
            "AND created_at < ?", (since, until))
        for chat_id, speaker, usage_json, created_at in rows:
            try:
                u = json.loads(usage_json)
            except (ValueError, TypeError):
                continue
            pref = u.get("cache_prefix") if isinstance(u, dict) else None
            if not isinstance(pref, dict):
                continue
            if "summary" not in pref:
                before += 1
                continue
            calls.append(Call(
                ts=created_at, chat_id=chat_id, speaker=speaker,
                model=pref.get("model") or u.get("model") or "",
                tools=pref.get("tools") or "", stable=pref.get("stable") or "",
                summary=pref["summary"],
                chars=int(pref.get("summary_chars") or 0),
                requests=int(pref.get("requests", 1) or 0),
                cache_read=int(u.get("cache_read") or 0),
                cost=float(u.get("cost") or 0.0)))
    calls.sort(key=lambda c: c.ts)
    return calls, before


def analyse(calls, pricing):
    """{model: ModelTally} over calls sorted oldest first."""
    tallies = {}
    last_sent = {}   # (model, tools, stable, summary) -> when it was last sent
    last_seat = {}   # (chat, seat) -> that seat's previous call in the chat
    for c in calls:
        t = tallies.setdefault(c.model, ModelTally(c.model))
        t.calls += 1
        t.requests += c.requests
        t.spend += c.cost
        card = price_for(c.model, pricing)
        if card is None:
            t.priced = False
        rates = (card or {}).get("cache") or ANTHROPIC_CACHE
        per_token = (card or {}).get("input", 0.0) / 1_000_000

        prev = last_seat.get((c.chat_id, c.speaker))
        last_seat[(c.chat_id, c.speaker)] = c
        if prev is not None and prev.summary != c.summary:
            t.changes += 1
            # The summary block would sit after the tools and the stable
            # block, so a new one throws away a warm conversation behind it.
            warm = (c.ts - prev.ts < TTL_SECONDS and prev.model == c.model
                    and prev.tools == c.tools and prev.stable == c.stable)
            if warm and c.requests:
                t.rewrites += 1
                read = c.cache_read / c.requests  # about the first request's
                t.rewrite += read * per_token * (rates["write_mult"]
                                                 - rates["read_mult"])

        if c.summary == NO_SUMMARY:
            t.no_summary += 1
            continue
        t.summaries.add(c.summary)
        t.chars.append(c.chars)
        if not c.requests:
            continue  # cut off before the provider took it: nothing sent
        key = (c.model, c.tools, c.stable, c.summary)
        seen = last_sent.get(key)
        last_sent[key] = c.ts
        repeat = seen is not None and c.ts - seen < TTL_SECONDS
        t.repeats += repeat
        served = c.requests - 1 + repeat
        written = 0 if repeat else 1
        t.served += served
        t.written += written
        tokens = c.chars / CHARS_PER_TOKEN
        t.saved += served * tokens * per_token * (1 - rates["read_mult"])
        t.premium += written * tokens * per_token * (rates["write_mult"] - 1)
    return tallies


def _when(ts):
    return time.strftime("%Y-%m-%d %H:%M", time.localtime(ts))


def _money(t, amount):
    return f"${amount:,.2f}" if t.priced else "not priced"


def _length(chars):
    if not chars:
        return "no length"
    median = statistics.median(chars)
    return (f"median length {median:,.0f} characters "
            f"(about {median / CHARS_PER_TOKEN:,.0f} tokens)")


def report(calls, before, tallies, since, until):
    """The report's lines."""
    lines = [f"Memory summary reuse, {_when(since)} to {_when(until)}"]
    if before:
        lines.append(f"{before} Claude seat call(s) in the period were "
                     "recorded before the summary fingerprint existed and "
                     "are left out.")
    if not calls:
        lines.append("No Claude seat calls with a summary fingerprint in "
                     "the period.")
        return lines
    lines.append(f"Claude seat calls: {len(calls)}, making "
                 f"{sum(c.requests for c in calls)} requests, from "
                 f"{_when(calls[0].ts)} to {_when(calls[-1].ts)}")
    distinct = {c.summary for c in calls if c.summary != NO_SUMMARY}
    lines.append(f"Distinct summaries: {len(distinct)}, "
                 f"{_length([c.chars for c in calls if c.summary != NO_SUMMARY])}")
    for t in sorted(tallies.values(), key=lambda t: -t.spend):
        lines += [
            "",
            t.model or "(no model recorded)",
            f"  calls {t.calls}, requests {t.requests}, distinct summaries "
            f"{len(t.summaries)}, {_length(t.chars)}",
            f"  sent no summary: {t.no_summary}",
            f"  new summary since the seat's last call in that chat: "
            f"{t.changes}",
            f"  repeats within five minutes: {t.repeats} of "
            f"{t.calls - t.no_summary} calls",
            f"  sends a cached summary would have served: {t.served} of "
            f"{t.served + t.written}",
            f"  new summaries that would rewrite a warm conversation: "
            f"{t.rewrites}",
            f"  saved {_money(t, t.saved)}, write premium "
            f"{_money(t, t.premium)}, rewrite at most "
            f"{_money(t, t.rewrite)}, net {_money(t, t.net)}",
        ]
        if t.priced and t.spend:
            lines.append(f"  net is {t.net / t.spend:.1%} of these calls' "
                         f"{_money(t, t.spend)} recorded spend")
    return lines


def _day(text):
    return dt.datetime.strptime(text, "%Y-%m-%d").timestamp()


def main(argv=None, settings=None, now=None, out=print):
    ap = argparse.ArgumentParser(
        description="How often the memory summary repeats between Claude "
                    "seat calls, and what caching it would save (#565).")
    ap.add_argument("--days", type=float, default=7,
                    help="the period ending now, in days (default 7)")
    ap.add_argument("--since", help="start date, YYYY-MM-DD, local time")
    ap.add_argument("--until", help="end date, YYYY-MM-DD, local time, "
                                    "not included (default now)")
    args = ap.parse_args(argv)

    now = time.time() if now is None else now
    until = _day(args.until) if args.until else now
    since = _day(args.since) if args.since else until - args.days * 86400

    settings = settings or load_settings()
    path = Path(settings.resolved_data_dir()) / "chat.db"
    if not path.exists():
        raise SystemExit(f"no database at {path}")
    con = sqlite3.connect(f"{path.as_uri()}?mode=ro", uri=True)
    try:
        calls, before = load_calls(con, since, until)
    finally:
        con.close()
    for line in report(calls, before, analyse(calls, settings.pricing),
                       since, until):
        out(line)
    return 0


if __name__ == "__main__":
    sys.exit(main())
