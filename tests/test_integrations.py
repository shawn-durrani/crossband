"""Unified integration status: the capability registry output and
its degraded/unconfigured behavior.

Two layers of coverage:
  • integrations.collect() directly, with fakes - exact control over configured
    vs missing creds, a passing/failing/exploding probe, and MCP server states.
  • GET /api/integrations end-to-end via TestClient - additive, read-only, and
    never crashes when external services are down (memory unroutable in CI).

Synthetic data only: fake keys, fake MCP server names, no real credentials.
Async is driven with asyncio.run (no pytest-asyncio in this suite)."""

import asyncio
import json

import pytest
from fastapi.testclient import TestClient

from backend import integrations
from backend.app import create_app
from backend.config import Settings

# Every entry, regardless of kind, carries this stable shape.
ENTRY_KEYS = {
    "id", "display_name", "kind", "description", "configured", "valid",
    "enabled", "available", "health", "detail", "seats",
    "cost_provenance", "lifecycle", "chat_toggle", "requires",
}

FAKE_KEY = "sk-test-a-very-long-fake-key-000000"


def run(coro):
    return asyncio.run(coro)


# ---------- fakes ----------

class FakeMemory:
    def __init__(self, available=False):
        self._available = available
        self.forced = None

    async def probe(self, force=False):
        self.forced = force
        return self._available

    def status(self):
        return {"available": self._available, "url": "http://127.0.0.1:8901",
                "contract_version": "1.0"}


class FakeMcp:
    def __init__(self, status):
        self._status = status

    def status(self):
        return self._status


def seats(*providers):
    """Minimal participant rows as db.get_participants would return them."""
    return [
        {"slug": f"seat{i}", "name": f"Seat {i}", "provider": prov,
         "model": f"{prov}-model", "enabled": True,
         "api_key_env": None, "base_url": None}
        for i, prov in enumerate(providers)
    ]


async def ok_validate(service, key, secret):
    return True, None


async def bad_validate(service, key, secret):
    return False, "That key wasn't accepted — check you copied all of it."


async def boom_validate(service, key, secret):
    raise RuntimeError("network exploded")


def collect(**kw):
    kw.setdefault("participants", [])
    kw.setdefault("memory", FakeMemory())
    kw.setdefault("mcp", FakeMcp({}))
    kw.setdefault("session_valid", {})
    kw.setdefault("validate", ok_validate)
    return {e["id"]: e for e in run(integrations.collect(**kw))}


# ---------- registry output shape ----------

def test_every_entry_has_the_stable_shape_and_kind():
    ids = collect(environ={})
    for sid in ("anthropic", "openai", "elevenlabs", "tavily", "brave", "reddit"):
        assert sid in ids
    assert "memory" in ids
    kinds = {e["kind"] for e in ids.values()}
    # Closed set, deliberately: a new kind means a new console section,
    # so adding one has to be a decision, not a side effect (the set was later
    # widened by three: code, toolset and channel).
    assert kinds <= {"llm", "audio", "search", "mcp", "memory",
                     "code", "toolset", "channel"}
    for e in ids.values():
        assert set(e) >= ENTRY_KEYS
        assert e["health"] in {"unconfigured", "unknown", "healthy",
                               "unhealthy", "disabled"}


def test_cost_provenance_shape_is_stable():
    # cost_provenance is a real record now. With no seats there's nothing
    # to price, so it stays an honest `unknown` - but always the full shape.
    for e in collect(environ={"ANTHROPIC_API_KEY": FAKE_KEY}).values():
        cp = e["cost_provenance"]
        assert set(cp) == {"source", "as_of", "source_ref", "estimated"}
        assert cp["source"] in {"provider_reported", "rate_card_estimate",
                                "self_hosted_zero_marginal",
                                "subscription_equivalent", "unknown"}
    # Non-LLM capabilities have no per-seat cost model → unknown lifecycle.
    assert collect(environ={})["memory"]["lifecycle"] == "unknown"


