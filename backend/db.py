"""SQLite storage: chats, messages, participants, projects, attachments,
tool events, voice usage, app settings.

Deliberately NO memory tables and NO messages_fts here: the durable memory
ledger and full-text history search live in Membro, the companion memory
service (see backend/memory_client.py). This database only tracks the
`ingested_upto` watermark per chat for the transcript handoff.

Fresh schema (PRAGMA user_version = 1); no legacy migrations.
"""

import os
import shutil
import sqlite3
import tarfile
import time
from pathlib import Path

from . import provenance
from .config import DEFAULT_PRICING, ROOT, provenance_for

SCHEMA_VERSION = 18  # v2: archived_at · v3: voice_gain · v4: import_uuid · v5: chats.code_enabled · v6: inbound_events · v7: participants.lifecycle (onboarding lifecycle) · v8: guest_jobs (async guest execution) · v9: voice_turn_traces (per-turn latency instrumentation) · v10: utility_usage (Haiku/utility-model spend attribution) · v11: utility_usage.provenance (cost provenance recorded at write time, not recomputed later) · v12: guest_jobs.status_label/status_at (ephemeral work-status ping, queryable on reconnect instead of a persisted fake message) · v13: messages.voice_labels/labels_updated_at (room-mode retro speaker labels, #28 phase 1) · v14: chats.room_mode + room_roster + room_flags (multi-human room mode, #28 phase 2) · v15: messages.voice_turn_id (exact diarization label targeting, #28 phase 3) · v16: chats.ambient_off (owner disarmed ambient room detection, #28) · v17: command_acks (machine tooling acknowledges consumed slash commands, #58) · v18: messages.web_sources (web-touched rounds stamped for the memory hold, #138)

# Configurable at runtime (tests, custom data dirs) via configure().
# Read DIRECTLY from the environment at import time, outside the Settings
# machinery, so it needs its own new-name-then-old-name chain: miss the old
# name here and a custom-data-dir install boots against a fresh empty
# database, which presents as total data loss. MMC_DATA_DIR support ends in
# v0.3 with the rest of the deprecated prefix; config.deprecated_env_vars
# reports it in the startup warning because DATA_DIR is a Settings field name,
# even though this module reads the variable directly.
DATA_DIR = Path(os.environ.get("CROSSBAND_DATA_DIR")
                or os.environ.get("MMC_DATA_DIR") or (ROOT / "data"))
DB_PATH = DATA_DIR / "chat.db"
ATTACH_DIR = DATA_DIR / "attachments"
BACKUP_DIR = DATA_DIR / "backups"
LOCK_PATH = DATA_DIR / ".lock"

BACKUP_KEEP = 14
MIRROR_DIR: Path | None = None
MIRROR_KEEP = 7


def configure(data_dir, backup_keep: int = 14, mirror_dir: str = "", mirror_keep: int = 7):
    """Point the module at a data directory. Called once from create_app()."""
    global DATA_DIR, DB_PATH, ATTACH_DIR, BACKUP_DIR, LOCK_PATH
    global BACKUP_KEEP, MIRROR_DIR, MIRROR_KEEP
    DATA_DIR = Path(data_dir)
    DB_PATH = DATA_DIR / "chat.db"
    ATTACH_DIR = DATA_DIR / "attachments"
    BACKUP_DIR = DATA_DIR / "backups"
    LOCK_PATH = DATA_DIR / ".lock"
    BACKUP_KEEP = backup_keep
    MIRROR_DIR = Path(mirror_dir) if mirror_dir else None
    MIRROR_KEEP = mirror_keep


