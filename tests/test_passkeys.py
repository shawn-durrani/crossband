"""Passkey (WebAuthn) login for the browser gate - #25 slice 2.

The acceptance surface, mirroring the proven membro#28 / spendglass#25
suites with crossband's wrinkles:

- Enrolment needs a live session AND an enrolled password (the fallback must
  exist before the passkey does); the two login steps are the only anonymous
  passkey paths.
- Per-origin: localhost and the trusted tailnet host enrol separately, an IP
  origin refuses ceremonies outright, and an assertion signed for one origin
  is refused on another.
- A successful assertion mints the same opaque session a password login
  mints; failed, replayed, and counter-regressed assertions mint nothing.
- Credentials persist across a restart; in-flight ceremonies do not; removal
  never locks the owner out.

Keyless and offline: the "authenticator" is a software P-256 passkey built
on py_webauthn's own dependencies, byte-identical in layout to a platform
authenticator's output.
"""

import hashlib
import json
import secrets as pysecrets

import cbor2
import pytest
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.asymmetric import ec
from fastapi.testclient import TestClient
from webauthn.helpers import base64url_to_bytes, bytes_to_base64url

from backend import db, passkeys
from backend.app import create_app
from backend.config import Settings

PASSWORD = "a-durable-owner-passphrase"
TAILNET = "my-mac.my-tailnet.ts.net"
LOCAL_ORIGIN = "http://localhost"
TAILNET_ORIGIN = f"https://{TAILNET}"


class SoftPasskey:
    """A P-256 passkey behaving like a platform authenticator for one RP."""

    def __init__(self, rp_id="localhost"):
        self.rp_id = rp_id
        self.key = ec.generate_private_key(ec.SECP256R1())
        self.cred_id = pysecrets.token_bytes(16)

    def _cose_key(self) -> bytes:
        nums = self.key.public_key().public_numbers()
        return cbor2.dumps({1: 2, 3: -7, -1: 1,
                            -2: nums.x.to_bytes(32, "big"),
                            -3: nums.y.to_bytes(32, "big")})

    @staticmethod
    def _client_data(kind, challenge_b64u, origin) -> bytes:
        return json.dumps({"type": kind, "challenge": challenge_b64u,
                           "origin": origin, "crossOrigin": False}).encode()

    def register(self, public_key_options, origin) -> dict:
        cdj = self._client_data("webauthn.create",
                                public_key_options["challenge"], origin)
        auth_data = (hashlib.sha256(self.rp_id.encode()).digest()
                     + bytes([0x01 | 0x04 | 0x40]) + (0).to_bytes(4, "big")
                     + bytes(16) + len(self.cred_id).to_bytes(2, "big")
                     + self.cred_id + self._cose_key())
        att = cbor2.dumps({"fmt": "none", "attStmt": {}, "authData": auth_data})
        return {"id": bytes_to_base64url(self.cred_id),
                "rawId": bytes_to_base64url(self.cred_id),
                "type": "public-key", "clientExtensionResults": {},
                "response": {"clientDataJSON": bytes_to_base64url(cdj),
                             "attestationObject": bytes_to_base64url(att)}}

    def assertion(self, public_key_options, origin, *, sign_count=0) -> dict:
        cdj = self._client_data("webauthn.get",
                                public_key_options["challenge"], origin)
        auth_data = (hashlib.sha256(self.rp_id.encode()).digest()
                     + bytes([0x01 | 0x04]) + sign_count.to_bytes(4, "big"))
        sig = self.key.sign(auth_data + hashlib.sha256(cdj).digest(),
                            ec.ECDSA(hashes.SHA256()))
        return {"id": bytes_to_base64url(self.cred_id),
                "rawId": bytes_to_base64url(self.cred_id),
                "type": "public-key", "clientExtensionResults": {},
                "response": {"clientDataJSON": bytes_to_base64url(cdj),
                             "authenticatorData": bytes_to_base64url(auth_data),
                             "signature": bytes_to_base64url(sig),
                             "userHandle": None}}


@pytest.fixture
def app(tmp_path):
    return create_app(Settings(data_dir=str(tmp_path / "data"),
                               memory_url="http://127.0.0.1:1",
                               trusted_hosts=TAILNET))


