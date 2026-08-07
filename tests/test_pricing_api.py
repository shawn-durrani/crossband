"""Operator-editable rate cards.

The gap this closes: pricing fails closed and a seat's promotion is gated on
known provenance, so the app could tell you a model was unpriced, block its
seat on that, and offer no remedy but hand-editing a JSON file on the
server. `docs/CONFIG.md` said "edit `pricing` to price a model the card doesn't
know" - which, before the merge fix below, was actively destructive advice.
"""

import json

import pytest

from backend import config
from backend.config import DEFAULT_PRICING, load_settings, price_for

# A valid estimate source is an http(s) URL and a real ISO date, "so the
# estimate can be checked and dated". These tests used to pass "s" and "d",
# which the ported validation correctly refuses.
SRC = "https://developers.openai.com/api/docs/pricing"
ASOF = "2026-07-31"


# ---------- the layering footgun this feature would have walked into ----------

def test_local_pricing_layers_over_defaults_instead_of_replacing(tmp_path):
    """THE regression guard. `pricing` is a dict field with a default_factory,
    and pydantic only runs that factory when the key is ABSENT - so a
    config.local.json carrying one model used to become the ENTIRE table,
    silently unpricing every other model, dropping every seat to `trial`, and
    recording cost=None across the roster."""
    (tmp_path / "config.local.json").write_text(json.dumps({
        "pricing": {"brand-new-model": {"input": 1.0, "output": 2.0,
                                        "provenance": "rate_card_estimate",
                                        "as_of": ASOF, "source": SRC}}
    }))
    s = load_settings(root=tmp_path, environ={})
    assert price_for("brand-new-model", s.pricing)["input"] == 1.0
    # every built-in card survives
    for m in ("claude-sonnet-5", "gpt-5.6-terra", "claude-opus-4-8"):
        assert price_for(m, s.pricing) is not None, m
    assert s.pricing["claude-sonnet-5"]["input"] == 3.0


def test_local_pricing_can_override_a_builtin_card_whole(tmp_path):
    """Per-ENTRY merge: an override replaces that model's card completely (the
    granularity a rate card is actually published at), and touches no other."""
    (tmp_path / "config.local.json").write_text(json.dumps({
        "pricing": {"claude-sonnet-5": {"input": 9.0, "output": 99.0,
                                        "provenance": "rate_card_estimate",
                                        "as_of": ASOF, "source": SRC}}
    }))
    s = load_settings(root=tmp_path, environ={})
    assert s.pricing["claude-sonnet-5"]["input"] == 9.0
    assert s.pricing["claude-opus-4-8"] == DEFAULT_PRICING["claude-opus-4-8"]


def test_local_voice_pricing_layers_too(tmp_path):
    """Same shape, same bug: setting one voice rate used to drop the other."""
    (tmp_path / "config.local.json").write_text(
        json.dumps({"voice_pricing": {"tts_per_1m_chars": 5.0}}))
    s = load_settings(root=tmp_path, environ={})
    assert s.voice_pricing["tts_per_1m_chars"] == 5.0
    assert "stt_per_hour" in s.voice_pricing


# ---------- the API ----------

@pytest.fixture
def api(tmp_path, monkeypatch):
    """A client whose overrides land in a temp config.local.json, never the
    developer's own."""
    from fastapi.testclient import TestClient
    from backend.routers import pricing as pricing_router
    local = tmp_path / "config.local.json"
    monkeypatch.setattr(pricing_router, "LOCAL_CONFIG_PATH", local)
    monkeypatch.setattr(config, "LOCAL_CONFIG_PATH", local)
    real_load = config.load_settings
    monkeypatch.setattr(pricing_router, "load_settings",
                        lambda: real_load(root=tmp_path, environ={}))
    from fastapi import FastAPI
    app = FastAPI()
    app.include_router(pricing_router.router)
    return TestClient(app), local


def test_list_reports_origin_and_names_the_unpriced(api):
    client, _ = api
    body = client.get("/api/pricing").json()
    by_model = {c["model"]: c for c in body["cards"]}
    assert by_model["claude-sonnet-5"]["origin"] == "builtin"
    assert by_model["claude-sonnet-5"]["card"]["input"] == 3.0
    # a genuinely unknown model is reported as unpriced, never as a silent $0
    assert all(c["priced"] for c in body["cards"] if c["origin"] != "unpriced")


