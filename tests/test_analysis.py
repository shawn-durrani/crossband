"""The Analysis page (#407): measurements run as background jobs.

The contract under test:

- Each measurement states what it measures, what it costs, how long it
  takes and what it touches, and the table carries no guard.
- A run's command comes from the table alone. A content flag in it is
  refused before anything starts, and a request that brings any option
  beyond the measurement and the practice flag is refused at the route.
- A run is a child process with a lifecycle: running, then done, failed,
  stopped or timed out. One at a time per measurement, counted on the busy
  route. A stop sends SIGINT, so the harness's own cleanup runs. A record
  left running by an earlier process reads as interrupted.
- The report folder is 0700 and every file in it 0600. A recall replay
  report that carries words from a chat is deleted.
- Every route needs a signed-in owner, before and after enrolment, from
  loopback and from the tailnet. No seat tool or guest diagnostic reaches it.
- Every harness writes the JSON report the page reads, and the headline
  reads each one.

The harnesses are stubbed with a tiny module on PYTHONPATH, so nothing here
calls a model, membro or the diariser. The practice runs at the end run the
real harnesses keyless, with --mock.
"""

import asyncio
import json
import os
import stat
import textwrap
import time
from dataclasses import replace
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from backend import analysis, busy, db
from backend.app import create_app
from backend.config import Settings
from backend.routers import voice as voice_router

REPO = Path(__file__).resolve().parents[1]
PASSWORD = "a-durable-owner-passphrase"
TAILNET = "my-mac.my-tailnet.ts.net"

STUB = textwrap.dedent('''
    import argparse, json, os, sys, time
    p = argparse.ArgumentParser()
    for flag in ("--format", "--out", "--json-out", "--db", "--memory-url"):
        p.add_argument(flag)
    p.add_argument("--mock", action="store_true")
    a, _ = p.parse_known_args()
    mode = os.environ.get("STUB_MODE", "ok")
    if mode == "ok":
        with open(a.out, "w") as fh:
            fh.write("# Stub report\\n\\nEverything the stub measured.\\n")
        with open(a.json_out, "w") as fh:
            json.dump({"n_turns": 4, "turns_with_hits": 3,
                       "recall_ms": {"p50": 12.0}, "corpus": [],
                       "argv": sys.argv[1:]}, fh)
    elif mode == "fail":
        print("boom: the stub gave up", file=sys.stderr)
        sys.exit(3)
    elif mode == "content":
        with open(a.out, "w") as fh:
            fh.write("# report\\n")
        with open(a.json_out, "w") as fh:
            json.dump({"corpus": [{"chat_id": 1, "query": "words from a chat",
                                   "hits": []}]}, fh)
    elif mode == "sleep":
        with open(os.environ["STUB_READY"], "w") as fh:
            fh.write("up")
        try:
            time.sleep(60)
        finally:
            with open(os.environ["STUB_MARKER"], "w") as fh:
                fh.write("cleaned up")
''')


@pytest.fixture(autouse=True)
def _clean_live():
    analysis._live.clear()
    yield
    analysis._live.clear()


@pytest.fixture
def data_dir(tmp_path, monkeypatch):
    d = tmp_path / "data"
    d.mkdir()
    monkeypatch.setattr(db, "DATA_DIR", d)
    monkeypatch.setattr(db, "DB_PATH", d / "chat.db")
    return d


@pytest.fixture
def stub(tmp_path, monkeypatch):
    """Swap the table for stub measurements that run the module above. The
    ids stay real ones, so run ids and the page's view are unchanged."""
    pkg = tmp_path / "stubs" / "analysis_stub"
    pkg.mkdir(parents=True)
    (pkg / "__main__.py").write_text(STUB)
    monkeypatch.setenv("PYTHONPATH", str(tmp_path / "stubs"))
    monkeypatch.setenv("STUB_READY", str(tmp_path / "ready"))
    monkeypatch.setenv("STUB_MARKER", str(tmp_path / "cleaned"))
    table = tuple(replace(m, module="analysis_stub", args=())
                  for m in analysis.MEASUREMENTS)
    monkeypatch.setattr(analysis, "MEASUREMENTS", table)
    monkeypatch.setattr(analysis, "_BY_ID", {m.id: m for m in table})
    return tmp_path


def _mode(monkeypatch, mode):
    monkeypatch.setenv("STUB_MODE", mode)


