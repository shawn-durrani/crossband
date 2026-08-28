"""Shared cost accounting: classification of spend by provenance
(metered vs subscription-equivalent vs unknown), time-window aggregation, the
breakdown groupings, and the /api/usage/summary + /api/usage/chats endpoints.

The load-bearing guardrail here is honesty: Claude Code guest cost must NOT be
summed into metered spend unless the turn recorded it ran on the API key, and
sources with no cost must surface as "not tracked", never a silent $0.00.
"""

import json
import time

import pytest

from backend import accounting, db, provenance
from backend.config import Settings


@pytest.fixture
def con(tmp_path):
    db.configure(str(tmp_path / "data"))
    db.init(Settings(data_dir=str(tmp_path / "data")))
    c = db.connect()
    yield c
    c.close()


def _msg(con, speaker, usage, created_at):
    con.execute(
        "INSERT INTO chats(id, title, created_at, updated_at) VALUES(1,'T',?,?)"
        " ON CONFLICT(id) DO NOTHING", (created_at, created_at))
    con.execute(
        "INSERT INTO messages(chat_id, speaker, content, usage_json, created_at) "
        "VALUES(1,?,?,?,?)",
        (speaker, "x", json.dumps(usage) if usage is not None else None, created_at))
    con.commit()


def _base_usage(cost, **extra):
    u = {"input": 100, "output": 200, "cache_read": 0, "cache_creation": 0,
         "cost": cost}
    u.update(extra)
    return u


NOW = time.time()


def test_resident_turn_is_metered(con):
    _msg(con, "claude", _base_usage(0.05, model="claude-opus-4-8"), NOW)
    events = list(accounting.iter_cost_events(con))
    assert len(events) == 1
    e = events[0]
    assert e.source == accounting.SOURCE_NORMAL
    assert e.category == accounting.CAT_METERED
    assert e.provider == "anthropic"  # from seeded participants
    assert e.cost == pytest.approx(0.05)


def test_guest_auth_drives_category(con):
    _msg(con, "claude-code", _base_usage(2.0, auth="api_key"), NOW)
    _msg(con, "claude-code", _base_usage(3.0, auth="subscription"), NOW)
    _msg(con, "claude-code", _base_usage(4.0), NOW)  # no auth → unknown
    by_cat = {}
    for e in accounting.iter_cost_events(con):
        by_cat[e.category] = by_cat.get(e.category, 0) + e.cost
    assert by_cat[accounting.CAT_METERED] == pytest.approx(2.0)
    assert by_cat[accounting.CAT_SUBSCRIPTION] == pytest.approx(3.0)
    assert by_cat[accounting.CAT_UNKNOWN] == pytest.approx(4.0)


def test_summarize_keeps_categories_separate(con):
    _msg(con, "claude", _base_usage(0.10, model="claude-opus-4-8"), NOW)
    _msg(con, "claude-code", _base_usage(9.0, auth="subscription"), NOW)
    s = accounting.summarize(list(accounting.iter_cost_events(con)))
    # subscription-equivalent must never be folded into metered
    assert s["totals"][accounting.CAT_METERED] == pytest.approx(0.10)
    assert s["totals"][accounting.CAT_SUBSCRIPTION] == pytest.approx(9.0)
    assert s["totals"][accounting.CAT_UNKNOWN] == 0.0


def test_verified_model_preferred(con):
    _msg(con, "claude-code",
         _base_usage(1.0, auth="api_key", model="default",
                     verified_model="claude-sonnet-4-5-20250929"), NOW)
    models = {e.model for e in accounting.iter_cost_events(con)}
    assert "claude-sonnet-4-5-20250929" in models


def test_voice_is_metered_and_unpriced_voice_not_tracked(con):
    con.execute("INSERT INTO chats(id, title, created_at, updated_at) "
                "VALUES(1,'T',?,?) ON CONFLICT(id) DO NOTHING", (NOW, NOW))
    db.log_voice_usage(con, 1, "tts", 5000, 0.55)
    db.log_voice_usage(con, 1, "stt", 60, None)  # logged without cost
    con.commit()
    s = accounting.summarize(list(accounting.iter_cost_events(con)))
    assert s["totals"][accounting.CAT_METERED] == pytest.approx(0.55)
    assert accounting.SOURCE_STT in s["not_tracked"]


