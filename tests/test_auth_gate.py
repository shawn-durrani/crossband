"""The browser gate (#25), slice 1: enrolment-activated sessions.

The contract under test:

- Before a password is enrolled, loopback keeps the historical open posture
  (every pre-#25 test in this suite pins that unchanged), while a trusted
  non-loopback host is held to the login surface.
- The moment a password is enrolled, EVERY /api route outside the login
  surface requires a session - loopback included - and the websocket guard
  enforces the same thing for the voice relays.
- Enrolment and reset are recovery-gated; login takes only the password;
  logout and reset revoke server-side; nothing on the login surface leaks
  the secret or the verifier.
"""

from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

from backend import auth
from backend.app import create_app
from backend.config import Settings
from backend.routers.voice import _ws_local

PASSWORD = "a-durable-owner-passphrase"
NEW_PASSWORD = "an-entirely-different-one"
TAILNET = "my-mac.my-tailnet.ts.net"


@pytest.fixture
def app(tmp_path):
    return create_app(Settings(data_dir=str(tmp_path / "data"),
                               memory_url="http://127.0.0.1:1",
                               trusted_hosts=TAILNET))


def _client(app, base_url="http://127.0.0.1"):
    return TestClient(app, base_url=base_url)


def _setup(app, client, recovery=None, password=PASSWORD):
    return client.post("/api/auth/setup", json={
        "recovery_secret": app.state.recovery_secret if recovery is None else recovery,
        "password": password,
    })


# ── pre-enrolment: loopback open, tailnet held to the login surface ─────────

def test_unenrolled_loopback_keeps_the_open_posture(app):
    c = _client(app)
    assert c.get("/api/state").status_code == 200
    s = c.get("/api/auth/session").json()
    assert s == {"enrolled": False, "authenticated": True, "passkey": False,
                 "passkey_elsewhere": []}


def test_unenrolled_trusted_host_gets_only_the_login_surface(app):
    c = _client(app, base_url=f"https://{TAILNET}")
    s = c.get("/api/auth/session")
    assert s.status_code == 200
    # the session answer must AGREE with the middleware: a tailnet caller on
    # an unenrolled install is held to the login surface, so it must not be
    # told "authenticated" - that renders a half-open app whose every data
    # fetch 401s. It gets the setup face instead.
    assert s.json() == {"enrolled": False, "authenticated": False,
                        "passkey": False, "passkey_elsewhere": []}
    r = c.get("/api/state")
    assert r.status_code == 401
    assert "enrol" in r.json()["detail"]


# ── enrolment flips the gate on ─────────────────────────────────────────────

def test_setup_requires_the_recovery_secret(app):
    c = _client(app)
    assert _setup(app, c, recovery="wrong").status_code == 403
    assert app.state.auth_enrolled is False
    assert "cb_session" not in c.cookies


def test_setup_enforces_minimum_length(app):
    assert _setup(app, _client(app), password="short").status_code == 400
    assert app.state.auth_enrolled is False


def test_setup_enrols_logs_in_and_activates_the_gate(app):
    owner = _client(app)
    assert _setup(app, owner).status_code == 200
    assert owner.cookies.get("cb_session")
    assert owner.get("/api/state").status_code == 200

    anon = _client(app)
    assert anon.get("/api/state").status_code == 401
    s = anon.get("/api/auth/session").json()
    assert s["enrolled"] is True and s["authenticated"] is False


def test_second_setup_is_refused_reset_is_the_path(app):
    _setup(app, _client(app))
    r = _setup(app, _client(app), password=NEW_PASSWORD)
    assert r.status_code == 409
    # original password still works
    assert _client(app).post("/api/auth/login",
                             json={"password": PASSWORD}).status_code == 200


# ── login / logout ──────────────────────────────────────────────────────────

def test_login_takes_only_the_password(app):
    _setup(app, _client(app))
    c = _client(app)
    # the recovery secret is NOT a login credential
    assert c.post("/api/auth/login",
                  json={"password": app.state.recovery_secret}).status_code == 403
    assert c.post("/api/auth/login",
                  json={"password": PASSWORD}).status_code == 200
    assert c.get("/api/state").status_code == 200