async def _until(pred, timeout=15.0):
    deadline = time.monotonic() + timeout
    while not pred():
        if time.monotonic() > deadline:
            raise AssertionError("timed out waiting")
        await asyncio.sleep(0.05)


def _run(measurement_id="recall", **kw):
    """Start a run, wait for it to settle, and return its stored record."""
    async def go():
        rec = analysis.start(measurement_id, **kw)
        await analysis._live[measurement_id].task
        return analysis.load_record(rec["run_id"])
    return asyncio.run(go())


def _mode_of(path):
    return stat.S_IMODE(os.stat(path).st_mode)


# ── what the page states ────────────────────────────────────────────────────

def test_every_measurement_states_its_cost_and_what_it_touches():
    for m in analysis.MEASUREMENTS:
        for field in ("title", "measures", "costs", "takes", "touches"):
            assert getattr(m, field).strip(), (m.id, field)
        assert (REPO / m.readme).is_file(), m.readme
        assert (REPO / m.module / "__main__.py").is_file(), m.module


def test_the_table_carries_measurements_and_no_guard():
    ids = [m.id for m in analysis.MEASUREMENTS]
    assert ids == ["critic", "attribution", "recall", "intent", "voice"]
    assert "eval_silence" not in {m.module for m in analysis.MEASUREMENTS}


def test_only_the_recall_replay_reads_your_own_data():
    assert [m.id for m in analysis.MEASUREMENTS if m.own_data] == ["recall"]


def test_the_catalogue_says_everything_before_the_run_button(data_dir):
    rows = analysis.catalogue()
    for row in rows:
        assert row["costs"] and row["touches"] and row["measures"]
        assert row["command"].startswith("python -m eval_")
        assert row["blocked"] == "" and row["running"] is None


# ── the command, and content refused ────────────────────────────────────────

def test_the_command_runs_the_harness_as_a_child_with_its_report_paths(tmp_path):
    m = analysis.get("critic")
    argv = analysis.build_command(m, False, tmp_path)
    assert argv[1:4] == ["-c", analysis.CHILD, "eval_critic"]
    assert argv[4:6] == ["--model", "claude-haiku-4-5"]
    assert argv[-6:] == ["--format", "markdown",
                         "--out", str(tmp_path / "report.md"),
                         "--json-out", str(tmp_path / "report.json")]
    practice = analysis.build_command(m, True, tmp_path)
    assert practice[4] == "--mock" and "claude-haiku-4-5" not in practice


def test_no_table_entry_carries_a_content_flag(tmp_path):
    for m in analysis.MEASUREMENTS:
        argv = analysis.build_command(m, False, tmp_path, "http://127.0.0.1:1")
        for flag in analysis.CONTENT_FLAGS:
            assert flag not in argv, (m.id, flag)


def test_the_recall_replay_reads_this_apps_database_and_membro(data_dir, tmp_path):
    argv = analysis.build_command(analysis.get("recall"), False, tmp_path,
                                  "http://127.0.0.1:8901")
    assert argv[argv.index("--db") + 1] == str(data_dir / "chat.db")
    assert argv[argv.index("--memory-url") + 1] == "http://127.0.0.1:8901"
    assert "--with-content" not in argv


@pytest.mark.parametrize("flag", ["--with-content", "--show-words",
                                  "--fixtures-dir=/somewhere"])
def test_a_content_flag_in_the_table_is_refused_before_anything_starts(
        data_dir, monkeypatch, flag):
    m = replace(analysis.get("recall"), args=(flag,))
    monkeypatch.setattr(analysis, "_BY_ID", {**analysis._BY_ID, "recall": m})

    async def go():
        with pytest.raises(analysis.Refused):
            analysis.start("recall")
    asyncio.run(go())
    assert not analysis._live
    assert not (data_dir / "analysis").exists()


# ── the lifecycle ───────────────────────────────────────────────────────────

def test_a_run_goes_from_running_to_done_with_a_headline(data_dir, stub, monkeypatch):
    _mode(monkeypatch, "ok")
    rec = _run("recall")
    assert rec["state"] == "done" and rec["has_report"] is True
    assert rec["exit_code"] == 0 and rec["practice"] is False
    assert rec["summary"]["headline"].startswith("Replayed 4 turns. 75%")
    assert rec["command"] == "python -m analysis_stub"  # what ran
    found = analysis.report_text(rec["run_id"])
    assert found[1].startswith("# Stub report")
    report = json.loads(analysis.report_json_path(rec["run_id"]).read_text())
    assert "--with-content" not in report["argv"]
    assert not analysis._live and analysis.running_count() == 0


