"""Localhost trust boundary: DNS-rebinding defense and SSRF guard hardening."""

import ipaddress

import pytest


def test_rebound_host_refused(client_factory):
    client = client_factory(base_url="http://evil.example.com")
    assert client.get("/api/state").status_code == 403


def test_loopback_hosts_allowed(client_factory):
    for base in ("http://127.0.0.1", "http://localhost"):
        assert client_factory(base_url=base).get("/api/state").status_code == 200


def test_ssrf_rejects_ipv4_mapped_ipv6(monkeypatch):
    from backend import egress, tools
    monkeypatch.setattr(egress.socket, "getaddrinfo",
                        lambda *a, **k: [(None, None, None, None, ("::ffff:127.0.0.1", 0))])
    with pytest.raises(ValueError, match="non-public"):
        tools._assert_public_url("http://mapped.example.com/")


def test_trusted_host_allowed_others_still_refused(tmp_path):
    # Remote access over Tailscale: the tailnet name is an ALLOWED HOST (403
    # for strangers), but since #25 an anonymous trusted-host caller reaches
    # only the login surface - 401 elsewhere, even before a password exists.
    # Loopback keeps the historical open posture until enrolment
    # (test_auth_gate.py owns the post-enrolment contract).
    from fastapi.testclient import TestClient

    from backend.app import create_app
    from backend.config import Settings
    s = Settings(data_dir=str(tmp_path / "d"), memory_url="http://127.0.0.1:59999",
                 trusted_hosts="my-mac.my-tailnet.ts.net")
    app = create_app(s)
    tailnet = TestClient(app, base_url="http://my-mac.my-tailnet.ts.net")
    assert tailnet.get("/api/auth/session").status_code == 200  # lock screen surface
    assert tailnet.get("/api/state").status_code == 401         # nothing else anonymous
    assert TestClient(app, base_url="http://127.0.0.1"
                      ).get("/api/state").status_code == 200      # loopback still fine
    assert TestClient(app, base_url="http://evil.example.com"
                      ).get("/api/state").status_code == 403      # stranger still refused


def test_nonloopback_bind_refused(monkeypatch):
    import backend.__main__ as entry
    from backend.config import Settings
    monkeypatch.setattr(entry, "load_settings", lambda: Settings(host="0.0.0.0"),
                        raising=False)
    # __main__.main() should refuse before uvicorn ever runs
    import uvicorn
    monkeypatch.setattr(uvicorn, "run",
                        lambda *a, **k: pytest.fail("uvicorn.run reached with 0.0.0.0"))
    with pytest.raises(SystemExit, match="refusing to bind"):
        entry.main()


def test_non_hex_participant_color_rejected(client_factory):
    c = client_factory()
    assert c.post("/api/participants", json={"name": "Bad", "provider": "openai",
                  "model": "gpt-5.1", "color": "red url(https://evil/x)"}).status_code == 422
    assert c.post("/api/participants", json={"name": "Good", "provider": "openai",
                  "model": "gpt-5.1", "color": "#3366ff"}).status_code == 200


def test_cross_site_api_request_refused(client_factory):
    c = client_factory()
    assert c.get("/api/state", headers={"sec-fetch-site": "cross-site"}).status_code == 403
    assert c.get("/api/state", headers={"sec-fetch-site": "same-origin"}).status_code == 200
