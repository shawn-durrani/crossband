"""The login surface (#25): session probe, enrolment, login, logout, reset.

JSON in, JSON out, because the frontend is a SPA that switches views off
`GET /api/auth/session` (the spendglass gate shape, carrying membro's
credential model). Every path here is reachable WITHOUT a session - that is
the definition of a login surface - and each write proves possession of
either the recovery secret (setup, reset) or the password (login).
Failures are uniform 403s: an anonymous caller learns nothing about which
part was wrong.
"""

import datetime
import hmac
import json
import logging
import secrets
import time

import webauthn as webauthn_lib
from fastapi import APIRouter, Depends, HTTPException, Request, Response
from pydantic import BaseModel, Field
from webauthn.helpers import base64url_to_bytes, bytes_to_base64url
from webauthn.helpers.exceptions import (InvalidAuthenticationResponse,
                                         InvalidRegistrationResponse)
from webauthn.helpers.structs import (AuthenticatorAttachment,
                                      AuthenticatorSelectionCriteria,
                                      PublicKeyCredentialDescriptor,
                                      ResidentKeyRequirement,
                                      UserVerificationRequirement)

from .. import auth, db, passkeys

log = logging.getLogger("crossband.auth")

router = APIRouter(tags=["auth"])

# The ONLY /api paths an anonymous caller may reach once a password is
# enrolled (and the only ones a trusted-host caller may reach before then).
# app.py's middleware and the voice websockets both consult this set. The two
# passkey LOGIN steps belong here because they are how a session comes to
# exist; passkey ENROLMENT is deliberately absent - it requires a session.
LOGIN_SURFACE = {
    "/api/auth/session", "/api/auth/setup", "/api/auth/login",
    "/api/auth/logout", "/api/auth/reset",
    "/api/webauthn/login/options", "/api/webauthn/login",
}

