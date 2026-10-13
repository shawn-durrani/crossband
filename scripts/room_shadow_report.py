"""Room mode against the room list, on real spoken turns (#669, #670).

Every spoken turn is credited two ways and nothing acts on the second:

- today, by room mode: a turn with no name is the owner's with the room
  off or solo, and not yet known with it on
- the room list: the same turn is the owner's while no one else is on the
  roster or named by the voice session and no "who's this?" is open, and
  not yet known otherwise

The report counts the turns, the room states they were said in, and where
the two credits differ. The lines that matter for switching over:

- someone else heard, mode off: turns the mode credited to the owner while
  another voice was in the room list. The room list would hold them.
- mode on, nobody else: turns the mode held as unknown with no one else
  heard. The room list would credit them to the owner.

Counts only, never words or names. Read-only: it opens chat.db read-only
and writes nothing.

Run:  .venv/bin/python scripts/room_shadow_report.py [--days 14]
"""

import argparse
import datetime as dt
import os
import sqlite3
import sys
import time
from collections import Counter
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from backend.config import load_settings  # noqa: E402


def load(con, since, until):
    con.row_factory = sqlite3.Row
    return [dict(r) for r in con.execute(
        "SELECT * FROM room_shadow WHERE created_at >= ? AND created_at < ? "
        "ORDER BY created_at", (since, until))]


def _state(r) -> str:
    return "solo" if r["solo"] and not r["room_mode"] else \
        "on" if r["room_mode"] else "off"


def report(rows, since, until) -> list:
    day = lambda ts: dt.datetime.fromtimestamp(ts).strftime("%Y-%m-%d")  # noqa: E731
    lines = [f"Spoken turns, {day(since)} to {day(until)}: {len(rows)}"]
    if not rows:
        return lines
    states = Counter(_state(r) for r in rows)
    lines.append("Room mode when they were said: " + ", ".join(
        f"{k} {states[k]}" for k in ("off", "on", "solo") if states[k]))
    unlabelled = [r for r in rows if not r["labelled"]]
    lines.append(f"With no name on them when the round began: {len(unlabelled)}")
    differ = [r for r in rows if r["old"] != r["new"]]
    lines += ["", f"Credited differently: {len(differ)}"]
    held = [r for r in differ if r["old"] == "owner" and r["new"] == "pending"]
    freed = [r for r in differ if r["old"] == "pending" and r["new"] == "owner"]
    lines.append(f"  someone else heard, mode off: {len(held)}"
                 + (f" ({sum(_state(r) == 'solo' for r in held)} of them in solo)"
                    if held else ""))
    lines.append(f"  mode on, nobody else: {len(freed)}")
    other = len(differ) - len(held) - len(freed)
    if other:
        lines.append(f"  other: {other}")
    seen = [r for r in rows if _state(r) != "on" and r["others"]]
    lines += ["", f"Turns said with the mode off while someone else was in the "
                  f"room list: {len(seen)}",
              f"Turns said with the mode on and nobody else in it: "
              f"{sum(_state(r) == 'on' and not r['others'] and not r['asking'] for r in rows)}"]
    return lines


def _day(text):
    return dt.datetime.strptime(text, "%Y-%m-%d").timestamp()


def main(argv=None, settings=None, now=None, out=print):
    ap = argparse.ArgumentParser(description="Room mode against the room list (#670).")
    ap.add_argument("--days", type=float, default=14)
    ap.add_argument("--since", help="start date, YYYY-MM-DD, local time")
    ap.add_argument("--until", help="end date, YYYY-MM-DD, not included")
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
                          "AND name='room_shadow'").fetchone()
        rows = load(con, since, until) if has else []
    finally:
        con.close()
    for line in report(rows, since, until):
        out(line)
    return 0


if __name__ == "__main__":
    sys.exit(main())