def test_time_window_filtering(con):
    old = NOW - 30 * 86400
    _msg(con, "claude", _base_usage(1.0, model="claude-opus-4-8"), old)
    _msg(con, "claude", _base_usage(2.0, model="claude-opus-4-8"), NOW)
    events = list(accounting.iter_cost_events(con))
    recent = accounting.summarize(events, since=NOW - 7 * 86400)
    all_time = accounting.summarize(events)
    assert recent["totals"][accounting.CAT_METERED] == pytest.approx(2.0)
    assert all_time["totals"][accounting.CAT_METERED] == pytest.approx(3.0)


def test_empty_db_is_zeros_not_crash(con):
    s = accounting.summarize(list(accounting.iter_cost_events(con)))
    assert s["totals"] == {c: 0.0 for c in accounting.CATEGORIES}
    assert s["by_source"] == [] and s["not_tracked"] == []


def test_breakdowns_group_by_chat_and_source(con):
    _msg(con, "claude", _base_usage(0.20, model="claude-opus-4-8"), NOW)
    _msg(con, "claude-code", _base_usage(5.0, auth="api_key"), NOW)
    s = accounting.summarize(list(accounting.iter_cost_events(con)),
                             chat_titles={1: "My chat"})
    sources = {g["key"] for g in s["by_source"]}
    assert sources == {accounting.SOURCE_NORMAL, accounting.SOURCE_CODE}
    assert s["by_chat"][0]["label"] == "My chat"
    # by_source top entry is the coding-agent turn (biggest cost)
    assert s["by_source"][0]["key"] == accounting.SOURCE_CODE


def test_code_source_label_is_coding_agent(con):
    # The Spend page's by_source row for Claude Code reads "Coding
    # agent", not "guest" - a category label, not a mechanism name. The key
    # (SOURCE_CODE) is unchanged; only the display label moved.
    _msg(con, "claude-code", _base_usage(1.0, auth="api_key"), NOW)
    s = accounting.summarize(list(accounting.iter_cost_events(con)))
    row = next(g for g in s["by_source"] if g["key"] == accounting.SOURCE_CODE)
    assert row["label"] == "Coding agent"


def test_by_party_groups_dynamically_on_speaker(con):
    """The per-party split is keyed on the event's own speaker, so any
    number of participants/guests/producers flow in with no hardcoded roster -
    and subscription-equivalent stays on its own axis, never in metered."""
    _msg(con, "claude", _base_usage(0.10, model="claude-opus-4-8"), NOW)
    _msg(con, "gpt", _base_usage(0.20, model="gpt-5.1"), NOW)
    _msg(con, "claude-code", _base_usage(9.0, auth="subscription"), NOW)
    _msg(con, "claude-code", _base_usage(3.0, auth="api_key"), NOW)
    con.execute("INSERT INTO chats(id, title, created_at, updated_at) "
                "VALUES(1,'T',?,?) ON CONFLICT(id) DO NOTHING", (NOW, NOW))
    db.log_voice_usage(con, 1, "tts", 5000, 0.55)
    con.commit()
    s = accounting.summarize(list(accounting.iter_cost_events(con)))
    by_party = {g["key"]: g for g in s["by_party"]}
    assert set(by_party) == {"claude", "gpt", "claude-code", "voice"}
    # the guest's two turns: api_key metered, subscription on its own axis
    assert by_party["claude-code"][accounting.CAT_METERED] == pytest.approx(3.0)
    assert by_party["claude-code"][accounting.CAT_SUBSCRIPTION] == pytest.approx(9.0)
    assert by_party["voice"][accounting.CAT_METERED] == pytest.approx(0.55)