SCHEMA = """
CREATE TABLE IF NOT EXISTS projects(
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  name TEXT NOT NULL,
  instructions TEXT NOT NULL DEFAULT '',
  memory TEXT NOT NULL DEFAULT '',
  created_at REAL NOT NULL,
  import_uuid TEXT                              -- provider-export idempotency
);
CREATE TABLE IF NOT EXISTS chats(
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  project_id INTEGER REFERENCES projects(id) ON DELETE SET NULL,
  title TEXT NOT NULL DEFAULT 'New chat',
  summary TEXT NOT NULL DEFAULT '',
  summary_upto INTEGER NOT NULL DEFAULT 0,      -- rolling-summary fold watermark
  distilled_upto INTEGER NOT NULL DEFAULT 0,    -- project-memory distill watermark
  title_upto INTEGER NOT NULL DEFAULT 0,        -- 0=auto; >0 titled thru id; -1 user-locked
  ingested_upto INTEGER NOT NULL DEFAULT 0,     -- memory-service /ingest watermark
  next_first TEXT NOT NULL DEFAULT 'claude',
  voice_mode INTEGER NOT NULL DEFAULT 0,
  web_enabled INTEGER NOT NULL DEFAULT 1,
  memory_enabled INTEGER NOT NULL DEFAULT 1,
  code_enabled INTEGER NOT NULL DEFAULT 0,      -- summon_claude_code guest (opt-in per chat)
  room_mode INTEGER NOT NULL DEFAULT 0,         -- multi-human room mode (#28 phase 2):
                                                -- flipped by a spoken introduction or the
                                                -- explicit toggle; durable per chat
  ambient_off INTEGER NOT NULL DEFAULT 0,       -- owner said "solo mode" (#28 ambient):
                                                -- suppresses automatic arming until an
                                                -- explicit re-enable; durable per chat
  archived_at REAL,                             -- hidden from the sidebar; nothing deleted
  created_at REAL NOT NULL,
  updated_at REAL NOT NULL,
  import_uuid TEXT                              -- provider-export idempotency
);
CREATE TABLE IF NOT EXISTS messages(
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  chat_id INTEGER NOT NULL REFERENCES chats(id) ON DELETE CASCADE,
  speaker TEXT NOT NULL,
  content TEXT NOT NULL DEFAULT '',
  usage_json TEXT,
  created_at REAL NOT NULL,
  import_uuid TEXT,                             -- provider-export idempotency
  -- Room mode (#28 phase 1): retro-attached diarization result for a user
  -- turn - JSON {"clusters": [...], "labels": ["Voice 1", ...]}, written a
  -- second or two AFTER insert by the parallel pass. Empty = unlabelled
  -- (the normal case, and the silent result of any diarization failure).
  -- The transcript's content itself stays immutable; this is audio metadata.
  voice_labels TEXT NOT NULL DEFAULT '',
  labels_updated_at REAL NOT NULL DEFAULT 0,    -- live-events cursor, like guest_jobs.updated_at
  -- #28 phase 3: the client's voice-trace turn id, persisted at insert so the
  -- diarization pass can key its label write to the EXACT message the
  -- utterance produced instead of guessing by time window. Empty for typed
  -- messages and for voice clients that predate the field.
  voice_turn_id TEXT NOT NULL DEFAULT '',
  -- #138 slice 4: json list of web sources (domains, or "web-search") the
  -- round that produced this assistant message read, or ''. Rides /ingest so
  -- the memory service can hold web-derived facts for review.
  web_sources TEXT NOT NULL DEFAULT ''
);
CREATE INDEX IF NOT EXISTS idx_messages_voice_turn
  ON messages(voice_turn_id) WHERE voice_turn_id != '';
CREATE INDEX IF NOT EXISTS idx_messages_labels_updated
  ON messages(labels_updated_at);
CREATE TABLE IF NOT EXISTS attachments(
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  message_id INTEGER REFERENCES messages(id) ON DELETE CASCADE,
  filename TEXT NOT NULL,
  stored_name TEXT NOT NULL,
  mime TEXT NOT NULL,
  size INTEGER NOT NULL,
  created_at REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS participants(
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  slug TEXT NOT NULL UNIQUE,
  name TEXT NOT NULL,
  provider TEXT NOT NULL,            -- 'anthropic' | 'openai' (covers OpenAI-compatible APIs)
  model TEXT NOT NULL,
  base_url TEXT,                     -- optional override for OpenAI-compatible endpoints
  api_key_env TEXT,                  -- NAME of the env var holding the key (never the key)
  system_prompt TEXT NOT NULL DEFAULT '',
  color TEXT NOT NULL DEFAULT '#a1a1aa',
  voice_id TEXT NOT NULL DEFAULT '',
  voice_gain REAL NOT NULL DEFAULT 1.0,  -- playback volume 0.2–1.0 (normalize loud voices)
  reasoning_effort TEXT NOT NULL DEFAULT '',
  enabled INTEGER NOT NULL DEFAULT 1,
  -- Onboarding lifecycle: 'trial' = added for manual evaluation
  -- only, never selectable-by-default or auto-selectable; 'onboarded' = has an
  -- explicit cost-provenance record and is a normal seat. Defaults to 'trial'
  -- conservatively so no model is ever silently treated as onboarded.
  lifecycle TEXT NOT NULL DEFAULT 'trial',
  position INTEGER NOT NULL DEFAULT 0,
  created_at REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS chat_participants(
  chat_id INTEGER NOT NULL REFERENCES chats(id) ON DELETE CASCADE,
  participant_id INTEGER NOT NULL REFERENCES participants(id) ON DELETE CASCADE,
  PRIMARY KEY(chat_id, participant_id)
);
CREATE TABLE IF NOT EXISTS settings(
  key TEXT PRIMARY KEY,
  value TEXT NOT NULL DEFAULT ''
);
CREATE TABLE IF NOT EXISTS tool_events(
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  message_id INTEGER NOT NULL REFERENCES messages(id) ON DELETE CASCADE,
  tool TEXT NOT NULL,
  input_json TEXT NOT NULL DEFAULT '{}',
  output_text TEXT NOT NULL DEFAULT '',
  created_at REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS voice_usage(
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  chat_id INTEGER REFERENCES chats(id) ON DELETE CASCADE,
  kind TEXT NOT NULL,             -- 'tts' (units = characters) | 'stt' (units = seconds)
  units REAL NOT NULL,
  cost REAL,
  created_at REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS utility_usage(
  -- Cheap-model spend attribution. chat_memory.py's rolling
  -- summaries/auto-titles/project-distillation calls never go through
  -- db.insert_message (they write directly to chats/projects, not the
  -- transcript) so they had no cost record at all - invisible utility-model
  -- (typically Haiku) spend. This table is their one, mirroring voice_usage.
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  chat_id INTEGER REFERENCES chats(id) ON DELETE CASCADE,
  kind TEXT NOT NULL,              -- 'summarize' | 'title' | 'distill'
  model TEXT NOT NULL DEFAULT '',
  input_tokens INTEGER NOT NULL DEFAULT 0,
  output_tokens INTEGER NOT NULL DEFAULT 0,
  cost REAL,
  -- Cost provenance SOURCE recorded AT WRITE TIME, e.g. 'rate_card_estimate':
  -- the same immutable-history discipline as every other cost source. NULL on
  -- rows written before this column existed; accounting.py falls back to
  -- resolving against the live rate card for those only, never for a row that
  -- already recorded its own provenance.
  provenance TEXT,
  created_at REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS inbound_events(
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  source TEXT NOT NULL,                -- producer name (badge label; stored as ext:<source> on the message)
  dedupe_key TEXT NOT NULL,            -- producer-chosen idempotency key
  chat_id INTEGER NOT NULL REFERENCES chats(id) ON DELETE CASCADE,
  message_id INTEGER REFERENCES messages(id) ON DELETE SET NULL,
  created_at REAL NOT NULL,
  UNIQUE(source, dedupe_key)
);
CREATE TABLE IF NOT EXISTS command_acks(
  -- Machine tooling acknowledging a slash command it consumed (#58): the
  -- watcher names the command's message id on its first notice, and the
  -- dead-man warning for that command stands down. Core still assigns no
  -- meaning to any command - this records only that SOMETHING read it.
  message_id INTEGER PRIMARY KEY REFERENCES messages(id) ON DELETE CASCADE,
  acked_at REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS guest_jobs(
  -- Async Claude Code guest execution: a guest run is a BACKGROUND job,
  -- decoupled from the voice/turn lifecycle that summoned it. This table is the
  -- durable, connection-independent status store - surviving a client
  -- disconnect, tab backgrounding, or a process restart mid-run (the same
  -- persistence gap tab/backgrounding voice playback needs, so it is built
  -- to be reused there).
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  chat_id INTEGER NOT NULL REFERENCES chats(id) ON DELETE CASCADE,
  status TEXT NOT NULL DEFAULT 'running',   -- running | completed | failed | cancelled
  kind TEXT NOT NULL DEFAULT '',            -- completion path: 'blocker' | 'result' | ''
  task TEXT NOT NULL DEFAULT '',
  repo TEXT NOT NULL DEFAULT '',
  mode TEXT NOT NULL DEFAULT 'investigate', -- investigate | implement
  requested_by TEXT NOT NULL DEFAULT '',    -- slug of the model that summoned it
  session_id TEXT NOT NULL DEFAULT '',      -- guest session handle (continue_last)
  message_id INTEGER REFERENCES messages(id) ON DELETE SET NULL,  -- the persisted reply
  error TEXT NOT NULL DEFAULT '',
  step_count INTEGER NOT NULL DEFAULT 0,    -- tool calls so far (chip's expand view)
  status_label TEXT NOT NULL DEFAULT '',    -- latest ephemeral check-in label
                                             -- (e.g. "Investigating the code"); NOT a
                                             -- chat message -- a reconnecting client
                                             -- reads this instead of replaying one
  status_at REAL NOT NULL DEFAULT 0,        -- when status_label was last set
  created_at REAL NOT NULL,
  updated_at REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS room_roster(
  -- Who is in the room for one chat (#28 phase 2). A row is appended by a
  -- confirmed spoken introduction (or the reassign path) and marked left on a
  -- confirmed departure; the cap (Settings.room_roster_max) counts PRESENT
  -- rows only. `name` is the display name the introduction used; `person_id`
  -- links the durable voice-anchor store entry (backend/anchors.py) once one
  -- exists - NULL means the anchor is still pending, so identification for
  -- this person stays uncertain. updated_at is the live-events cursor, like
  -- guest_jobs.updated_at.
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  chat_id INTEGER NOT NULL REFERENCES chats(id) ON DELETE CASCADE,
  name TEXT NOT NULL,
  person_id TEXT NOT NULL DEFAULT '',   -- anchor-store person id ('' = anchor pending)
  status TEXT NOT NULL DEFAULT 'present',  -- 'present' | 'left'
  joined_at REAL NOT NULL,
  left_at REAL,
  updated_at REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS room_flags(
  -- Attribution doubts the UI must surface (#28 phase 2), one row each:
  --   'unknown_voice' - a diarization cluster matched nobody's anchor
  --                     ("someone new is speaking - who?");
  --   'mismatch'      - the LLM cross-check thinks a turn's label disagrees
  --                     with what was said ("turn labelled X reads like
  --                     someone else"). NEVER changes a label - it exists to
  --                     be resolved by a correction or dismissed.
  -- Content-free by design: label/suspected hold roster NAMES only, never
  -- transcript text. updated_at is the live-events cursor.
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  chat_id INTEGER NOT NULL REFERENCES chats(id) ON DELETE CASCADE,
  message_id INTEGER REFERENCES messages(id) ON DELETE SET NULL,
  kind TEXT NOT NULL,                   -- 'unknown_voice' | 'mismatch'
  label TEXT NOT NULL DEFAULT '',       -- the turn's current label (a name/ordinal)
  suspected TEXT NOT NULL DEFAULT '',   -- who the cross-check thinks spoke ('' = unknown)
  resolved_at REAL,                     -- set by a correction/dismissal; NULL = open
  created_at REAL NOT NULL,
  updated_at REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_room_roster_chat ON room_roster(chat_id);
CREATE INDEX IF NOT EXISTS idx_room_roster_updated ON room_roster(updated_at);
CREATE INDEX IF NOT EXISTS idx_room_flags_chat ON room_flags(chat_id);
CREATE INDEX IF NOT EXISTS idx_room_flags_updated ON room_flags(updated_at);
CREATE TABLE IF NOT EXISTS voice_turn_traces(
  -- Per-turn voice latency instrumentation. One row per measured STAGE
  -- of one voice turn, correlated by turn_id (a client-owned opaque id). This
  -- is deliberately CONTENT-FREE: it stores stage names, durations, and the
  -- provider/model/voice identifiers that produced them - never a transcript,
  -- a reply, or any spoken words. That privacy floor is enforced on ingest
  -- (voice_trace.sanitize), so the table can never accrue sensitive text even
  -- if a future caller tried to send it.
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  turn_id TEXT NOT NULL,               -- correlation id across a turn's stages
  chat_id INTEGER REFERENCES chats(id) ON DELETE CASCADE,
  stage TEXT NOT NULL,                 -- allowlisted stage name (voice_trace.ALLOWED_STAGES)
  ms REAL NOT NULL,                    -- stage duration in milliseconds (>= 0)
  provider TEXT NOT NULL DEFAULT '',   -- model provider (anthropic|openai) where applicable
  model TEXT NOT NULL DEFAULT '',      -- model id where applicable
  tts_provider TEXT NOT NULL DEFAULT '', -- voice/TTS provider (e.g. elevenlabs)
  speaker TEXT NOT NULL DEFAULT '',    -- participant slug for per-speaker stages
  created_at REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_voice_traces_turn ON voice_turn_traces(turn_id);
CREATE INDEX IF NOT EXISTS idx_voice_traces_created ON voice_turn_traces(created_at);
CREATE INDEX IF NOT EXISTS idx_guest_jobs_chat ON guest_jobs(chat_id);
CREATE INDEX IF NOT EXISTS idx_guest_jobs_updated ON guest_jobs(updated_at);
CREATE INDEX IF NOT EXISTS idx_messages_chat ON messages(chat_id);
CREATE INDEX IF NOT EXISTS idx_attachments_msg ON attachments(message_id);
CREATE INDEX IF NOT EXISTS idx_tool_events_msg ON tool_events(message_id);
CREATE INDEX IF NOT EXISTS idx_voice_usage_chat ON voice_usage(chat_id);
CREATE INDEX IF NOT EXISTS idx_utility_usage_chat ON utility_usage(chat_id);
"""


