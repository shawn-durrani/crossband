"""Connections-page MCP management: CRUD writes config.local.json
atomically, both slots surface with honest restart state, and the test
endpoint reports failure as an answer, never a crash. Keyless throughout."""

import json

import pytest

from backend import config


@pytest.fixture
def api(tmp_path, monkeypatch):
    """A client whose writes land in a temp config.local.json, never the
    developer's own - same harness shape as test_pricing_api."""
    from fastapi.testclient import TestClient
    from backend.routers import mcp_servers as r
    local = tmp_path / "config.local.json"
    monkeypatch.setattr(r, "LOCAL_CONFIG_PATH", local)
    monkeypatch.setattr(config, "LOCAL_CONFIG_PATH", local)
    from fastapi import FastAPI
    app = FastAPI()
    app.include_router(r.router)
    # Loopback: these exercise the operator-at-the-machine path.
    return TestClient(app, base_url="http://127.0.0.1"), local


def test_add_edit_remove_round_trip(api):
    client, local = api
    # seed an unrelated key: the writer must never clobber it
    local.write_text(json.dumps({"user_name": "Alex-test-placeholder"}))
    r = client.put("/api/mcp-servers/models/spend-tracker", json={
        "command": "/usr/bin/true", "args": ["-m", "x"],
        "env": {"PYTHONPATH": "/tmp/x"}, "label": "Checking spending"})
    assert r.status_code == 200 and r.json()["restart_required"] is True
    written = json.loads(local.read_text())
    assert written["mcp_servers"]["spend-tracker"]["label"] == "Checking spending"
    assert written["user_name"] == "Alex-test-placeholder"  # preserved

    # edit in place
    client.put("/api/mcp-servers/models/spend-tracker", json={
        "command": "/usr/bin/true", "args": []})
    written = json.loads(local.read_text())
    assert written["mcp_servers"]["spend-tracker"]["args"] == []
    assert "label" not in written["mcp_servers"]["spend-tracker"]

    # remove - and the empty block disappears entirely
    assert client.delete("/api/mcp-servers/models/spend-tracker").status_code == 200
    written = json.loads(local.read_text())
    assert "mcp_servers" not in written
    assert written["user_name"] == "Alex-test-placeholder"


def test_guests_slot_writes_code_mcp(api):
    client, local = api
    client.put("/api/mcp-servers/guests/membro", json={
        "command": "/usr/bin/true"})
    assert "membro" in json.loads(local.read_text())["code_mcp"]


def test_validation_rejects_bad_names_and_slots(api):
    client, _ = api
    ok = {"command": "/usr/bin/true"}
    assert client.put("/api/mcp-servers/models/Bad Name!", json=ok).status_code == 400
    assert client.put("/api/mcp-servers/models/" + "x" * 40, json=ok).status_code == 400
    assert client.put("/api/mcp-servers/nope/fine", json=ok).status_code == 404
    assert client.put("/api/mcp-servers/models/fine",
                      json={"command": ""}).status_code == 422
    assert client.delete("/api/mcp-servers/models/ghost").status_code == 404


def test_list_merges_slots_and_reports_restart_state(api):
    client, local = api
    local.write_text(json.dumps({
        "mcp_servers": {"a": {"command": "/usr/bin/true", "args": []}},
        "code_mcp": {"b": {"command": "/usr/bin/true", "args": []}},
    }))
    body = client.get("/api/mcp-servers").json()
    by = {(s["slot"], s["name"]): s for s in body["servers"]}
    assert ("models", "a") in by and ("guests", "b") in by
    # nothing is running in this app instance -> everything pends a restart
    assert body["restart_required"] is True
    assert by[("models", "a")]["pending_restart"] is True
    assert by[("models", "a")]["connected"] is False


def test_probe_failure_is_an_answer_not_a_crash(api):
    client, _ = api
    r = client.post("/api/mcp-servers/test", json={
        "command": "/usr/bin/false", "args": []})
    assert r.status_code == 200
    body = r.json()
    assert body["ok"] is False and body["error"]
