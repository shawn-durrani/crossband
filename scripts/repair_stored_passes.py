"""One-off repair for #472: remove the passes stored before #457 and #468.

A pass is invisible: nothing stored, shown, spoken or sent to memory (#98).
Before #457 and #468 a few slipped through as real rows. Some were replies
cut off while writing the pass token, saved with the cut-off marker on the
end. Others were short "nothing to add" remarks ending in the token. The
fixes stop new ones. This script removes the old rows, which the seats
still read as context on every round in that chat.

What it targets: every seat message with no tool events whose stored text
the current pass rules read as a pass. The rules are backend/passes.py's
own, the ones the engine uses today, so the script removes exactly what
the engine would not store now:

- a row ending in the cut-off marker was interrupted, so the text before
  the marker is judged with passes.is_cut_pass;
- any other row is judged with passes.is_pass, the finished-reply rule.

A tool turn is left alone, because the engine keeps a tool turn whose only
text is [pass]. User, system and guest rows are never looked at.

Dry run by default: each row prints its id, chat, speaker, time, cost and
whether memory already holds a copy, never its text. With --apply it
refuses while a round is running (the same rule as discarding a turn),
snapshots chat.db beside itself, deletes the rows in one transaction
(attachments and tool events go with them, as when a turn is discarded),
and appends one content-free line per removed row to data/repairs.jsonl.

Memory keeps its own copy of any row the handoff already sent. This script
leaves that copy alone: membro's message eraser removes one, and the dry
run names each copy's conversation and message id.

Run:  .venv/bin/python scripts/repair_stored_passes.py [--apply]
"""

import argparse
import json
import os
import re
import sqlite3
import sys
import time
import urllib.error
import urllib.request

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from backend import busy, db, passes  # noqa: E402
from backend.config import load_settings  # noqa: E402
from backend.memory_client import SOURCE_APP  # noqa: E402

# The marker the engine appends to a reply cut off mid-stream
# (engine.run_round's persist_live), whoever the owner is named.
CUT_OFF_MARKER = re.compile(r"\n\n\[cut off by [^\]\n]*\]\s*$")

JOURNAL_NAME = "repairs.jsonl"
REPAIR = "stored-passes-472"


def classify(content: str) -> str:
    """'cut off' or 'finished' for a stored text the pass rules read as a
    pass, '' for a real reply."""
    marker = CUT_OFF_MARKER.search(content or "")
    if marker:
        return "cut off" if passes.is_cut_pass(content[:marker.start()]) else ""
    return "finished" if passes.is_pass(content) else ""


def find_stored_passes(con) -> list[dict]:
    """Every seat row with no tool events whose text is a pass, oldest
    first, with what the dry run shows. The text itself is read here and
    never returned."""
    slugs = {r["slug"] for r in con.execute("SELECT slug FROM participants")}
    rows = con.execute(
        "SELECT m.id, m.chat_id, m.speaker, m.content, m.usage_json, "
        "m.created_at, c.ingested_upto FROM messages m "
        "JOIN chats c ON c.id = m.chat_id "
        "WHERE NOT EXISTS (SELECT 1 FROM tool_events t "
        "WHERE t.message_id = m.id) ORDER BY m.id")
    found = []
    for r in rows:
        if r["speaker"] not in slugs:
            continue
        kind = classify(r["content"])
        if not kind:
            continue
        cost = None
        if r["usage_json"]:
            try:
                cost = json.loads(r["usage_json"]).get("cost")
            except (ValueError, AttributeError):
                cost = None
        found.append({
            "message_id": r["id"], "chat_id": r["chat_id"],
            "speaker": r["speaker"], "created_at": r["created_at"],
            "kind": kind, "cost": cost,
            # The discard route's test: the handoff watermark is past it.
            "in_memory": r["id"] <= (r["ingested_upto"] or 0),
        })
    return found


def describe(row: dict) -> str:
    when = time.strftime("%Y-%m-%d %H:%M:%S",
                         time.localtime(row["created_at"]))
    cost = "-" if row["cost"] is None else f"${row['cost']:.4f}"
    memory = (f"in memory as {SOURCE_APP} conversation {row['chat_id']} "
              f"message {row['message_id']}") if row["in_memory"] \
        else "not in memory"
    return (f"message {row['message_id']}  chat {row['chat_id']}  "
            f"{row['speaker']}  {when}  {row['kind']}  cost {cost}  {memory}")


