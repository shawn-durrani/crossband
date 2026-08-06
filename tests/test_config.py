"""Config layering: defaults < config.json < config.local.json < env."""

import json

import pytest

from backend.config import (DEFAULT_PRICING, Settings, compute_cost,
                            load_settings, price_for)


def _write(path, data):
    path.write_text(json.dumps(data))


def test_defaults_apply_without_any_files(tmp_path):
    s = load_settings(root=tmp_path, environ={})
    assert s.port == 8902
    assert s.host == "127.0.0.1"
    assert s.user_name == "User"
    assert s.memory_url == "http://127.0.0.1:8901"


def test_config_json_overrides_defaults(tmp_path):
    _write(tmp_path / "config.json", {"user_name": "Json", "port": 1111})
    s = load_settings(root=tmp_path, environ={})
    assert s.user_name == "Json"
    assert s.port == 1111
    assert s.utility_model == "claude-haiku-4-5"  # untouched default


def test_local_config_overrides_config_json(tmp_path):
    _write(tmp_path / "config.json", {"user_name": "Json", "port": 1111})
    _write(tmp_path / "config.local.json", {"user_name": "Local"})
    s = load_settings(root=tmp_path, environ={})
    assert s.user_name == "Local"
    assert s.port == 1111  # not overridden by local


def test_env_overrides_everything(tmp_path):
    _write(tmp_path / "config.json", {"user_name": "Json"})
    _write(tmp_path / "config.local.json", {"user_name": "Local", "port": 2222})
    env = {"MMC_USER_NAME": "Env", "MMC_PORT": "3333", "MMC_TTS_SPEED": "1.1",
           "MMC_REQUIRE_KEYS": "true"}
    s = load_settings(root=tmp_path, environ=env)
    assert s.user_name == "Env"
    assert s.port == 3333
    assert s.tts_speed == 1.1
    assert s.require_keys is True


def test_unknown_and_malformed_values_never_brick_startup(tmp_path):
    _write(tmp_path / "config.json", {"mystery_key": 42, "user_name": "Ok"})
    (tmp_path / "config.local.json").write_text("{not json")
    s = load_settings(root=tmp_path, environ={"MMC_PORT": "not-a-number"})
    assert s.user_name == "Ok"
    assert s.port == 8902  # bad env value ignored


def test_price_for_exact_and_dated_variant_only():
    # Exact ids match; so do date/build-stamped reissues of the SAME model
    # (a boundary separator then a digit). Nothing else.
    assert price_for("gpt-5.1", DEFAULT_PRICING) == DEFAULT_PRICING["gpt-5.1"]
    assert price_for("gpt-5", DEFAULT_PRICING) == DEFAULT_PRICING["gpt-5"]
    assert price_for("gpt-5.5-2026-01-15", DEFAULT_PRICING) == DEFAULT_PRICING["gpt-5.5"]
    assert price_for("claude-opus-4-8-20260101", DEFAULT_PRICING) == DEFAULT_PRICING["claude-opus-4-8"]
    assert price_for("unknown-model", DEFAULT_PRICING) is None


def test_price_for_fails_closed_on_new_named_model():
    """The load-bearing regression. A newly configured model with a NEW
    name must NOT be silently priced as an older family — it fails closed to
    unknown until an exact/aliased rate card is supplied. A `-mini`/`-pro`/named
    variant is a different model, not a reissue.

    `gpt-5.6-terra` was this test's headline case until it was given a real,
    verified card. Its separately-published sibling `gpt-5.6-terra-pro` takes
    over the role — and is the sharper case, because it now sits one suffix away
    from a priced entry and still must not inherit it."""
    from backend.config import provenance_for, compute_cost
    for m in ("gpt-5.6-terra-pro", "gpt-5-mini", "claude-sonnet-5-pro"):
        assert price_for(m, DEFAULT_PRICING) is None
        assert provenance_for(m, DEFAULT_PRICING)["source"] == "unknown"
        assert compute_cost(m, {"input": 1_000_000, "output": 1_000_000},
                            DEFAULT_PRICING) is None


def test_price_for_explicit_alias_is_the_supported_escape_hatch():
    """The extensible path: an operator attests a new model is priced
    like an existing card by adding it to that card's `aliases`. Then it prices,
    with the aliased card's provenance, WITHOUT a broad family guess."""
    from backend import provenance
    from backend.config import _openai_rate_card, provenance_for
    pricing = dict(DEFAULT_PRICING)
    pricing["gpt-5.6-terra"] = _openai_rate_card(
        2.0, 16.0, "2026-07-31", "https://openai.com/api/pricing",
        aliases=("gpt-5.6-terra-2026-08-01",))
    # exact hit on the new card
    assert price_for("gpt-5.6-terra", pricing) is pricing["gpt-5.6-terra"]
    assert provenance_for("gpt-5.6-terra", pricing)["source"] == provenance.RATE_CARD_ESTIMATE
    # its explicitly-declared compatible id resolves to the same card
    assert price_for("gpt-5.6-terra-2026-08-01", pricing) is pricing["gpt-5.6-terra"]