def _client(app, base_url=LOCAL_ORIGIN):
    return TestClient(app, base_url=base_url)


def _owner(app, base_url=LOCAL_ORIGIN):
    c = _client(app, base_url=base_url)
    if not app.state.auth_enrolled:
        r = c.post("/api/auth/setup", json={
            "recovery_secret": app.state.recovery_secret, "password": PASSWORD})
        assert r.status_code == 200
    else:
        assert c.post("/api/auth/login",
                      json={"password": PASSWORD}).status_code == 200
    return c


def _enrol_passkey(client, origin=LOCAL_ORIGIN, rp="localhost", pk=None):
    pk = pk or SoftPasskey(rp)
    o = client.post("/api/webauthn/register/options", headers={"Origin": origin})
    assert o.status_code == 200, o.text
    r = client.post("/api/webauthn/register",
                    json={"cid": o.json()["cid"],
                          "credential": pk.register(o.json()["publicKey"], origin)},
                    headers={"Origin": origin})
    return pk, r


def _passkey_login(client, pk, origin=LOCAL_ORIGIN, *, sign_count=0, mangle=None):
    o = client.post("/api/webauthn/login/options", headers={"Origin": origin})
    if o.status_code != 200:
        return o
    cred = pk.assertion(o.json()["publicKey"],
                        origin if mangle is None else mangle,
                        sign_count=sign_count)
    return client.post("/api/webauthn/login",
                       json={"cid": o.json()["cid"], "credential": cred},
                       headers={"Origin": origin})


# ── policy ──────────────────────────────────────────────────────────────────

def test_rp_policy():
    trusted = {TAILNET, "127.0.0.1", "localhost", "::1"}  # allowed_hosts shape
    assert passkeys.rp_for_host("localhost", trusted) == "localhost"
    assert passkeys.rp_for_host("127.0.0.1", trusted) is None  # IP never an RP
    assert passkeys.rp_for_host("::1", trusted) is None
    assert passkeys.rp_for_host(TAILNET, trusted) == TAILNET
    assert passkeys.rp_for_host("evil.example", trusted) is None
    assert passkeys.origin_ok(TAILNET_ORIGIN, TAILNET)
    assert not passkeys.origin_ok(f"http://{TAILNET}", TAILNET)  # https only
    assert passkeys.origin_ok("http://localhost", "localhost")
    assert not passkeys.origin_ok("https://evil.example", "localhost")


def test_enrolment_requires_password_then_session(app):
    # no password yet: no session can exist, so even a loopback caller on the
    # open posture is refused (the session dependency answers first; the
    # handler's own password check is defence-in-depth behind it)
    c = _client(app)
    assert c.post("/api/webauthn/register/options",
                  headers={"Origin": LOCAL_ORIGIN}).status_code == 401
    # password enrolled: an anonymous caller has no session, so 401
    _owner(app)
    anon = _client(app)
    assert anon.post("/api/webauthn/register/options",
                     headers={"Origin": LOCAL_ORIGIN}).status_code == 401
    assert anon.get("/api/webauthn/credentials").status_code == 401


def test_ip_origin_refuses_ceremonies(app):
    owner = _owner(app, base_url="http://127.0.0.1")
    r = owner.post("/api/webauthn/register/options",
                   headers={"Origin": "http://127.0.0.1"})
    assert r.status_code == 400
    assert "localhost" in r.json()["detail"]


# ── the round trip ──────────────────────────────────────────────────────────

def test_enrol_then_unlock(app):
    pk, r = _enrol_passkey(_owner(app))
    assert r.status_code == 200 and r.json()["ok"]

    visitor = _client(app)
    assert visitor.get("/api/state").status_code == 401
    assert _passkey_login(visitor, pk).status_code == 200
    assert visitor.cookies.get("cb_session")
    assert visitor.get("/api/state").status_code == 200


def test_session_flag_is_per_origin(app):
    owner = _owner(app)
    assert _client(app).get("/api/auth/session").json()["passkey"] is False
    _enrol_passkey(owner)
    assert _client(app).get("/api/auth/session").json()["passkey"] is True
    # the same install seen via the IP: no passkey offered, password form
    ip = _client(app, base_url="http://127.0.0.1")
    assert ip.get("/api/auth/session").json()["passkey"] is False
    # and via the (un-enrolled) tailnet host: none there either
    tn = _client(app, base_url=TAILNET_ORIGIN)
    assert tn.get("/api/auth/session").json()["passkey"] is False