def test_seats_carry_real_provenance_and_default_trial():
    # A priced hosted model resolves to a rate_card_estimate; its seat still
    # defaults to trial (never silently onboarded) and so is NOT auto-eligible.
    parts = [{"slug": "claude", "name": "Claude", "provider": "anthropic",
              "model": "claude-opus-4-8", "enabled": True,
              "api_key_env": None, "base_url": None}]  # no lifecycle key → trial
    anthropic = collect(environ={"ANTHROPIC_API_KEY": FAKE_KEY},
                        participants=parts)["anthropic"]
    seat = anthropic["seats"][0]
    assert seat["cost_provenance"]["source"] == "rate_card_estimate"
    # #234: the label and the gate ship with the record, so the console
    # renders them instead of keeping a copy of PROVENANCE_LABELS.
    assert seat["cost_provenance_label"] == "Rate-card estimate"
    assert seat["onboardable"] is True
    assert seat["cost_provenance"]["estimated"] is True
    assert seat["cost_provenance"]["as_of"]  # a dated source is recorded
    assert seat["lifecycle"] == "trial"
    assert seat["eligible_for_auto_selection"] is False
    assert anthropic["lifecycle"] == "trial"  # rolls up conservatively


def test_onboarded_priced_seat_is_auto_eligible():
    parts = [{"slug": "claude", "name": "Claude", "provider": "anthropic",
              "model": "claude-opus-4-8", "enabled": True, "lifecycle": "onboarded",
              "api_key_env": None, "base_url": None}]
    anthropic = collect(environ={"ANTHROPIC_API_KEY": FAKE_KEY},
                        participants=parts)["anthropic"]
    seat = anthropic["seats"][0]
    assert seat["eligible_for_auto_selection"] is True
    assert anthropic["lifecycle"] == "onboarded"


def test_onboarded_but_unpriced_seat_is_not_auto_eligible():
    # Onboarding an unsourced model can't happen via the API, but even if a row
    # were forced onboarded, unknown provenance keeps it out of auto-selection.
    # The seat is deliberately HOSTED (a remote endpoint with a key): a keyless
    # loopback seat is a self-hosted declaration, not an unknown (see the
    # test below), so an unknown-provenance seat has to be a hosted one.
    parts = [{"slug": "hosted", "name": "Hosted", "provider": "openai",
              "model": "some-vendor/unlisted-model", "enabled": True,
              "lifecycle": "onboarded", "api_key_env": "VENDOR_KEY",
              "base_url": "https://api.example.com/v1"}]
    seat = collect(environ={"OPENAI_API_KEY": FAKE_KEY},
                   participants=parts)["openai"]["seats"][0]
    assert seat["cost_provenance"]["source"] == "unknown"
    assert seat["eligible_for_auto_selection"] is False
    assert seat["onboardable"] is False
    assert seat["cost_provenance_label"].startswith("Unknown")


def test_keyless_local_seat_is_self_hosted_without_a_rate_card_entry():
    # The seat below is the documented zero-key path: a model the user
    # pulled themselves, which no rate card names. Served from this machine with
    # no key, so nothing meters it: a declared $0, not a gap. That is what makes
    # it promotable, and auto-eligible once promoted.
    from backend import provenance
    parts = [{"slug": "local", "name": "Local", "provider": "openai",
              "model": "some-unlisted-local-model", "enabled": True,
              "lifecycle": "onboarded", "api_key_env": None,
              "base_url": "http://127.0.0.1:11434/v1"}]
    seat = collect(environ={"OPENAI_API_KEY": FAKE_KEY},
                   participants=parts)["openai"]["seats"][0]
    assert seat["cost_provenance"]["source"] == provenance.SELF_HOSTED_ZERO_MARGINAL
    # No dated price list was read - the $0 follows from where the endpoint is.
    assert seat["cost_provenance"]["as_of"] is None
    assert seat["eligible_for_auto_selection"] is True


def test_self_hosted_declaration_is_onboardable_and_distinct():
    # An explicit self-hosted rate-card entry (input/output 0) declares a
    # zero-marginal cost that is NOT unknown - so the seat can be onboarded and
    # becomes auto-eligible.
    from backend import provenance
    pricing = {"llama": {"input": 0.0, "output": 0.0,
                         "provenance": provenance.SELF_HOSTED_ZERO_MARGINAL,
                         "as_of": None, "source": "local Ollama"}}
    parts = [{"slug": "llama", "name": "Llama", "provider": "openai",
              "model": "llama-3", "enabled": True, "lifecycle": "onboarded",
              "api_key_env": None, "base_url": "http://127.0.0.1:11434/v1"}]
    seat = collect(environ={"OPENAI_API_KEY": FAKE_KEY}, participants=parts,
                   pricing=pricing)["openai"]["seats"][0]
    assert seat["cost_provenance"]["source"] == "self_hosted_zero_marginal"
    assert seat["eligible_for_auto_selection"] is True


