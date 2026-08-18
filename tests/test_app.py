"""Boot smoke test: the app comes up keyless with the memory service down -
GET /api/state returns 200 with memory.available false - plus the
single-instance lockfile behaviour."""

import logging

import pytest
from fastapi.testclient import TestClient

from backend.app import acquire_lock, create_app
from backend.config import Settings


@pytest.fixture
def app(tmp_path):
    settings = Settings(data_dir=str(tmp_path / "data"),
                        memory_url="http://127.0.0.1:1")  # unroutable: memoryless
    return create_app(settings)


def test_state_boots_keyless_and_memoryless(app):
    with TestClient(app, base_url="http://127.0.0.1") as client:
        r = client.get("/api/state")
        assert r.status_code == 200
        data = r.json()
        assert data["memory"] == {"available": False, "url": "http://127.0.0.1:1"}
        assert data["memory_writes"] == {"failed": [], "pending": []}
        slugs = {p["slug"] for p in data["participants"]}
        assert slugs == {"claude", "gpt"}  # seeded default roster
        assert data["settings"]["shared_instructions"]
        assert data["chats"] == []
        assert data["running_chat_ids"] == []  # nothing generating at boot
        # Seed value for the frontend's global live-events
        # watermark - 0 on a fresh database with no messages yet.
        assert data["latest_message_id"] == 0


def test_latest_message_id_tracks_the_highest_row(app):
    """/api/state's watermark seed must reflect reality: a
    stale/zero value here would make the frontend under-replay on first
    connect (missing genuinely new messages) rather than over-replay
    (harmless extra dirty-marks)."""
    with TestClient(app, base_url="http://127.0.0.1") as client:
        chat = client.post("/api/chats", json={}).json()
        msg = client.post(f"/api/chats/{chat['id']}/notice", json={"text": "hi"}).json()
        assert client.get("/api/state").json()["latest_message_id"] == msg["id"]


def test_chat_crud_and_leave_hook(app):
    with TestClient(app, base_url="http://127.0.0.1") as client:
        chat = client.post("/api/chats", json={}).json()
        assert chat["web_enabled"] == 1
        assert len(chat["participant_ids"]) == 2

        got = client.get(f"/api/chats/{chat['id']}").json()
        assert got["chat"]["id"] == chat["id"]
        assert got["messages"] == []
        assert got["chat"]["context"]["memory"] == 0  # memoryless

        renamed = client.patch(f"/api/chats/{chat['id']}",
                               json={"title": "My chat"}).json()
        assert renamed["title"] == "My chat"
        assert renamed["title_upto"] == -1  # user-renamed: auto-title locked

        # leave hook queues fine with the service down (silently off)
        r = client.post(f"/api/chats/{chat['id']}/distill")
        assert r.json()["ok"] is True

        assert client.delete(f"/api/chats/{chat['id']}").json() == {"ok": True}


def test_settings_roundtrip(app):
    with TestClient(app, base_url="http://127.0.0.1") as client:
        out = client.patch("/api/settings",
                           json={"shared_instructions": "be brief"}).json()
        assert out["shared_instructions"] == "be brief"
        assert client.get("/api/settings").json()["shared_instructions"] == "be brief"


def test_lockfile_is_exclusive(tmp_path):
    lock_path = str(tmp_path / ".lock")
    held = acquire_lock(lock_path)
    # wait_s=0 keeps this instant: acquire_lock now waits for a predecessor that
    # may still be shutting down: right in production, pointless here
    # where the holder is this very process and never releases.
    with pytest.raises(RuntimeError, match="already running"):
        acquire_lock(lock_path, wait_s=0)
    held.close()


def test_archive_hides_nothing_deletes_nothing(app):
    """Archive = hidden from the sidebar list, fully intact underneath."""
    with TestClient(app, base_url="http://127.0.0.1") as client:
        chat = client.post("/api/chats", json={"title": "Sensitive demo chat"}).json()
        archived = client.patch(f"/api/chats/{chat['id']}",
                                json={"archived": True}).json()
        assert archived["archived_at"] is not None
        state = client.get("/api/state").json()
        row = next(c for c in state["chats"] if c["id"] == chat["id"])
        assert row["archived_at"] is not None  # flag travels; UI filters on it
        # still fully retrievable - nothing was deleted
        got = client.get(f"/api/chats/{chat['id']}").json()
        assert got["chat"]["title"] == "Sensitive demo chat"
        restored = client.patch(f"/api/chats/{chat['id']}",
                                json={"archived": False}).json()
        assert restored["archived_at"] is None


def test_voice_gain_clamped_roundtrip(app):
    with TestClient(app, base_url="http://127.0.0.1") as client:
        pid = client.get("/api/state").json()["participants"][0]["id"]
        r = client.patch(f"/api/participants/{pid}", json={"voice_gain": 0.6}).json()
        assert r["voice_gain"] == 0.6
        # #163: above 1.0 is a boost (relative weights, ducked client-side)
        r = client.patch(f"/api/participants/{pid}", json={"voice_gain": 2.0}).json()
        assert r["voice_gain"] == 2.0
        r = client.patch(f"/api/participants/{pid}", json={"voice_gain": 5}).json()
        assert r["voice_gain"] == 3.0  # clamped
        r = client.patch(f"/api/participants/{pid}", json={"voice_gain": 0.01}).json()
        assert r["voice_gain"] == 0.2  # clamped


