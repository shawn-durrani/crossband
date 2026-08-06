"""One-off: mirror every existing chat attachment into the memory service.

The handoff only sends messages past each chat's ingested watermark, so
attachments from conversations that were ingested before the attachment
pipeline existed never reached the memory service. This re-sends just the
messages that carry attachments: ingest is idempotent (the messages are skipped
as duplicates) and attachments attach to already-ingested messages by design.

Respects each chat's memory toggle. Safe to re-run: a second pass is a no-op.

Run:  .venv/bin/python scripts/backfill_attachments.py
"""

import asyncio
import base64
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from backend import attachments as att_mod  # noqa: E402
from backend import db  # noqa: E402
from backend.config import Settings  # noqa: E402
from backend.memory_client import MAX_ATTACH_BYTES, MemoryClient  # noqa: E402


async def main():
    settings = Settings()
    db.init(settings)
    memory = MemoryClient(settings.memory_url)
    if not await memory.probe():
        raise SystemExit("memory service unreachable: start it first")

    con = db.connect()
    rows = [dict(r) for r in con.execute(
        "SELECT m.id, m.chat_id, m.speaker, m.content, m.created_at "
        "FROM messages m JOIN chats c ON c.id = m.chat_id "
        "WHERE c.memory_enabled = 1 AND EXISTS "
        "(SELECT 1 FROM attachments a WHERE a.message_id = m.id) "
        "ORDER BY m.chat_id, m.id")]
    atts = {}
    for a in con.execute(
            "SELECT a.* FROM attachments a JOIN messages m ON a.message_id=m.id "
            "JOIN chats c ON c.id=m.chat_id WHERE c.memory_enabled=1 ORDER BY a.id"):
        atts.setdefault(a["message_id"], []).append(dict(a))
    con.close()

    sent = skipped_big = missing = 0
    by_chat: dict[int, list[dict]] = {}
    for m in rows:
        payload_atts = []
        for a in atts.get(m["id"], []):
            if a["size"] > MAX_ATTACH_BYTES:
                skipped_big += 1
                print(f"  ! {a['filename']} ({a['size']}B) over cap, kept local only")
                continue
            try:
                with open(att_mod.file_path(a), "rb") as f:
                    data = base64.standard_b64encode(f.read()).decode()
            except OSError:
                missing += 1
                print(f"  ! {a['filename']} file missing on disk, skipped")
                continue
            payload_atts.append({"filename": a["filename"], "mime": a["mime"],
                                 "data_b64": data})
        if payload_atts:
            m["attachments"] = payload_atts
            by_chat.setdefault(m["chat_id"], []).append(m)

    for chat_id, msgs in by_chat.items():
        res = await memory.ingest(str(chat_id), msgs)
        attached = (res or {}).get("attached", 0)
        sent += attached
        print(f"chat {chat_id}: {len(msgs)} message(s) with files → "
              f"{attached} newly attached")
    await memory.aclose()
    print(f"\ndone: {sent} attachments mirrored"
          + (f", {skipped_big} over size cap" if skipped_big else "")
          + (f", {missing} missing on disk" if missing else ""))


if __name__ == "__main__":
    asyncio.run(main())