def test_add_a_card_for_an_unpriced_model_then_delete_it(api):
    client, local = api
    r = client.put("/api/pricing/gpt-5.6-terra-pro", json={
        "input": 4.0, "output": 24.0, "as_of": ASOF,
        "source": SRC})
    assert r.status_code == 200, r.text
    assert r.json()["origin"] == "override"
    assert r.json()["card"]["input"] == 4.0
    # it really landed in config.local.json, and nothing else was clobbered
    written = json.loads(local.read_text())
    assert written["pricing"]["gpt-5.6-terra-pro"]["output"] == 24.0

    # and it is now live through the normal settings path
    s = load_settings(root=local.parent, environ={})
    assert price_for("gpt-5.6-terra-pro", s.pricing)["output"] == 24.0

    d = client.delete("/api/pricing/gpt-5.6-terra-pro")
    assert d.status_code == 200
    # no built-in to fall back to → honestly unpriced again, not a stale figure
    assert d.json()["origin"] == "unpriced"
    assert d.json()["priced"] is False


def test_deleting_an_override_falls_back_to_the_builtin_card(api):
    client, _ = api
    client.put("/api/pricing/claude-sonnet-5", json={
        "input": 9.0, "output": 99.0, "as_of": ASOF, "source": SRC})
    assert client.get("/api/pricing").json()
    d = client.delete("/api/pricing/claude-sonnet-5").json()
    assert d["origin"] == "builtin"
    assert d["card"]["input"] == 3.0


def test_a_rate_without_a_source_is_refused(api):
    """The load-bearing rule. A form is a much faster way to invent a number
    than a text editor was, and the pricing bugs behind this rule were both an
    untranscribed figure standing in for one nobody had. Making pricing easy
    must not make guessing easy."""
    client, _ = api
    for bad in ({"input": 1.0, "output": 2.0, "as_of": "2026-07-31", "source": "   "},
                {"input": 1.0, "output": 2.0, "as_of": "2026-07-31", "source": ""},
                {"input": 1.0, "output": 2.0, "source": "x"},          # no as_of
                {"input": -1.0, "output": 2.0, "as_of": "d", "source": "x"}):
        r = client.put("/api/pricing/some-model", json=bad)
        assert r.status_code in (400, 422), (bad, r.status_code)


def test_saved_cards_are_always_rate_card_estimate(api):
    """An operator transcribing a list price makes the same claim the built-in
    table makes - no stronger. (A declared self-hosted $0 is the ONE other
    provenance that may be attested; see the validation block below.)"""
    from backend import provenance
    client, _ = api
    r = client.put("/api/pricing/some-model", json={
        "input": 1.0, "output": 2.0, "as_of": ASOF, "source": SRC}).json()
    assert r["card"]["provenance"] == provenance.RATE_CARD_ESTIMATE


def test_cache_terms_are_optional_and_default_like_a_builtin(api):
    """Omitted cache terms inherit Anthropic's, exactly as config._rate_card
    does for a built-in entry - one rule, not two."""
    client, _ = api
    r = client.put("/api/pricing/some-model", json={
        "input": 10.0, "output": 20.0, "as_of": ASOF, "source": SRC}).json()
    from backend.config import compute_cost
    s = load_settings(root=None, environ={})
    # 1M cache writes at Anthropic's 1.25x of a $10 input rate
    assert compute_cost("some-model", {"cache_creation": 1_000_000},
                        {"some-model": r["card"]}) == pytest.approx(12.5)

    r2 = client.put("/api/pricing/other-model", json={
        "input": 10.0, "output": 20.0, "as_of": ASOF, "source": SRC,
        "cache": {"read_mult": 0.5, "write_mult": 0.0}}).json()
    assert compute_cost("other-model", {"cache_creation": 1_000_000},
                        {"other-model": r2["card"]}) == 0.0


def test_deleting_a_card_that_was_never_overridden_is_a_404(api):
    client, _ = api
    assert client.delete("/api/pricing/claude-sonnet-5").status_code == 404


def test_write_preserves_unrelated_local_config(api):
    """config.local.json holds the operator's real settings. A torn or careless
    write here would silently drop them - worse than crashing."""
    client, local = api
    local.write_text(json.dumps({"user_name": "Alex", "log_level": "INFO"}))
    client.put("/api/pricing/some-model", json={
        "input": 1.0, "output": 2.0, "as_of": ASOF, "source": SRC})
    written = json.loads(local.read_text())
    assert written["user_name"] == "Alex"
    assert written["log_level"] == "INFO"
    assert "some-model" in written["pricing"]


# ---------- ported validation invariants ----------
#
# This surface was built twice in parallel: one effort shipped first with
# weaker inline checks, without noticing the other was already open. Rather
# than discard that work, the second effort's invariants became the ones
# enforced here. The one thing NOT taken: making `source` and `as_of`
# optional. They stay required for an estimate, which is precisely the hole
# fail-closed pricing exists to shut.