DEFAULT_SHARED_INSTRUCTIONS = (
    "Talk like a sharp friend in a group chat - not a consultant, not a coach. "
    "Default reply: ONE to THREE sentences. One thought per message; this is a "
    "conversation, so leave room - don't say everything you know, and never lecture. "
    "No lists, no headings, no wind-ups ('Here's the thing…'), no recapping the "
    "question. Research/tool results: lead with THE answer in one sentence, max two "
    "supporting facts. If something truly needs length, say the headline and ask "
    "first. Write long ONLY when explicitly asked ('go deep', 'write it up')."
)


def connect():
    os.makedirs(ATTACH_DIR, exist_ok=True)
    con = sqlite3.connect(DB_PATH)
    con.row_factory = sqlite3.Row
    con.execute("PRAGMA foreign_keys = ON")
    # don't fail with "database is locked" when a reply stream, a background
    # fold, and the UI hit the file at once - wait politely instead
    con.execute("PRAGMA busy_timeout = 5000")
    return con


def backup_database():
    """Consistent snapshot via SQLite's online backup API (safe while the app
    is running and mid-write). Keeps the newest BACKUP_KEEP copies, and mirrors
    completed snapshots to an optional secondary directory (never the live DB -
    sync daemons watching a live WAL database cause lock hangs)."""
    if not os.path.exists(DB_PATH):
        return None
    os.makedirs(BACKUP_DIR, exist_ok=True)
    dest = BACKUP_DIR / f"chat-{time.strftime('%Y%m%d-%H%M%S')}.db"
    src = sqlite3.connect(DB_PATH)
    dst = sqlite3.connect(dest)
    try:
        with dst:
            src.backup(dst)
    finally:
        src.close()
        dst.close()
    backups = sorted(f for f in os.listdir(BACKUP_DIR) if f.endswith(".db"))
    for old in backups[:-BACKUP_KEEP]:
        try:
            os.remove(BACKUP_DIR / old)
        except OSError:
            pass
    _mirror_snapshot(dest)
    _backup_voice_anchors(dest.name.replace("chat-", "").replace(".db", ""))
    return str(dest)


def _backup_voice_anchors(stamp: str):
    """#33: the learned voices ride the same snapshot cycle as the database.
    voice_anchors/ (the clip files and their index) was in no backup at all,
    so losing the data directory forgot every learned voice while the chats
    survived. Each cycle tars the whole directory as voices-<stamp>.tar
    beside the DB snapshot, owner-only, same retention, same mirror.

    Best-effort and point-in-time like any file backup: a clip evicted
    mid-tar can leave one dangling index entry in the copy, which the store
    already tolerates on read (clip_path checks the file). Never blocks or
    breaks a startup."""
    src = DATA_DIR / "voice_anchors"
    try:
        if not src.is_dir() or not any(src.iterdir()):
            return
        os.makedirs(BACKUP_DIR, exist_ok=True)
        dest = BACKUP_DIR / f"voices-{stamp}.tar"
        tmp = BACKUP_DIR / f".voices-{stamp}.tar.part"
        with tarfile.open(tmp, "w") as tar:
            tar.add(src, arcname="voice_anchors")
        os.chmod(tmp, 0o600)
        os.replace(tmp, dest)
        kept = sorted(f for f in os.listdir(BACKUP_DIR)
                      if f.startswith("voices-") and f.endswith(".tar"))
        for old_f in kept[:-BACKUP_KEEP]:
            try:
                os.remove(BACKUP_DIR / old_f)
            except OSError:
                pass
        _mirror_snapshot(dest)
    except Exception:
        pass