def test_same_model_from_chat_and_guest_stays_separate(con):
    """The load-bearing regression. The Claude chat seat and a summoned
    Claude Code guest can run the SAME concrete model (claude-sonnet-5). The
    landing breakdown must NOT merge them into one bar - that merge is what made
    "Sonnet costs way more than GPT" a lie (guest tool-loop spend masquerading
    as chat spend). by_model still merges (it groups on model alone); the
    producer-aware axis keeps them apart."""
    _msg(con, "claude", _base_usage(0.30, model="claude-sonnet-5"), NOW)
    _msg(con, "claude-code",
         _base_usage(5.0, auth="api_key", verified_model="claude-sonnet-5"), NOW)
    s = accounting.summarize(list(accounting.iter_cost_events(con)))

    # by_model (drilldown) still exists and merges on model id - one bucket.
    by_model = {g["key"]: g for g in s["by_model"]}
    assert by_model["claude-sonnet-5"][accounting.CAT_METERED] == pytest.approx(5.30)

    # by_producer_model keeps like with like: two rows, one per producer.
    bpm = {g["key"]: g for g in s["by_producer_model"]}
    chat_key = str((accounting.PRODUCER_CHAT, "claude-sonnet-5"))
    agent_key = str((accounting.PRODUCER_AGENT, "claude-sonnet-5"))
    assert bpm[chat_key][accounting.CAT_METERED] == pytest.approx(0.30)
    assert bpm[agent_key][accounting.CAT_METERED] == pytest.approx(5.0)
    # and the coarse producer axis separates chat from agents too.
    by_prod = {g["key"]: g for g in s["by_producer"]}
    assert by_prod[accounting.PRODUCER_CHAT][accounting.CAT_METERED] == pytest.approx(0.30)
    assert by_prod[accounting.PRODUCER_AGENT][accounting.CAT_METERED] == pytest.approx(5.0)


def test_producer_axes_reconcile_to_totals_without_double_count(con):
    """Every producer-aware grouping is the SAME events grouped differently, so
    each must sum back to the metered total - no bar invents or drops a dollar."""
    _msg(con, "claude", _base_usage(0.30, model="claude-sonnet-5"), NOW)
    _msg(con, "gpt", _base_usage(0.20, model="gpt-5.1"), NOW)
    _msg(con, "claude-code",
         _base_usage(5.0, auth="api_key", verified_model="claude-sonnet-5"), NOW)
    con.execute("INSERT INTO chats(id, title, created_at, updated_at) "
                "VALUES(1,'T',?,?) ON CONFLICT(id) DO NOTHING", (NOW, NOW))
    db.log_voice_usage(con, 1, "tts", 5000, 0.55)
    con.commit()
    s = accounting.summarize(list(accounting.iter_cost_events(con)))
    total = s["totals"][accounting.CAT_METERED]
    assert total == pytest.approx(0.30 + 0.20 + 5.0 + 0.55)
    for axis in ("by_producer", "by_producer_model", "by_model", "by_source"):
        summed = sum(g[accounting.CAT_METERED] for g in s[axis])
        assert summed == pytest.approx(total), axis


def test_new_unpriced_model_fails_closed_in_accounting(con):
    """Fail-closed pricing end to end: a resident turn on a brand-new named
    model with no rate card records no cost (compute_cost fails closed),
    surfaced as a gap / unknown provenance, never a silent $0 and never priced
    as an older family.

    Used `gpt-5.6-terra` until it was given a verified card; its separately
    published sibling `gpt-5.6-terra-pro` now stands in, and is the stronger
    case - one suffix from a priced entry, and still must not inherit it."""
    # No 'cost' recorded and no matching card → has_cost False, provenance unknown.
    _msg(con, "gpt", {"input": 100, "output": 200, "model": "gpt-5.6-terra-pro"}, NOW)
    events = list(accounting.iter_cost_events(con))
    e = next(ev for ev in events if ev.model == "gpt-5.6-terra-pro")
    assert e.provenance == "unknown"
    assert e.has_cost is False
    s = accounting.summarize(events)
    assert accounting.SOURCE_NORMAL in s["not_tracked"]
    assert s["billed"] == 0.0


def test_daily_series_buckets_by_day(con):
    day1 = NOW - 2 * 86400
    _msg(con, "claude", _base_usage(1.0, model="claude-opus-4-8"), day1)
    _msg(con, "claude", _base_usage(2.0, model="claude-opus-4-8"), NOW)
    series = accounting.daily_series(list(accounting.iter_cost_events(con)))
    assert len(series) == 2
    assert series[0]["day"] < series[1]["day"]