def test_the_report_folder_and_every_file_are_owner_only(data_dir, stub, monkeypatch):
    _mode(monkeypatch, "ok")
    rec = _run("critic")
    run_dir = data_dir / "analysis" / rec["run_id"]
    assert _mode_of(data_dir / "analysis") == 0o700
    assert _mode_of(run_dir) == 0o700
    names = sorted(p.name for p in run_dir.iterdir())
    assert names == ["report.json", "report.md", "run.json"]
    for name in names:
        assert _mode_of(run_dir / name) == 0o600, name


def test_a_failed_run_keeps_the_end_of_stderr(data_dir, stub, monkeypatch):
    _mode(monkeypatch, "fail")
    rec = _run("intent")
    assert rec["state"] == "failed" and rec["exit_code"] == 3
    assert "boom: the stub gave up" in rec["error"]
    assert rec["has_report"] is False and "summary" not in rec


def test_one_run_at_a_time_per_measurement_and_the_busy_route_counts_it(
        data_dir, stub, monkeypatch):
    _mode(monkeypatch, "sleep")

    async def go():
        rec = analysis.start("attribution")
        await _until(lambda: (stub / "ready").exists())
        with pytest.raises(analysis.Busy):
            analysis.start("attribution")
        seen = busy.reasons(), analysis.running_ids()
        assert analysis.load_record(rec["run_id"])["state"] == "running"
        assert analysis.delete_run(rec["run_id"]).startswith("That run is still")
        assert analysis.stop(rec["run_id"]) == ""
        await analysis._live["attribution"].task
        return rec, seen

    rec, (reasons, ids) = asyncio.run(go())
    assert "measurement running" in reasons and ids == ["attribution"]
    assert busy.reasons() == []


def test_a_stop_lets_the_harness_clean_up(data_dir, stub, monkeypatch):
    """SIGINT, not a kill: the voice rig's cleanup (stopping its second app)
    lives in a finally block, and this is the stub's version of it."""
    _mode(monkeypatch, "sleep")

    async def go():
        rec = analysis.start("voice")
        await _until(lambda: (stub / "ready").exists())
        assert analysis.stop(rec["run_id"]) == ""
        await analysis._live["voice"].task
        return analysis.load_record(rec["run_id"])

    rec = asyncio.run(go())
    assert rec["state"] == "stopped" and rec["error"] == "You stopped it."
    assert (stub / "cleaned").read_text() == "cleaned up"
    assert analysis.stop(rec["run_id"]) == "That run isn't running."


def test_a_run_past_its_time_is_stopped_and_says_so(data_dir, stub, monkeypatch):
    _mode(monkeypatch, "sleep")
    m = replace(analysis._BY_ID["critic"], timeout_s=1.0)
    monkeypatch.setitem(analysis._BY_ID, "critic", m)
    rec = _run("critic")
    assert rec["state"] == "timed out"
    assert (stub / "cleaned").exists()


def test_shutting_down_stops_every_run_with_its_cleanup(data_dir, stub, monkeypatch):
    _mode(monkeypatch, "sleep")

    async def go():
        rec = analysis.start("voice")
        await _until(lambda: (stub / "ready").exists())
        await analysis.stop_all()
        return analysis.load_record(rec["run_id"])

    rec = asyncio.run(go())
    assert rec["state"] == "stopped"
    assert rec["error"] == "The app restarted while it ran."
    assert (stub / "cleaned").exists()


def test_a_recall_report_with_words_in_it_is_deleted(data_dir, stub, monkeypatch):
    _mode(monkeypatch, "content")
    rec = _run("recall")
    assert rec["state"] == "failed" and "deleted" in rec["error"]
    run_dir = data_dir / "analysis" / rec["run_id"]
    assert not (run_dir / "report.json").exists()
    assert not (run_dir / "report.md").exists()
    assert analysis.report_json_path(rec["run_id"]) is None


def test_a_record_left_running_reads_as_interrupted(data_dir):
    run_dir = analysis._private_dir(data_dir / "analysis" / "recall-20260101-000000")
    analysis._write_record(run_dir, {"run_id": "recall-20260101-000000",
                                     "state": "running", "created_at_unix": 1})
    rec = analysis.load_record("recall-20260101-000000")
    assert rec["state"] == "interrupted"
    assert analysis.running_count() == 0 and busy.reasons() == []