def test_tailnet_enrols_and_unlocks_separately(app):
    local_pk, _ = _enrol_passkey(_owner(app))
    remote_owner = _owner(app, base_url=TAILNET_ORIGIN)
    remote_pk, r = _enrol_passkey(remote_owner, origin=TAILNET_ORIGIN, rp=TAILNET)
    assert r.status_code == 200

    anon_remote = _client(app, base_url=TAILNET_ORIGIN)
    assert _passkey_login(anon_remote, remote_pk,
                          origin=TAILNET_ORIGIN).status_code == 200
    # the localhost credential cannot serve the tailnet RP
    assert _passkey_login(_client(app, base_url=TAILNET_ORIGIN), local_pk,
                          origin=TAILNET_ORIGIN).status_code == 403


def test_password_fallback_survives_passkeys(app):
    _enrol_passkey(_owner(app))
    c = _client(app)
    assert c.post("/api/auth/login", json={"password": PASSWORD}).status_code == 200


# ── what must fail, fails ───────────────────────────────────────────────────

def test_foreign_origin_assertion_refused(app):
    pk, _ = _enrol_passkey(_owner(app))
    c = _client(app)
    assert _passkey_login(c, pk, mangle="https://evil.example").status_code == 403
    assert not c.cookies.get("cb_session")


def test_unknown_credential_refused(app):
    _enrol_passkey(_owner(app))
    assert _passkey_login(_client(app), SoftPasskey()).status_code == 403


def test_challenge_single_use(app):
    pk, _ = _enrol_passkey(_owner(app))
    c = _client(app)
    o = c.post("/api/webauthn/login/options",
               headers={"Origin": LOCAL_ORIGIN}).json()
    body = {"cid": o["cid"],
            "credential": pk.assertion(o["publicKey"], LOCAL_ORIGIN)}
    assert c.post("/api/webauthn/login", json=body,
                  headers={"Origin": LOCAL_ORIGIN}).status_code == 200
    assert _client(app).post("/api/webauthn/login", json=body,
                             headers={"Origin": LOCAL_ORIGIN}).status_code == 403


def test_sign_count_regression_refused(app):
    pk, _ = _enrol_passkey(_owner(app))
    assert _passkey_login(_client(app), pk, sign_count=5).status_code == 200
    assert _passkey_login(_client(app), pk, sign_count=3).status_code == 403
    assert _passkey_login(_client(app), pk, sign_count=6).status_code == 200


def test_duplicate_enrolment_refused(app):
    owner = _owner(app)
    pk, first = _enrol_passkey(owner)
    assert first.status_code == 200
    assert _enrol_passkey(owner, pk=pk)[1].status_code == 409


def test_login_options_disclose_no_key_material(app):
    """Narrowed since the sheet-scoping change (#204, owner call): the login
    OPTIONS now deliberately name this app's credential ids, so the line
    held here is what still matters - no surface ever carries key
    material."""
    pk, _ = _enrol_passkey(_owner(app))
    options = _client(app).post("/api/webauthn/login/options",
                                headers={"Origin": LOCAL_ORIGIN}).json()
    text = json.dumps(options)
    assert "public_key" not in text
    assert bytes_to_base64url(pk._cose_key()) not in text


# ── lifecycle ───────────────────────────────────────────────────────────────

def test_credentials_survive_restart_ceremonies_do_not(app, tmp_path):
    pk, _ = _enrol_passkey(_owner(app))
    o = _client(app).post("/api/webauthn/login/options",
                          headers={"Origin": LOCAL_ORIGIN}).json()

    app2 = create_app(Settings(data_dir=str(tmp_path / "data"),
                               memory_url="http://127.0.0.1:1",
                               trusted_hosts=TAILNET))
    stale = _client(app2).post("/api/webauthn/login", json={
        "cid": o["cid"],
        "credential": pk.assertion(o["publicKey"], LOCAL_ORIGIN)},
        headers={"Origin": LOCAL_ORIGIN})
    assert stale.status_code == 403                       # ceremony died
    assert _passkey_login(_client(app2), pk).status_code == 200  # credential lived