# ---------- provider-aware reasoning_effort validation ----------

def test_create_participant_accepts_anthropic_adaptive(app):
    with TestClient(app, base_url="http://127.0.0.1") as client:
        r = client.post("/api/participants", json={
            "name": "Claude Deep", "provider": "anthropic",
            "model": "claude-opus-4-8", "reasoning_effort": "adaptive"})
        assert r.status_code == 200
        assert r.json()["reasoning_effort"] == "adaptive"


def test_create_participant_rejects_adaptive_for_openai(app):
    with TestClient(app, base_url="http://127.0.0.1") as client:
        r = client.post("/api/participants", json={
            "name": "GPT Deep", "provider": "openai",
            "model": "gpt-5.1", "reasoning_effort": "adaptive"})
        assert r.status_code == 400
        assert "reasoning_effort" in r.json()["detail"]


def test_create_participant_rejects_unknown_effort_value(app):
    with TestClient(app, base_url="http://127.0.0.1") as client:
        r = client.post("/api/participants", json={
            "name": "Bad Effort", "provider": "anthropic",
            "model": "claude-opus-4-8", "reasoning_effort": "turbo"})
        assert r.status_code == 400


def test_update_participant_rejects_adaptive_after_switching_to_openai(app):
    """A PATCH that changes provider AND effort together is validated against
    the EFFECTIVE (new) provider, not the seat's old one."""
    with TestClient(app, base_url="http://127.0.0.1") as client:
        pid = next(p["id"] for p in client.get("/api/state").json()["participants"]
                   if p["provider"] == "anthropic")
        r = client.patch(f"/api/participants/{pid}",
                         json={"provider": "openai", "reasoning_effort": "adaptive"})
        assert r.status_code == 400


def test_update_participant_reasoning_effort_validated_against_existing_provider(app):
    """A PATCH that only touches reasoning_effort (provider untouched) is
    still validated against the seat's current provider."""
    with TestClient(app, base_url="http://127.0.0.1") as client:
        pid = next(p["id"] for p in client.get("/api/state").json()["participants"]
                   if p["provider"] == "openai")
        r = client.patch(f"/api/participants/{pid}", json={"reasoning_effort": "adaptive"})
        assert r.status_code == 400
        r = client.patch(f"/api/participants/{pid}", json={"reasoning_effort": "high"})
        assert r.status_code == 200
        assert r.json()["reasoning_effort"] == "high"


@pytest.fixture
def _restore_logging():
    """`_configure_log_level` mutates process-global logging state (root
    logger + the "crossband" logger), so every test that exercises it must put both
    back exactly as found - otherwise one test's CROSSBAND_LOG_LEVEL leaks into
    every test that runs after it in the same process."""
    root = logging.getLogger()
    app_logger = logging.getLogger("crossband")
    root_level, root_handlers = root.level, list(root.handlers)
    app_level = app_logger.level
    yield
    root.setLevel(root_level)
    root.handlers[:] = root_handlers
    app_logger.setLevel(app_level)


def test_log_level_unset_leaves_logging_untouched(tmp_path, _restore_logging):
    """Default: CROSSBAND_LOG_LEVEL unset must be a no-op, so a deployment that
    never sets it behaves byte-for-byte as it did before this existed: the
    content-free INFO-level cache telemetry stays silent by default, same as
    every other "crossband.*" INFO log line."""
    root = logging.getLogger()
    before_level, before_handlers = root.level, list(root.handlers)
    settings = Settings(data_dir=str(tmp_path / "data"), log_level="",
                        memory_url="http://127.0.0.1:1")
    create_app(settings)
    assert root.level == before_level
    assert root.handlers == before_handlers


def test_log_level_set_raises_crossband_logger_verbosity(tmp_path, _restore_logging):
    """Set CROSSBAND_LOG_LEVEL=info (case-insensitive) for a deliberate sampling
    session and the "crossband.*" hierarchy - including providers.py's Claude-chat
    cache-telemetry line - becomes reachable."""
    settings = Settings(data_dir=str(tmp_path / "data"), log_level="info",
                        memory_url="http://127.0.0.1:1")
    create_app(settings)
    assert logging.getLogger("crossband.providers").getEffectiveLevel() <= logging.INFO


def test_log_level_unrecognized_value_is_ignored_not_fatal(tmp_path, _restore_logging, caplog):
    """A typo in CROSSBAND_LOG_LEVEL must never crash startup - it's a diagnostics
    knob, not a required setting - and is reported so the typo is easy to
    catch rather than silently doing nothing."""
    settings = Settings(data_dir=str(tmp_path / "data"), log_level="not-a-level",
                        memory_url="http://127.0.0.1:1")
    with caplog.at_level(logging.WARNING, logger="crossband"):
        create_app(settings)  # must not raise
    assert any("not-a-level" in r.message for r in caplog.records)