def _mirror_snapshot(snapshot_path):
    """Best-effort copy of a COMPLETED snapshot to a secondary dir; never
    blocks or breaks startup."""
    if not MIRROR_DIR or not snapshot_path:
        return
    try:
        os.makedirs(MIRROR_DIR, exist_ok=True)
        shutil.copy2(snapshot_path, MIRROR_DIR / Path(snapshot_path).name)
        prefix = "voices-" if Path(snapshot_path).name.startswith("voices-") \
            else "chat-"
        kept = sorted(f for f in os.listdir(MIRROR_DIR)
                      if f.startswith(prefix))
        for old in kept[:-MIRROR_KEEP]:
            try:
                os.remove(MIRROR_DIR / old)
            except OSError:
                pass
    except Exception:
        pass


def init(settings=None):
    """Create/verify the schema and seed defaults. Fresh DB only - no legacy
    migration ladder; user_version stamps the schema for future migrations."""
    backup_database()  # pre-init snapshot, every startup (no-op on first run)
    con = connect()
    # WAL: survives abrupt power-off/kill without corruption, and lets readers
    # and the writer coexist
    con.execute("PRAGMA journal_mode = WAL")
    version = con.execute("PRAGMA user_version").fetchone()[0]
    if version > SCHEMA_VERSION:
        con.close()
        raise RuntimeError(
            f"data/chat.db has schema version {version}, newer than this build "
            f"({SCHEMA_VERSION}) - refusing to open it. Update the app."
        )
    if version == 1:  # v1 → v2: archive flag (CREATE IF NOT EXISTS won't add it)
        con.execute("ALTER TABLE chats ADD COLUMN archived_at REAL")
    if 1 <= version <= 2:  # v3: per-agent voice volume (normalize loud voices)
        con.execute("ALTER TABLE participants ADD COLUMN voice_gain REAL NOT NULL DEFAULT 1.0")
    if 1 <= version <= 3:  # v4: provider-export importer idempotency keys
        con.execute("ALTER TABLE projects ADD COLUMN import_uuid TEXT")
        con.execute("ALTER TABLE chats ADD COLUMN import_uuid TEXT")
        con.execute("ALTER TABLE messages ADD COLUMN import_uuid TEXT")
    if 1 <= version <= 4:  # v5: per-chat Claude Code guest toggle (off by default)
        con.execute("ALTER TABLE chats ADD COLUMN code_enabled INTEGER NOT NULL DEFAULT 0")
    if 1 <= version <= 17:  # v18: web_sources stamp on messages (#138 slice 4)
        # Guard on the live table: a stamped-but-minimal db (migration
        # fixtures) has no messages table yet - the executescript below
        # creates it with the column already in place.
        mcols = {r[1] for r in con.execute("PRAGMA table_info(messages)")}
        if mcols and "web_sources" not in mcols:
            con.execute("ALTER TABLE messages ADD COLUMN web_sources "
                        "TEXT NOT NULL DEFAULT ''")
    # v6: inbound_events - new table, created by the executescript below
    if 1 <= version <= 6:  # v7: per-seat onboarding lifecycle. The
        # column DEFAULTS to 'trial' (conservative), so the ALTER lands every
        # pre-existing row on 'trial'. We then grandfather ONLY the rows whose
        # model has a KNOWN cost provenance up to 'onboarded' - i.e. exactly the
        # gate the /api/participants PATCH endpoint enforces (409 unless
        # provenance_for() resolves a known source). Rationale: trial is a real
        # behavioral gate (trial seats don't auto-speak - engine.pick_responders),
        # so a priced/sourced seat must keep working (no regression), but an
        # existing UNPRICED/custom/local seat must NOT be waved through to
        # onboarded for free - it goes through the same gate as a freshly-added
        # unsourced seat and stays a manually-invokable trial. (Skip if the table
        # isn't there yet; executescript creates the column on a fresh DB.)
        # NOTE: provenance_for() can now also resolve a *seat* - a keyless
        # loopback endpoint is self-hosted - but this backfill deliberately keeps
        # asking by MODEL only. A local seat is exactly the case that should land
        # on trial and be promoted deliberately, which is what a freshly-added
        # local seat does too; grandfathering them here would auto-onboard seats
        # nobody has evaluated.
        if con.execute("SELECT 1 FROM sqlite_master WHERE type='table' "
                       "AND name='participants'").fetchone():
            con.execute("ALTER TABLE participants ADD COLUMN lifecycle "
                        "TEXT NOT NULL DEFAULT 'trial'")
            pricing = settings.pricing if settings is not None else DEFAULT_PRICING
            for row in con.execute("SELECT id, model FROM participants").fetchall():
                if provenance_for(row["model"], pricing)["source"] != provenance.UNKNOWN:
                    con.execute("UPDATE participants SET lifecycle='onboarded' "
                                "WHERE id=?", (row["id"],))
    # v8: guest_jobs - a NEW table, created by the CREATE IF NOT EXISTS in
    # the executescript below on both fresh and upgraded DBs; no ALTER needed.
    # v9: voice_turn_traces - likewise a NEW table, created by the
    # executescript below; no ALTER, nothing to backfill.
    # v10: utility_usage - likewise a NEW table, created by the
    # executescript below; no ALTER, nothing to backfill.
    if 1 <= version <= 10:  # v11: utility_usage.provenance -
        # persist cost provenance AT WRITE TIME instead of recomputing it from
        # the live rate card at read time, matching every other cost source
        # (engine.py turns, guest.py turns) so a later rate-card edit can never
        # rewrite an already-recorded row's history. NULL on rows written
        # before this migration; accounting.iter_cost_events falls back to
        # resolving against the live rate card for those, exactly as it always
        # did before provenance was recorded at all.
        if con.execute("SELECT 1 FROM sqlite_master WHERE type='table' "
                       "AND name='utility_usage'").fetchone():
            con.execute("ALTER TABLE utility_usage ADD COLUMN provenance TEXT")
    if 1 <= version <= 11:  # v12: guest_jobs.status_label/status_at -
        # the periodic guest-job ping stopped inserting a fake chat message;
        # its latest check-in now lives here instead, so a reconnecting
        # client reads current state (GET /api/chats/{id}/guest_jobs) rather
        # than replaying history that never actually happened in the chat.
        if con.execute("SELECT 1 FROM sqlite_master WHERE type='table' "
                       "AND name='guest_jobs'").fetchone():
            con.execute("ALTER TABLE guest_jobs ADD COLUMN status_label "
                        "TEXT NOT NULL DEFAULT ''")
            con.execute("ALTER TABLE guest_jobs ADD COLUMN status_at "
                        "REAL NOT NULL DEFAULT 0")
    if 1 <= version <= 12:  # v13: room-mode retro speaker labels (#28 phase 1)
        # Both default-empty/zero, so every existing row is simply "unlabelled"
        # - exactly what it was before the columns existed. The index the
        # executescript adds backs the live-events stream's labels cursor.
        if con.execute("SELECT 1 FROM sqlite_master WHERE type='table' "
                       "AND name='messages'").fetchone():
            con.execute("ALTER TABLE messages ADD COLUMN voice_labels "
                        "TEXT NOT NULL DEFAULT ''")
            con.execute("ALTER TABLE messages ADD COLUMN labels_updated_at "
                        "REAL NOT NULL DEFAULT 0")
    if 1 <= version <= 13:  # v14: room mode goes multi-human (#28 phase 2).
        # chats.room_mode defaults 0, so every existing chat simply has room
        # mode off - exactly its pre-migration behaviour. room_roster and
        # room_flags are NEW tables, created by the executescript below.
        if con.execute("SELECT 1 FROM sqlite_master WHERE type='table' "
                       "AND name='chats'").fetchone():
            con.execute("ALTER TABLE chats ADD COLUMN room_mode "
                        "INTEGER NOT NULL DEFAULT 0")
    if 1 <= version <= 14:  # v15: exact diarization label targeting (#28 phase 3)
        # Default-empty, so every existing row simply has no recorded turn id
        # - the diarization pass then behaves exactly as before for it. The
        # partial index the executescript adds backs the exact-match lookup.
        if con.execute("SELECT 1 FROM sqlite_master WHERE type='table' "
                       "AND name='messages'").fetchone():
            con.execute("ALTER TABLE messages ADD COLUMN voice_turn_id "
                        "TEXT NOT NULL DEFAULT ''")
    if 1 <= version <= 15:  # v16: ambient room detection off-switch (#28)
        # Defaults 0, so every existing chat starts with ambient allowed -
        # exactly the behaviour before the flag existed. Set to 1 only when
        # the owner explicitly says "solo mode"; cleared on any re-enable.
        if con.execute("SELECT 1 FROM sqlite_master WHERE type='table' "
                       "AND name='chats'").fetchone():
            con.execute("ALTER TABLE chats ADD COLUMN ambient_off "
                        "INTEGER NOT NULL DEFAULT 0")
    con.executescript(SCHEMA)
    con.execute(f"PRAGMA user_version = {SCHEMA_VERSION}")

    # seed the default roster on first run
    if not con.execute("SELECT 1 FROM participants LIMIT 1").fetchone():
        cfg = settings.as_cfg() if settings is not None else {}
        # The out-of-the-box roster is 'onboarded': these are the product's
        # default always-on seats with priced models, not unevaluated trials.
        # Seeding them trial would mean a fresh install where nobody
        # auto-replies, because of the onboarding gate - a broken first run.
        # Only user-added seats start as trial.
        con.execute(
            "INSERT INTO participants(slug, name, provider, model, color, lifecycle, position, created_at) "
            "VALUES('claude', ?, 'anthropic', ?, '#fb923c', 'onboarded', 0, ?)",
            (cfg.get("claude_display_name", "Claude"),
             cfg.get("anthropic_model", "claude-opus-4-8"), now()),
        )
        con.execute(
            "INSERT INTO participants(slug, name, provider, model, color, lifecycle, position, created_at) "
            "VALUES('gpt', ?, 'openai', ?, '#34d399', 'onboarded', 1, ?)",
            (cfg.get("gpt_display_name", "GPT"),
             cfg.get("openai_model", "gpt-5.1"), now()),
        )
    con.execute(
        "INSERT OR IGNORE INTO settings(key, value) VALUES('shared_instructions', ?)",
        (DEFAULT_SHARED_INSTRUCTIONS,),
    )
    con.commit()
    con.close()