# ---------- capability contract: chat_toggle + requires[] ----------

def test_every_entry_carries_chat_toggle_and_requires():
    ids = collect(environ={})
    for e in ids.values():
        assert "chat_toggle" in e
        assert isinstance(e["requires"], list)
        for r in e["requires"]:
            # env carries NAMES only - never a value - and the full requirement shape.
            assert set(r) >= {"type", "label", "env", "optional", "satisfied",
                              "setup_service", "any_of"}
            assert all(name.isupper() or "_" in name for name in r["env"])


def test_requires_expose_env_names_never_values():
    # A configured key must NOT leak its value into requires[] (names only).
    ids = collect(environ={"TAVILY_API_KEY": FAKE_KEY, "BRAVE_API_KEY": FAKE_KEY})
    tavily = ids["tavily"]["requires"][0]
    assert tavily["env"] == ["TAVILY_API_KEY"]
    assert tavily["satisfied"] is True
    for e in ids.values():
        assert FAKE_KEY not in repr(e["requires"])


def test_chat_toggle_wiring_connects_keys_to_chips():
    ids = collect(environ={})
    assert ids["elevenlabs"]["chat_toggle"] == "voice_mode"
    assert ids["memory"]["chat_toggle"] == "memory_enabled"
    for sid in ("tavily", "brave", "reddit"):
        assert ids[sid]["chat_toggle"] == "web_enabled"
    # LLM providers have no single chat toggle.
    assert ids["anthropic"]["chat_toggle"] is None
    assert ids["openai"]["chat_toggle"] is None


# ---------- the any-of search group ----------

def web_search_met(environ):
    entries = list(run(integrations.collect(
        participants=[], memory=FakeMemory(), mcp=FakeMcp({}),
        session_valid={}, validate=ok_validate, environ=environ)))
    return integrations.requirements_met(
        integrations.requirements_for_toggle(entries, "web_enabled"))


def test_any_one_search_key_satisfies_the_group():
    assert web_search_met({"TAVILY_API_KEY": FAKE_KEY}) is True
    assert web_search_met({"BRAVE_API_KEY": FAKE_KEY}) is True
    assert web_search_met({"TAVILY_API_KEY": FAKE_KEY,
                           "BRAVE_API_KEY": FAKE_KEY}) is True


def test_no_search_key_does_not_satisfy_the_group():
    assert web_search_met({}) is False


def test_reddit_only_does_not_satisfy_the_search_group():
    # Reddit is a SEPARATE enhancement, not a member of the {Tavily, Brave} OR:
    # backend/tools.py reads only TAVILY/BRAVE_API_KEY, so a Reddit-only config
    # has NO real web search even though page/Reddit fetch still work.
    assert web_search_met({"REDDIT_CLIENT_ID": FAKE_KEY,
                           "REDDIT_CLIENT_SECRET": FAKE_KEY}) is False
    reddit = collect(environ={"REDDIT_CLIENT_ID": FAKE_KEY,
                              "REDDIT_CLIENT_SECRET": FAKE_KEY})["reddit"]
    assert reddit["requires"][0]["any_of"] is None  # not in the search group
    assert reddit["configured"] is True


def test_requirements_met_rules_directly():
    # Vacuously met when nothing is required.
    assert integrations.requirements_met([]) is True
    # A non-optional, ungrouped requirement blocks until satisfied.
    dep = {"any_of": None, "optional": False, "satisfied": False}
    assert integrations.requirements_met([dep]) is False
    # An optional, ungrouped requirement never blocks (pure enhancement).
    assert integrations.requirements_met(
        [{"any_of": None, "optional": True, "satisfied": False}]) is True


# ---------- configured / unconfigured / valid ----------

def test_unconfigured_service_is_unconfigured_never_crashes():
    tavily = collect(environ={})["tavily"]
    assert tavily["configured"] is False
    assert tavily["valid"] is None
    assert tavily["health"] == "unconfigured"
    assert tavily["available"] is False


def test_configured_but_unverified_is_unknown():
    # Key present, but not validated this session and probe not requested.
    tavily = collect(environ={"TAVILY_API_KEY": FAKE_KEY})["tavily"]
    assert tavily["configured"] is True
    assert tavily["valid"] is None
    assert tavily["health"] == "unknown"