def test_auth_provenance_reflects_config():
    on = accounting.auth_provenance({"code_use_api_key": True})
    off = accounting.auth_provenance({"code_use_api_key": False})
    assert on["mode"] == "api_key" and off["mode"] == "subscription"


def test_resident_turn_provenance_is_rate_card_estimate(con):
    # A dollar computed from the local price table is an ESTIMATE, never
    # billed spend - even though it stays in the metered cash bucket.
    _msg(con, "claude", _base_usage(0.05, model="claude-opus-4-8"), NOW)
    e = list(accounting.iter_cost_events(con))[0]
    assert e.provenance == "rate_card_estimate"
    s = accounting.summarize([e])
    assert s["billed"] == 0.0  # nothing provider-reported → nothing billed
    assert s["provenance_totals"]["rate_card_estimate"] == pytest.approx(0.05)


def test_guest_api_key_turn_is_provider_reported_billed(con):
    _msg(con, "claude-code", _base_usage(2.0, auth="api_key"), NOW)
    s = accounting.summarize(list(accounting.iter_cost_events(con)))
    assert s["billed"] == pytest.approx(2.0)
    assert s["provenance_totals"]["provider_reported"] == pytest.approx(2.0)


def test_recorded_provenance_is_immutable_to_rate_card_edits(con):
    # A turn recorded its provenance at turn time; later "editing the rate card"
    # (passing a different pricing table) must NOT rewrite that history.
    from backend import provenance
    _msg(con, "claude",
         _base_usage(0.05, model="claude-opus-4-8",
                     cost_provenance=provenance.record(
                         provenance.RATE_CARD_ESTIMATE, as_of="2025-01-01",
                         source_ref="old card")), NOW)
    edited = {"claude-opus-4-8": {"input": 999.0, "output": 999.0,
                                  "provenance": provenance.SELF_HOSTED_ZERO_MARGINAL,
                                  "as_of": "2099-01-01", "source": "new"}}
    e = list(accounting.iter_cost_events(con, pricing=edited))[0]
    assert e.provenance == "rate_card_estimate"  # recorded value wins


def test_self_hosted_zero_is_distinct_from_not_tracked(con):
    from backend import provenance
    # zero-marginal self-hosted: cost 0.0 but explicitly declared (has_cost)
    _msg(con, "llama",
         _base_usage(0.0, model="llama-3",
                     cost_provenance=provenance.record(
                         provenance.SELF_HOSTED_ZERO_MARGINAL)), NOW)
    # a genuinely untracked resident turn: no cost at all
    _msg(con, "gpt", {"input": 5, "output": 5, "cost": None}, NOW)
    s = accounting.summarize(list(accounting.iter_cost_events(con)))
    assert "self_hosted_zero_marginal" in s["tracked_provenances"]
    assert accounting.SOURCE_NORMAL in s["not_tracked"]
    # the self-hosted $0.00 is a declared fact, not folded into billed spend
    assert s["billed"] == 0.0
    by_prov = {g["key"]: g for g in s["by_provenance"]}
    assert by_prov["self_hosted_zero_marginal"]["not_tracked"] is False


def test_summary_endpoint(tmp_path):
    from fastapi.testclient import TestClient
    from backend.app import create_app
    settings = Settings(data_dir=str(tmp_path / "data"),
                        memory_url="http://127.0.0.1:1")
    with TestClient(create_app(settings), base_url="http://127.0.0.1") as client:
        chat = client.post("/api/chats", json={}).json()
        con = db.connect()
        con.execute(
            "INSERT INTO messages(chat_id, speaker, content, usage_json, created_at) "
            "VALUES(?,?,?,?,?)",
            (chat["id"], "claude-code", "x",
             json.dumps({"input": 10, "output": 20, "cost": 7.5,
                         "auth": "subscription"}), time.time()))
        con.commit()
        con.close()
        r = client.get("/api/usage/summary?window=all")
        assert r.status_code == 200
        data = r.json()
        assert data["window"] == "all"
        assert data["windows"]["all"]["totals"]["subscription_equiv"] == pytest.approx(7.5)
        assert data["windows"]["all"]["totals"]["metered"] == 0.0
        assert "auth" in data and "series" in data