def test_runs_list_newest_first_and_unsafe_ids_read_nothing(data_dir):
    for i, run_id in enumerate(["critic-20260101-000000", "voice-20260102-000000",
                                "recall-20260103-000000"]):
        run_dir = analysis._private_dir(data_dir / "analysis" / run_id)
        analysis._write_record(run_dir, {"run_id": run_id, "state": "done",
                                         "created_at_unix": 100 + i})
    (data_dir / "analysis" / "notes").mkdir()
    assert [r["run_id"] for r in analysis.list_runs()] == [
        "recall-20260103-000000", "voice-20260102-000000", "critic-20260101-000000"]
    for bad in ("../chat", "critic-2026", "silence-20260101-000000", ""):
        assert analysis.load_record(bad) is None
        assert analysis.delete_run(bad) == "No such run."
    assert analysis.delete_run("voice-20260102-000000") == ""
    assert not (data_dir / "analysis" / "voice-20260102-000000").exists()


def test_the_voice_rig_waits_while_a_voice_chat_is_live(data_dir):
    rig = analysis.get("voice")
    assert "voice chat is live" in analysis.blocked(rig, voice_live=True)
    assert analysis.blocked(analysis.get("critic"), voice_live=True) == ""

    async def go():
        with pytest.raises(analysis.Busy):
            analysis.start("voice", voice_live=True)
    asyncio.run(go())


# ── the headline ────────────────────────────────────────────────────────────

def test_each_headline_reads_its_own_report():
    s = analysis.summarise
    critic = s("critic", {"unsafe_draft_recall": 0.91, "control_false_alarm_rate": 0.05,
                          "results": [{"cost_usd": 0.001}, {"cost_usd": 0.002}]})
    assert critic == {"headline": "Caught 91% of the made-up facts and flagged 5% "
                                  "of the correct replies.", "spent_usd": 0.003}
    attr = s("attribution", {"accuracy": {"current|haiku": 0.96, "envelope|haiku": 1.0,
                                          "current|gpt-5": 1.0},
                             "cost_usd": {"current|haiku": 0.5, "current|gpt-5": 0.5}})
    assert attr["headline"] == ("Today's layout answered 96% on haiku, 100% on "
                                "gpt-5. The best other layout reached 100%.")
    assert attr["spent_usd"] == 1.0
    recall = s("recall", {"n_turns": 200, "turns_with_hits": 200,
                          "recall_ms": {"p50": 611.2}})
    assert recall == {"headline": "Replayed 200 turns. 100% brought facts back, "
                                  "in 611 ms at the median.", "spent_usd": None}
    intent = s("intent", {"strategies": {
        "today": {"all_axes_right": 0.63, "cost_per_turn_usd": 0.0003, "turns": 100},
        "merged": {"all_axes_right": 0.86, "cost_per_turn_usd": 0.0011, "turns": 100}}})
    assert intent == {"headline": "The one call heard 86% of turns right on every "
                                  "axis, the phrase lists 63%.", "spent_usd": 0.14}
    voice = s("voice", {"turns": 29, "summary": {"right": {"share": 0.862}},
                        "targets": {"wrong_per_100": 3.4},
                        "cost": {"tts": {"usd": 0.3453}, "stt_usd": 0.01}})
    assert voice == {"headline": "Named 86% of 29 turns right, with 3.4 wrong "
                                 "names per 100 turns.", "spent_usd": 0.3553}


def test_a_run_with_no_key_says_so_before_its_numbers():
    """Without a key every call fails, and the harness scores the misses as
    real ones: a 0% catch rate that is really an unset key."""
    critic = analysis.summarise("critic", {
        "unsafe_draft_recall": 0.0, "control_false_alarm_rate": 0.0,
        "results": [{"failure_mode": "missing_key"}] * 17})
    assert critic["headline"].startswith("17 calls had no key, so read this")
    attr = analysis.summarise("attribution", {"missing_key": 1,
                                              "accuracy": {"current|m": 0.5}})
    assert attr["headline"].startswith("1 call had no key")
    intent = analysis.summarise("intent", {"strategies": {
        "merged": {"all_axes_right": 0.0, "missing_key": 120}}})
    assert intent["headline"].startswith("120 calls had no key")