def test_cache_terms_are_per_provider_not_a_global_multiplier():
    """The mechanism: each card carries its OWN cache terms and compute_cost
    applies them, instead of one global ratio.

    A later correction fixed the numbers this test used to assert. It claimed
    OpenAI "charges no cache-write premium" and pinned $0, but the published
    card bills gpt-5.6-terra cache writes at $2.50/M against $2.00/M input:
    the SAME 1.25x premium Anthropic charges. The mechanism is still
    load-bearing (a card genuinely can declare different terms), so it is now
    exercised with an explicit card rather than by relying on two providers
    happening to differ."""
    from backend.config import compute_cost, _rate_card
    usage = {"cache_creation": 1_000_000}
    # anthropic sonnet: 1M cache-write * $3 input * 1.25 = $3.75
    assert compute_cost("claude-sonnet-5", usage, DEFAULT_PRICING) == pytest.approx(3.75)
    # openai terra: $2.00 input * 1.25 = $2.50/M written (verified)
    assert compute_cost("gpt-5.6-terra", usage, DEFAULT_PRICING) == pytest.approx(2.50)
    # a cache READ discounts to 0.1x on both
    assert compute_cost("gpt-5.6-terra", {"cache_read": 1_000_000}, DEFAULT_PRICING) \
        == pytest.approx(0.20)
    # the per-card mechanism itself: a card declaring its own terms is honoured
    own_terms = {"m": _rate_card(10.0, 20.0, "2026-01-01", "test",
                                 cache={"read_mult": 0.5, "write_mult": 0.0})}
    assert compute_cost("m", usage, own_terms) == pytest.approx(0.0)
    assert compute_cost("m", {"cache_read": 1_000_000}, own_terms) == pytest.approx(5.0)


def test_gpt56_terra_is_priced_from_the_published_card():
    """The live GPT seat's model, priced from OpenAI's published standard
    short-context row ($2.00 in / $0.20 cached / $12.00 out) rather than aliased
    onto a neighbouring family. Stamped with its OWN verification date — the
    older OpenAI entries were not re-checked, and restamping unchecked figures
    is how a rate card starts lying."""
    from backend import provenance
    from backend.config import provenance_for
    card = price_for("gpt-5.6-terra", DEFAULT_PRICING)
    assert card is not None
    assert (card["input"], card["output"]) == (2.0, 12.0)
    assert card["as_of"] == "2026-07-31"
    assert "developers.openai.com" in card["source"]
    assert provenance_for("gpt-5.6-terra", DEFAULT_PRICING)["source"] \
        == provenance.RATE_CARD_ESTIMATE
    # 1M in + 1M out at the published rate
    assert compute_cost("gpt-5.6-terra", {"input": 1_000_000, "output": 1_000_000},
                        DEFAULT_PRICING) == pytest.approx(14.0)


def test_compute_cost_math():
    pricing = {"m": {"input": 10.0, "output": 20.0}}
    usage = {"input": 1_000_000, "cache_creation": 1_000_000,
             "cache_read": 1_000_000, "output": 1_000_000}
    # 10 + 10*1.25 + 10*0.1 + 20 = 43.5
    assert compute_cost("m", usage, pricing) == 43.5
    assert compute_cost("nope", usage, pricing) is None


def test_as_cfg_roundtrip():
    cfg = Settings(user_name="X").as_cfg()
    assert cfg["user_name"] == "X"
    assert cfg["max_tool_rounds"] == 6
    assert "pricing" in cfg


def test_pricing_entries_carry_provenance_metadata():
    # Every default entry carries a real, tracked provenance: a published
    # list price (rate_card_estimate) or a declared local $0
    # (self_hosted_zero_marginal), never "unknown".
    from backend import provenance
    for model, entry in DEFAULT_PRICING.items():
        assert entry["provenance"] in (provenance.RATE_CARD_ESTIMATE,
                                       provenance.SELF_HOSTED_ZERO_MARGINAL)
        assert entry["as_of"] and entry["source"]
        assert "input" in entry and "output" in entry  # compute_cost still works
    assert compute_cost("claude-opus-4-8", {"output": 1_000_000}, DEFAULT_PRICING) \
        == pytest.approx(25.0)


def test_current_roster_models_are_priced_and_onboardable():
    """The models the roster actually runs must have KNOWN provenance, else
    their seats are stuck in trial and can't be activated. Regression
    for the stale table that omitted claude-sonnet-5 and gpt-oss:20b."""
    from backend import provenance
    from backend.config import provenance_for
    assert provenance_for("claude-sonnet-5", DEFAULT_PRICING)["source"] \
        == provenance.RATE_CARD_ESTIMATE
    local = provenance_for("gpt-oss:20b", DEFAULT_PRICING)
    assert local["source"] == provenance.SELF_HOSTED_ZERO_MARGINAL
    # a declared local $0 is a REAL tracked cost, not "unknown"
    assert compute_cost("gpt-oss:20b", {"output": 1_000_000}, DEFAULT_PRICING) == 0.0
    for m in ("claude-sonnet-5", "gpt-oss:20b"):
        assert provenance_for(m, DEFAULT_PRICING)["source"] != "unknown"


def test_provenance_for_known_and_unknown():
    from backend.config import provenance_for
    rec = provenance_for("gpt-5.1", DEFAULT_PRICING)
    assert rec["source"] == "rate_card_estimate"
    assert rec["estimated"] is True
    assert rec["as_of"] and rec["source_ref"]
    # a model absent from the table has no provenance record → unknown
    unk = provenance_for("mystery-model", DEFAULT_PRICING)
    assert unk["source"] == "unknown"
    assert unk["estimated"] is None