# ---------- helpers ----------

def now():
    return time.time()


def row_to_dict(row):
    return dict(row) if row is not None else None


def get_participants(con, enabled_only=False):
    q = "SELECT * FROM participants"
    if enabled_only:
        q += " WHERE enabled=1"
    return [dict(r) for r in con.execute(q + " ORDER BY position, id")]


def get_chat_participants(con, chat_id):
    return [dict(r) for r in con.execute(
        "SELECT p.* FROM participants p JOIN chat_participants cp ON cp.participant_id=p.id "
        "WHERE cp.chat_id=? AND p.enabled=1 ORDER BY p.position, p.id", (chat_id,))]


def participant_names(con):
    return {r["slug"]: r["name"] for r in con.execute("SELECT slug, name FROM participants")}


def get_setting(con, key, default=""):
    row = con.execute("SELECT value FROM settings WHERE key=?", (key,)).fetchone()
    return row["value"] if row else default


def set_setting(con, key, value):
    con.execute(
        "INSERT INTO settings(key, value) VALUES(?,?) "
        "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
        (key, value),
    )


def log_voice_usage(con, chat_id, kind, units, cost):
    con.execute(
        "INSERT INTO voice_usage(chat_id, kind, units, cost, created_at) VALUES(?,?,?,?,?)",
        (chat_id, kind, units, cost, now()),
    )


def chat_voice_totals(con, chat_id):
    out = {"tts_chars": 0, "stt_seconds": 0, "cost": 0.0}
    for r in con.execute(
        "SELECT kind, SUM(units) AS u, SUM(COALESCE(cost,0)) AS c FROM voice_usage "
        "WHERE chat_id=? GROUP BY kind", (chat_id,)
    ):
        if r["kind"] == "tts":
            out["tts_chars"] = int(r["u"] or 0)
        elif r["kind"] == "stt":
            out["stt_seconds"] = round(r["u"] or 0, 1)
        out["cost"] += r["c"] or 0
    return out


def log_utility_usage(con, chat_id, kind, model, input_tokens, output_tokens, cost,
                       provenance=None):
    """Persist one utility-model call's cost - the rolling-summary/
    auto-title/project-distillation calls in chat_memory.py never go through
    insert_message, so this is their only cost record. `cost` may be None
    (model absent from the pricing table) - recorded as-is, same "not
    tracked, not $0.00" honesty as voice_usage.

    `provenance` is the cost provenance SOURCE string (e.g.
    'rate_card_estimate') resolved AT CALL TIME by the caller and recorded
    immutably alongside the cost - mirroring the discipline every other cost
    source follows, so a later rate-card edit can
    never rewrite what this row's provenance meant when it was logged.
    Defaults to None for callers that don't resolve one (e.g. a bare test
    row); accounting.py falls back to recomputing from the live rate card for
    those, same as it does for any pre-migration row."""
    con.execute(
        "INSERT INTO utility_usage(chat_id, kind, model, input_tokens, output_tokens, "
        "cost, provenance, created_at) VALUES(?,?,?,?,?,?,?,?)",
        (chat_id, kind, model, input_tokens, output_tokens, cost, provenance, now()),
    )