def test_removal_stops_unlocking_password_remains(app):
    owner = _owner(app)
    pk, _ = _enrol_passkey(owner)
    listed = owner.get("/api/webauthn/credentials").json()["credentials"]
    assert len(listed) == 1 and listed[0]["rp_id"] == "localhost"
    assert owner.post("/api/webauthn/credentials/remove",
                      json={"id": listed[0]["id"]}).status_code == 200
    assert _client(app).post("/api/webauthn/login/options",
                             headers={"Origin": LOCAL_ORIGIN}).status_code == 400
    assert _client(app).post("/api/auth/login",
                             json={"password": PASSWORD}).status_code == 200


def test_stored_record_is_public_material_only(app):
    pk, _ = _enrol_passkey(_owner(app))
    con = db.connect()
    try:
        rows = passkeys.list_credentials(con)
    finally:
        con.close()
    assert len(rows) == 1
    rec = rows[0]
    assert rec["rp_id"] == "localhost"
    assert base64url_to_bytes(rec["id"]) == pk.cred_id
    priv = pk.key.private_numbers().private_value.to_bytes(32, "big")
    assert bytes_to_base64url(priv) not in json.dumps(rec)


# ── honest passkey state and owner labels (#87/#88) ─────────────────────────
#
# The field failure: an empty credential store rendered exactly like a broken
# gate - the passkey button was simply absent, and nothing said whether that
# meant "never enrolled", "enrolled somewhere else", or "bug". And with one
# credential per device, the two were indistinguishable twins in the list.

def test_session_names_where_a_passkey_does_exist(app):
    owner = _owner(app)
    # nothing enrolled anywhere: elsewhere is honestly empty
    s = _client(app, base_url="http://127.0.0.1").get("/api/auth/session").json()
    assert s["passkey"] is False and s["passkey_elsewhere"] == []
    # enrol at localhost; the IP-origin lock screen names it
    _enrol_passkey(owner)
    s = _client(app, base_url="http://127.0.0.1").get("/api/auth/session").json()
    assert s["passkey"] is False
    assert s["passkey_elsewhere"] == ["localhost"]
    # at localhost itself: offered, nothing to point elsewhere
    s = _client(app).get("/api/auth/session").json()
    assert s["passkey"] is True and s["passkey_elsewhere"] == []


def test_credential_labels_are_owner_editable(app):
    owner = _owner(app)
    _enrol_passkey(owner)
    creds = owner.get("/api/webauthn/credentials").json()["credentials"]
    cid = creds[0]["id"]
    assert creds[0].get("label") in (None, "")
    assert "last_used_at" in creds[0]

    r = owner.post("/api/webauthn/credentials/label",
                   json={"id": cid, "label": "  MacBook   Touch ID  "})
    assert r.status_code == 200
    creds = owner.get("/api/webauthn/credentials").json()["credentials"]
    assert creds[0]["label"] == "MacBook Touch ID"      # whitespace collapsed

    assert owner.post("/api/webauthn/credentials/label",
                      json={"id": "nope", "label": "x"}).status_code == 404
    # session-gated like every credential surface
    assert _client(app).post("/api/webauthn/credentials/label",
                             json={"id": cid, "label": "x"}).status_code == 401


def test_successful_unlock_stamps_last_used(app):
    owner = _owner(app)
    pk, _ = _enrol_passkey(owner)
    anon = _client(app)
    assert _passkey_login(anon, pk).status_code == 200
    creds = owner.get("/api/webauthn/credentials").json()["credentials"]
    assert creds[0]["last_used_at"] is not None


def test_login_sheet_offers_only_this_apps_keys(app):
    """The fleet shares the localhost RP, so an empty allow-list meant every
    app's sheet offered every app's passkey (#204). The gate now names
    exactly its own enrolled ids - a deliberate owner-approved disclosure:
    an id is a key handle, not a secret, and existence was already
    disclosed by the no-passkey 400."""
    c = _owner(app)
    pk, r = _enrol_passkey(c)
    assert r.status_code == 200, r.text
    o = _client(app).post("/api/webauthn/login/options",
                          headers={"Origin": LOCAL_ORIGIN})
    allowed = o.json()["publicKey"]["allowCredentials"]
    assert [a["id"] for a in allowed] == [bytes_to_base64url(pk.cred_id)]
