"""GET /api/models/status: read-only readout of what model each
participant is ACTUALLY running — the live participants-row model, the model
stamped on their last completed message, and drift from the config seed.

Synthetic data only (public repo): fake slugs/model IDs, no real keys."""

import json

import pytest
from fastapi.testclient import TestClient

from backend import db
from backend.app import create_app
from backend.config import Settings


@pytest.fixture
def client(tmp_path):
    # Pin the seed so drift assertions are deterministic regardless of defaults.
    settings = Settings(
        data_dir=str(tmp_path / "data"),
        memory_url="http://127.0.0.1:1",  # unroutable: memoryless
        anthropic_model="claude-opus-4-8",
        openai_model="gpt-5.1",
    )
    with TestClient(create_app(settings), base_url="http://127.0.0.1") as c:
        yield c


def _by_slug(resp):
    return {p["slug"]: p for p in resp.json()["participants"]}


def _stamp(chat_id, speaker, model, created_at):
    """Insert a completed message with a usage_json.model stamp."""
    con = db.connect()
    con.execute(
        "INSERT INTO messages(chat_id, speaker, content, usage_json, created_at) "
        "VALUES(?,?,?,?,?)",
        (chat_id, speaker, "hi", json.dumps({"model": model}), created_at),
    )
    con.commit()
    con.close()


def test_fresh_roster_matches_seed_no_drift(client):
    parts = _by_slug(client.get("/api/models/status"))
    assert set(parts) == {"claude", "gpt"}
    gpt = parts["gpt"]
    assert gpt["provider"] == "openai"
    assert gpt["configured"] == "gpt-5.1"   # live participants row
    assert gpt["seed"] == "gpt-5.1"         # config first-run seed
    assert gpt["seed_drift"] is False
    assert gpt["last_used"] is None         # nothing generated yet
    assert gpt["pending"] is False


def test_last_used_reflects_message_stamp_not_config(client):
    chat = client.post("/api/chats", json={}).json()
    # last reply was actually produced by an older model ID
    _stamp(chat["id"], "gpt", "gpt-5.0", created_at=1751600000.0)

    gpt = _by_slug(client.get("/api/models/status"))["gpt"]
    assert gpt["configured"] == "gpt-5.1"   # what the NEXT turn will use
    assert gpt["last_used"] == "gpt-5.0"    # what actually stamped the last reply
    # configured != last_used → the switch hasn't produced a reply yet
    assert gpt["pending"] is True


def test_latest_stamp_wins(client):
    chat = client.post("/api/chats", json={}).json()
    _stamp(chat["id"], "gpt", "gpt-5.0", created_at=1751600000.0)
    _stamp(chat["id"], "gpt", "gpt-5.1", created_at=1751600100.0)  # newer
    gpt = _by_slug(client.get("/api/models/status"))["gpt"]
    assert gpt["last_used"] == "gpt-5.1"
    assert gpt["pending"] is False  # newest stamp matches configured


def test_editing_live_model_shows_seed_drift(client):
    pid = next(p["id"] for p in client.get("/api/state").json()["participants"]
               if p["slug"] == "gpt")
    client.patch(f"/api/participants/{pid}", json={"model": "gpt-5.1-mini"})

    gpt = _by_slug(client.get("/api/models/status"))["gpt"]
    assert gpt["configured"] == "gpt-5.1-mini"  # live row now
    assert gpt["seed"] == "gpt-5.1"             # config seed unchanged
    assert gpt["seed_drift"] is True            # config disagrees with live


def test_status_surfaces_lifecycle_and_provenance(client):
    # The seeded roster is onboarded on priced models, so the readout the
    # Participants UI reads must say exactly that: onboarded, a known
    # rate-card provenance, and eligible for future auto-selection.
    gpt = _by_slug(client.get("/api/models/status"))["gpt"]
    assert gpt["lifecycle"] == "onboarded"
    assert gpt["cost_provenance"] == "rate_card_estimate"
    assert gpt["cost_provenance_label"]  # a human label, never blank
    assert gpt["onboardable"] is True
    assert gpt["eligible_for_auto_selection"] is True


def test_status_flags_unsourced_trial_seat_as_not_onboardable(client):
    # A user-added seat on an unpriced model is a trial with unknown provenance:
    # the UI must be able to show it is NOT onboardable (promote stays disabled)
    # rather than letting the owner hit a 409 on save.
    client.post("/api/participants", json={
        "name": "Mystery", "provider": "openai", "model": "totally-unlisted"})
    row = _by_slug(client.get("/api/models/status"))["mystery"]
    assert row["lifecycle"] == "trial"
    assert row["cost_provenance"] == "unknown"
    assert row["onboardable"] is False
    assert row["eligible_for_auto_selection"] is False


def test_status_flags_priced_trial_seat_as_onboardable(client):
    # An existing/priced seat left on trial IS onboardable — the recovery path:
    # the UI can offer promotion, and the backend will accept it.
    pid = client.post("/api/participants", json={
        "name": "Priced Trial", "provider": "anthropic",
        "model": "claude-opus-4-8"}).json()["id"]
    row = _by_slug(client.get("/api/models/status"))["priced-trial"]
    assert row["lifecycle"] == "trial"
    assert row["onboardable"] is True
    assert row["eligible_for_auto_selection"] is False  # not onboarded yet
    # And promotion actually lands.
    assert client.patch(f"/api/participants/{pid}",
                        json={"lifecycle": "onboarded"}).status_code == 200
    row = _by_slug(client.get("/api/models/status"))["priced-trial"]
    assert row["lifecycle"] == "onboarded"
    assert row["eligible_for_auto_selection"] is True


def test_malformed_usage_json_does_not_500(client):
    chat = client.post("/api/chats", json={}).json()
    con = db.connect()
    con.execute(
        "INSERT INTO messages(chat_id, speaker, content, usage_json, created_at) "
        "VALUES(?,?,?,?,?)",
        (chat["id"], "gpt", "hi", "{not json", 1751600000.0),
    )
    con.commit()
    con.close()
    r = client.get("/api/models/status")
    assert r.status_code == 200
    assert _by_slug(r)["gpt"]["last_used"] is None
