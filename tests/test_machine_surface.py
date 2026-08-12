"""The machine side-channel vs the browser gate (#62).

Once a password is enrolled, the gate requires a session on every /api
route outside the login surface - which silently locked out BOTH machine
routes (`/api/ingest` and `/api/chats/{id}/notice`): local tooling has no
cookie jar, and ingest's own bearer check sat behind the gate, unreachable.
The deploy watcher's notices 401'd, so a working deploy looked identical
to a dead one.

The contract under test:

- A configured `ingest_token` is the machine credential for both routes,
  and a valid bearer passes the gate itself - no session needed.
- A missing or wrong bearer is still 401 once enrolled: the gate gains
  exactly one authenticated caller, it does not weaken.
- The token never buys anything BEYOND the two machine routes.
- Unconfigured installs keep the historical posture: loopback-open before
  enrolment, locked after.
"""

import pytest
from fastapi.testclient import TestClient

from backend.app import create_app
from backend.config import Settings

PASSWORD = "a-durable-owner-passphrase"
TOKEN = "machine-side-channel-token"


def _app(tmp_path, **kw):
    return create_app(Settings(data_dir=str(tmp_path / "data"),
                               memory_url="http://127.0.0.1:1", **kw))


def _enrol(app):
    """Enrol the owner and return their (session-bearing) client."""
    owner = TestClient(app, base_url="http://127.0.0.1")
    r = owner.post("/api/auth/setup", json={
        "recovery_secret": app.state.recovery_secret, "password": PASSWORD})
    assert r.status_code == 200
    return owner


def _anon(app, token=None):
    c = TestClient(app, base_url="http://127.0.0.1")
    if token is not None:
        c.headers["Authorization"] = f"Bearer {token}"
    return c


def _ingest_body(chat_id):
    return {"source": "deploy-watcher", "target_chat": chat_id,
            "dedupe_key": "k1", "payload": {"title": "merged #1"}}


# ── enrolled + token configured: the bearer is the machine's way in ─────────

def test_enrolled_notice_with_token_lands(tmp_path):
    app = _app(tmp_path, ingest_token=TOKEN)
    owner = _enrol(app)
    chat = owner.post("/api/chats", json={}).json()

    r = _anon(app, TOKEN).post(f"/api/chats/{chat['id']}/notice",
                               json={"text": "deployed + healthy"})
    assert r.status_code == 200
    assert r.json()["speaker"] == "system"


def test_enrolled_notice_without_token_is_401(tmp_path):
    app = _app(tmp_path, ingest_token=TOKEN)
    owner = _enrol(app)
    chat = owner.post("/api/chats", json={}).json()

    r = _anon(app).post(f"/api/chats/{chat['id']}/notice", json={"text": "x"})
    assert r.status_code == 401


def test_enrolled_notice_with_wrong_token_is_401(tmp_path):
    app = _app(tmp_path, ingest_token=TOKEN)
    owner = _enrol(app)
    chat = owner.post("/api/chats", json={}).json()

    r = _anon(app, "not-the-token").post(
        f"/api/chats/{chat['id']}/notice", json={"text": "x"})
    assert r.status_code == 401


def test_enrolled_ingest_with_token_lands(tmp_path):
    """The regression that motivated the middleware half: ingest's own
    bearer check was unreachable behind the gate once enrolled."""
    app = _app(tmp_path, ingest_token=TOKEN)
    owner = _enrol(app)
    chat = owner.post("/api/chats", json={}).json()

    r = _anon(app, TOKEN).post("/api/ingest", json=_ingest_body(chat["id"]))
    assert r.status_code == 200
    assert r.json()["deduped"] is False


def test_enrolled_ingest_without_token_is_401(tmp_path):
    app = _app(tmp_path, ingest_token=TOKEN)
    owner = _enrol(app)
    chat = owner.post("/api/chats", json={}).json()

    r = _anon(app).post("/api/ingest", json=_ingest_body(chat["id"]))
    assert r.status_code == 401


def test_token_buys_nothing_beyond_the_machine_routes(tmp_path):
    """The bearer is a side-channel credential, not a session: every other
    gated route still refuses it."""
    app = _app(tmp_path, ingest_token=TOKEN)
    _enrol(app)

    assert _anon(app, TOKEN).get("/api/state").status_code == 401


# ── token configured, not yet enrolled: required outright, like ingest ──────

def test_configured_token_is_required_pre_enrolment_too(tmp_path):
    app = _app(tmp_path, ingest_token=TOKEN)
    c = _anon(app)
    chat = c.post("/api/chats", json={}).json()  # loopback still open

    assert c.post(f"/api/chats/{chat['id']}/notice",
                  json={"text": "x"}).status_code == 401
    assert _anon(app, TOKEN).post(f"/api/chats/{chat['id']}/notice",
                                  json={"text": "x"}).status_code == 200


# ── no token configured: the historical posture is unchanged ────────────────

def test_unconfigured_unenrolled_loopback_notice_stays_open(tmp_path):
    app = _app(tmp_path)
    c = _anon(app)
    chat = c.post("/api/chats", json={}).json()

    assert c.post(f"/api/chats/{chat['id']}/notice",
                  json={"text": "x"}).status_code == 200


def test_unconfigured_enrolled_notice_stays_locked(tmp_path):
    app = _app(tmp_path)
    owner = _enrol(app)
    chat = owner.post("/api/chats", json={}).json()

    r = _anon(app).post(f"/api/chats/{chat['id']}/notice", json={"text": "x"})
    assert r.status_code == 401
