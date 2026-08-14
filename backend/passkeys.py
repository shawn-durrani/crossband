"""Passkey (WebAuthn) credentials for the browser gate (#25, slice 2).

The everyday unlock becomes a passkey: Touch ID at the desk, Face ID on the
phone over the tailnet. The password (auth.py) stays as the fallback and the
recovery secret keeps its enrolment/reset role; a passkey replaces only the
password proof, never the session machinery. The implementation is the one
proven in membro#28 and spendglass#25, with crossband's two wrinkles:

- Per-origin enrolment: `localhost` and the tailnet hostname share no domain
  suffix, so each holds its own credential; an IP origin (127.0.0.1) can
  hold none at all (browser rule, verified live during the membro build) and
  quietly keeps the password-first lock screen.
- The tailnet Relying Party ID is the bare hostname - the SAME RP membro's
  tailnet origin uses, since RP IDs ignore ports. Both apps' discoverable
  credentials therefore sit under one RP in the keychain, so this app enrols
  with its own user display name ("crossband owner") to keep the browser's
  account picker legible. No security impact: verification is against THIS
  app's stored public keys and THIS app's exact origin.

Storage is the settings table, beside the password verifier: public key and
credential id only. The private half lives in the platform authenticator and
never reaches this process. Policy and storage live here; ceremonies
(challenge lifecycle, py_webauthn verification) live in the HTTP layer.
"""

import ipaddress
import json
import secrets
from urllib.parse import urlsplit

from . import db

CREDENTIALS_KEY = "webauthn_credentials"
USER_HANDLE_KEY = "webauthn_user_handle"


def rp_for_host(host: str | None, trusted_hosts) -> str | None:
    """The WebAuthn Relying Party ID a request on `host` may use, or None
    where passkeys cannot work: IPs never (invalid RP by browser rule),
    `localhost` always, any other hostname only if the operator listed it in
    `trusted_hosts` (the #25 outer boundary)."""
    h = (host or "").strip().lower().rstrip(".")
    if not h:
        return None
    try:
        ipaddress.ip_address(h.strip("[]"))
        return None
    except ValueError:
        pass
    if h == "localhost":
        return h
    if h in {str(t).strip().lower() for t in trusted_hosts}:
        return h
    return None


def origin_ok(origin: str | None, host: str | None) -> bool:
    """True iff `origin` (an Origin request header) names this same host with
    an acceptable scheme: https anywhere, plain http only for localhost."""
    try:
        parts = urlsplit((origin or "").strip())
        o_host = (parts.hostname or "").lower()
    except ValueError:
        return False
    h = (host or "").strip().lower().rstrip(".")
    if not o_host or o_host != h:
        return False
    if parts.scheme == "https":
        return True
    return parts.scheme == "http" and o_host == "localhost"


# ---- durable storage (the settings table, beside the verifier) ----

def list_credentials(con) -> list[dict]:
    """Every enrolled passkey, oldest first; malformed stored state reads as
    "none enrolled" so the lock screen always renders."""
    raw = db.get_setting(con, CREDENTIALS_KEY)
    if not raw:
        return []
    try:
        rows = json.loads(raw)
    except ValueError:
        return []
    return [r for r in rows if isinstance(r, dict)] if isinstance(rows, list) else []


def credentials_for_rp(con, rp_id: str) -> list[dict]:
    return [r for r in list_credentials(con) if r.get("rp_id") == rp_id]


def add_credential(con, rec: dict) -> None:
    rows = list_credentials(con)
    rows.append(rec)
    db.set_setting(con, CREDENTIALS_KEY, json.dumps(rows))
    con.commit()


def remove_credential(con, cred_id: str) -> bool:
    rows = list_credentials(con)
    kept = [r for r in rows if r.get("id") != cred_id]
    if len(kept) == len(rows):
        return False
    db.set_setting(con, CREDENTIALS_KEY, json.dumps(kept))
    con.commit()
    return True


def update_sign_count(con, cred_id: str, sign_count: int) -> None:
    """Persist the authenticator's signature counter so a cloned credential
    (counter going backwards) is detectable next login. Apple authenticators
    report a constant 0, which stores and verifies fine. Also stamps
    last_used_at (#88): a successful login is the one honest signal of which
    device a credential lives on."""
    import time
    rows = list_credentials(con)
    for r in rows:
        if r.get("id") == cred_id:
            r["sign_count"] = int(sign_count)
            r["last_used_at"] = time.time()
    db.set_setting(con, CREDENTIALS_KEY, json.dumps(rows))
    con.commit()


def set_label(con, cred_id: str, label: str) -> bool:
    """Owner-editable credential label (#88): mobile and desktop passkeys
    were indistinguishable twins. Bounded, plain text, empty allowed (the
    UI falls back to address + date)."""
    clean = " ".join((label or "").split())[:40]
    rows = list_credentials(con)
    hit = False
    for r in rows:
        if r.get("id") == cred_id:
            r["label"] = clean
            hit = True
    if hit:
        db.set_setting(con, CREDENTIALS_KEY, json.dumps(rows))
        con.commit()
    return hit


def user_handle(con) -> bytes:
    """One stable random user handle for the single owner: 16 random bytes,
    minted at first enrolment, personal-data-free by construction."""
    stored = db.get_setting(con, USER_HANDLE_KEY)
    if stored:
        try:
            return bytes.fromhex(stored)
        except ValueError:
            pass
    handle = secrets.token_bytes(16)
    db.set_setting(con, USER_HANDLE_KEY, handle.hex())
    con.commit()
    return handle
