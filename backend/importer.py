"""Import chat history from AI-provider data exports (Claude.ai and ChatGPT),
then seed Membro so long-term recall works from day one.

Parsing/normalization is ported from the predecessor app. Memory seeding is
NOT: the predecessor wrote its ledger directly; here every byte reaches Membro
through the versioned HTTP contract — /ingest for the verbatim record, /distill
per conversation for fact mining (summary rebuild deferred to one final call),
/facts for the export's own memory entries (origin user, trusted).

Claude.ai: Settings → Privacy → Export data (conversations.json, projects.json).
ChatGPT:   Settings → Data controls → Export data (conversations.json with
           a node "mapping" per conversation).

Deliberate exemption from db.insert_message(): the inserts below
are historical bulk backfill — hundreds of rows per import, backdated
timestamps, run inside one transaction — not live activity a connected
client should be pinged about (a single import would otherwise fire hundreds
of individual wake-ups for messages the user is about to see via a normal
page load anyway, immediately after the import call returns). This is the
one explicitly-documented raw-INSERT exemption; the guard in
tests/test_insert_message_guard.py whitelists this file by name for exactly
this reason and no other."""

import datetime
import io
import json
import re
import zipfile

from . import db

IMPORT_SPEAKER_FALLBACK = {"claude": "claude", "gpt": "gpt"}


def _epoch(value):
    if value is None or value == "":
        return db.now()
    if isinstance(value, (int, float)):
        return float(value)
    try:
        return datetime.datetime.fromisoformat(str(value).replace("Z", "+00:00")).timestamp()
    except Exception:
        return db.now()


# ---------- Claude format ----------

_EXPORT_PLACEHOLDER = "This block is not supported on your current device yet."
_PLACEHOLDER_NOTE = ("[content not included in Claude's export — likely an artifact "
                     "or tool output created in the original conversation]")


def _tool_use_text(block):
    """Recover the valuable payloads from tool_use blocks: created files and
    artifacts come through with their complete content."""
    name = block.get("name") or "tool"
    inp = block.get("input") or {}
    if isinstance(inp, dict):
        file_text = inp.get("file_text")  # create_file in claude.ai
        if file_text:
            label = inp.get("path") or inp.get("description") or "file"
            return f"[Claude created file: {label}]\n```\n{file_text}\n```"
        if name == "artifacts" and inp.get("content"):
            label = inp.get("title") or inp.get("id") or "artifact"
            return f"[Claude created artifact: {label}]\n```\n{inp['content']}\n```"
        if inp.get("query"):
            return f"[tool: {name}(\"{inp['query']}\")]"
    return f"[tool: {name}]"


def _claude_message_text(m):
    parts = []
    for block in m.get("content") or []:
        if not isinstance(block, dict):
            continue
        if block.get("type") == "text" and block.get("text"):
            parts.append(block["text"])
        elif block.get("type") == "tool_use":
            parts.append(_tool_use_text(block))
        # tool_result blocks are environment echo — skipped
    text = "\n\n".join(p.strip() for p in parts if p.strip()).strip()
    if not text:
        text = (m.get("text") or "").strip()
    return text.replace(_EXPORT_PLACEHOLDER, _PLACEHOLDER_NOTE)


def _normalize_claude(conversations):
    for c in conversations:
        msgs = sorted(c.get("chat_messages") or [], key=lambda m: str(m.get("created_at") or ""))
        out = []
        for m in msgs:
            text = _claude_message_text(m)
            if not text:
                continue
            out.append({
                "uuid": m.get("uuid"),
                "speaker": "user" if m.get("sender") == "human" else "claude",
                "text": text,
                "created_at": _epoch(m.get("created_at")),
            })
        yield {
            "uuid": c.get("uuid"),
            "title": (c.get("name") or "").strip(),
            "created_at": _epoch(c.get("created_at")),
            "updated_at": _epoch(c.get("updated_at")),
            "project_uuid": c.get("project_uuid") or (c.get("project") or {}).get("uuid"),
            "messages": out,
        }


# ---------- ChatGPT format ----------

def _chatgpt_node_text(msg):
    content = msg.get("content") or {}
    ctype = content.get("content_type")
    if ctype in ("text", "multimodal_text"):
        parts = [p for p in (content.get("parts") or []) if isinstance(p, str) and p.strip()]
        return "\n".join(parts).strip()
    if ctype == "code" and content.get("text"):
        return f"```\n{content['text']}\n```"
    return ""


def _chatgpt_thread(conv):
    """Walk the canonical thread: follow parent pointers back from current_node
    (this picks the surviving branch when messages were edited/regenerated)."""
    mapping = conv.get("mapping") or {}
    node_id = conv.get("current_node")
    chain = []
    seen = set()
    while node_id and node_id in mapping and node_id not in seen:
        seen.add(node_id)
        node = mapping[node_id]
        if node.get("message"):
            chain.append(node)
        node_id = node.get("parent")
    if not chain:  # fallback: every message node by timestamp
        chain = [n for n in mapping.values() if n.get("message")]
        chain.sort(key=lambda n: (n["message"].get("create_time") or 0))
        return chain
    return list(reversed(chain))


