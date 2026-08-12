"""One-off repair for #65: remove AI-participant "voices" and phantom seats.

The introduction path could seat an AI participant's name on a room roster;
by-elimination then banked HUMAN audio under that seat until the phantom was
a remembered voice (see the forensic on the issue). The code fix stops it
recurring; this script repairs the data it already produced, through the same
semantics the app itself uses:

- forget: AnchorStore.forget() (clips deleted from disk, index entry dropped)
  plus the roster unlink the DELETE /api/voice/people endpoint performs.
- leave:  mark_room_person_left(), the departure semantics - the row stays as
  honest history, it just stops being a present, pending seat.

What it targets:

- Automatically: every anchor-store person and every PRESENT roster row whose
  name is a participant alias (same matcher the scan layer now uses).
- Explicitly: --forget-person <person_id> and --leave <chat_id>:<name> for
  phantoms that are not participant-named (misheard one-off names).

Dry-run by default; nothing is written without --apply. Refuses to run while
the service is up: the store is process-local state, and editing it under a
live writer is a race. Take a snapshot of data/ first if you want one.

Run:  .venv/bin/python scripts/repair_participant_voices.py [--apply]
          [--forget-person ID]... [--leave CHAT:NAME]...
"""

import argparse
import os
import socket
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from backend import anchors, db  # noqa: E402
from backend.config import Settings  # noqa: E402
from backend.introductions import participant_alias, _participant_names  # noqa: E402


def service_is_up(port: int) -> bool:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.settimeout(0.5)
        return s.connect_ex(("127.0.0.1", port)) == 0


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--apply", action="store_true",
                    help="actually write; default is a dry run")
    ap.add_argument("--forget-person", action="append", default=[],
                    metavar="PERSON_ID",
                    help="also forget this anchor-store person id")
    ap.add_argument("--leave", action="append", default=[],
                    metavar="CHAT_ID:NAME",
                    help="also mark this present roster row left")
    args = ap.parse_args()

    settings = Settings()
    if service_is_up(settings.port):
        raise SystemExit(f"service is answering on :{settings.port} - "
                         "stop it first (the store must have one writer)")
    db.init(settings)
    store = anchors.store()

    con = db.connect()
    try:
        pnames = _participant_names(con)

        # 1. Anchor-store people named like a participant, plus explicit ids.
        doomed = {p["person_id"]: p["name"] for p in store.people()
                  if participant_alias(p["name"], pnames)}
        for pid in args.forget_person:
            match = next((p for p in store.people()
                          if p["person_id"] == pid), None)
            if match is None:
                raise SystemExit(f"--forget-person {pid}: no such person")
            doomed[pid] = match["name"]

        # 2. Present roster rows named like a participant, plus explicit rows.
        rows = [dict(r) for r in con.execute(
            "SELECT id, chat_id, name, person_id FROM room_roster "
            "WHERE status='present'")]
        to_leave = [r for r in rows if participant_alias(r["name"], pnames)]
        for spec in args.leave:
            chat_s, _, name = spec.partition(":")
            if not (chat_s.strip().isdigit() and name.strip()):
                raise SystemExit(f"--leave {spec!r}: expected CHAT_ID:NAME")
            hit = [r for r in rows if r["chat_id"] == int(chat_s)
                   and r["name"].strip().lower() == name.strip().lower()]
            if not hit:
                raise SystemExit(f"--leave {spec!r}: no present row matches")
            to_leave.extend(h for h in hit if h not in to_leave)

        mode = "APPLY" if args.apply else "dry-run"
        print(f"[{mode}] participants: {len(pnames)} names")
        for pid, name in sorted(doomed.items()):
            print(f"[{mode}] forget person {pid} ({name!r})")
        for r in to_leave:
            print(f"[{mode}] leave chat={r['chat_id']} name={r['name']!r} "
                  f"(row {r['id']})")
        if not doomed and not to_leave:
            print(f"[{mode}] nothing to repair")
            return
        if not args.apply:
            print("[dry-run] no changes written; re-run with --apply")
            return

        for pid in doomed:
            if not store.forget(pid):
                raise SystemExit(f"forget({pid}) failed - store changed "
                                 "underneath us? re-run from a dry run")
            # The endpoint's unlink: rows pointing at a forgotten person drop
            # back to 'anchor pending' rather than dangling.
            con.execute("UPDATE room_roster SET person_id='', updated_at=? "
                        "WHERE person_id=?", (db.now(), pid))
        con.commit()
        for r in to_leave:
            db.mark_room_person_left(con, r["chat_id"], r["name"])
        print(f"[APPLY] done: {len(doomed)} person(s) forgotten, "
              f"{len(to_leave)} seat(s) left")
    finally:
        con.close()


if __name__ == "__main__":
    main()