def test_session_validity_lights_up_healthy_without_probe():
    tavily = collect(environ={"TAVILY_API_KEY": FAKE_KEY},
                     session_valid={"tavily": True})["tavily"]
    assert tavily["valid"] is True
    assert tavily["health"] == "healthy"


# ---------- capability-specific probe (?probe=true) ----------

def test_probe_true_runs_live_validator_healthy():
    brave = collect(environ={"BRAVE_API_KEY": FAKE_KEY}, probe=True,
                    validate=ok_validate)["brave"]
    assert brave["valid"] is True
    assert brave["health"] == "healthy"


def test_failed_probe_is_unhealthy_not_false_configured():
    brave = collect(environ={"BRAVE_API_KEY": FAKE_KEY}, probe=True,
                    validate=bad_validate)["brave"]
    assert brave["configured"] is True          # a bad key is still a present key
    assert brave["valid"] is False
    assert brave["health"] == "unhealthy"
    assert "accepted" in brave["detail"]


def test_exploding_probe_degrades_gracefully():
    openai = collect(environ={"OPENAI_API_KEY": FAKE_KEY}, probe=True,
                     validate=boom_validate, participants=seats("openai"))["openai"]
    assert openai["configured"] is True
    assert openai["valid"] is False
    assert openai["health"] == "unhealthy"


def test_probe_skips_validator_for_unconfigured():
    called = []

    async def spy(service, key, secret):
        called.append(service)
        return True, None

    collect(environ={}, probe=True, validate=spy)
    assert called == []  # nothing configured → no live calls made


# ---------- LLM seats ----------

def test_llm_entry_lists_related_model_seats():
    ids = collect(environ={"ANTHROPIC_API_KEY": FAKE_KEY, "OPENAI_API_KEY": FAKE_KEY},
                  participants=seats("anthropic", "openai", "openai"))
    assert len(ids["anthropic"]["seats"]) == 1
    assert len(ids["openai"]["seats"]) == 2
    assert ids["tavily"]["seats"] == []  # non-LLM carry no seats


def test_llm_disabled_when_all_seats_off():
    parts = seats("openai")
    parts[0]["enabled"] = False
    openai = collect(environ={"OPENAI_API_KEY": FAKE_KEY}, participants=parts)["openai"]
    assert openai["configured"] is True
    assert openai["enabled"] is False
    assert openai["health"] == "disabled"


# ---------- memory ----------

def test_memory_absent_is_unconfigured_not_unhealthy():
    mem = collect(memory=FakeMemory(available=False))["memory"]
    assert mem["kind"] == "memory"
    assert mem["available"] is False
    assert mem["health"] == "unconfigured"  # absence is by design, not a fault


def test_memory_present_is_healthy_and_probe_forces_fresh():
    fake = FakeMemory(available=True)
    mem = collect(memory=fake, probe=True)["memory"]
    assert mem["health"] == "healthy"
    assert mem["available"] is True
    assert fake.forced is True  # probe=true forced a fresh /health


# ---------- MCP (read-only, connect-time state) ----------

def test_mcp_entries_reflect_connection_state():
    mcp = FakeMcp({
        "up": {"connected": True, "tools": ["mcp__up__a", "mcp__up__b"], "error": None},
        "down": {"connected": False, "tools": [], "error": "spawn failed"},
        "pending": {"connected": False, "tools": [], "error": None},
    })
    ids = collect(mcp=mcp)
    assert ids["mcp:up"]["kind"] == "mcp"
    assert ids["mcp:up"]["health"] == "healthy"
    assert ids["mcp:up"]["tools"] == ["mcp__up__a", "mcp__up__b"]
    assert ids["mcp:down"]["health"] == "unhealthy"
    assert "spawn failed" in ids["mcp:down"]["detail"]
    assert ids["mcp:pending"]["health"] == "unknown"


def test_no_mcp_servers_yields_no_mcp_entries():
    ids = collect(mcp=FakeMcp({}))
    assert not [e for e in ids.values() if e["kind"] == "mcp"]


# ---------- end-to-end via the router ----------