WEBAUTHN_CEREMONY_TTL_S = 300  # a Touch ID prompt answers in far less


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
    with `enrolled: false` telling the UI to offer setup. `passkey` is
    per-origin: true only when THIS request's host is a valid Relying Party
    (localhost or a trusted host, never an IP) with a credential enrolled -
    the same one-bit disclosure as `enrolled`."""
    enrolled = request.app.state.auth_enrolled
    rp = passkeys.rp_for_host(request.url.hostname,
                              request.app.state.allowed_hosts)
    has_passkey = False
    if enrolled and rp:
        con = db.connect()
        try:
            has_passkey = bool(passkeys.credentials_for_rp(con, rp))
        finally:
            con.close()
    return {
        "enrolled": enrolled,
        "authenticated": (not enrolled) or auth.request_session_ok(request),
        "passkey": has_passkey,
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


# ── passkeys (#25 slice 2): WebAuthn as the everyday unlock ────────────────
# The membro#28 implementation, JSON-shaped for the SPA. Ceremonies are
# server-side, single-use, and in-memory (an abandoned Touch ID prompt should
# evaporate; a restart mid-ceremony just means tapping the button again).


class WebAuthnFinishBody(BaseModel):
    cid: str = Field("", max_length=128)
    credential: dict = Field(default_factory=dict)


def _ceremony_mint(app, purpose: str, challenge: bytes, rp_id: str,
                   origin: str) -> str:
    now = time.time()
    pending = app.state.webauthn_pending
    for k, v in list(pending.items()):
        if v["expires"] < now:
            pending.pop(k, None)
    cid = secrets.token_urlsafe(24)
    pending[cid] = {"purpose": purpose, "challenge": challenge,
                    "rp_id": rp_id, "origin": origin,
                    "expires": now + WEBAUTHN_CEREMONY_TTL_S}
    return cid


def _ceremony_take(app, cid: str, purpose: str) -> dict | None:
    pend = app.state.webauthn_pending.pop(cid or "", None)
    if not pend or pend["purpose"] != purpose or pend["expires"] < time.time():
        return None
    return pend


def _ceremony_context(request: Request) -> tuple[str, str] | None:
    """(origin, rp_id) for a ceremony on this request, or None where passkeys
    cannot work. The RP comes from the request's own hostname gated by the
    allowed-hosts boundary; the origin is the browser's Origin header,
    required to name that same host. 127.0.0.1 yields None and keeps the
    password-first lock screen."""
    host = request.url.hostname or ""
    rp = passkeys.rp_for_host(host, request.app.state.allowed_hosts)
    origin = request.headers.get("origin", "")
    if not rp or not passkeys.origin_ok(origin, host):
        return None
    return origin, rp


@router.post("/api/webauthn/register/options",
             dependencies=[Depends(require_session)])
def webauthn_register_options(request: Request) -> dict:
    """Start enrolment for the origin the page is open on. Requires a live
    session AND an enrolled password: a passkey always has the password
    behind it as the fallback."""
    app = request.app
    if not app.state.auth_enrolled:
        raise HTTPException(status_code=409,
                            detail="set an owner password first - a passkey "
                                   "needs it as the fallback")
    ctx = _ceremony_context(request)
    if ctx is None:
        raise HTTPException(status_code=400, detail=(
            "passkeys need http://localhost:"
            f"{app.state.settings.port} or a trusted https host; an IP "
            "address such as 127.0.0.1 cannot hold one (browser rule)"))
    origin, rp = ctx
    con = db.connect()
    try:
        handle = passkeys.user_handle(con)
        existing = passkeys.credentials_for_rp(con, rp)
    finally:
        con.close()
    opts = webauthn_lib.generate_registration_options(
        rp_id=rp, rp_name="crossband",
        user_id=handle, user_name="crossband owner",
        # Distinct display name on purpose: on the tailnet this RP is shared
        # with membro (RP IDs ignore ports), and the keychain's account
        # picker must stay legible with both apps' credentials under it.
        user_display_name="crossband owner",
        authenticator_selection=AuthenticatorSelectionCriteria(
            authenticator_attachment=AuthenticatorAttachment.PLATFORM,
            resident_key=ResidentKeyRequirement.REQUIRED,
            user_verification=UserVerificationRequirement.REQUIRED),
        exclude_credentials=[
            PublicKeyCredentialDescriptor(id=base64url_to_bytes(r["id"]))
            for r in existing])
    cid = _ceremony_mint(app, "register", opts.challenge, rp, origin)
    return {"cid": cid,
            "publicKey": json.loads(webauthn_lib.options_to_json(opts))}


@router.post("/api/webauthn/register",
             dependencies=[Depends(require_session)])
def webauthn_register(body: WebAuthnFinishBody, request: Request) -> dict:
    """Finish enrolment: verify the attestation against the challenge and
    origin recorded at options time, then persist ONLY public material.
    py_webauthn owns the parsing of these attacker-suppliable bytes."""
    pend = _ceremony_take(request.app, body.cid, "register")
    if pend is None:
        raise HTTPException(status_code=400,
                            detail="enrolment challenge missing or expired; "
                                   "start again")
    try:
        v = webauthn_lib.verify_registration_response(
            credential=body.credential,
            expected_challenge=pend["challenge"],
            expected_rp_id=pend["rp_id"],
            expected_origin=pend["origin"],
            require_user_verification=True)
    except (InvalidRegistrationResponse, ValueError):
        raise HTTPException(status_code=400,
                            detail="the browser's enrolment response did not "
                                   "verify")
    rec = {
        "id": bytes_to_base64url(v.credential_id),
        "public_key": bytes_to_base64url(v.credential_public_key),
        "sign_count": v.sign_count,
        "rp_id": pend["rp_id"],
        "origin": pend["origin"],
        "created_at": datetime.datetime.now(datetime.timezone.utc)
                      .isoformat(timespec="seconds"),
        "backed_up": bool(v.credential_backed_up),
    }
    con = db.connect()
    try:
        if any(r.get("id") == rec["id"] for r in passkeys.list_credentials(con)):
            raise HTTPException(status_code=409,
                                detail="this passkey is already enrolled")
        passkeys.add_credential(con, rec)
    finally:
        con.close()
    log.info("passkey enrolled for rp %s", pend["rp_id"])
    return {"ok": True, "credential": {k: rec[k] for k in
                                       ("id", "rp_id", "origin", "created_at")}}


@router.post("/api/webauthn/login/options")
def webauthn_login_options(request: Request) -> dict:
    """Anonymous by design (the lock screen), disclosing only what the lock
    screen already says: a passkey exists here. Credentials are enrolled as
    discoverable, so allowCredentials stays empty and no credential ids ever
    reach an anonymous caller."""
    ctx = _ceremony_context(request)
    if ctx is None:
        raise HTTPException(status_code=400,
                            detail="passkey unlock is not available on this "
                                   "origin")
    origin, rp = ctx
    con = db.connect()
    try:
        enrolled_here = bool(passkeys.credentials_for_rp(con, rp))
    finally:
        con.close()
    if not enrolled_here:
        raise HTTPException(status_code=400,
                            detail="no passkey is enrolled for this origin")
    opts = webauthn_lib.generate_authentication_options(
        rp_id=rp, user_verification=UserVerificationRequirement.REQUIRED)
    cid = _ceremony_mint(request.app, "login", opts.challenge, rp, origin)
    return {"cid": cid,
            "publicKey": json.loads(webauthn_lib.options_to_json(opts))}


@router.post("/api/webauthn/login")
def webauthn_login(body: WebAuthnFinishBody, request: Request,
                   response: Response) -> dict:
    """The passkey PROOF step. Failures are uniform 403s like a wrong
    password: an anonymous caller learns nothing about which part failed. A
    success mints exactly the session a password login mints."""
    app = request.app
    pend = _ceremony_take(app, body.cid, "login")
    if pend is None:
        raise HTTPException(status_code=403,
                            detail="unlock challenge missing or expired; "
                                   "try again")
    cred_id = str(body.credential.get("rawId")
                  or body.credential.get("id") or "")
    con = db.connect()
    try:
        rec = next((r for r in passkeys.credentials_for_rp(con, pend["rp_id"])
                    if r.get("id") == cred_id), None)
        if rec is None:
            raise HTTPException(status_code=403, detail="passkey not recognised")
        try:
            v = webauthn_lib.verify_authentication_response(
                credential=body.credential,
                expected_challenge=pend["challenge"],
                expected_rp_id=pend["rp_id"],
                expected_origin=pend["origin"],
                credential_public_key=base64url_to_bytes(rec["public_key"]),
                credential_current_sign_count=int(rec.get("sign_count") or 0),
                require_user_verification=True)
        except (InvalidAuthenticationResponse, ValueError):
            raise HTTPException(status_code=403, detail="passkey not recognised")
        passkeys.update_sign_count(con, cred_id, v.new_sign_count)
    finally:
        con.close()
    auth.attach_session_cookie(response, auth.mint_session(app))
    return {"ok": True}


@router.get("/api/webauthn/credentials",
            dependencies=[Depends(require_session)])
def webauthn_credentials() -> dict:
    """The management listing: metadata only, never the public key."""
    con = db.connect()
    try:
        rows = passkeys.list_credentials(con)
    finally:
        con.close()
    return {"credentials": [
        {k: r.get(k) for k in ("id", "rp_id", "origin", "created_at",
                               "backed_up")}
        for r in rows]}


@router.post("/api/webauthn/credentials/remove",
             dependencies=[Depends(require_session)])
def webauthn_remove(body: dict, request: Request) -> dict:
    """POST, not DELETE, so the cross-site middleware guard covers it like
    every other write. Removal can never lock the owner out: the password
    always remains."""
    con = db.connect()
    try:
        removed = passkeys.remove_credential(con, str((body or {}).get("id", "")))
    finally:
        con.close()
    if not removed:
        raise HTTPException(status_code=404, detail="no passkey with that id")
    return {"ok": True}
