"""scripts/repair_stored_passes.py, the one-off repair for #472.

Before #457 and #468 some passes were stored as real rows: a reply cut off
mid-token with the cut-off marker on the end, and a short quiet remark
ending in [pass]. The script finds exactly those with the engine's own
pass rules, prints them without their text, and deletes them only with
--apply, after a snapshot, journalling each removal without its text.
Everything here runs on a throwaway database.
"""

import importlib.util
import json
import sqlite3
from pathlib import Path

import pytest

from backend import busy, db
from backend.config import Settings

REPO = Path(__file__).resolve().parents[1]

# (label, speaker, content, has a tool event, expected kind or None)
ROWS = [
    ("cut mid-token", "gpt", "[pa\n\n[cut off by Sam]", False, "cut off"),
    ("cut after token", "gpt", "[pass]\n\n[cut off by Sam]", False,
     "cut off"),
    ("cut quiet remark", "claude", "Nothing to add. [pa\n\n[cut off by Sam]",
     False, "cut off"),
    ("bare pass", "claude", "[pass]", False, "finished"),
    ("quiet remark", "claude", "Their answer covers it, nothing to add.\n\n"
     "[pass]", False, "finished"),
    ("staying quiet", "gpt", "Staying quiet unless called on, passing. "
     "[pass]", False, "finished"),
    # Kept: real replies, tool turns and rows that aren't a seat's.
    ("real reply, token", "gpt", "Sand it with 120 grit first. [pass]", False,
     None),
    ("real reply, cut", "gpt", "Sand it first.\n\n[cut off by Sam]", False,
     None),
    ("finished [p", "gpt", "[p", False, None),
    ("tool turn", "gpt", "[pass]", True, None),
    ("user", "user", "[pass]", False, None),
    ("system", "system", "[pass]", False, None),
    ("guest", "guest:unknown", "Nothing to add. [pass]", False, None),
]


