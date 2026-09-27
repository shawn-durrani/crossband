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
- Sign-ins are stored hashed with their expiry (#471), so one outlives a
  restart, while sign-out, a reset and expiry still end it. A tampered or
  unknown cookie is refused, the stored hash is not itself a cookie, and a
  backup snapshot carries no sign-ins.
"""

import hashlib
import sqlite3
import time
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

from backend import auth, db
from backend.app import create_app
from backend.config import Settings
from backend.routers.voice import _ws_local

PASSWORD = "a-durable-owner-passphrase"
NEW_PASSWORD = "an-entirely-different-one"
TAILNET = "my-mac.my-tailnet.ts.net"


def _make_app(tmp_path):
    return create_app(Settings(data_dir=str(tmp_path / "data"),
                               memory_url="http://127.0.0.1:1",
                               trusted_hosts=TAILNET))


@pytest.fixture
def app(tmp_path):
    return _make_app(tmp_path)


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
                 "passkey_elsewhere": [], "app": "crossband",
                 "browser_origin": f"https://{TAILNET}"}


def test_unenrolled_trusted_host_gets_only_the_login_surface(app):
    c = _client(app, base_url=f"https://{TAILNET}")
    s = c.get("/api/auth/session")
    assert s.status_code == 200
    # the session answer must AGREE with the middleware: a tailnet caller on
    # an unenrolled install is held to the login surface, so it must not be
    # told "authenticated" - that renders a half-open app whose every data
    # fetch 401s. It gets the setup face instead.
    assert s.json() == {"enrolled": False, "authenticated": False,
                        "passkey": False, "passkey_elsewhere": [],
                        "app": "crossband", "browser_origin": f"https://{TAILNET}"}
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


def _rows():
    con = db.connect()
    try:
        return {r["sid_hash"]: r["expires_at"] for r in
                con.execute("SELECT sid_hash, expires_at FROM auth_sessions")}
    finally:
        con.close()


def _hash(sid):
    return hashlib.sha256(sid.encode()).hexdigest()


def _expire(sid):
    con = db.connect()
    try:
        con.execute("UPDATE auth_sessions SET expires_at = 1.0 "
                    "WHERE sid_hash = ?", (_hash(sid),))
        con.commit()
    finally:
        con.close()


def test_sessions_expire(app):
    _setup(app, _client(app))
    c = _client(app)
    c.post("/api/auth/login", json={"password": PASSWORD})
    sid = c.cookies.get("cb_session")
    _expire(sid)  # long past
    assert c.get("/api/state").status_code == 401
    assert _hash(sid) not in _rows()  # deleted on sight


def test_a_session_lasts_a_day():
    """The lifetime is unchanged by moving sessions to disk: 24 hours from
    sign-in, on the row and on the cookie alike."""
    assert auth.SESSION_TTL_S == 24 * 3600


# ── sign-ins outlive a restart (#471) ───────────────────────────────────────

def test_a_sign_in_survives_a_restart(app, tmp_path):
    """Every deploy is a restart, and every restart used to sign every
    browser out. A second app on the same data folder is the next start."""
    owner = _client(app)
    _setup(app, owner)
    sid = owner.cookies.get("cb_session")
    phone = _client(app, base_url=f"https://{TAILNET}")
    phone.post("/api/auth/login", json={"password": PASSWORD})

    after = _make_app(tmp_path)
    again = _client(after)
    again.cookies.set("cb_session", sid)
    assert again.get("/api/state").status_code == 200
    assert again.get("/api/auth/session").json()["authenticated"] is True
    tail = _client(after, base_url=f"https://{TAILNET}")
    tail.cookies.set("cb_session", phone.cookies.get("cb_session"))
    assert tail.get("/api/state").status_code == 200
    # the voice relays' socket guard reads the same store
    assert _ws_local(_ws_stub(after, cookies={"cb_session": sid})) is True


def test_only_the_hash_is_stored(app, tmp_path):
    owner = _client(app)
    _setup(app, owner)
    sid = owner.cookies.get("cb_session")
    rows = _rows()
    assert list(rows) == [_hash(sid)]
    assert rows[_hash(sid)] == pytest.approx(time.time() + auth.SESSION_TTL_S,
                                             abs=60)
    raw = b"".join(p.read_bytes() for p in (tmp_path / "data").glob("chat.db*"))
    assert sid.encode() not in raw


def test_the_stored_hash_is_not_a_cookie(app):
    """A copy of the database, or a backup, holds only hashes. Presenting a
    hash as the cookie gets nothing."""
    owner = _client(app)
    _setup(app, owner)
    stolen = _client(app)
    stolen.cookies.set("cb_session", _hash(owner.cookies.get("cb_session")))
    assert stolen.get("/api/state").status_code == 401


@pytest.mark.parametrize("forge", [
    lambda sid: sid[:-1] + ("A" if sid[-1] != "A" else "B"),  # one char off
    lambda sid: sid + "x",                                    # extended
    lambda sid: sid[:20],                                     # truncated
    lambda sid: "forged",                                     # unknown
    lambda sid: "x" * 5000,                                   # oversized
])
def test_a_tampered_or_unknown_cookie_is_refused(app, forge):
    owner = _client(app)
    _setup(app, owner)
    bad = _client(app)
    bad.cookies.set("cb_session", forge(owner.cookies.get("cb_session")))
    assert bad.get("/api/state").status_code == 401
    assert bad.get("/api/auth/session").json()["authenticated"] is False
    assert owner.get("/api/state").status_code == 200


def test_sign_out_stays_signed_out_across_a_restart(app, tmp_path):
    _setup(app, _client(app))
    c = _client(app)
    c.post("/api/auth/login", json={"password": PASSWORD})
    sid = c.cookies.get("cb_session")
    c.post("/api/auth/logout")
    assert _hash(sid) not in _rows()
    stale = _client(_make_app(tmp_path))
    stale.cookies.set("cb_session", sid)
    assert stale.get("/api/state").status_code == 401


def test_a_restart_clears_sign_ins_that_ran_out(app, tmp_path):
    _setup(app, _client(app))
    c = _client(app)
    c.post("/api/auth/login", json={"password": PASSWORD})
    gone = c.cookies.get("cb_session")
    _expire(gone)
    kept = _client(app)
    kept.post("/api/auth/login", json={"password": PASSWORD})
    _make_app(tmp_path)
    rows = _rows()
    assert _hash(gone) not in rows
    assert _hash(kept.cookies.get("cb_session")) in rows


def test_a_new_sign_in_clears_sign_ins_that_ran_out(app):
    _setup(app, _client(app))
    old = _client(app)
    old.post("/api/auth/login", json={"password": PASSWORD})
    _expire(old.cookies.get("cb_session"))
    _client(app).post("/api/auth/login", json={"password": PASSWORD})
    assert _hash(old.cookies.get("cb_session")) not in _rows()


def test_a_backup_carries_no_sign_ins(app, tmp_path):
    """Restoring a snapshot must not bring back a sign-in revoked after it was
    taken, so a snapshot keeps the table and drops its rows. The live
    database keeps them."""
    owner = _client(app)
    _setup(app, owner)
    sid = owner.cookies.get("cb_session")
    snap = db.backup_database()
    assert snap
    con = sqlite3.connect(snap)
    try:
        assert con.execute("SELECT COUNT(*) FROM auth_sessions").fetchone()[0] == 0
        assert con.execute("SELECT COUNT(*) FROM participants").fetchone()[0] > 0
    finally:
        con.close()
    with open(snap, "rb") as f:
        assert _hash(sid).encode() not in f.read()
    assert _hash(sid) in _rows()
    assert owner.get("/api/state").status_code == 200


def test_a_database_before_the_table_gains_it(tmp_path):
    """The table rides the migration ladder: a v31 database opens, gains an
    empty auth_sessions, and is stamped current."""
    data = tmp_path / "old"
    data.mkdir()
    con0 = sqlite3.connect(data / "chat.db")
    con0.execute("CREATE TABLE settings(key TEXT PRIMARY KEY, value TEXT)")
    con0.execute("PRAGMA user_version = 31")
    con0.commit()
    con0.close()
    db.configure(data)
    db.init()
    con = db.connect()
    try:
        assert con.execute("PRAGMA user_version").fetchone()[0] == db.SCHEMA_VERSION
        assert con.execute("SELECT COUNT(*) FROM auth_sessions").fetchone()[0] == 0
    finally:
        con.close()


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


def test_reset_revocation_holds_across_a_restart(app, tmp_path):
    """A reset is how a stolen cookie is killed. With sign-ins on disk, the
    kill has to be on disk too: only the resetting browser's new sign-in is
    left, and the next start doesn't bring the old ones back."""
    owner = _client(app)
    _setup(app, owner)
    thief = _client(app)
    thief.post("/api/auth/login", json={"password": PASSWORD})
    fixer = _client(app)
    fixer.post("/api/auth/reset", json={
        "recovery_secret": app.state.recovery_secret,
        "password": NEW_PASSWORD})
    assert list(_rows()) == [_hash(fixer.cookies.get("cb_session"))]
    after = _make_app(tmp_path)
    for gone in (owner, thief):
        c = _client(after)
        c.cookies.set("cb_session", gone.cookies.get("cb_session"))
        assert c.get("/api/state").status_code == 401
    kept = _client(after)
    kept.cookies.set("cb_session", fixer.cookies.get("cb_session"))
    assert kept.get("/api/state").status_code == 200


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
        # a tailnet user's browser: Tailscale serve adds the identity
        # header, and a trusted-host socket without it is refused (#363)
        headers={"tailscale-user-login": "owner@example.com",
                 **({} if origin is None else {"origin": origin})},
        cookies=cookies or {})


