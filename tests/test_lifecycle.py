"""Model onboarding lifecycle: every seat defaults conservatively
to `trial`, may still be invoked manually there, and can only become `onboarded`
once an explicit cost-provenance record exists - never a silent upgrade.

Synthetic data only. Async endpoints exercised via TestClient."""

import pytest
from fastapi.testclient import TestClient

from backend import db, provenance
from backend.app import create_app
from backend.config import Settings


@pytest.fixture
def con(tmp_path):
    db.configure(str(tmp_path / "data"))
    db.init(Settings(data_dir=str(tmp_path / "data")))
    c = db.connect()
    yield c
    c.close()


@pytest.fixture
def client(tmp_path):
    settings = Settings(data_dir=str(tmp_path / "data"),
                        memory_url="http://127.0.0.1:1")
    with TestClient(create_app(settings), base_url="http://127.0.0.1") as c:
        yield c


def test_seeded_roster_is_onboarded(con):
    # The out-of-the-box claude/gpt seats are the product's default always-on
    # roster with priced models - seeded 'onboarded' so a fresh install actually
    # has auto-responders. Trial is now a real gate (trial seats don't auto-speak,
    # engine.pick_responders); defaulting the seed to trial would mean nobody
    # replies on first run. Only USER-added seats default to trial.
    rows = con.execute("SELECT slug, lifecycle FROM participants").fetchall()
    assert rows
    assert all(r["lifecycle"] == "onboarded" for r in rows)


def test_new_participant_column_default_is_trial(con):
    # The DB column default is still 'trial': a seat inserted WITHOUT an explicit
    # lifecycle (the participants router's path) is a conservative trial.
    con.execute("INSERT INTO participants(slug,name,provider,model,created_at) "
                "VALUES('added','Added','openai','some-model',0)")
    row = con.execute("SELECT lifecycle FROM participants WHERE slug='added'").fetchone()
    assert row["lifecycle"] == "trial"


def _seed_v6_db(tmp_path, rows):
    """Build a pre-lifecycle (v6) DB with `rows` = [(slug, model), …] and migrate it."""
    import sqlite3
    data = tmp_path / "data"
    data.mkdir()
    c = sqlite3.connect(data / "chat.db")
    c.executescript(
        "CREATE TABLE participants(id INTEGER PRIMARY KEY, slug TEXT, name TEXT,"
        " provider TEXT, model TEXT, enabled INTEGER DEFAULT 1,"
        " position INTEGER DEFAULT 0, created_at REAL DEFAULT 0);")
    for slug, model in rows:
        c.execute("INSERT INTO participants(slug,name,provider,model) VALUES(?,?,?,?)",
                  (slug, slug.title(), "anthropic", model))
    c.execute("PRAGMA user_version = 6")
    c.commit()
    c.close()
    db.configure(data)
    db.init()


def test_migration_grandfathers_only_priced_rows(tmp_path):
    # The v7 migration must apply the SAME provenance gate the PATCH endpoint
    # enforces: an existing row is grandfathered to 'onboarded' only when its
    # model has a KNOWN cost provenance. A known/priced model (claude-opus-4-8)
    # keeps working (no regression); an unpriced/custom model must NOT be waved
    # through - it stays a manually-invokable 'trial', exactly like a freshly
    # added unsourced seat. No special-casing for "it already existed".
    _seed_v6_db(tmp_path, [("priced", "claude-opus-4-8"),
                           ("custom", "my-local-llama-42")])
    c = db.connect()
    got = {r["slug"]: r["lifecycle"]
           for r in c.execute("SELECT slug, lifecycle FROM participants")}
    c.close()
    assert got["priced"] == "onboarded"   # known provenance → grandfathered
    assert got["custom"] == "trial"       # unknown provenance → gated, no free pass


