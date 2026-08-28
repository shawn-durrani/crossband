"""A saved rate card has to reach the next round, and the Spend page (#230).

Two defects with one root. app.state.settings is assigned once at boot, and a
round prices from that snapshot: engine.run_round calls settings.as_cfg(), then
tools.refresh_repo_maps, which overlaid github_repos and code_repos and not
pricing. engine stamps provenance at turn time, so a round priced from a stale
table writes rows that are permanently wrong rather than recoverably wrong.

Separately, both Spend endpoints called iter_cost_events with no pricing
argument, so it fell back to module-level DEFAULT_PRICING. Cache writes are
computed on read rather than stamped, so an override-priced model read $0.00
there while the diagnostics surface, which passes cfg['pricing'], read
correctly.

routers/pricing.py's docstring and frontend/src/api.js both promised the first
behaviour before it existed. These cases hold them to it.
"""
import json

import pytest
from fastapi.testclient import TestClient

from backend import accounting, config, provenance as prov, tools
from backend.app import create_app
from backend.config import Settings

MODEL = "acme-forge-1"
SRC = "https://acmeco.example/pricing"
ASOF = "2026-08-01"
CARD = {"input": 12.0, "output": 60.0, "source": SRC, "as_of": ASOF}


@pytest.fixture
def api(tmp_path, monkeypatch):
    """A real app, so app.state.settings exists, with overrides landing in a
    temp config.local.json rather than the developer's own."""
    from backend.routers import pricing as pricing_router
    local = tmp_path / "config.local.json"
    local.write_text("{}")
    monkeypatch.setattr(pricing_router, "LOCAL_CONFIG_PATH", local)
    monkeypatch.setattr(config, "LOCAL_CONFIG_PATH", local)
    real_load = config.load_settings
    monkeypatch.setattr(pricing_router, "load_settings",
                        lambda: real_load(root=tmp_path, environ={}))
    monkeypatch.setattr(tools, "load_settings",
                        lambda: real_load(root=tmp_path, environ={}))
    app = create_app(Settings(data_dir=str(tmp_path / "data"),
                              memory_url="http://127.0.0.1:1"))
    with TestClient(app, base_url="http://127.0.0.1") as c:
        yield c, app, local


def _save(client, model=MODEL, card=None):
    return client.put(f"/api/pricing/{model}", json=card or CARD)


# ---------- the round ----------

def test_a_saved_card_is_in_the_next_rounds_config(api):
    client, app, _ = api
    before = tools.refresh_repo_maps(app.state.settings.as_cfg())
    assert MODEL not in (before.get("pricing") or {})

    assert _save(client).status_code == 200

    after = tools.refresh_repo_maps(app.state.settings.as_cfg())
    assert after["pricing"][MODEL]["input"] == 12.0


def test_a_hand_edited_config_still_reaches_the_round(api):
    """The router is not the only way in. pricing.py's docstring names
    hand-editing config.local.json as the fallback, and then no route runs."""
    _, app, local = api
    local.write_text(json.dumps({"pricing": {MODEL: dict(
        CARD, provenance="rate_card_estimate")}}))
    cfg = tools.refresh_repo_maps(app.state.settings.as_cfg())
    assert cfg["pricing"][MODEL]["output"] == 60.0


def test_an_unreadable_config_keeps_the_prices_already_in_cfg(api, monkeypatch):
    """Same contract the repo maps already had: never drop prices mid-round
    because a config read failed."""
    _, app, _ = api

    def boom():
        raise OSError("config gone")

    monkeypatch.setattr(tools, "load_settings", boom)
    cfg = app.state.settings.as_cfg()
    had = dict(cfg.get("pricing") or {})
    out = tools.refresh_repo_maps(cfg)
    assert (out.get("pricing") or {}) == had
    assert "claude-sonnet-5" in out["pricing"]


# ---------- everything else that reads the shared snapshot ----------

def test_a_saved_card_reaches_the_shared_settings_snapshot(api):
    """Not only the round. The benchmark, the voice routes and the attachment
    cap all read app.state.settings at request time."""
    client, app, _ = api
    assert MODEL not in (app.state.settings.pricing or {})
    assert _save(client).status_code == 200
    assert app.state.settings.pricing[MODEL]["input"] == 12.0


def test_deleting_a_card_also_republishes(api):
    client, app, _ = api
    _save(client)
    assert MODEL in app.state.settings.pricing
    assert client.delete(f"/api/pricing/{MODEL}").status_code == 200
    assert MODEL not in (app.state.settings.pricing or {})


def test_a_refused_save_republishes_nothing(api):
    """A card with no source is refused, and a refusal must not swap the
    snapshot underneath a running round."""
    client, app, _ = api
    before = app.state.settings
    bad = {"input": 1.0, "output": 2.0, "source": "", "as_of": ASOF}
    assert client.put(f"/api/pricing/{MODEL}", json=bad).status_code == 400
    assert app.state.settings is before


# ---------- the Spend page prices cache writes off the right table ----------

def test_cache_write_cost_follows_the_table_it_is_given():
    """_cache_write_cost is computed on read, so the table passed in decides
    the figure. Given DEFAULT_PRICING it knows nothing about an
    override-priced model and returns zero."""
    usage = {"cache_creation": 1_000_000}
    with_card = accounting._cache_write_cost(
        usage, MODEL, {MODEL: CARD}, prov.RATE_CARD_ESTIMATE)
    without = accounting._cache_write_cost(
        usage, MODEL, config.DEFAULT_PRICING, prov.RATE_CARD_ESTIMATE)
    assert with_card == pytest.approx(1_000_000 * 12.0 * 1.25 / 1_000_000)
    assert without == 0.0


def test_both_spend_endpoints_take_the_effective_table(api):
    """Wiring guard rather than arithmetic: both called iter_cost_events bare,
    so both read DEFAULT_PRICING no matter what the operator had saved."""
    client, _, _ = api
    _save(client)
    seen = {}
    real = accounting.iter_cost_events

    def spy(con, **kw):
        seen.setdefault("tables", []).append(kw.get("pricing"))
        return real(con, **kw)

    import backend.routers.chats as chats_router
    chats_router.accounting.iter_cost_events = spy
    try:
        assert client.get("/api/usage/chats").status_code == 200
        assert client.get("/api/usage/summary").status_code == 200
    finally:
        chats_router.accounting.iter_cost_events = real

    assert len(seen["tables"]) == 2
    for table in seen["tables"]:
        assert table is not None, "endpoint still calls iter_cost_events bare"
        assert "claude-sonnet-5" in table