def test_ws_guard_open_before_enrolment_gated_after(app):
    assert _ws_local(_ws_stub(app)) is True
    _setup(app, _client(app))
    assert _ws_local(_ws_stub(app)) is False  # no session, no socket
    sid = auth.mint_session(app)
    assert _ws_local(_ws_stub(app, cookies={"cb_session": sid})) is True
    assert _ws_local(_ws_stub(app, cookies={"cb_session": "forged"})) is False


def test_ws_guard_holds_the_tailnet_before_enrolment(app):
    """The pre-enrolment posture has two answers, not one.

    Loopback is open before a password exists; a trusted non-loopback host is
    not, because the tailnet should never see more than the lock screen. The
    HTTP middleware has always drawn that line. This guard did not, so a
    tailnet client refused on every /api route could still open the metered
    ElevenLabs relays. The relays are the whole reason the guard exists."""
    assert _ws_local(_ws_stub(app)) is True                      # loopback: open
    assert _ws_local(_ws_stub(app, host=TAILNET)) is False        # tailnet: gated
    assert _ws_local(_ws_stub(app, host=TAILNET,
                              origin=f"https://{TAILNET}")) is False
    # A session still lets the tailnet through, before enrolment as after.
    sid = auth.mint_session(app)
    assert _ws_local(_ws_stub(app, host=TAILNET,
                              cookies={"cb_session": sid})) is True


def test_ws_guard_and_http_gate_agree_on_loopback(app):
    """One set, read by both guards. config._LOOPBACK_HOSTS answers a different
    question and carries 0.0.0.0; a bind address is not a caller, so it must
    never widen a gate."""
    assert "0.0.0.0" not in auth.GATE_LOOPBACK_HOSTS
    for host in auth.GATE_LOOPBACK_HOSTS:
        assert _ws_local(_ws_stub(app, host=host)) is True
    assert app.state.allowed_hosts >= auth.GATE_LOOPBACK_HOSTS


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
