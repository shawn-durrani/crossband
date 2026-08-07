"""Host-level routes stay on the host.

The app has no authentication, and the documented remote path (tailscale
serve) proxies through loopback, so the non-loopback bind guard never fires
for it. Spawning a command from a request body, or persisting one to be
spawned at every startup, therefore has to be gated on the request's own
host rather than on the process bind.
"""

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from backend import config
from backend.routers import mcp_servers as r

TAILNET = "http://my-mac.my-tailnet.ts.net"
LOOPBACK = "http://127.0.0.1"
SPEC = {"command": "/bin/echo", "args": ["pwned"]}


@pytest.fixture()
def app(tmp_path, monkeypatch):
    """Writes land in a temp config.local.json, never the developer's own -
    same harness shape as test_mcp_servers_api."""
    local = tmp_path / "config.local.json"
    monkeypatch.setattr(r, "LOCAL_CONFIG_PATH", local)
    monkeypatch.setattr(config, "LOCAL_CONFIG_PATH", local)
    app = FastAPI()
    app.include_router(r.router)
    return app


def test_remote_host_cannot_test_or_write_servers(app):
    with TestClient(app, base_url=TAILNET) as c:
        assert c.post("/api/mcp-servers/test", json=SPEC).status_code == 403
        assert c.put("/api/mcp-servers/models/evil", json=SPEC).status_code == 403
        assert c.delete("/api/mcp-servers/models/evil").status_code == 403


def test_remote_host_sees_env_names_without_values(app):
    with TestClient(app, base_url=LOOPBACK) as c:
        assert c.put("/api/mcp-servers/models/demo", json={
            **SPEC, "env": {"DEMO_TOKEN": "literal-secret"}}).status_code == 200
        local = c.get("/api/mcp-servers").json()["servers"]
        assert local[0]["env"] == {"DEMO_TOKEN": "literal-secret"}
    with TestClient(app, base_url=TAILNET) as c:
        remote = c.get("/api/mcp-servers").json()["servers"]
        assert remote[0]["env"] == {"DEMO_TOKEN": ""}   # name visible, value not


def test_loopback_keeps_full_control(app):
    with TestClient(app, base_url=LOOPBACK) as c:
        assert c.put("/api/mcp-servers/models/ok", json=SPEC).status_code == 200
        assert c.delete("/api/mcp-servers/models/ok").status_code == 200