def test_chats_usage_endpoint_keeps_buckets_apart(tmp_path):
    """The export picker's per-chat totals must not collapse a
    subscription-covered guest turn into apparent spend: the same defect fixed
    in the chat header, one layer lower where the browser can't undo it."""
    from fastapi.testclient import TestClient
    from backend.app import create_app
    settings = Settings(data_dir=str(tmp_path / "data"),
                        memory_url="http://127.0.0.1:1")
    with TestClient(create_app(settings), base_url="http://127.0.0.1") as client:
        chat = client.post("/api/chats", json={}).json()
        con = db.connect()
        for usage in ({"input": 10, "output": 20, "cost": 111.0,
                       "auth": "subscription"},
                      {"input": 10, "output": 20, "cost": 2.0, "auth": "api_key"},
                      {"input": 10, "output": 20, "cost": 5.0}):   # no auth
            con.execute(
                "INSERT INTO messages(chat_id, speaker, content, usage_json, "
                "created_at) VALUES(?,?,?,?,?)",
                (chat["id"], "claude-code", "x", json.dumps(usage), time.time()))
        con.commit()
        con.close()

        row = client.get("/api/usage/chats").json()[str(chat["id"])]

    # There is deliberately no single `cost` - a consumer cannot accidentally
    # re-sum what this endpoint exists to keep apart.
    assert "cost" not in row
    assert row["metered"] == pytest.approx(2.0)
    assert row["subscription_equiv"] == pytest.approx(111.0)
    assert row["unknown"] == pytest.approx(5.0)
    assert row["messages"] == 3
    assert row["tokens"] == 90
    assert row["not_tracked"] is False


def test_chats_usage_reports_untracked_cost_as_a_gap(tmp_path):
    """A turn with no recorded cost is a gap, never a silent $0.00."""
    from fastapi.testclient import TestClient
    from backend.app import create_app
    settings = Settings(data_dir=str(tmp_path / "data"),
                        memory_url="http://127.0.0.1:1")
    with TestClient(create_app(settings), base_url="http://127.0.0.1") as client:
        chat = client.post("/api/chats", json={}).json()
        con = db.connect()
        con.execute(
            "INSERT INTO messages(chat_id, speaker, content, usage_json, created_at) "
            "VALUES(?,?,?,?,?)",
            (chat["id"], "claude", "x", json.dumps({"input": 5, "output": 5}),
             time.time()))
        con.commit()
        con.close()
        row = client.get("/api/usage/chats").json()[str(chat["id"])]

    assert row["not_tracked"] is True
    assert row["tokens"] == 10
    assert row["metered"] == 0.0


def test_chats_usage_lists_a_chat_with_no_usage_at_all(tmp_path):
    """The picker lists every chat; one with nothing priced reports zeros and
    its message count, not a missing key the client has to guess about."""
    from fastapi.testclient import TestClient
    from backend.app import create_app
    settings = Settings(data_dir=str(tmp_path / "data"),
                        memory_url="http://127.0.0.1:1")
    with TestClient(create_app(settings), base_url="http://127.0.0.1") as client:
        chat = client.post("/api/chats", json={}).json()
        con = db.connect()
        con.execute(
            "INSERT INTO messages(chat_id, speaker, content, created_at) "
            "VALUES(?,?,?,?)", (chat["id"], "user", "hello", time.time()))
        con.commit()
        con.close()
        row = client.get("/api/usage/chats").json()[str(chat["id"])]

    assert row["messages"] == 1
    assert row["tokens"] == 0
    assert all(row[c] == 0.0 for c in accounting.CATEGORIES)