def insert_voice_trace(con, turn_id, chat_id, stage, ms, *, provider="",
                       model="", tts_provider="", speaker=""):
    """Persist ONE stage of a voice turn's latency trace. Callers must
    pass already-sanitized values (see voice_trace.sanitize) - this is a thin
    writer, not a validator. Content-free by construction: no transcript, reply,
    or spoken text is ever a parameter here."""
    con.execute(
        "INSERT INTO voice_turn_traces(turn_id, chat_id, stage, ms, provider, "
        "model, tts_provider, speaker, created_at) VALUES(?,?,?,?,?,?,?,?,?)",
        (turn_id, chat_id, stage, ms, provider, model, tts_provider, speaker, now()),
    )


def get_voice_traces(con, since=None, limit=2000):
    """Recent trace rows (newest first), optionally only those created at/after
    `since` (epoch seconds). Bounded by `limit` so a diagnostics call can never
    scan an unbounded table."""
    q = "SELECT * FROM voice_turn_traces"
    args = []
    if since is not None:
        q += " WHERE created_at >= ?"
        args.append(since)
    q += " ORDER BY id DESC LIMIT ?"
    args.append(int(limit))
    return [dict(r) for r in con.execute(q, args)]


def prune_voice_traces(con, keep_days=30, keep_max=50000):
    """Retention for the trace table: drop rows older than `keep_days`, then cap
    the total at `keep_max` newest rows. Diagnostics data has no long-term value,
    so it self-limits rather than growing forever."""
    cutoff = now() - keep_days * 86400
    con.execute("DELETE FROM voice_turn_traces WHERE created_at < ?", (cutoff,))
    con.execute(
        "DELETE FROM voice_turn_traces WHERE id NOT IN "
        "(SELECT id FROM voice_turn_traces ORDER BY id DESC LIMIT ?)", (int(keep_max),))


def get_chat_messages(con, chat_id):
    msgs = [dict(r) for r in con.execute(
        "SELECT * FROM messages WHERE chat_id=? ORDER BY id", (chat_id,))]
    by_id = {m["id"]: m for m in msgs}
    for m in msgs:
        m["attachments"] = []
        m["tool_events"] = []
    for a in con.execute(
        "SELECT a.* FROM attachments a JOIN messages m ON a.message_id=m.id "
        "WHERE m.chat_id=? ORDER BY a.id", (chat_id,),
    ):
        by_id[a["message_id"]]["attachments"].append(dict(a))
    for t in con.execute(
        "SELECT t.* FROM tool_events t JOIN messages m ON t.message_id=m.id "
        "WHERE m.chat_id=? ORDER BY t.id", (chat_id,),
    ):
        by_id[t["message_id"]]["tool_events"].append(dict(t))
    return msgs


def get_messages_after(con, since, chat_id=None):
    """Rows with id > `since` - the single global monotonic watermark the
    live-events stream and its DB catch-up both key off (message ids
    are AUTOINCREMENT across ALL chats, not per-chat, so one integer is a
    complete cursor for "everything I haven't seen yet").

    chat_id=None (the events-stream case): minimal rows (id + chat_id only) -
    the global stream deliberately never carries message content.
    chat_id=<int> (the messages-after endpoint case): full rows, same
    attachments/tool_events shape as get_chat_messages, scoped to one chat -
    what the frontend fetches for the chat it's actually looking at."""
    if chat_id is None:
        # speaker is metadata, not content (#64): the voice client uses it to
        # decide whether a message could belong to a round worth replaying
        # aloud (a participant's narration) or never could (system notices,
        # guest job output, external ingest). Transcript text still never
        # rides this stream.
        return [dict(r) for r in con.execute(
            "SELECT id, chat_id, speaker FROM messages WHERE id > ? ORDER BY id",
            (since,))]
    msgs = [dict(r) for r in con.execute(
        "SELECT * FROM messages WHERE chat_id=? AND id > ? ORDER BY id",
        (chat_id, since))]
    by_id = {m["id"]: m for m in msgs}
    for m in msgs:
        m["attachments"] = []
        m["tool_events"] = []
    if msgs:
        placeholders = ",".join("?" * len(msgs))
        ids = tuple(by_id)
        for a in con.execute(
            f"SELECT * FROM attachments WHERE message_id IN ({placeholders}) ORDER BY id", ids):
            by_id[a["message_id"]]["attachments"].append(dict(a))
        for t in con.execute(
            f"SELECT * FROM tool_events WHERE message_id IN ({placeholders}) ORDER BY id", ids):
            by_id[t["message_id"]]["tool_events"].append(dict(t))
    return msgs


def insert_message(con, chat_id, speaker, content, *, usage_json=None,
                    import_uuid=None, tool_events=None, attachment_ids=None,
                    notify=True, voice_turn_id="", voice_labels=None,
                    web_sources=None):
    """THE single write path for a LIVE message - every insert
    that should be pushed to a connected client goes through here, not a raw
    `INSERT INTO messages`. Centralizing this is what makes the live-events
    bus (backend/events.py) trustworthy: a future insert site literally cannot
    forget to wake connected clients, because there's only one way in.

    tool_events, if given, is the engine's own shape: a list of
    {"tool", "input" (dict), "output" (str)} - matched to engine.py's
    `live["tools"]` accumulator so callers don't need to pre-serialize.

    Commits BEFORE notifying, always, with no way for a caller to reorder
    that: the notify() call is the last thing this function does, after
    con.commit() has already returned, so a waiter that wakes up can always
    read the row it was woken for."""
    # voice_labels at INSERT (#28, twelfth field test): the identity check
    # usually finishes before this row exists, and writing the label
    # afterwards was always too late for the round that renders the
    # transcript on this very insert. Stamped here it is on the row before
    # anything can read it. labels_updated_at is stamped too, so the live
    # cursor treats it exactly like a later label write.
    labels_json = _json_dumps(voice_labels) if voice_labels else ""
    cur = con.execute(
        "INSERT INTO messages(chat_id, speaker, content, usage_json, created_at, "
        "import_uuid, voice_turn_id, voice_labels, labels_updated_at, web_sources) "
        "VALUES(?,?,?,?,?,?,?,?,?,?)",
        (chat_id, speaker, content, usage_json, now(), import_uuid,
         voice_turn_id or "", labels_json, now() if labels_json else 0,
         _json_dumps(sorted(web_sources)) if web_sources else ""),
    )
    msg_id = cur.lastrowid
    for att_id in (attachment_ids or []):
        con.execute(
            "UPDATE attachments SET message_id=? WHERE id=? AND message_id IS NULL",
            (msg_id, att_id))
    for rec in (tool_events or []):
        con.execute(
            "INSERT INTO tool_events(message_id, tool, input_json, output_text, created_at) "
            "VALUES(?,?,?,?,?)",
            (msg_id, rec["tool"], _json_dumps(rec["input"]), rec["output"], now()),
        )
    con.execute("UPDATE chats SET updated_at=? WHERE id=?", (now(), chat_id))
    con.commit()  # durable BEFORE anyone is told about it - see docstring

    msg = dict(con.execute("SELECT * FROM messages WHERE id=?", (msg_id,)).fetchone())
    msg["tool_events"] = [dict(r) for r in con.execute(
        "SELECT * FROM tool_events WHERE message_id=? ORDER BY id", (msg_id,))]
    msg["attachments"] = [dict(r) for r in con.execute(
        "SELECT * FROM attachments WHERE message_id=? ORDER BY id", (msg_id,))]

    if notify:
        # Lazy import: db.py stays a dependency-free leaf module (nothing else
        # in backend/ imports FROM db at import time, only INTO it), and
        # events.py imports db normally - a module-level import here would be
        # circular. notify_new_message() is a fire-and-forget, thread-safe
        # no-op when no stream has bound the event loop yet (e.g. plain unit
        # tests that never open /api/events/stream).
        from . import events
        events.notify_new_message()
    return msg


