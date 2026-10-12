"""Who Jev thought each spoken turn was for, against what the AIs did (#650).

With `addressee_shadow` on, Jev answers three questions about every spoken
turn in the background: who it was for (the AIs, someone in the room,
nobody, or the TV), whether the AIs owed it a reply, and which seat it
named. The app acts on none of it. This report sets those answers against
what the seats did with the turn:

- answered: at least one seat replied before the next turn of yours
- all passed: no seat replied, and at least one passed
- no reply: no seat replied or passed, such as a round cut short

The lines that matter for a decision:

- not for the AIs, but answered: rounds Jev would have skipped that a seat
  thought worth answering. Some will be Jev's mistakes, some the seat's.
- not for the AIs, and all passed: rounds Jev would have saved.
- for the AIs, but all passed: rounds Jev would still have run for nothing.
- a reply owed, but all passed: turns where Jev and the seats disagree on
  whether anyone owed an answer.

Counts only by default, with no words. `--show-text` lists the turns behind
the disagreeing lines with their words, for reading in your own terminal.
Keep that output out of issues and chats.

Read-only: it opens chat.db read-only and writes nothing. Which seat
answered is found by message order, and a pass by its time, between this
turn and your next one.

Run:  .venv/bin/python scripts/addressee_shadow_report.py [--days 14]
      .venv/bin/python scripts/addressee_shadow_report.py --since 2026-10-12 --show-text
"""

import argparse
import datetime as dt
import os
import sqlite3
import statistics
import sys
import time
from collections import Counter
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from backend.config import load_settings  # noqa: E402

# A pass after your last turn counts towards it for this long.
LAST_TURN_WINDOW_S = 600
NOT_FOR_AIS = ("someone", "nobody", "background")
SHOW_CHARS = 200


def load_turns(con, since, until):
    """One dict per judged turn in the period, with what the seats did."""
    con.row_factory = sqlite3.Row
    seats = {r["slug"] for r in con.execute("SELECT slug FROM participants")}
    rows = con.execute(
        "SELECT s.*, m.created_at AS said_at, m.content FROM addressee_shadow s "
        "JOIN messages m ON m.id = s.message_id "
        "WHERE m.created_at >= ? AND m.created_at < ? ORDER BY s.message_id",
        (since, until)).fetchall()
    turns = []
    for r in rows:
        nxt = con.execute(
            "SELECT id, created_at FROM messages WHERE chat_id=? AND id>? "
            "AND speaker='user' ORDER BY id LIMIT 1",
            (r["chat_id"], r["message_id"])).fetchone()
        upto_id = nxt["id"] if nxt else 2 ** 62
        upto_at = nxt["created_at"] if nxt else r["said_at"] + LAST_TURN_WINDOW_S
        replied = [x["speaker"] for x in con.execute(
            "SELECT speaker FROM messages WHERE chat_id=? AND id>? AND id<? "
            "ORDER BY id", (r["chat_id"], r["message_id"], upto_id))
            if x["speaker"] in seats]
        passes = con.execute(
            "SELECT COUNT(*) FROM seat_usage WHERE chat_id=? AND outcome='pass' "
            "AND created_at>=? AND created_at<?",
            (r["chat_id"], r["said_at"], upto_at)).fetchone()[0]
        did = "answered" if replied else ("all passed" if passes else "no reply")
        turns.append({"id": r["message_id"], "for": r["said_for"] or "none",
                      "asks": r["asks"], "seat": r["seat"], "speaker": r["speaker"],
                      "others": r["others"], "ms": r["ms"], "error": r["error"],
                      "did": did, "text": r["content"] or ""})
    return turns


def _flags(t):
    out = []
    if t["for"] in NOT_FOR_AIS and t["did"] == "answered":
        out.append("not for the AIs, but answered")
    if t["for"] == "ais" and t["did"] == "all passed":
        out.append("for the AIs, but all passed")
    if t["asks"] == "yes" and t["did"] == "all passed":
        out.append("a reply owed, but all passed")
    return out


def report(turns, since, until, show_text=False):
    day = lambda ts: dt.datetime.fromtimestamp(ts).strftime("%Y-%m-%d")  # noqa: E731
    ok = [t for t in turns if not t["error"]]
    lines = [f"Spoken turns Jev judged, {day(since)} to {day(until)}: {len(turns)}"
             + (f" ({len(turns) - len(ok)} without an answer)" if len(ok) < len(turns) else "")]
    if not ok:
        return lines
    for label, group in (("with others in the room", [t for t in ok if t["others"]]),
                         ("alone", [t for t in ok if not t["others"]])):
        if not group:
            continue
        lines += ["", f"{label.capitalize()}: {len(group)} turns"]
        grid = Counter((t["for"], t["did"]) for t in group)
        for f in ("ais", *NOT_FOR_AIS, "none"):
            row = [grid[(f, d)] for d in ("answered", "all passed", "no reply")]
            if any(row):
                lines.append(f"  Jev said {f}: answered {row[0]}, all passed {row[1]}, "
                             f"no reply {row[2]}")
        not_ais = [t for t in group if t["for"] in NOT_FOR_AIS]
        lines += [
            f"  not for the AIs, but answered: "
            f"{sum(t['did'] == 'answered' for t in not_ais)} of {len(not_ais)}",
            f"  not for the AIs, and all passed: "
            f"{sum(t['did'] == 'all passed' for t in not_ais)} of {len(not_ais)}",
            f"  for the AIs, but all passed: "
            f"{sum(t['for'] == 'ais' and t['did'] == 'all passed' for t in group)}",
            f"  a reply owed, but all passed: "
            f"{sum(t['asks'] == 'yes' and t['did'] == 'all passed' for t in group)}",
        ]
    speakers = Counter(t["speaker"] or "unknown" for t in ok)
    lines += ["", "Who spoke: " + ", ".join(f"{k} {v}" for k, v in sorted(speakers.items()))]
    ms = sorted(t["ms"] for t in ok)
    lines.append(f"Jev's time: median {round(statistics.median(ms))} ms, "
                 f"slowest {ms[-1]} ms")
    if show_text:
        lines += ["", "The turns behind the disagreeing lines. These carry your "
                      "words: keep them out of issues and chats."]
        for t in ok:
            for flag in _flags(t):
                text = " ".join(t["text"].split())[:SHOW_CHARS]
                lines.append(f"  #{t['id']} {flag} (Jev: for {t['for']}, asks "
                             f"{t['asks'] or 'none'}, seat {t['seat'] or 'none'}): {text}")
    return lines


def _day(text):
    return dt.datetime.strptime(text, "%Y-%m-%d").timestamp()


def main(argv=None, settings=None, now=None, out=print):
    ap = argparse.ArgumentParser(
        description="Who Jev thought each spoken turn was for, against what "
                    "the AIs did (#650).")
    ap.add_argument("--days", type=float, default=14,
                    help="the period ending now, in days (default 14)")
    ap.add_argument("--since", help="start date, YYYY-MM-DD, local time")
    ap.add_argument("--until", help="end date, YYYY-MM-DD, local time, "
                                    "not included (default now)")
    ap.add_argument("--show-text", action="store_true",
                    help="list the disagreeing turns with their words")
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
        has = con.execute("SELECT 1 FROM sqlite_master WHERE type='table' "
                          "AND name='addressee_shadow'").fetchone()
        turns = load_turns(con, since, until) if has else []
    finally:
        con.close()
    for line in report(turns, since, until, show_text=args.show_text):
        out(line)
    return 0


if __name__ == "__main__":
    sys.exit(main())