def test_chats_usage_agrees_with_the_spend_page(tmp_path):
    """The picker and the Spend page's per-chat rows are the same accounting.
    Two surfaces disagreeing about one chat's spend is the bug class these
    fixes exist to close, so this pins them to the same numbers."""
    from fastapi.testclient import TestClient
    from backend.app import create_app
    settings = Settings(data_dir=str(tmp_path / "data"),
                        memory_url="http://127.0.0.1:1")
    with TestClient(create_app(settings), base_url="http://127.0.0.1") as client:
        chat = client.post("/api/chats", json={}).json()
        con = db.connect()
        con.execute(
            "INSERT INTO messages(chat_id, speaker, content, usage_json, created_at) "
            "VALUES(?,?,?,?,?)",
            (chat["id"], "claude-code", "x",
             json.dumps({"input": 10, "output": 20, "cost": 9.0,
                         "auth": "subscription"}), time.time()))
        # Voice rides along on the same chat: the old hand-rolled query counted
        # it, so dropping it here would be a silent regression.
        con.execute(
            "INSERT INTO voice_usage(chat_id, kind, units, cost, created_at) "
            "VALUES(?,?,?,?,?)", (chat["id"], "tts", 400, 0.25, time.time()))
        con.commit()
        con.close()

        picker = client.get("/api/usage/chats").json()[str(chat["id"])]
        spend = client.get("/api/usage/summary?window=all").json()
        by_chat = {g["key"]: g for g in spend["breakdown"]["by_chat"]}
        row = by_chat[str(chat["id"])]

    for cat in accounting.CATEGORIES:
        assert picker[cat] == pytest.approx(row[cat])
    assert picker["tokens"] == row["tokens"]
    assert picker["metered"] == pytest.approx(0.25)       # the voice cost
    assert picker["subscription_equiv"] == pytest.approx(9.0)


def test_selected_span_drives_window_previous_and_series(tmp_path):
    """The chosen timeframe drives everything: the breakdown, the trend,
    and a same-length PREVIOUS period so 'up or down' has an honest baseline.

    Spend is planted 3 days ago and 10 days ago. Over 7 days only the first is
    in-window and the second lands in the comparison period, which is exactly
    the arithmetic a direction claim rests on."""
    from fastapi.testclient import TestClient
    from backend.app import create_app
    settings = Settings(data_dir=str(tmp_path / "data"),
                        memory_url="http://127.0.0.1:1")
    with TestClient(create_app(settings), base_url="http://127.0.0.1") as client:
        chat = client.post("/api/chats", json={}).json()
        con = db.connect()
        for cost, days_ago in ((5.0, 3), (9.0, 10)):
            con.execute(
                "INSERT INTO messages(chat_id, speaker, content, usage_json, created_at) "
                "VALUES(?,?,?,?,?)",
                (chat["id"], "claude", "x",
                 json.dumps({"input": 1, "output": 1, "cost": cost,
                             "model": "claude-opus-4-8"}),
                 time.time() - days_ago * 86400))
        con.commit()
        con.close()

        d = client.get("/api/usage/summary?window=7d").json()
        assert d["span_days"] == 7
        assert round(d["breakdown"]["totals"]["metered"], 2) == 5.0   # in window
        assert round(d["previous"]["totals"]["metered"], 2) == 9.0    # the 7 days before
        # the trend covers the SELECTED span, not a fixed 30 days
        assert len(d["series"]) <= 7

        # a longer span pulls both in, and its comparison period is empty
        d30 = client.get("/api/usage/summary?window=30d").json()
        assert d30["span_days"] == 30
        assert round(d30["breakdown"]["totals"]["metered"], 2) == 14.0
        assert round(d30["previous"]["totals"]["metered"], 2) == 0.0

        # all-time has no "before", so it must offer no comparison at all
        allt = client.get("/api/usage/summary?window=all").json()
        assert allt["span_days"] is None
        assert allt["previous"] is None

        # an unknown span falls back rather than 500ing
        assert client.get("/api/usage/summary?window=nonsense").status_code == 200


# ---------- cache health ----------
#
# A cache regression can sit in plain sight for days: the tokens are already in
# usage_json, but nothing aggregates them. A cache WRITE bills at 1.25x input and a
# READ at 0.1x, so a write is 12.5x a read - and neither the dollar total nor
# the token total moves much when a prefix starts being re-written instead of
# re-read. The ratio is the signal.


def test_cache_health_verdicts():
    ch = accounting.cache_health
    assert ch(30, 10)["verdict"] == "good"          # 3:1
    assert ch(20, 10)["verdict"] == "watch"         # 2:1
    # the failure signature: re-writing more than it ever reads back
    poor = ch(840_000, 1_000_000)
    assert poor["verdict"] == "poor"
    assert poor["ratio"] == pytest.approx(0.84, abs=0.01)