def set_message_voice_labels(con, message_id, payload):
    """THE update path for room-mode speaker labels (#28 phase 1) - the
    labels sibling of insert_message's insert monopoly. The parallel
    diarization pass attaches its result here and nowhere else, so a label
    write can never forget to wake connected clients.

    Deliberately narrow: it may touch ONLY voice_labels (audio metadata
    about who spoke) and its cursor column. Message content, speaker, cost
    and provenance stay immutable - this is not a general message editor.

    Same commit-before-notify discipline as insert_message: the row is
    durable before any client is woken to fetch it."""
    con.execute(
        "UPDATE messages SET voice_labels=?, labels_updated_at=? WHERE id=?",
        (_json_dumps(payload), now(), message_id))
    con.commit()
    from . import events  # lazy for the same circularity reason as insert_message
    events.notify_message_update()


def get_message_by_voice_turn(con, chat_id, voice_turn_id):
    """The one user message a diarization pass with a turn id may label:
    matched on the client's own correlation id, persisted at insert (#28
    phase 3). Exact by construction - a short interjection whose transcript
    was dropped client-side simply has no row, and its labels attach
    nowhere instead of smearing onto a neighbouring turn."""
    if not voice_turn_id:
        return None
    row = con.execute(
        "SELECT id, voice_labels, created_at, content FROM messages "
        "WHERE chat_id=? AND speaker='user' AND voice_turn_id=? "
        "ORDER BY id LIMIT 1",
        (chat_id, voice_turn_id)).fetchone()
    return dict(row) if row else None


def get_voice_label_candidates(con, chat_id, since_ts):
    """User-turn rows a diarization pass may still label: user speaker,
    created after the utterance's commit instant, id-ordered so the caller's
    pick_target takes the OLDEST unlabelled match first."""
    return [dict(r) for r in con.execute(
        "SELECT id, voice_labels, created_at, content FROM messages "
        "WHERE chat_id=? AND speaker='user' AND created_at>=? ORDER BY id",
        (chat_id, since_ts))]


def get_label_updates_after(con, ts):
    """Label writes since `ts` - the live-events stream's catch-up query for
    message_update pushes, mirroring get_guest_jobs_after: the DATABASE is
    the buffer, the in-memory bell is only the wake-up."""
    return [dict(r) for r in con.execute(
        "SELECT id, chat_id, labels_updated_at FROM messages "
        "WHERE labels_updated_at > ? ORDER BY labels_updated_at, id", (ts,))]


def get_chat_label_updates_after(con, chat_id, ts):
    """One chat's label writes since `ts`, labels included - the round loop's
    per-seat refresh (#28, night test 4): a diarization pass often resolves a
    turn's identity while the round is already replying, and the rows a later
    seat holds were fetched before that write landed. Same cursor discipline
    as get_label_updates_after (the DATABASE is the buffer); this variant
    carries voice_labels because its caller folds them into already-fetched
    rows rather than telling a client to refetch."""
    return [dict(r) for r in con.execute(
        "SELECT id, voice_labels, labels_updated_at FROM messages "
        "WHERE chat_id=? AND labels_updated_at > ? "
        "ORDER BY labels_updated_at, id", (chat_id, ts))]


def _json_dumps(obj):
    import json
    return json.dumps(obj)


# ---------- room mode: roster + flags (#28 phase 2) ----------
#
# Same store discipline as guest_jobs: the DATABASE is the durable state and
# the live-events catch-up buffer; the in-memory bell (events.notify_room_update)
# is only the wake-up. Every write here commits BEFORE notifying.

def set_chat_room_mode(con, chat_id, on):
    """Flip the durable per-chat room-mode flag. Callers must ALSO update
    diarize's in-process registry (diarize.set_room_enabled) so a live STT
    session sees the flip at its next commit boundary without a DB read on
    the audio path."""
    con.execute("UPDATE chats SET room_mode=? WHERE id=?",
                (1 if on else 0, chat_id))
    con.commit()


def set_chat_ambient_off(con, chat_id, off):
    """Flip the durable per-chat ambient-off flag (#28). Set when the owner
    says "solo mode", so automatic arming is suppressed until an explicit
    re-enable clears it. Callers must ALSO update diarize's live mirror
    (diarize.set_ambient_off) so a running STT session honours it without a
    DB read on the audio path."""
    con.execute("UPDATE chats SET ambient_off=? WHERE id=?",
                (1 if off else 0, chat_id))
    con.commit()


def get_chat_ambient_off(con, chat_id) -> bool:
    row = con.execute("SELECT ambient_off FROM chats WHERE id=?",
                      (chat_id,)).fetchone()
    return bool(row and row["ambient_off"])


def get_room_roster(con, chat_id, present_only=False):
    q = "SELECT * FROM room_roster WHERE chat_id=?"
    if present_only:
        q += " AND status='present'"
    return [dict(r) for r in con.execute(q + " ORDER BY id", (chat_id,))]


def add_room_person(con, chat_id, name, person_id=""):
    """Append one person to a chat's roster (or re-mark a previously-left row
    present). Returns the row, or None for a refused name. Cap enforcement is
    the CALLER's job (introductions.apply_scan) - this is a thin writer, with
    ONE refusal of its own (#65): an AI participant's exact slug or display
    name never becomes a roster person, whatever path asked. Spelt-by-ear
    variants are the scan layer's job (participant_alias); this is the
    last-ditch guard under every seat writer."""
    if con.execute(
            "SELECT 1 FROM participants WHERE lower(slug)=lower(?) "
            "OR lower(name)=lower(?)", (name, name)).fetchone():
        return None
    row = con.execute(
        "SELECT * FROM room_roster WHERE chat_id=? AND lower(name)=lower(?)",
        (chat_id, name)).fetchone()
    if row:
        con.execute(
            "UPDATE room_roster SET status='present', left_at=NULL, "
            "person_id=CASE WHEN person_id='' THEN ? ELSE person_id END, "
            "updated_at=? WHERE id=?", (person_id, now(), row["id"]))
        con.commit()
        out = dict(con.execute("SELECT * FROM room_roster WHERE id=?",
                               (row["id"],)).fetchone())
    else:
        cur = con.execute(
            "INSERT INTO room_roster(chat_id, name, person_id, status, "
            "joined_at, updated_at) VALUES(?,?,?,'present',?,?)",
            (chat_id, name, person_id, now(), now()))
        con.commit()
        out = dict(con.execute("SELECT * FROM room_roster WHERE id=?",
                               (cur.lastrowid,)).fetchone())
    from . import events  # lazy, same circularity reason as insert_message
    events.notify_room_update()
    return out