def _normalize_chatgpt(conversations):
    for c in conversations:
        out = []
        for node in _chatgpt_thread(c):
            msg = node["message"]
            role = ((msg.get("author") or {}).get("role")) or ""
            if role not in ("user", "assistant"):
                continue  # skip system/tool plumbing
            text = _chatgpt_node_text(msg)
            if not text:
                continue
            out.append({
                "uuid": node.get("id") or msg.get("id"),
                "speaker": "user" if role == "user" else "gpt",
                "text": text,
                "created_at": _epoch(msg.get("create_time") or c.get("create_time")),
            })
        yield {
            "uuid": c.get("conversation_id") or c.get("id"),
            "title": (c.get("title") or "").strip(),
            "created_at": _epoch(c.get("create_time")),
            "updated_at": _epoch(c.get("update_time")),
            "project_uuid": None,  # ChatGPT exports don't carry project structure
            "messages": out,
        }


# ---------- upload parsing & format detection ----------

def parse_upload(data, filename):
    out = {"conversations": [], "projects": [], "memories": [], "format": None}
    if (filename or "").lower().endswith(".zip") or data[:2] == b"PK":
        zf = zipfile.ZipFile(io.BytesIO(data))
        for name in sorted(zf.namelist()):
            if "__MACOSX" in name:  # resource-fork junk in re-zipped folders
                continue
            base = name.rsplit("/", 1)[-1].lower()
            try:
                # ChatGPT shards large exports: conversations-000.json, -001, …
                if re.fullmatch(r"conversations(-\d+)?\.json", base):
                    out["conversations"].extend(json.loads(zf.read(name)))
                elif base == "projects.json":
                    out["projects"] = json.loads(zf.read(name))
                elif base in ("memories.json", "memory.json"):
                    out["memories"] = json.loads(zf.read(name))
            except Exception:
                continue  # tolerate malformed/unrelated files in the zip
    else:
        payload = json.loads(data)
        if isinstance(payload, list) and payload:
            if "chat_messages" in payload[0] or "mapping" in payload[0]:
                out["conversations"] = payload
            elif "docs" in payload[0] or "prompt_template" in payload[0]:
                out["projects"] = payload
            else:
                out["conversations"] = payload
    convs = out["conversations"]
    if convs and isinstance(convs[0], dict):
        out["format"] = "chatgpt" if "mapping" in convs[0] else "claude"
    elif out["projects"] or out["memories"]:
        out["format"] = "claude"
    return out


def _export_memory_lines(memories):
    """The export's own memory entries, as clean single-line facts."""
    items = memories if isinstance(memories, list) else [memories]
    for item in items:
        if isinstance(item, str):
            content = item.strip()
        elif isinstance(item, dict):
            content = (item.get("content") or item.get("text") or item.get("memory") or "").strip()
        else:
            continue
        for line in content.splitlines():
            line = line.strip().lstrip("-•* ").strip()
            if len(line) >= 8:
                yield line


# ---------- shared import / incremental merge ----------

async def import_stream(data, filename, memory, mine=True):
    """Async generator of progress events, ending with the counts. Chats land
    in the local DB (idempotent by import_uuid, incremental merge for grown
    conversations), then Membro is seeded through the contract."""
    con = db.connect()
    try:
        async for ev in _import(con, data, filename, memory, mine):
            yield ev
    except Exception as e:
        yield {"type": "error", "message": f"Could not parse export: {e}"}
    finally:
        con.close()


