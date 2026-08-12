"""Owner auth for the browser UI (#25): scrypt password + opaque sessions.

Crossband's gate has always been network position: loopback binding plus a
tailnet-only proxy. This module adds the credential layer, ported from
membro's model (membro#51/#27), because the fleet treats that model as
settled:

- The everyday login is a durable PASSWORD, stored only as a memory-hard
  scrypt verifier in the settings table. Never the recovery secret, never
  reversible, survives restarts.
- The RECOVERY SECRET (CROSSBAND_RECOVERY_SECRET, or a fresh random value
  per start) gates enrolment and reset. It is proof of operator access:
  a process that cannot read .env or the terminal cannot enrol itself a
  password and let itself in.
- Sessions are opaque, expiring, server-revocable ids in an httpOnly
  SameSite=Strict cookie. In-memory on purpose: a restart logs every
  browser out, matching membro, and keeps auth state out of the database
  beyond the one verifier row.

The gate is ENROLMENT-ACTIVATED: before a password exists, loopback keeps
exactly today's behaviour (open API, nagged by the startup banner), because
that is the shipped posture every existing test pins; a trusted (tailnet)
host gets only the login surface. The moment the owner enrols, every /api
route outside the login surface requires a session, loopback included. The
tradeoff is stated plainly: an install whose owner never enrols keeps the
old open-loopback posture, with a banner saying so.

Only mechanism lives here; the HTTP layer (routers/auth.py, app.py's
middleware) decides who may invoke what.
"""

import hashlib
import hmac
import json
import secrets
import time

from . import db

SESSION_COOKIE = "cb_session"
SESSION_TTL_S = 24 * 3600
VERIFIER_KEY = "owner_password_verifier"
MIN_PASSWORD_LEN = 8

# scrypt cost parameters, identical to membro's: ~16 MiB per derivation,
# instant for one interactive login, expensive at brute-force scale. Stored
# WITH each hash so they can be raised later without a migration.
_N = 2 ** 14
_R = 8
_P = 1
_DKLEN = 32
_SALT_BYTES = 16
_MAXMEM = 64 * 1024 * 1024


def hash_password(password: str) -> str:
    """A fresh salted scrypt verifier for `password`, as the JSON row to
    persist. A new random salt every call: the same password enrolled twice
    yields two different verifiers."""
    salt = secrets.token_bytes(_SALT_BYTES)
    dk = hashlib.scrypt(password.encode("utf-8"), salt=salt, n=_N, r=_R, p=_P,
                        dklen=_DKLEN, maxmem=_MAXMEM)
    return json.dumps({
        "alg": "scrypt", "n": _N, "r": _R, "p": _P, "dklen": _DKLEN,
        "salt": salt.hex(), "hash": dk.hex(),
    })


def verify_password(password: str, stored: str) -> bool:
    """True iff `password` matches the persisted verifier. Recomputes with the
    STORED parameters (so a future cost bump keeps old verifiers verifying)
    and compares constant-time. Malformed or absent verifier is a clean False,
    never an exception."""
    if not stored:
        return False
    try:
        rec = json.loads(stored)
        if rec.get("alg") != "scrypt":
            return False
        salt = bytes.fromhex(rec["salt"])
        expected = bytes.fromhex(rec["hash"])
        dk = hashlib.scrypt(password.encode("utf-8"), salt=salt, n=rec["n"],
                            r=rec["r"], p=rec["p"], dklen=rec["dklen"],
                            maxmem=_MAXMEM)
    except (ValueError, KeyError, TypeError):
        return False
    return hmac.compare_digest(dk, expected)


# ---- the one durable row ----

def is_enrolled(con) -> bool:
    return bool(db.get_setting(con, VERIFIER_KEY, ""))


def set_owner_password(con, password: str) -> None:
    """Persist (or replace) the verifier. Overwriting IS how reset works;
    this is operational auth state, not conversation history."""
    db.set_setting(con, VERIFIER_KEY, hash_password(password))
    con.commit()


def check_owner_password(con, password: str) -> bool:
    return verify_password(password, db.get_setting(con, VERIFIER_KEY, ""))


# ---- sessions (in-memory on app.state, revocable, expiring) ----

def mint_session(app) -> str:
    """A fresh random opaque sid, never derived from anything the client
    sent (no fixation)."""
    sid = secrets.token_urlsafe(32)
    app.state.auth_sessions[sid] = time.time() + SESSION_TTL_S
    return sid


def session_ok(app, sid: str | None) -> bool:
    """True only for a sid this process minted that hasn't expired; lazily
    evicts the expired so the store never grows unbounded."""
    if not sid:
        return False
    exp = app.state.auth_sessions.get(sid)
    if exp is None:
        return False
    if exp < time.time():
        app.state.auth_sessions.pop(sid, None)
        return False
    return True


def request_session_ok(request) -> bool:
    return session_ok(request.app, request.cookies.get(SESSION_COOKIE))


def machine_token_ok(request) -> bool:
    """The machine side-channel's credential (`/api/ingest` and the deploy
    notice route): True only when an `ingest_token` is configured AND this
    request bears it. Local tooling has no cookie jar, so once the browser
    gate is enrolled this bearer is its only way in. An unconfigured token
    is never permission - it just leaves the route to the gate's posture."""
    token = getattr(request.app.state.settings, "ingest_token", "") or ""
    if not token:
        return False
    got = (request.headers.get("authorization") or "").removeprefix("Bearer ").strip()
    return hmac.compare_digest(got, token)


def revoke_session(app, sid: str | None) -> None:
    app.state.auth_sessions.pop(sid or "", None)


def revoke_all_sessions(app) -> None:
    """Reset semantics: a recovery-gated password change invalidates every
    outstanding session, so a stolen cookie dies with the old password."""
    app.state.auth_sessions.clear()


def attach_session_cookie(response, sid: str) -> None:
    response.set_cookie(SESSION_COOKIE, sid, httponly=True, samesite="strict",
                        path="/", max_age=SESSION_TTL_S)


# ---- startup guidance (secret-safe after enrolment) ----

def startup_lines(*, enrolled: bool, secret_configured: bool,
                  secret: str) -> list[str]:
    """What to log at startup about the gate. The full recovery secret
    appears ONLY while nothing is enrolled AND no durable secret is
    configured - the one moment the operator genuinely needs to read it.
    After enrolment it never hits the log again: data/service.log
    accumulates, and a pasted log must not leak a live secret (the lesson
    spendglass#2 already paid for)."""
    if enrolled:
        return ["browser gate: enrolled - /api requires a session; reset "
                "needs the recovery secret (CROSSBAND_RECOVERY_SECRET, or "
                "this start's random one, shown only pre-enrolment)"]
    lines = ["browser gate: NO owner password enrolled - loopback API is "
             "open (pre-#25 posture); enrol from the app to close it"]
    if secret_configured:
        lines.append("enrolment recovery secret: configured "
                     "(CROSSBAND_RECOVERY_SECRET)")
    else:
        lines.append(f"enrolment RECOVERY SECRET (random this start): {secret}")
    return lines