def running_work(port: int):
    """The service's busy reasons, [] when idle, or None when nothing is
    answering on the port. Any other answer raises: an --apply that can't
    tell whether a round is running doesn't go ahead."""
    url = f"http://127.0.0.1:{port}{busy.PATH}"
    try:
        with urllib.request.urlopen(url, timeout=3) as resp:
            return list(json.load(resp).get("reasons") or [])
    except urllib.error.URLError as e:
        if isinstance(e.reason, ConnectionRefusedError):
            return None
        raise


def snapshot(data_dir) -> str:
    """A consistent copy of chat.db beside it, taken with SQLite's online
    backup the way the app's own snapshots are, safe while it's running."""
    dest = os.path.join(
        data_dir, f"chat-before-pass-repair-{time.strftime('%Y%m%d-%H%M%S')}.db")
    src = sqlite3.connect(db.DB_PATH)
    dst = sqlite3.connect(dest)
    try:
        with dst:
            src.backup(dst)
    finally:
        src.close()
        dst.close()
    return dest


def apply(con, found, data_dir, backup) -> None:
    """Delete the rows in one transaction, each re-checked first, and
    journal each removal without its text."""
    held = sqlite3.connect(backup)
    try:
        ids = [f["message_id"] for f in found]
        kept = held.execute(
            f"SELECT count(*) FROM messages WHERE id IN "
            f"({','.join('?' * len(ids))})", ids).fetchone()[0]
    finally:
        held.close()
    if kept != len(ids):
        raise SystemExit(f"the snapshot {backup} doesn't hold every row; "
                         "nothing deleted")
    again = {f["message_id"]: f for f in find_stored_passes(con)}
    if set(again) != set(ids):
        raise SystemExit("the rows changed since the dry run; nothing "
                         "deleted, run it again")
    try:
        for i in ids:
            con.execute("DELETE FROM messages WHERE id=?", (i,))
        con.commit()
    except Exception:
        con.rollback()
        raise
    at = time.time()
    with open(os.path.join(data_dir, JOURNAL_NAME), "a") as journal:
        for f in found:
            journal.write(json.dumps({
                "at": at, "repair": REPAIR, "action": "deleted",
                "message_id": f["message_id"], "chat_id": f["chat_id"],
                "speaker": f["speaker"], "created_at": f["created_at"],
                "kind": f["kind"], "in_memory": f["in_memory"],
                "backup": os.path.basename(backup)}) + "\n")


def main(argv=None, settings=None, probe=running_work, out=print):
    ap = argparse.ArgumentParser(
        description="Remove the passes stored before #457 and #468 (#472).")
    ap.add_argument("--apply", action="store_true",
                    help="delete the rows; the default is a dry run")
    args = ap.parse_args(argv)

    settings = settings or load_settings()
    data_dir = str(settings.resolved_data_dir())
    db.configure(data_dir)
    # The snapshot and the journal hold chat data: owner-only, like the
    # app's own files.
    db.secure_data_dir()
    if not os.path.exists(db.DB_PATH):
        # db.connect() would create an empty database here.
        raise SystemExit(f"no database at {db.DB_PATH}")

    mode = "APPLY" if args.apply else "dry-run"
    con = db.connect()
    try:
        found = find_stored_passes(con)
        for f in found:
            out(f"[{mode}] {describe(f)}")
        if not found:
            out(f"[{mode}] no stored passes; nothing to do")
            return 0
        costs = [f["cost"] for f in found if f["cost"]]
        in_memory = sum(f["in_memory"] for f in found)
        out(f"[{mode}] {len(found)} stored pass(es), {in_memory} in memory, "
            f"recorded cost ${sum(costs):.4f}")
        if not args.apply:
            out("[dry-run] nothing changed; run again with --apply")
            return 0

        try:
            work = probe(settings.port)
        except Exception as e:
            raise SystemExit(f"couldn't ask the service on :{settings.port} "
                             f"whether a round is running ({type(e).__name__}); "
                             "nothing deleted")
        if work and busy.ROUND_RUNNING in work:
            raise SystemExit("a round is running; nothing deleted, run it "
                             "again when it's finished")
        backup = snapshot(data_dir)
        out(f"[APPLY] snapshot: {backup}")
        apply(con, found, data_dir, backup)
        out(f"[APPLY] deleted {len(found)} row(s); journal: "
            f"{os.path.join(data_dir, JOURNAL_NAME)}")
        return 0
    finally:
        con.close()


if __name__ == "__main__":
    sys.exit(main())
