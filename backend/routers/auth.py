"""The login surface (#25): session probe, enrolment, login, logout, reset.

JSON in, JSON out, because the frontend is a SPA that switches views off
`GET /api/auth/session` (the spendglass gate shape, carrying membro's
credential model). Every path here is reachable WITHOUT a session - that is
the definition of a login surface - and each write proves possession of
either the recovery secret (setup, reset) or the password (login).
Failures are uniform 403s: an anonymous caller learns nothing about which
part was wrong.
"""

import hmac
import logging

from fastapi import APIRouter, HTTPException, Request, Response
from pydantic import BaseModel, Field

from .. import auth, db

log = logging.getLogger("crossband.auth")

router = APIRouter(tags=["auth"])

# The ONLY /api paths an anonymous caller may reach once a password is
# enrolled (and the only ones a trusted-host caller may reach before then).
# app.py's middleware and the voice websockets both consult this set.
LOGIN_SURFACE = {
    "/api/auth/session", "/api/auth/setup", "/api/auth/login",
    "/api/auth/logout", "/api/auth/reset",
}


class SetupBody(BaseModel):
    recovery_secret: str = ""
    password: str = ""
    confirm: str | None = None  # optional: the SPA validates match client-side


class LoginBody(BaseModel):
    password: str = Field("", max_length=1024)


def require_session(request: Request) -> None:
    """Dependency for routes that must NEVER ride on the pre-enrolment open
    posture (passkey enrolment, credential management): a live session,
    full stop."""
    if not auth.request_session_ok(request):
        raise HTTPException(status_code=401, detail="not authenticated")


@router.get("/api/auth/session")
def session_state(request: Request) -> dict:
    """The gate's view-switch. `authenticated` is True on an unenrolled
    install by design: the gate is enrolment-activated (see backend/auth.py),
    so before a password exists the app behaves exactly as it always has,
    with `enrolled: false` telling the UI to offer setup."""
    enrolled = request.app.state.auth_enrolled
    return {
        "enrolled": enrolled,
        "authenticated": (not enrolled) or auth.request_session_ok(request),
        "passkey": False,  # slice 2 (#25) makes this real per-origin
    }


@router.post("/api/auth/setup")
def setup(body: SetupBody, request: Request, response: Response) -> dict:
    """First-run enrolment, recovery-gated. 409 once enrolled: changing a
    set password goes through /api/auth/reset, so a stray setup can never
    clobber one."""
    app = request.app
    if app.state.auth_enrolled:
        raise HTTPException(status_code=409,
                            detail="a password is already set - use reset")
    return _set_password(body, request, response)


@router.post("/api/auth/reset")
def reset(body: SetupBody, request: Request, response: Response) -> dict:
    """Recovery-gated replacement of the password. Revokes EVERY outstanding
    session: a reset is a recovery action, and a stolen cookie must die with
    the old password."""
    return _set_password(body, request, response, revoke_first=True)


def _set_password(body: SetupBody, request: Request, response: Response,
                  revoke_first: bool = False) -> dict:
    app = request.app
    if not hmac.compare_digest(body.recovery_secret or "",
                               app.state.recovery_secret):
        raise HTTPException(status_code=403,
                            detail="recovery secret does not match the one in "
                                   ".env (CROSSBAND_RECOVERY_SECRET) or this "
                                   "start's terminal output")
    if body.confirm is not None and body.password != body.confirm:
        raise HTTPException(status_code=400,
                            detail="the two passwords didn't match")
    if len(body.password) < auth.MIN_PASSWORD_LEN:
        raise HTTPException(status_code=400,
                            detail=f"password must be at least "
                                   f"{auth.MIN_PASSWORD_LEN} characters")
    con = db.connect()
    try:
        auth.set_owner_password(con, body.password)
    finally:
        con.close()
    if revoke_first:
        auth.revoke_all_sessions(app)
    app.state.auth_enrolled = True
    log.info("owner password %s - browser gate is now active",
             "reset" if revoke_first else "enrolled")
    auth.attach_session_cookie(response, auth.mint_session(app))
    return {"ok": True}


@router.post("/api/auth/login")
def login(body: LoginBody, request: Request, response: Response) -> dict:
    """The everyday password proof. The recovery secret is deliberately NOT
    accepted here; before enrolment there is nothing to check against, so
    login simply fails and the UI offers setup instead."""
    con = db.connect()
    try:
        ok = auth.check_owner_password(con, body.password)
    finally:
        con.close()
    if not ok:
        raise HTTPException(status_code=403, detail="wrong password")
    auth.attach_session_cookie(response, auth.mint_session(request.app))
    return {"ok": True}


@router.post("/api/auth/logout")
def logout(request: Request, response: Response) -> dict:
    """True server-side revocation: this exact instant invalidates every
    copy of the cookie anywhere, not just this browser's."""
    auth.revoke_session(request.app,
                        request.cookies.get(auth.SESSION_COOKIE))
    response.delete_cookie(auth.SESSION_COOKIE, path="/")
    return {"ok": True}