def ack_command(con, chat_id, message_id) -> bool:
    """Record that machine tooling consumed a slash command (#58). True only
    when the id names a USER message in THIS chat - strict on purpose, so a
    producer bug can never ack a message across chats or ack model text."""
    row = con.execute(
        "SELECT 1 FROM messages WHERE id=? AND chat_id=? AND speaker='user'",
        (message_id, chat_id)).fetchone()
    if not row:
        return False
    con.execute(
        "INSERT OR IGNORE INTO command_acks(message_id, acked_at) VALUES(?,?)",
        (message_id, now()))
    con.commit()
    return True


def command_acked(con, message_id) -> bool:
    return bool(con.execute("SELECT 1 FROM command_acks WHERE message_id=?",
                            (message_id,)).fetchone())


def mark_room_person_left(con, chat_id, name):
    """Mark a roster row left (frees the cap). Returns True if a present row
    was actually updated."""
    cur = con.execute(
        "UPDATE room_roster SET status='left', left_at=?, updated_at=? "
        "WHERE chat_id=? AND lower(name)=lower(?) AND status='present'",
        (now(), now(), chat_id, name))
    con.commit()
    if cur.rowcount:
        from . import events
        events.notify_room_update()
    return bool(cur.rowcount)


def link_room_person(con, chat_id, name, person_id):
    """Attach the anchor-store person id to a roster row once anchors exist -
    the moment 'anchor pending' ends for that name in this chat."""
    cur = con.execute(
        "UPDATE room_roster SET person_id=?, updated_at=? "
        "WHERE chat_id=? AND lower(name)=lower(?) AND person_id=''",
        (person_id, now(), chat_id, name))
    con.commit()
    if cur.rowcount:
        from . import events
        events.notify_room_update()


def insert_room_flag(con, chat_id, kind, message_id=None, label="", suspected=""):
    """One attribution-doubt row ('unknown_voice' or 'mismatch'). Carries
    roster NAMES only - transcript text never enters this table."""
    cur = con.execute(
        "INSERT INTO room_flags(chat_id, message_id, kind, label, suspected, "
        "created_at, updated_at) VALUES(?,?,?,?,?,?,?)",
        (chat_id, message_id, kind, label, suspected, now(), now()))
    con.commit()
    row = dict(con.execute("SELECT * FROM room_flags WHERE id=?",
                           (cur.lastrowid,)).fetchone())
    from . import events
    events.notify_room_update()
    return row


def resolve_room_flags(con, chat_id, message_id=None, flag_id=None, kind=None):
    """Close open flags - a correction resolves its message's flags; a
    dismissal resolves one flag by id; an answered introduction resolves the
    chat's open 'unknown_voice' asks by kind. Returns how many closed."""
    q = "UPDATE room_flags SET resolved_at=?, updated_at=? WHERE chat_id=? AND resolved_at IS NULL"
    args = [now(), now(), chat_id]
    if message_id is not None:
        q += " AND message_id=?"
        args.append(message_id)
    if flag_id is not None:
        q += " AND id=?"
        args.append(flag_id)
    if kind is not None:
        q += " AND kind=?"
        args.append(kind)
    cur = con.execute(q, args)
    con.commit()
    if cur.rowcount:
        from . import events
        events.notify_room_update()
    return cur.rowcount


def get_room_flags(con, chat_id, open_only=True):
    q = "SELECT * FROM room_flags WHERE chat_id=?"
    if open_only:
        q += " AND resolved_at IS NULL"
    return [dict(r) for r in con.execute(q + " ORDER BY id", (chat_id,))]


def get_room_updates_after(con, roster_ts, flag_ts):
    """Roster and flag rows whose updated_at moved past each table's own
    cursor - the live-events stream's catch-up query, mirroring
    get_guest_jobs_after. Returns (roster_rows, flag_rows)."""
    roster = [dict(r) for r in con.execute(
        "SELECT * FROM room_roster WHERE updated_at > ? ORDER BY updated_at, id",
        (roster_ts,))]
    flags = [dict(r) for r in con.execute(
        "SELECT * FROM room_flags WHERE updated_at > ? ORDER BY updated_at, id",
        (flag_ts,))]
    return roster, flags


# ---------- guest jobs ----------
#
# The durable status store for async Claude Code guest runs. Like messages, a
# guest job is written here first (source of truth), then a live push wakes any
# connected client (backend/events.py) - so status survives a disconnect, a tab
# backgrounding, or a process restart mid-run. Kept intentionally generic
# (status + kind + a message pointer) so tab/backgrounding persistence for
# voice playback/connection can reuse the same store without a schema fork.

def insert_guest_job(con, chat_id, task, repo, mode, requested_by):
    cur = con.execute(
        "INSERT INTO guest_jobs(chat_id, status, task, repo, mode, requested_by, "
        "created_at, updated_at) VALUES(?, 'running', ?, ?, ?, ?, ?, ?)",
        (chat_id, task, repo, mode, requested_by, now(), now()))
    con.commit()
    return dict(con.execute("SELECT * FROM guest_jobs WHERE id=?",
                            (cur.lastrowid,)).fetchone())


def update_guest_job(con, job_id, **fields):
    """Patch a job row and bump updated_at (the events-stream cursor). Returns
    the fresh row. Unknown keys are ignored so callers can't corrupt columns."""
    allowed = {"status", "kind", "session_id", "message_id", "error", "step_count",
               "status_label", "status_at"}
    sets = {k: v for k, v in fields.items() if k in allowed}
    cols = ", ".join(f"{k}=?" for k in sets) + (", " if sets else "") + "updated_at=?"
    con.execute(f"UPDATE guest_jobs SET {cols} WHERE id=?",
                (*sets.values(), now(), job_id))
    con.commit()
    row = con.execute("SELECT * FROM guest_jobs WHERE id=?", (job_id,)).fetchone()
    return dict(row) if row else None


def get_guest_job(con, job_id):
    row = con.execute("SELECT * FROM guest_jobs WHERE id=?", (job_id,)).fetchone()
    return dict(row) if row else None


def get_chat_guest_jobs(con, chat_id, limit=20):
    """Most-recent-first jobs for one chat - the snapshot a client fetches for
    the chat it is viewing (live changes then arrive over the events stream)."""
    return [dict(r) for r in con.execute(
        "SELECT * FROM guest_jobs WHERE chat_id=? ORDER BY id DESC LIMIT ?",
        (chat_id, limit))]


def get_guest_jobs_after(con, ts):
    """Jobs whose status changed after `ts` - the events-stream cursor query,
    across all chats. Mirrors get_messages_after's id cursor, but keyed on
    updated_at because a job row is UPDATED in place across its lifecycle."""
    return [dict(r) for r in con.execute(
        "SELECT * FROM guest_jobs WHERE updated_at > ? ORDER BY updated_at, id",
        (ts,))]