def _script():
    spec = importlib.util.spec_from_file_location(
        "repair_stored_passes", REPO / "scripts" / "repair_stored_passes.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


@pytest.fixture
def seeded(tmp_path):
    """A throwaway database with one row per case in ROWS. Returns the
    settings and {label: message id}."""
    settings = Settings(data_dir=str(tmp_path / "data"), port=1)
    db.configure(settings.resolved_data_dir())
    db.init(settings)
    con = db.connect()
    con.execute("INSERT INTO chats(id, title, created_at, updated_at) "
                "VALUES(7, 'T', 0, 0)")
    ids = {}
    for label, speaker, content, tool, _ in ROWS:
        tools = [{"tool": "web_search", "input": {"q": "x"},
                  "output": "y"}] if tool else None
        msg = db.insert_message(con, 7, speaker, content, tool_events=tools,
                                notify=False,
                                usage_json=json.dumps({"cost": 0.01}))
        ids[label] = msg["id"]
    # The memory handoff has passed the first three rows.
    con.execute("UPDATE chats SET ingested_upto=? WHERE id=7",
                (ids["cut quiet remark"],))
    con.commit()
    con.close()
    return settings, ids


def _expected(ids):
    return {ids[label]: kind for label, _, _, _, kind in ROWS if kind}


def test_classify_matches_the_engine_rules():
    mod = _script()
    for label, _, content, _, kind in ROWS:
        if label in ("tool turn", "user", "system", "guest"):
            continue
        assert mod.classify(content) == (kind or ""), label


def test_finds_exactly_the_stored_passes(seeded):
    settings, ids = seeded
    mod = _script()
    con = db.connect()
    try:
        found = mod.find_stored_passes(con)
    finally:
        con.close()
    assert {f["message_id"]: f["kind"] for f in found} == _expected(ids)
    memory = {f["message_id"]: f["in_memory"] for f in found}
    assert memory[ids["cut mid-token"]] and memory[ids["cut quiet remark"]]
    assert not memory[ids["bare pass"]]


def test_dry_run_changes_nothing_and_prints_no_text(seeded):
    settings, ids = seeded
    mod = _script()
    lines = []
    probe_calls = []
    assert mod.main([], settings=settings, out=lines.append,
                    probe=lambda port: probe_calls.append(port) or []) == 0
    text = "\n".join(lines)
    for message_id in _expected(ids):
        assert f"message {message_id} " in text
    for words in ("Nothing to add", "Sand it", "[pa", "cut off by"):
        assert words not in text
    assert "6 stored pass(es)" in text
    assert not probe_calls  # a dry run never asks the service anything
    con = db.connect()
    try:
        assert con.execute("SELECT count(*) FROM messages").fetchone()[0] \
            == len(ROWS)
    finally:
        con.close()
    data = settings.resolved_data_dir()
    assert not (data / "repairs.jsonl").exists()
    assert not list(data.glob("chat-before-pass-repair-*.db"))


def test_apply_deletes_snapshots_and_journals(seeded):
    settings, ids = seeded
    mod = _script()
    lines = []
    assert mod.main(["--apply"], settings=settings, out=lines.append,
                    probe=lambda port: None) == 0
    expected = _expected(ids)
    con = db.connect()
    try:
        left = {r["id"] for r in con.execute("SELECT id FROM messages")}
        tool_rows = con.execute("SELECT count(*) FROM tool_events") \
            .fetchone()[0]
    finally:
        con.close()
    assert left == set(ids.values()) - set(expected)
    assert tool_rows == 1  # the tool turn and its event stay

    data = settings.resolved_data_dir()
    snaps = list(data.glob("chat-before-pass-repair-*.db"))
    assert len(snaps) == 1
    held = sqlite3.connect(snaps[0])
    try:
        assert held.execute("SELECT count(*) FROM messages").fetchone()[0] \
            == len(ROWS)
    finally:
        held.close()

    journal = [json.loads(line) for line in
               (data / "repairs.jsonl").read_text().splitlines()]
    assert {j["message_id"] for j in journal} == set(expected)
    for j in journal:
        assert j["action"] == "deleted" and j["backup"] == snaps[0].name
        assert "content" not in j
        assert "[pa" not in json.dumps(j) and "Nothing" not in json.dumps(j)

    # A second run finds nothing left to do.
    lines = []
    mod.main([], settings=settings, out=lines.append, probe=lambda p: None)
    assert any("nothing to do" in line for line in lines)


def test_apply_refuses_while_a_round_is_running(seeded):
    settings, ids = seeded
    mod = _script()
    with pytest.raises(SystemExit, match="round is running"):
        mod.main(["--apply"], settings=settings, out=lambda s: None,
                 probe=lambda port: [busy.ROUND_RUNNING])
    con = db.connect()
    try:
        assert con.execute("SELECT count(*) FROM messages").fetchone()[0] \
            == len(ROWS)
    finally:
        con.close()
    assert not list(settings.resolved_data_dir()
                    .glob("chat-before-pass-repair-*.db"))


def test_apply_refuses_when_the_service_cannot_say(seeded):
    settings, _ = seeded
    mod = _script()

    def broken(port):
        raise TimeoutError("no answer")

    with pytest.raises(SystemExit, match="nothing deleted"):
        mod.main(["--apply"], settings=settings, out=lambda s: None,
                 probe=broken)
    con = db.connect()
    try:
        assert con.execute("SELECT count(*) FROM messages").fetchone()[0] \
            == len(ROWS)
    finally:
        con.close()


def test_other_busy_work_does_not_block(seeded):
    settings, ids = seeded
    mod = _script()
    mod.main(["--apply"], settings=settings, out=lambda s: None,
             probe=lambda port: [busy.BACKUP_RUNNING])
    con = db.connect()
    try:
        left = con.execute("SELECT count(*) FROM messages").fetchone()[0]
    finally:
        con.close()
    assert left == len(ROWS) - len(_expected(ids))


def test_a_missing_database_is_never_created(tmp_path):
    mod = _script()
    settings = Settings(data_dir=str(tmp_path / "nowhere"))
    with pytest.raises(SystemExit, match="no database"):
        mod.main([], settings=settings, out=lambda s: None)
    assert not (tmp_path / "nowhere" / "chat.db").exists()


def test_probe_reads_the_busy_route_and_a_closed_port():
    """The real probe: the labels from a live busy route, and None when
    nothing listens on the port."""
    import http.server
    import socket
    import threading

    class Busy(http.server.BaseHTTPRequestHandler):
        def do_GET(self):
            body = json.dumps({"busy": True,
                               "reasons": [busy.ROUND_RUNNING]}).encode()
            status = 200 if self.path == busy.PATH else 404
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *args):
            pass

    server = http.server.HTTPServer(("127.0.0.1", 0), Busy)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        mod = _script()
        assert mod.running_work(server.server_address[1]) \
            == [busy.ROUND_RUNNING]
    finally:
        server.shutdown()
        server.server_close()

    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        free = s.getsockname()[1]
    assert mod.running_work(free) is None
