"""The busy route (#343): the deploy watcher's question before a restart.

The contract, fixed with the watcher (workbench#69):

- `GET /api/busy` answers `{"busy": false, "reasons": []}` on a fresh app.
- Each kind of in-flight work flips it true with its own fixed label: a
  round generating in any chat, a live voice capture, a guest visit, a
  person sync pass, a benchmark, an import, a backup mid-copy. Settled
  work (a finished round whose buffer stays for catch-up, a completed
  guest job still in the registry) does not count.
- Loopback reaches it without a session even once the owner has
  enrolled, exactly like the health probe; a trusted host still needs one.
- The reasons come from a fixed vocabulary and never carry content: no
  chat id, title, name, task text or session id, whatever is running.
"""

import asyncio
import re
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

from backend import benchmark, busy, db, guestjobs, importer, person_sync, rounds
from backend.app import create_app
from backend.config import Settings
from backend.routers import voice as voice_router
from backend.routers.auth import LOGIN_SURFACE

PASSWORD = "a-durable-owner-passphrase"
TAILNET = "my-mac.my-tailnet.ts.net"
IDLE = {"busy": False, "reasons": []}


@pytest.fixture
def app(tmp_path):
    return create_app(Settings(data_dir=str(tmp_path / "data"),
                               memory_url="http://127.0.0.1:1",
                               trusted_hosts=TAILNET))


@pytest.fixture(autouse=True)
def _clean_registries():
    """The round, guest-job and benchmark registries are process globals
    that conftest's room-state reset does not cover."""
    for reg in (rounds._rounds, guestjobs._jobs, benchmark._active):
        reg.clear()
    yield
    for reg in (rounds._rounds, guestjobs._jobs, benchmark._active):
        reg.clear()


def _client(app, base_url="http://127.0.0.1"):
    return TestClient(app, base_url=base_url)


def _busy(app):
    r = _client(app).get("/api/busy")
    assert r.status_code == 200
    return r.json()


def _enrol(app):
    owner = _client(app)
    r = owner.post("/api/auth/setup", json={
        "recovery_secret": app.state.recovery_secret, "password": PASSWORD})
    assert r.status_code == 200
    return owner


def _fake_round(chat_id, done=False):
    # A live round is registry state; fabricate an entry rather than race
    # a real task across test event loops (as test_discard_turn does).
    rounds._rounds[chat_id] = SimpleNamespace(done=done, round_id=1,
                                              events=[], task=None)


def _fake_capture(sid, chat_id, client):
    voice_router._captures[sid] = {"sid": sid, "chat_id": chat_id,
                                   "started_at": 0.0, "client": client,
                                   "ws": None}


def _fake_guest_job(chat_id, task, repo, status="running"):
    job = guestjobs.GuestJob({"id": 7, "chat_id": chat_id, "task": task,
                              "repo": repo, "mode": "investigate",
                              "status": status, "kind": "result"})
    guestjobs._jobs[job.id] = job
    return job


# ── idle ────────────────────────────────────────────────────────────────────

def test_fresh_app_is_idle(app):
    assert _busy(app) == IDLE


# ── each source flips it, with its own label ────────────────────────────────

def test_a_round_generating_in_any_chat(app):
    _fake_round(42)
    assert _busy(app) == {"busy": True, "reasons": ["round running"]}
    rounds._rounds.clear()
    assert _busy(app) == IDLE


def test_a_finished_round_does_not_count(app):
    _fake_round(42, done=True)  # the buffer stays for catch-up (#64)
    assert _busy(app) == IDLE


def test_a_live_voice_capture(app):
    _fake_capture("sid-1", 3, "phone")
    assert _busy(app) == {"busy": True, "reasons": ["voice capture running"]}
    voice_router._captures.clear()
    assert _busy(app) == IDLE


def test_a_guest_visit(app):
    job = _fake_guest_job(5, "look at the tests", "app")
    assert _busy(app) == {"busy": True, "reasons": ["guest visit running"]}
    job.status = "completed"  # settled but not yet reaped from the registry
    assert _busy(app) == IDLE