def test_migrated_unpriced_seat_is_excluded_from_unaddressed_round(tmp_path):
    # End-to-end: a migrated unpriced seat lands on 'trial', so the existing
    # pick_responders gate keeps it out of a normal round (it's manual-only).
    from backend import engine
    _seed_v6_db(tmp_path, [("priced", "claude-opus-4-8"),
                           ("custom", "my-local-llama-42")])
    c = db.connect()
    roster = [dict(r) for r in c.execute(
        "SELECT * FROM participants ORDER BY id")]
    c.close()
    chat = {"next_first": "priced"}
    responders, _ = engine.pick_responders("hello all", chat, roster)
    slugs = [p["slug"] for p in responders]
    assert "priced" in slugs        # onboarded seat auto-speaks
    assert "custom" not in slugs    # trial seat sits out the unaddressed round
    # …but is still reachable by name (the manual path).
    responders, _ = engine.pick_responders("@custom you there?", chat, roster)
    assert [p["slug"] for p in responders] == ["custom"]


def test_new_participant_is_trial_and_still_invocable(client):
    r = client.post("/api/participants",
                    json={"name": "Trial Bot", "provider": "openai",
                          "model": "some-unlisted-model", "enabled": True})
    assert r.status_code == 200
    p = r.json()
    assert p["lifecycle"] == "trial"
    # trial never blocks manual use: the seat is enabled and shows up in state.
    assert p["enabled"] == 1
    assert any(x["slug"] == p["slug"]
               for x in client.get("/api/state").json()["participants"])


def test_cannot_onboard_unsourced_model(client):
    pid = client.post("/api/participants",
                      json={"name": "Mystery", "provider": "openai",
                            "model": "totally-unlisted-model"}).json()["id"]
    r = client.patch(f"/api/participants/{pid}", json={"lifecycle": "onboarded"})
    assert r.status_code == 409
    assert "provenance" in r.json()["detail"]
    # stays trial after the refused upgrade
    assert client.get("/api/state").json()
    row = [x for x in client.get("/api/state").json()["participants"]
           if x["id"] == pid][0]
    assert row["lifecycle"] == "trial"


def test_can_onboard_priced_model(client):
    pid = client.post("/api/participants",
                      json={"name": "Priced", "provider": "anthropic",
                            "model": "claude-opus-4-8"}).json()["id"]
    r = client.patch(f"/api/participants/{pid}", json={"lifecycle": "onboarded"})
    assert r.status_code == 200
    assert r.json()["lifecycle"] == "onboarded"


def test_invalid_lifecycle_rejected(client):
    pid = client.post("/api/participants",
                      json={"name": "X", "provider": "openai",
                            "model": "gpt-5.1"}).json()["id"]
    r = client.patch(f"/api/participants/{pid}", json={"lifecycle": "normal"})
    assert r.status_code == 422


# ── the zero-key local path must not dead-end ────────────────────────────────
#
# A model served from THIS machine with no API key cannot be metering anyone, so
# it resolves to a declared $0 (self_hosted_zero_marginal) instead of `unknown`
# - which is what makes it promotable. The gate itself is unchanged: the seat
# still LANDS as trial and still has to be promoted deliberately.

@pytest.mark.parametrize("base_url", [
    "http://localhost:11434/v1",     # Ollama's default (the documented path)
    "http://127.0.0.1:1234/v1",      # LM Studio's default
    "http://127.0.0.5:8080/v1",      # anywhere in 127.0.0.0/8
    "http://[::1]:11434/v1",         # IPv6 loopback
    "http://0.0.0.0:11434/v1",
    "localhost:11434/v1",            # typed without a scheme
])
def test_keyless_loopback_seat_is_self_hosted(base_url):
    from backend.config import DEFAULT_PRICING, compute_cost, provenance_for
    rec = provenance_for("some-model-nobody-priced", DEFAULT_PRICING,
                         base_url=base_url)
    assert rec["source"] == provenance.SELF_HOSTED_ZERO_MARGINAL
    # …and the cost agrees: a tracked, declared $0, never an untracked None.
    # If these two ever disagree the seat reports a self-hosted provenance while
    # its spend reads as "not tracked".
    assert compute_cost("some-model-nobody-priced", {"input": 10, "output": 10},
                        DEFAULT_PRICING, base_url=base_url) == 0.0