def test_only_the_two_operator_declarable_provenances_are_accepted(api):
    """A user must never be able to relabel their own estimate as BILLED spend.
    provider_reported and subscription_equivalent are DERIVED per turn from what
    a provider or subscription actually returned; they are not attestable."""
    client, _ = api
    for forged in ("provider_reported", "subscription_equivalent", "unknown", ""):
        r = client.put("/api/pricing/some-model", json={
            "input": 1.0, "output": 2.0, "as_of": ASOF, "source": SRC,
            "provenance": forged})
        assert r.status_code == 400, forged
        assert "provenance" in r.json()["detail"].lower()


def test_a_self_hosted_zero_can_now_be_declared_from_the_ui(api):
    """New capability from the port: a local model's $0 is a DECLARED fact with
    its own provenance, distinct from 'no data' - and it needs no rates, which
    is why the wire model can't require input/output."""
    from backend import provenance
    client, _ = api
    r = client.put("/api/pricing/llama-local", json={
        "provenance": provenance.SELF_HOSTED_ZERO_MARGINAL,
        "source": "local (Ollama, self-hosted)"})
    assert r.status_code == 200, r.text
    card = r.json()["card"]
    assert (card["input"], card["output"]) == (0.0, 0.0)
    assert card["provenance"] == provenance.SELF_HOSTED_ZERO_MARGINAL


def test_a_fat_fingered_rate_is_refused_as_a_typo(api):
    client, _ = api
    r = client.put("/api/pricing/some-model", json={
        "input": 1e9, "output": 2.0, "as_of": ASOF, "source": SRC})
    assert r.status_code == 400
    assert "too large" in r.json()["detail"]


def test_an_as_of_must_be_a_real_date_and_a_source_a_checkable_url(api):
    """'d' and 's' used to pass. A date nobody can parse and a source nobody can
    open are the same failure as no date and no source."""
    client, _ = api
    bad_date = client.put("/api/pricing/some-model", json={
        "input": 1.0, "output": 2.0, "as_of": "sometime", "source": SRC})
    assert bad_date.status_code == 400
    assert "real date" in bad_date.json()["detail"]
    bad_src = client.put("/api/pricing/some-model", json={
        "input": 1.0, "output": 2.0, "as_of": ASOF, "source": "the pricing page"})
    assert bad_src.status_code == 400
    assert "http" in bad_src.json()["detail"].lower()


def test_a_broad_alias_cannot_even_be_saved(api):
    """price_for already matches aliases exactly, never by prefix - this makes
    an unsafe one impossible to STORE, not just impossible to match."""
    client, _ = api
    r = client.put("/api/pricing/some-model", json={
        "input": 1.0, "output": 2.0, "as_of": ASOF, "source": SRC,
        "aliases": ["gpt-5*"]})
    assert r.status_code == 400
    assert "pattern" in r.json()["detail"]


def test_an_alias_colliding_with_another_priced_model_is_refused(api):
    """Two cards claiming the same id is ambiguous pricing: exactly the class
    of silent wrongness fail-closed pricing exists to prevent."""
    client, _ = api
    r = client.put("/api/pricing/some-model", json={
        "input": 1.0, "output": 2.0, "as_of": ASOF, "source": SRC,
        "aliases": ["claude-sonnet-5"]})
    assert r.status_code == 400
    assert "already a separate priced model" in r.json()["detail"]


def test_a_refusal_is_a_plain_english_400_not_a_bare_422(api):
    """The form displays `detail` verbatim, so the reason must be readable by a
    person rather than a pydantic field trace."""
    client, _ = api
    d = client.put("/api/pricing/some-model", json={
        "input": 1.0, "output": 2.0, "source": SRC}).json()["detail"]
    assert isinstance(d, str) and "As-of date is required" in d


def test_a_backup_of_the_previous_config_is_kept(api):
    """Atomicity guarantees the file is never half-written; it does not
    guarantee the new contents are what the operator meant, and this is their
    personal config."""
    client, local = api
    local.write_text(json.dumps({"user_name": "Alex"}))
    client.put("/api/pricing/some-model", json={
        "input": 1.0, "output": 2.0, "as_of": ASOF, "source": SRC})
    bak = local.with_suffix(".json.bak")
    assert bak.exists()
    assert json.loads(bak.read_text())["user_name"] == "Alex"
    assert "pricing" not in json.loads(bak.read_text())   # the PREVIOUS contents