@pytest.fixture
def client(tmp_path):
    settings = Settings(data_dir=str(tmp_path / "data"),
                        memory_url="http://127.0.0.1:1")  # unroutable: memoryless
    with TestClient(create_app(settings), base_url="http://127.0.0.1") as c:
        yield c


def test_endpoint_additive_and_read_only(client, monkeypatch):
    monkeypatch.setenv("ANTHROPIC_API_KEY", FAKE_KEY)
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    r = client.get("/api/integrations")
    assert r.status_code == 200
    ids = {e["id"]: e for e in r.json()["integrations"]}
    # default roster: claude(anthropic), gpt(openai)
    assert ids["anthropic"]["configured"] is True
    assert ids["anthropic"]["seats"]  # claude seat wired to it
    assert ids["openai"]["configured"] is False
    # memory unroutable → reported down, but the endpoint still 200s
    assert ids["memory"]["available"] is False
    assert ids["memory"]["health"] == "unconfigured"
    # no key material ever leaks into the response
    assert FAKE_KEY not in r.text


def test_endpoint_does_not_alter_chat_execution(client):
    """The read view must leave the setup wizard, participants and MCP intact."""
    before = client.get("/api/state").json()
    client.get("/api/integrations")
    after = client.get("/api/state").json()
    assert before["participants"] == after["participants"]
    # setup wizard status endpoint still answers unchanged in shape
    assert set(client.get("/api/setup/status").json()) == {"services", "any_model_key"}


def test_repo_map_edits_apply_without_a_restart(client, tmp_path, monkeypatch):
    """#24/#86: the repo maps are re-read per request, so a config.local.json
    edit (the repo-rename case that filed #24) shows up on the next status
    read and state blob with no restart - both the GitHub map and the guest's
    worktree map."""
    from backend import config
    from backend import tools as tools_mod
    local = tmp_path / "config.local.json"
    local.write_text(json.dumps({"github_repos": {"app": "alex/app"},
                                 "code_repos": {"app": "/src/app"}}))
    real = config.load_settings
    monkeypatch.setattr(tools_mod, "load_settings",
                        lambda: real(root=tmp_path, environ={}))
    monkeypatch.setattr(tools_mod, "github_token", lambda: "tok")

    def entry(eid):
        entries = client.get("/api/integrations").json()["integrations"]
        return {e["id"]: e for e in entries}[eid]

    e = entry("toolset:github")
    assert e["available"] is True and "app" in e["detail"]
    assert e["repos"] == {"app": "alex/app"}  # typed map for the access panel
    assert entry("code:claude_code")["guest"]["repos"] == ["app"]
    assert client.get("/api/state").json()["config"]["github"]["repos"] == ["app"]

    # the server keeps running; only the file changes
    local.write_text(json.dumps({"github_repos": {"renamed": "alex/renamed"},
                                 "code_repos": {}}))
    e = entry("toolset:github")
    assert "renamed" in e["detail"] and "app" not in e["detail"]
    assert e["repos"] == {"renamed": "alex/renamed"}
    assert entry("code:claude_code")["guest"]["repos"] == []
    assert client.get("/api/state").json()["config"]["github"]["repos"] == ["renamed"]


# ---------- the room + abilities capabilities ----------
#
# The coding guest, the GitHub tools and event ingestion are real, shipped,
# user-visible capabilities that appeared in NO registry section - they existed
# only inside the /api/state config blob, so the console could not show their
# health and nothing listed what the room can actually do.

def test_room_and_ability_entries_are_absent_without_cfg():
    # cfg omitted (an older caller, or a stub) → skipped, never invented.
    ids = collect(environ={})
    for missing in ("code:claude_code", "toolset:github", "channel:ingest"):
        assert missing not in ids


def test_the_three_capabilities_appear_with_the_stable_shape():
    ids = collect(environ={}, cfg={})
    for sid, kind in (("code:claude_code", "code"),
                      ("toolset:github", "toolset"),
                      ("channel:ingest", "channel")):
        e = ids[sid]
        assert e["kind"] == kind
        assert set(e) >= ENTRY_KEYS          # same contract as every other row
        assert e["health"] in {"unconfigured", "unknown", "healthy",
                               "unhealthy", "disabled"}