def test_no_cache_activity_is_never_a_misleading_zero_ratio():
    """`0:0` must not render as a problem, and reads-with-no-writes is a
    distinct, fine state (the OpenAI seat, whose writes we don't capture)."""
    assert accounting.cache_health(0, 0) == {"ratio": None, "verdict": "none"}
    assert accounting.cache_health(500, 0) == {"ratio": None, "verdict": "no_writes"}


def test_cache_tokens_ride_every_axis_and_reconcile(con):
    _msg(con, "claude", _base_usage(0.5, model="claude-sonnet-5",
                                    cache_read=1000, cache_creation=4000), NOW)
    s = accounting.summarize(list(accounting.iter_cost_events(con)))
    assert s["cache"]["read"] == 1000
    assert s["cache"]["written"] == 4000
    assert s["cache"]["verdict"] == "poor"
    for axis in ("by_model", "by_producer_model", "by_source", "by_chat", "by_party"):
        rows = [g for g in s[axis] if g["cache_creation"]]
        assert rows, axis
        assert sum(g["cache_read"] for g in rows) == 1000, axis
        assert sum(g["cache_creation"] for g in rows) == 4000, axis
    # cache tokens are a SUBSET of the row's token total, never additional to it
    row = next(g for g in s["by_model"] if g["key"] == "claude-sonnet-5")
    assert row["cache_read"] + row["cache_creation"] <= row["tokens"]


def test_cache_write_cost_is_derived_only_from_a_rate_card(con):
    """1M cache writes on claude-sonnet-5 = 1M * $2 input * 1.25 = $2.50."""
    _msg(con, "claude", _base_usage(2.50, model="claude-sonnet-5",
                                    input=0, output=0, cache_creation=1_000_000), NOW)
    s = accounting.summarize(list(accounting.iter_cost_events(con)))
    assert s["cache"]["write_cost"] == pytest.approx(2.50)
    assert s["cache"]["write_cost_share"] == pytest.approx(1.0)


def test_a_subscription_equivalent_turn_never_gets_a_derived_dollar_split(con):
    """A Claude Code guest's cost is the SDK's own opaque total_cost_usd,
    bundling thinking and tool loops. Its cache TOKENS are reported; splitting
    its dollars would be inventing a breakdown the provider never gave us."""
    _msg(con, "claude-code",
         {"input": 10, "output": 20, "cache_read": 900_000, "cache_creation": 100_000,
          "cost": 7.0, "auth": "subscription", "verified_model": "claude-opus-4-8"}, NOW)
    s = accounting.summarize(list(accounting.iter_cost_events(con)))
    assert s["cache"]["written"] == 100_000          # tokens: reported
    assert s["cache"]["write_cost"] == 0.0           # dollars: never fabricated
    row = next(g for g in s["by_producer_model"] if "claude-opus-4-8" in g["label"])
    assert row["cache"]["verdict"] == "good"         # 9:1, and honestly so
    assert row["cache_write_cost"] == 0.0


def test_an_unpriced_model_reports_cache_tokens_but_no_write_cost(con):
    """Fail-closed pricing extends here: no card, no derived dollars."""
    _msg(con, "gpt", {"input": 1, "output": 1, "cache_read": 10, "cache_creation": 500,
                      "model": "gpt-5.6-terra-pro"}, NOW)
    s = accounting.summarize(list(accounting.iter_cost_events(con)))
    assert s["cache"]["written"] == 500
    assert s["cache"]["write_cost"] == 0.0


