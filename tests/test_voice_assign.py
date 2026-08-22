"""Voice auto-assignment stays stable (#161). Two pins: a seat that already
has a voice is never reassigned by another assign pass (every voice start
calls one), and the fallback pool is deterministic under the provider's
floating list order, so identical accounts pick identical voices. The
fixtures size the voice list to the seeded roster, and use names outside
PREFERRED_VOICES so the remainder pool - the half that floated - is what
gets exercised."""

import pytest
from fastapi.testclient import TestClient

from backend import voice
from backend.app import create_app
from backend.config import Settings


@pytest.fixture
def client(tmp_path, monkeypatch):
    # The suite is keyless by design, so the fixture simulates a keyed
    # account through the seam's own selection point (provider_for was the
    # key check "enabled" before the provider seam existed).
    monkeypatch.setattr(voice, "provider_for",
                        lambda cfg: voice.PROVIDER_ELEVENLABS)
    settings = Settings(data_dir=str(tmp_path / "data"),
                        memory_url="http://127.0.0.1:1")
    with TestClient(create_app(settings), base_url="http://127.0.0.1") as c:
        yield c


def _voices(n):
    # "Custom A".."Custom N": deliberately absent from PREFERRED_VOICES.
    return [{"name": f"Custom {chr(65 + i)}", "voice_id": f"v-{chr(97 + i)}"}
            for i in range(n)]


def _assigned(client):
    return {p["id"]: p["voice_id"]
            for p in client.get("/api/state").json()["participants"]}


def test_assigned_seats_survive_a_reordered_provider_list(client, monkeypatch):
    roster = client.get("/api/state").json()["participants"]
    pool = _voices(len(roster))
    monkeypatch.setattr(voice, "list_voices", lambda: list(pool))
    client.post("/api/voice/assign")
    first = _assigned(client)
    assert all(first.values()), "every seeded seat gets a voice"
    # The provider list order flips; nobody's voice may move.
    monkeypatch.setattr(voice, "list_voices", lambda: list(reversed(pool)))
    client.post("/api/voice/assign")
    assert _assigned(client) == first


def test_a_new_seat_draws_deterministically_despite_list_order(client, monkeypatch):
    roster = client.get("/api/state").json()["participants"]
    pool = _voices(len(roster) + 1)
    monkeypatch.setattr(voice, "list_voices", lambda: list(pool))
    client.post("/api/voice/assign")
    seat = client.post("/api/participants",
                       json={"name": "Mateo Seat", "provider": "openai",
                             "model": "gpt-test"}).json()
    # The one unused voice is the alphabetically-last id whatever order the
    # provider lists them in this time.
    monkeypatch.setattr(voice, "list_voices", lambda: list(reversed(pool)))
    client.post("/api/voice/assign")
    assert _assigned(client)[seat["id"]] == pool[-1]["voice_id"]