def test_a_person_sync_pass(app):
    """sync_once holds person_sync._lock for the whole pass on its worker
    thread. The route reads that lock, so a pass in flight shows without
    the route touching the module."""
    assert person_sync._lock.acquire(blocking=False)
    try:
        assert _busy(app) == {"busy": True, "reasons": ["person sync running"]}
    finally:
        person_sync._lock.release()
    assert _busy(app) == IDLE


def test_a_benchmark_run(app):
    benchmark._active["bench-20260906-120000"] = {}
    assert _busy(app) == {"busy": True, "reasons": ["benchmark running"]}
    benchmark._active.clear()
    assert _busy(app) == IDLE


def test_an_import_mid_stream(app):
    """The counter is the real one: an import_stream suspended mid-stream
    counts, and closing it (the client dropping) releases it."""
    async def go():
        agen = importer.import_stream(b"not an export", "x.json", None)
        first = await agen.__anext__()   # suspended after its first event
        mid = busy.reasons()
        await agen.aclose()
        return first, mid, busy.reasons()

    first, mid, after = asyncio.run(go())
    assert first["type"] == "error"
    assert mid == ["import running"]
    assert after == []


def test_a_backup_mid_copy(app, monkeypatch):
    seen = []

    def copying():
        seen.append(busy.reasons())
        return "snapshot"

    monkeypatch.setattr(db, "_backup_database", copying)
    assert db.backup_database() == "snapshot"
    assert seen == [["backup running"]]
    assert busy.reasons() == []


def test_a_failed_backup_still_clears_the_flag(app, monkeypatch):
    def boom():
        raise OSError("disk full")

    monkeypatch.setattr(db, "_backup_database", boom)
    with pytest.raises(OSError):
        db.backup_database()
    assert busy.reasons() == []


# ── the gate ────────────────────────────────────────────────────────────────

def test_loopback_answers_without_a_session_once_enrolled(app):
    _enrol(app)
    anon = _client(app)
    assert anon.get("/api/state").status_code == 401  # the gate is on
    assert anon.get("/api/busy").status_code == 200
    assert anon.get("/api/busy").json() == IDLE


def test_a_trusted_host_still_needs_a_session(app):
    tail = _client(app, base_url=f"https://{TAILNET}")
    assert tail.get("/api/busy").status_code == 401  # before enrolment
    owner = _enrol(app)
    assert tail.get("/api/busy").status_code == 401  # and after
    tail.cookies.set("cb_session", owner.cookies.get("cb_session"))
    assert tail.get("/api/busy").status_code == 200


def test_the_route_is_not_part_of_the_login_surface():
    assert busy.PATH == "/api/busy"
    assert busy.PATH not in LOGIN_SURFACE


# ── never content ───────────────────────────────────────────────────────────

def test_reasons_are_the_fixed_vocabulary_and_nothing_else(app, monkeypatch):
    """Everything running at once, each source carrying something the
    answer must never repeat: the response is every label in the module's
    order, and none of the markers appear anywhere in it."""
    chat_id, sid, device = 4242, "sid-secret-9", "phone-of-alex"
    task, repo, run_id = "renovate the Fairhaven kitchen", "acme-app", "bench-20260906-120000"
    _fake_round(chat_id)
    _fake_capture(sid, chat_id, device)
    _fake_guest_job(chat_id, task, repo)
    benchmark._active[run_id] = {"note": "AcmeCo timings"}
    monkeypatch.setattr(importer, "_in_flight", 1)
    monkeypatch.setattr(db, "_backups_running", 1)
    assert person_sync._lock.acquire(blocking=False)
    try:
        r = _client(app).get("/api/busy")
    finally:
        person_sync._lock.release()
    assert r.status_code == 200
    assert r.json() == {"busy": True, "reasons": list(busy.LABELS)}
    for marker in (str(chat_id), sid, device, task, repo, run_id, "AcmeCo"):
        assert marker not in r.text


def test_every_label_is_short_fixed_prose():
    assert len(set(busy.LABELS)) == len(busy.LABELS)
    for label in busy.LABELS:
        assert re.fullmatch(r"[a-z]+( [a-z]+){1,3}", label), label
        assert label.endswith(" running")