def test_guest_with_no_repos_reads_as_unconfigured_not_broken():
    # code_repos is the opt-in: with none, the tool is never offered. That is
    # "not set up", not "failing" - the distinction the health vocabulary exists
    # for, and the one a user needs to know which way to act.
    e = collect(environ={}, cfg={})["code:claude_code"]
    assert e["configured"] is False
    assert e["health"] == "unconfigured"
    assert e["guest"] == {"repos": [], "writes": False, "use_api_key": False}


def test_guest_billing_facts_ride_a_typed_object_not_prose():
    # The billing-drift warning stays PRESENTATION logic: the backend
    # reports facts, never a sentence it guessed at.
    #
    # These are CONFIG facts, so they must hold on a machine where the guest
    # cannot actually run. `guest.status()` zeroes repos/writes whenever the SDK
    # or CLI is missing, so reading them from there reported a fully-configured
    # room as "not set up" - which is exactly the unconfigured-vs-unhealthy
    # confusion this entry exists to prevent. CI (no Claude Code installed)
    # caught it; this test now fails on ANY machine if that regresses.
    e = collect(environ={}, cfg={"code_repos": {"app": "/tmp/app"},
                                 "code_allow_writes": True})["code:claude_code"]
    assert e["guest"]["repos"] == ["app"]
    assert e["guest"]["writes"] is True
    assert e["configured"] is True          # configured, whatever this machine has
    assert isinstance(e["guest"]["use_api_key"], bool)


def test_configured_but_unusable_guest_is_unhealthy_not_unconfigured():
    """A missing CLI is not a missing configuration. The owner who set up two
    repos needs to be told the CLI is absent, not that they never set it up -
    different problems, different fixes."""
    from backend import guest as guest_mod
    real = guest_mod.status
    guest_mod.status = lambda cfg: {"available": False, "repos": [], "writes": False,
                                    "use_api_key": False,
                                    "reason": "Claude Code CLI not found on this machine"}
    try:
        e = collect(environ={}, cfg={"code_repos": {"app": "/tmp/app"}})["code:claude_code"]
    finally:
        guest_mod.status = real
    assert e["configured"] is True
    assert e["available"] is False
    assert e["health"] == "unhealthy"
    assert "CLI not found" in e["detail"]
    assert e["guest"]["repos"] == ["app"]   # the configuration is still reported


def test_github_unconfigured_vs_configured_but_tokenless():
    # Two different failures that must not read the same: nothing set up, versus
    # set up but no token resolvable.
    bare = collect(environ={}, cfg={})["toolset:github"]
    assert bare["configured"] is False and bare["health"] == "unconfigured"

    named = collect(environ={}, cfg={"github_repos": {"app": "you/app"}})["toolset:github"]
    assert named["configured"] is True
    assert "GH_TOKEN" in named["requires"][0]["env"]        # NAMES only
    assert "you/app" not in str(named["requires"][0]["env"])


def test_ingest_is_always_available_and_its_token_is_optional():
    # The endpoint ships with the app; the token only matters off-loopback, so
    # an unset one is a fact about the deployment, never a fault.
    e = collect(environ={}, cfg={})["channel:ingest"]
    assert e["available"] is True and e["health"] == "healthy"
    assert e["requires"][0]["optional"] is True
    assert e["requires"][0]["satisfied"] is False
    assert "any local caller" in e["detail"]

    tokened = collect(environ={}, cfg={"ingest_token": "s3cret"})["channel:ingest"]
    assert tokened["requires"][0]["satisfied"] is True
    assert "s3cret" not in str(tokened)      # a secret must never reach this view


def test_mcp_entries_say_who_the_tools_are_for():
    # models vs code is a real distinction with different blast radius, and it
    # was completely invisible before.
    mcp = FakeMcp({"radar": {"connected": True, "tools": ["a"]},
                   "membro": {"connected": True, "tools": ["b"]},
                   "shared": {"connected": True, "tools": ["c"]}})
    ids = collect(environ={}, mcp=mcp, cfg={
        "mcp_servers": {"radar": {}, "shared": {}},
        "code_mcp": {"membro": {}, "shared": {}},
    })
    assert ids["mcp:radar"]["used_by"] == "models"
    assert ids["mcp:membro"]["used_by"] == "code"
    assert ids["mcp:shared"]["used_by"] == "both"


def test_used_by_is_unknown_rather_than_guessed():
    mcp = FakeMcp({"srv": {"connected": True, "tools": []}})
    assert collect(environ={}, mcp=mcp, cfg={})["mcp:srv"]["used_by"] is None