def test_the_chat_header_reads_the_same_total_as_the_export_picker(tmp_path):
    """The defect this closes (#231). The header derived its own total in the
    browser by summing usage_json off message rows, which can only ever see
    messages. Voice spend reached it separately, outside the billed figure,
    and utility spend could not reach it at all, because those calls never go
    through db.insert_message. Three sources, two of them missing, one
    formatter: two different dollar figures for one chat."""
    from fastapi.testclient import TestClient
    from backend.app import create_app
    settings = Settings(data_dir=str(tmp_path / "data"),
                        memory_url="http://127.0.0.1:1")
    with TestClient(create_app(settings), base_url="http://127.0.0.1") as client:
        chat = client.post("/api/chats", json={}).json()
        cid = chat["id"]
        con = db.connect()
        con.execute(
            "INSERT INTO messages(chat_id, speaker, content, usage_json, created_at) "
            "VALUES(?,?,?,?,?)",
            (cid, "claude", "x", json.dumps({"input": 10, "output": 20,
                                             "cost": 2.0}), time.time()))
        con.execute(
            "INSERT INTO voice_usage(chat_id, kind, units, cost, created_at) "
            "VALUES(?,?,?,?,?)", (cid, "tts", 400, 0.25, time.time()))
        db.log_utility_usage(con, cid, "title", "claude-haiku-4-5", 100, 10,
                             0.5, provenance=provenance.RATE_CARD_ESTIMATE)
        con.commit()
        con.close()

        picker = client.get("/api/usage/chats").json()[str(cid)]
        header = client.get(f"/api/chats/{cid}").json()["chat"]["cost"]

    for cat in accounting.CATEGORIES:
        assert header[cat] == pytest.approx(picker[cat]), cat
    assert header["tokens"] == picker["tokens"]
    assert header["not_tracked"] == picker["not_tracked"]
    # All three sources are in it, which is the whole point.
    assert header["metered"] == pytest.approx(2.0 + 0.25 + 0.5)


def test_a_chat_whose_only_spend_is_utility_still_reports_it(tmp_path):
    """The headline case. Today that chat's header shows nothing at all while
    the picker shows the cost."""
    from fastapi.testclient import TestClient
    from backend.app import create_app
    settings = Settings(data_dir=str(tmp_path / "data"),
                        memory_url="http://127.0.0.1:1")
    with TestClient(create_app(settings), base_url="http://127.0.0.1") as client:
        chat = client.post("/api/chats", json={}).json()
        con = db.connect()
        db.log_utility_usage(con, chat["id"], "title", "claude-haiku-4-5",
                             100, 10, 0.5,
                             provenance=provenance.RATE_CARD_ESTIMATE)
        con.commit()
        con.close()
        header = client.get(f"/api/chats/{chat['id']}").json()["chat"]["cost"]

    assert header["metered"] == pytest.approx(0.5)
    assert header["tokens"] == 110


def test_voice_cost_is_counted_once_not_twice(tmp_path):
    """Voice now sits inside the billed figure. chat['voice'] keeps carrying
    the chars and seconds for the readout beside it, so the header must not
    print the same dollars in both places. headerView.usageSummary drops the
    voice cost clause for exactly this reason."""
    from fastapi.testclient import TestClient
    from backend.app import create_app
    settings = Settings(data_dir=str(tmp_path / "data"),
                        memory_url="http://127.0.0.1:1")
    with TestClient(create_app(settings), base_url="http://127.0.0.1") as client:
        chat = client.post("/api/chats", json={}).json()
        con = db.connect()
        con.execute(
            "INSERT INTO voice_usage(chat_id, kind, units, cost, created_at) "
            "VALUES(?,?,?,?,?)", (chat["id"], "tts", 400, 0.25, time.time()))
        con.commit()
        con.close()
        payload = client.get(f"/api/chats/{chat['id']}").json()["chat"]

    assert payload["cost"]["metered"] == pytest.approx(0.25)
    assert payload["voice"]["cost"] == pytest.approx(0.25)
    assert payload["voice"]["tts_chars"] == 400


def test_a_patched_chat_keeps_its_cost_block(tmp_path):
    """PATCH returns the same payload, so a capability toggle must not blank
    the header's total."""
    from fastapi.testclient import TestClient
    from backend.app import create_app
    settings = Settings(data_dir=str(tmp_path / "data"),
                        memory_url="http://127.0.0.1:1")
    with TestClient(create_app(settings), base_url="http://127.0.0.1") as client:
        chat = client.post("/api/chats", json={}).json()
        assert "cost" in chat
        patched = client.patch(f"/api/chats/{chat['id']}",
                               json={"title": "renamed"}).json()
    assert "cost" in patched
    assert set(patched["cost"]) >= set(accounting.CATEGORIES)
