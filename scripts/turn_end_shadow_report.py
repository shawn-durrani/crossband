"""The end-of-turn trial: what two readers guessed at each pause, against
what you did next (#661).

With `turn_end_shadow` on, every pause in a spoken turn asks two readers
whether you'd finished: Jev, from the words so far, and Smart Turn, a
small model on this Mac, from the audio. You give the answer yourself. A
pause you talked on from was mid-thought, and one that ended the turn was
finished. Nothing acts on the guesses.

For each reader the report says:

- ended sooner: turns that ended where the reader said finished. Each could
  have ended at the pause hint plus the reader's time, instead of after
  the full pause. The saving is estimated from the pause setting.
- cut you off: pauses you talked on from where the reader said finished.
  This is the mistake that matters, because the app would have answered
  half a sentence.

Smart Turn gives a chance, so it's read at a few thresholds. Counts only,
never words. Read-only: it opens chat.db read-only and writes nothing.

Run:  .venv/bin/python scripts/turn_end_shadow_report.py [--days 14]
      .venv/bin/python scripts/turn_end_shadow_report.py --since 2026-10-13
"""

import argparse
import datetime as dt
import os
import sqlite3
import statistics
import sys
import time
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from backend.config import load_settings  # noqa: E402

THRESHOLDS = (0.5, 0.7, 0.9)
HINT_S = 0.3          # the browser's pause hint comes this far into a pause


def load(con, since, until):
    con.row_factory = sqlite3.Row
    return [dict(r) for r in con.execute(
        "SELECT * FROM turn_end_shadow WHERE created_at >= ? AND created_at < ? "
        "ORDER BY id", (since, until))]


def _line(name, says_finished, rows, ms, pause_s):
    ended = [r for r in rows if r["outcome"] == "ended"]
    resumed = [r for r in rows if r["outcome"] == "resumed"]
    sooner = [r for r in ended if says_finished(r)]
    cut = [r for r in resumed if says_finished(r)]
    times = [m for m in (ms(r) for r in rows) if m is not None]
    median_ms = statistics.median(times) if times else 0.0
    saving = max(0.0, pause_s - HINT_S - median_ms / 1000)
    return (f"  {name}: ended sooner {len(sooner)} of {len(ended)}, by about "
            f"{saving:.1f}s each. Cut you off {len(cut)} of {len(resumed)}."
            f" Median time {median_ms:.0f} ms.")


def report(rows, since, until, pause_s=1.5) -> list:
    day = lambda ts: dt.datetime.fromtimestamp(ts).strftime("%Y-%m-%d")  # noqa: E731
    ended = sum(r["outcome"] == "ended" for r in rows)
    lines = [f"Pauses in spoken turns, {day(since)} to {day(until)}: {len(rows)}. "
             f"{ended} ended the turn and {len(rows) - ended} were talked on from."]
    if not rows:
        return lines
    jev_rows = [r for r in rows if r["jev"]]
    if jev_rows:
        lines += ["", f"Jev, on {len(jev_rows)} pauses:",
                  _line("said finished", lambda r: r["jev"] == "finished",
                        jev_rows, lambda r: r["jev_ms"], pause_s),
                  f"  said unsure on {sum(r['jev'] == 'unsure' for r in jev_rows)}."]
    local_rows = [r for r in rows if r["local_prob"] is not None]
    if local_rows:
        lines += ["", f"Smart Turn, on {len(local_rows)} pauses:"]
        for t in THRESHOLDS:
            lines.append(_line(f"at {t:g}", lambda r, t=t: r["local_prob"] >= t,
                               local_rows, lambda r: r["local_ms"], pause_s))
    both = [r for r in rows if r["jev"] and r["local_prob"] is not None]
    if both:
        lines += ["", f"Both, on {len(both)} pauses:",
                  _line("Jev finished and Smart Turn at 0.5",
                        lambda r: r["jev"] == "finished" and r["local_prob"] >= 0.5,
                        both, lambda r: max(r["jev_ms"] or 0, r["local_ms"] or 0),
                        pause_s)]
    errors = sum(bool(r["error"]) for r in rows)
    if errors:
        lines += ["", f"{errors} pauses had a reader fail."]
    return lines


def _day(text):
    return dt.datetime.strptime(text, "%Y-%m-%d").timestamp()


def main(argv=None, settings=None, now=None, out=print):
    ap = argparse.ArgumentParser(description="The end-of-turn trial (#661).")
    ap.add_argument("--days", type=float, default=14)
    ap.add_argument("--since", help="start date, YYYY-MM-DD, local time")
    ap.add_argument("--until", help="end date, YYYY-MM-DD, not included")
    ap.add_argument("--pause", type=float, default=1.5,
                    help="the pause setting the saving is measured against")
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
                          "AND name='turn_end_shadow'").fetchone()
        rows = load(con, since, until) if has else []
    finally:
        con.close()
    for line in report(rows, since, until, args.pause):
        out(line)
    return 0


if __name__ == "__main__":
    sys.exit(main())