async def _import(con, data, filename, memory, mine):
    parsed = parse_upload(data, filename)
    if not parsed["format"]:
        yield {"type": "error", "message": "Unrecognized export format — expected a "
               "Claude or ChatGPT data export (zip or conversations.json)"}
        return
    counts = {"format": parsed["format"], "projects": 0, "chats": 0, "updated_chats": 0,
              "messages": 0, "skipped_existing": 0, "memory_entries": 0,
              "ingested": 0, "mined_chats": 0}
    total_convs = len(parsed["conversations"])
    yield {"type": "start", "format": parsed["format"], "conversations": total_convs,
           "projects": len(parsed["projects"]), "mine": mine}

    # projects first (Claude only), so conversations can link to them
    proj_map = {}
    for p in parsed["projects"]:
        uuid = p.get("uuid")
        if uuid:
            row = con.execute("SELECT id FROM projects WHERE import_uuid=?", (uuid,)).fetchone()
            if row:
                proj_map[uuid] = row["id"]
                continue
        instructions = (p.get("prompt_template") or p.get("description") or "").strip()
        cur = con.execute(
            "INSERT INTO projects(name, instructions, created_at, import_uuid) VALUES(?,?,?,?)",
            (p.get("name") or "Imported project", instructions, _epoch(p.get("created_at")), uuid))
        if uuid:
            proj_map[uuid] = cur.lastrowid
        counts["projects"] += 1

    normalize = _normalize_chatgpt if parsed["format"] == "chatgpt" else _normalize_claude
    participant_ids = [x["id"] for x in db.get_participants(con, enabled_only=True)]
    touched_chat_ids = []
    step = max(1, total_convs // 100)
    for idx, conv in enumerate(normalize(parsed["conversations"]), 1):
        if idx % step == 0 or idx == total_convs:
            yield {"type": "progress", "phase": "Importing conversations",
                   "done": idx, "total": total_convs}
        if not conv["messages"]:
            counts["skipped_existing"] += 1
            continue

        existing = None
        if conv["uuid"]:
            existing = con.execute(
                "SELECT id FROM chats WHERE import_uuid=?", (conv["uuid"],)).fetchone()

        if existing:
            # Incremental merge: top up with messages from a newer export,
            # deduplicated by message id (timestamp fallback).
            chat_id = existing["id"]
            seen_uuids = {r["import_uuid"] for r in con.execute(
                "SELECT import_uuid FROM messages WHERE chat_id=? AND import_uuid IS NOT NULL",
                (chat_id,))}
            max_ts = con.execute(
                "SELECT COALESCE(MAX(created_at), 0) AS t FROM messages WHERE chat_id=?",
                (chat_id,)).fetchone()["t"]
            appended = 0
            for m in conv["messages"]:
                if m["uuid"]:
                    if m["uuid"] in seen_uuids:
                        continue
                elif m["created_at"] <= max_ts:
                    continue
                con.execute(
                    "INSERT INTO messages(chat_id, speaker, content, created_at, import_uuid) "
                    "VALUES(?,?,?,?,?)",
                    (chat_id, m["speaker"], m["text"], m["created_at"], m["uuid"]))
                appended += 1
            if appended:
                con.execute("UPDATE chats SET updated_at=MAX(updated_at, ?) WHERE id=?",
                            (conv["updated_at"], chat_id))
                counts["messages"] += appended
                counts["updated_chats"] += 1
                touched_chat_ids.append(chat_id)
            else:
                counts["skipped_existing"] += 1
            continue

        title = conv["title"]
        if not title:
            first_user = next((m["text"] for m in conv["messages"] if m["speaker"] == "user"),
                              "Imported chat")
            title = first_user.splitlines()[0][:60]
        cur = con.execute(
            "INSERT INTO chats(project_id, title, title_upto, created_at, updated_at, "
            "import_uuid, next_first) VALUES(?,?,?,?,?,?,'claude')",
            (proj_map.get(conv["project_uuid"]), title[:80], -1,
             conv["created_at"], conv["updated_at"], conv["uuid"]))
        chat_id = cur.lastrowid
        for pid in participant_ids:
            con.execute(
                "INSERT OR IGNORE INTO chat_participants(chat_id, participant_id) VALUES(?,?)",
                (chat_id, pid))
        for m in conv["messages"]:
            con.execute(
                "INSERT INTO messages(chat_id, speaker, content, created_at, import_uuid) "
                "VALUES(?,?,?,?,?)",
                (chat_id, m["speaker"], m["text"], m["created_at"], m["uuid"]))
            counts["messages"] += 1
        counts["chats"] += 1
        touched_chat_ids.append(chat_id)
    con.commit()

    # ---- seed Membro through the contract (never its database) ----
    if memory is not None and await memory.probe():
        # the export's own memory entries → trusted user facts
        for line in _export_memory_lines(parsed["memories"]):
            if await memory.save_fact(line, origin_agent="user"):
                counts["memory_entries"] += 1

        total = len(touched_chat_ids)
        for i, chat_id in enumerate(touched_chat_ids, 1):
            yield {"type": "progress", "phase": "Sending history to memory",
                   "done": i, "total": total}
            msgs = [dict(r) for r in con.execute(
                "SELECT id, speaker, content, created_at FROM messages "
                "WHERE chat_id=? AND id > (SELECT ingested_upto FROM chats WHERE id=?) "
                "ORDER BY id", (chat_id, chat_id))]
            if not msgs:
                continue
            # Membro's /ingest caps 5000 messages per call — chunk the rare giants
            for j in range(0, len(msgs), 4000):
                if await memory.ingest(str(chat_id), msgs[j:j + 4000]):
                    counts["ingested"] += len(msgs[j:j + 4000])
            con.execute("UPDATE chats SET ingested_upto=? WHERE id=?",
                        (msgs[-1]["id"], chat_id))
            con.commit()

        if mine:
            for i, chat_id in enumerate(touched_chat_ids, 1):
                yield {"type": "progress", "phase": "Mining facts (the note-taker reads each chat)",
                       "done": i, "total": total}
                # sequential + summary deferred: one utility call per chat, one
                # summary rebuild at the end — not one per conversation
                await memory.distill_and_wait(str(chat_id), regenerate=False)
                counts["mined_chats"] += 1
            yield {"type": "progress", "phase": "Building your profile", "done": 0, "total": 0}
            await memory.regenerate_summary_and_wait()
    else:
        yield {"type": "note", "message": "Membro not reachable — chats imported "
               "locally; memory seeding will happen as you revisit chats."}

    yield {"type": "done", **counts}