@pytest.mark.parametrize("base_url,api_key_env", [
    ("https://api.groq.com/openai/v1", None),   # hosted → someone is billing
    ("http://192.168.1.40:11434/v1", None),     # LAN: another machine, unknown
    ("http://10.0.0.7:11434/v1", None),
    ("http://localhost:11434/v1", "SOME_KEY"),  # keyed loopback = likely a tunnel
    (None, None),                               # no endpoint at all
])
def test_everything_else_still_resolves_unknown(base_url, api_key_env):
    # A wrong $0 is worse than an honest "unknown", so the declaration is narrow.
    from backend.config import DEFAULT_PRICING, provenance_for
    rec = provenance_for("some-model-nobody-priced", DEFAULT_PRICING,
                         base_url=base_url, api_key_env=api_key_env)
    assert rec["source"] == provenance.UNKNOWN


def test_priced_model_ignores_the_endpoint():
    # An explicit rate-card entry always wins: running a priced model id behind a
    # local proxy must not silently reprice it to $0.
    from backend.config import DEFAULT_PRICING, provenance_for
    rec = provenance_for("claude-opus-4-8", DEFAULT_PRICING,
                         base_url="http://localhost:11434/v1")
    assert rec["source"] == provenance.RATE_CARD_ESTIMATE


def test_local_seat_lands_as_trial_but_can_be_promoted(client):
    # The regression, end to end: follow the documented zero-key path with a
    # model that is NOT in the pricing table (i.e. any model a user pulls, not
    # just the one named there) and it must be promotable.
    pid = client.post("/api/participants",
                      json={"name": "Local Llama", "provider": "openai",
                            "model": "llama3.1", "enabled": True,
                            "base_url": "http://localhost:11434/v1"}).json()["id"]
    row = [x for x in client.get("/api/state").json()["participants"]
           if x["id"] == pid][0]
    assert row["lifecycle"] == "trial"   # the gate still applies on the way in

    r = client.patch(f"/api/participants/{pid}", json={"lifecycle": "onboarded"})
    assert r.status_code == 200, r.text   # …and is escapable, which it wasn't
    assert r.json()["lifecycle"] == "onboarded"


def test_promoting_while_pointing_a_seat_at_a_local_endpoint(client):
    # One PATCH may both move a seat to a local endpoint and promote it; the gate
    # must judge the seat as it will BE, not as it was.
    pid = client.post("/api/participants",
                      json={"name": "Moving", "provider": "openai",
                            "model": "qwen3:32b"}).json()["id"]
    assert client.patch(f"/api/participants/{pid}",
                        json={"lifecycle": "onboarded"}).status_code == 409
    r = client.patch(f"/api/participants/{pid}",
                     json={"base_url": "http://localhost:11434/v1",
                           "lifecycle": "onboarded"})
    assert r.status_code == 200, r.text


def test_hosted_unpriced_seat_still_cannot_be_onboarded(client):
    # The honest-cost gate is unchanged for anything that isn't local.
    pid = client.post("/api/participants",
                      json={"name": "Hosted", "provider": "openai",
                            "model": "some-vendor/mystery-model",
                            "base_url": "https://api.example.com/v1",
                            "api_key_env": "EXAMPLE_KEY"}).json()["id"]
    r = client.patch(f"/api/participants/{pid}", json={"lifecycle": "onboarded"})
    assert r.status_code == 409


def test_eligibility_derivation():
    assert provenance.eligible_for_auto_selection(
        provenance.ONBOARDED, provenance.RATE_CARD_ESTIMATE) is True
    assert provenance.eligible_for_auto_selection(
        provenance.TRIAL, provenance.RATE_CARD_ESTIMATE) is False
    assert provenance.eligible_for_auto_selection(
        provenance.ONBOARDED, provenance.UNKNOWN) is False