def test_a_practice_headline_says_so_and_spends_nothing():
    got = analysis.summarise("recall", {"n_turns": 8, "turns_with_hits": 6,
                                        "recall_ms": {"p50": 0.01}}, practice=True)
    assert got["headline"].startswith("Practice run, made-up numbers. Replayed 8")
    assert got["spent_usd"] is None


def test_a_report_of_an_unknown_shape_still_gets_a_line():
    assert analysis.summarise("critic", ["not", "a", "report"])["headline"] == \
        "The report is saved."


# ── the gate: owner only ────────────────────────────────────────────────────

@pytest.fixture
def app(tmp_path):
    return create_app(Settings(data_dir=str(tmp_path / "data"),
                               memory_url="http://127.0.0.1:1",
                               trusted_hosts=TAILNET))


def _enrol(client):
    r = client.post("/api/auth/setup", json={
        "recovery_secret": client.app.state.recovery_secret,
        "password": PASSWORD})
    assert r.status_code == 200


ROUTES = [("get", "/api/analysis"),
          ("post", "/api/analysis/runs"),
          ("get", "/api/analysis/runs/recall-20260101-000000"),
          ("get", "/api/analysis/runs/recall-20260101-000000/report.json"),
          ("post", "/api/analysis/runs/recall-20260101-000000/stop"),
          ("delete", "/api/analysis/runs/recall-20260101-000000")]


def _statuses(client, **kw):
    out = []
    for method, path in ROUTES:
        body = {"json": {"measurement": "critic", "practice": True}} \
            if method == "post" and path.endswith("/runs") else {}
        out.append(getattr(client, method)(path, **body, **kw).status_code)
    return out


def test_before_enrolment_loopback_is_refused_too(app):
    """Loopback is open to the rest of the app until a password exists. A
    Claude Code guest runs on loopback, so this page is not."""
    c = TestClient(app, base_url="http://127.0.0.1")
    assert c.get("/api/state").status_code == 200  # the open posture
    assert _statuses(c) == [401] * len(ROUTES)
    assert "owner password" in c.get("/api/analysis").json()["detail"]
    assert not analysis._live


def test_the_tailnet_without_a_session_is_refused_before_and_after(app):
    tail = TestClient(app, base_url=f"https://{TAILNET}")
    assert _statuses(tail) == [401] * len(ROUTES)
    _enrol(TestClient(app, base_url="http://127.0.0.1"))
    assert _statuses(tail) == [401] * len(ROUTES)


def test_the_machine_bearer_is_not_a_session(tmp_path):
    app = create_app(Settings(data_dir=str(tmp_path / "data"),
                              memory_url="http://127.0.0.1:1",
                              ingest_token="machine-token-for-tests"))
    c = TestClient(app, base_url="http://127.0.0.1")
    _enrol(TestClient(app, base_url="http://127.0.0.1"))
    head = {"Authorization": "Bearer machine-token-for-tests"}
    assert _statuses(c, headers=head) == [401] * len(ROUTES)


def test_a_signed_in_owner_runs_and_reads_a_measurement(app, stub, monkeypatch):
    _mode(monkeypatch, "ok")
    with TestClient(app, base_url="http://127.0.0.1") as c:
        _enrol(c)
        view = c.get("/api/analysis").json()
        assert [m["id"] for m in view["measurements"]] == [
            m.id for m in analysis.MEASUREMENTS]
        assert view["runs"] == []
        r = c.post("/api/analysis/runs", json={"measurement": "recall"})
        assert r.status_code == 202 and r.json()["state"] == "running"
        run_id = r.json()["run_id"]
        deadline = time.monotonic() + 15
        while c.get(f"/api/analysis/runs/{run_id}").json()["run"]["state"] == "running":
            assert time.monotonic() < deadline
            time.sleep(0.05)
        got = c.get(f"/api/analysis/runs/{run_id}").json()
        assert got["run"]["state"] == "done"
        assert got["report"].startswith("# Stub report")
        j = c.get(f"/api/analysis/runs/{run_id}/report.json")
        assert j.status_code == 200 and j.json()["n_turns"] == 4
        assert [x["run_id"] for x in c.get("/api/analysis").json()["runs"]] == [run_id]
        assert c.post(f"/api/analysis/runs/{run_id}/stop").status_code == 409
        assert c.delete(f"/api/analysis/runs/{run_id}").status_code == 200
        assert c.get(f"/api/analysis/runs/{run_id}").status_code == 404