def test_logout_revokes_server_side(app):
    _setup(app, _client(app))
    c = _client(app)
    c.post("/api/auth/login", json={"password": PASSWORD})
    sid = c.cookies.get("cb_session")
    assert c.get("/api/state").status_code == 200
    c.post("/api/auth/logout")
    # the copied sid is dead everywhere, not just cleared client-side
    stale = _client(app)
    stale.cookies.set("cb_session", sid)
    assert stale.get("/api/state").status_code == 401


def test_sessions_expire(app):
    _setup(app, _client(app))
    c = _client(app)
    c.post("/api/auth/login", json={"password": PASSWORD})
    sid = c.cookies.get("cb_session")
    app.state.auth_sessions[sid] = 1.0  # long past
    assert c.get("/api/state").status_code == 401
    assert sid not in app.state.auth_sessions  # lazily evicted


# ── reset ───────────────────────────────────────────────────────────────────

def test_reset_replaces_password_and_revokes_all_sessions(app):
    owner = _client(app)
    _setup(app, owner)
    other = _client(app)
    other.post("/api/auth/login", json={"password": PASSWORD})

    r = _client(app).post("/api/auth/reset", json={
        "recovery_secret": app.state.recovery_secret,
        "password": NEW_PASSWORD})
    assert r.status_code == 200
    # every pre-reset session died with the old password
    assert owner.get("/api/state").status_code == 401
    assert other.get("/api/state").status_code == 401
    assert _client(app).post("/api/auth/login",
                             json={"password": PASSWORD}).status_code == 403
    assert _client(app).post("/api/auth/login",
                             json={"password": NEW_PASSWORD}).status_code == 200


def test_reset_requires_the_recovery_secret(app):
    _setup(app, _client(app))
    r = _client(app).post("/api/auth/reset", json={
        "recovery_secret": "wrong", "password": NEW_PASSWORD})
    assert r.status_code == 403
    assert _client(app).post("/api/auth/login",
                             json={"password": PASSWORD}).status_code == 200


# ── the websocket guard mirrors the gate ────────────────────────────────────

def _ws_stub(app, host="127.0.0.1", origin=None, cookies=None):
    return SimpleNamespace(
        app=app,
        url=SimpleNamespace(hostname=host),
        headers={} if origin is None else {"origin": origin},
        cookies=cookies or {})


def test_ws_guard_open_before_enrolment_gated_after(app):
    assert _ws_local(_ws_stub(app)) is True
    _setup(app, _client(app))
    assert _ws_local(_ws_stub(app)) is False  # no session, no socket
    sid = auth.mint_session(app)
    assert _ws_local(_ws_stub(app, cookies={"cb_session": sid})) is True
    assert _ws_local(_ws_stub(app, cookies={"cb_session": "forged"})) is False


def test_ws_guard_still_enforces_host_and_origin(app):
    sid = auth.mint_session(app)
    ok = {"cb_session": sid}
    assert _ws_local(_ws_stub(app, host="evil.example", cookies=ok)) is False
    assert _ws_local(_ws_stub(app, origin="https://evil.example", cookies=ok)) is False
    assert _ws_local(_ws_stub(app, host=TAILNET,
                              origin=f"https://{TAILNET}", cookies=ok)) is True


# ── nothing leaks ───────────────────────────────────────────────────────────

def test_login_surface_leaks_no_secret_or_verifier(app, tmp_path):
    secret = app.state.recovery_secret
    _setup(app, _client(app))
    c = _client(app)
    surfaces = [
        c.get("/api/auth/session").text,
        c.post("/api/auth/login", json={"password": "nope"}).text,
        c.post("/api/auth/reset", json={"recovery_secret": "nope",
                                        "password": "x" * 12}).text,
        c.get("/api/state").text,  # the 401 body
    ]
    for text in surfaces:
        assert secret not in text
        assert PASSWORD not in text
        assert "scrypt" not in text