@pytest.mark.parametrize("body", [
    {"measurement": "recall", "with_content": True},
    {"measurement": "recall", "args": ["--with-content"]},
    {"measurement": "recall", "fixtures_dir": "/private/replay"},
    {"measurement": "recall", "practice": "yes"},
    ["recall"],
])
def test_a_request_with_options_is_refused(app, body):
    c = TestClient(app, base_url="http://127.0.0.1")
    _enrol(c)
    r = c.post("/api/analysis/runs", json=body)
    assert r.status_code == 400
    assert not analysis._live
    assert not (Path(db.DATA_DIR) / "analysis").exists()


def test_unknown_and_busy_starts_answer_plainly(app, monkeypatch):
    c = TestClient(app, base_url="http://127.0.0.1")
    _enrol(c)
    assert c.post("/api/analysis/runs",
                  json={"measurement": "silence"}).status_code == 404
    monkeypatch.setitem(voice_router._captures, "sid-1",
                        {"sid": "sid-1", "chat_id": 1, "started_at": 0.0,
                         "client": "phone"})
    r = c.post("/api/analysis/runs", json={"measurement": "voice"})
    assert r.status_code == 409 and "voice chat is live" in r.json()["detail"]
    blocked = {m["id"]: m["blocked"] for m in c.get("/api/analysis").json()["measurements"]}
    assert blocked["voice"] and not blocked["critic"]


def test_state_names_the_running_measurements(app):
    c = TestClient(app, base_url="http://127.0.0.1")
    assert c.get("/api/state").json()["running_measurements"] == []
    analysis._live["recall"] = analysis.Job(analysis.get("recall"),
                                            {"run_id": "x", "state": "running"}, None)
    assert c.get("/api/state").json()["running_measurements"] == ["recall"]


# ── never a seat's or a guest's ─────────────────────────────────────────────

def _imports(rel):
    """Every module name a file imports, relative ones by their last part."""
    import ast
    names = set()
    for node in ast.walk(ast.parse((REPO / rel).read_text())):
        if isinstance(node, ast.Import):
            names.update(a.name.rsplit(".", 1)[-1] for a in node.names)
        elif isinstance(node, ast.ImportFrom):
            names.add((node.module or "").rsplit(".", 1)[-1])
            names.update(a.name for a in node.names)
    return names


def test_no_seat_tool_or_guest_diagnostic_reaches_the_page():
    """The seats' tools, the guest's diagnostic and the round never import
    the module, and no tool is named for it. A `run_eval` seat tool waits for
    the owner's call."""
    from backend import diagnostics
    for name in diagnostics.DIAGNOSTIC_NAMES:
        assert "eval" not in name and "analysis" not in name
    for rel in ("backend/tools.py", "backend/diagnostics.py",
                "backend/diag_mcp.py", "backend/guest.py", "backend/engine.py",
                "backend/providers.py", "backend/mcp_client.py"):
        assert "analysis" not in _imports(rel), rel
        assert "run_eval" not in (REPO / rel).read_text(), rel


def test_the_harnesses_are_never_imported_by_the_app():
    src = (REPO / "backend" / "analysis.py").read_text()
    for m in analysis.MEASUREMENTS:
        assert f"import {m.module}" not in src
        assert f"from {m.module}" not in src


# ── the real harnesses, keyless ─────────────────────────────────────────────

@pytest.mark.parametrize("measurement_id",
                         [m.id for m in analysis.MEASUREMENTS])
def test_a_practice_run_of_each_real_harness(data_dir, monkeypatch, measurement_id):
    """--mock through the real harness: its --json-out report lands, and the
    headline reads it. No key, no membro, no second app."""
    for key in ("ANTHROPIC_API_KEY", "OPENAI_API_KEY", "ELEVENLABS_API_KEY"):
        monkeypatch.delenv(key, raising=False)
    monkeypatch.setenv("CROSSBAND_DATA_DIR", str(data_dir))
    rec = _run(measurement_id, practice=True)
    assert rec["state"] == "done", rec.get("error")
    assert rec["practice"] is True
    assert rec["command"] == f"python -m {analysis.get(measurement_id).module} --mock"
    headline = rec["summary"]["headline"]
    assert headline.startswith("Practice run, made-up numbers. ")
    assert headline != "Practice run, made-up numbers. The report is saved."
    run_dir = data_dir / "analysis" / rec["run_id"]
    for name in ("report.md", "report.json", "run.json"):
        assert _mode_of(run_dir / name) == 0o600
