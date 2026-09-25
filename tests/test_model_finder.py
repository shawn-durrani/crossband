"""The finder behind a stronger model for one chat (#254, slice 2).

The owner's decision of 25 September: research the stronger model live, at
the moment someone asks. Pinned here, all keyless, with the model list, the
search and the utility model mocked:

1. Discovery is the seat's own provider and key, once per provider, and a
   custom endpoint, a keyless seat or a seat already stepped up is left
   alone without a word.
2. Eligibility: both models priced (an unpriced one is never offered), the
   stated window holds the chat, images and PDFs need a stated yes, the
   trusted system-turn channel is kept, a raised depth lands on a model
   that takes an effort.
3. Ranking comes from the search: an id no result names is dropped, price
   orders nothing, and a current model the results don't mention counts
   below anything ranked. A fit model with no price that ranks higher is
   named, never chosen.
4. Every failure is a "stays on" with its reason: no search engine, no
   model list, an unpriced current model, nothing that fits, a search that
   failed or didn't rank.
5. The lines: the per-turn estimate from the seat's own turns, the card
   prices when it has none, the one-off re-cache on a long chat, and the
   way back. Shared reasons collapse into one line.

Names are the synthetic roster (Alex)."""

import asyncio
import datetime
import json

import pytest
from fastapi.testclient import TestClient

from backend import db, model_step, providers, tools
from backend.app import create_app
from backend.config import DEFAULT_PRICING, Settings

TODAY = datetime.date(2026, 9, 25)

CAPS = {"image_input": {"supported": True}, "pdf_input": {"supported": True},
        "effort": {"supported": True}}


def _m(mid, label, window=1_000_000, caps=None):
    return {"id": mid, "label": label, "max_input_tokens": window,
            "max_tokens": 128_000, "capabilities": caps or CAPS}


MODELS = [
    _m("claude-opus-6", "Claude Opus 6"),          # not on the price card
    _m("claude-fable-5", "Claude Fable 5"),
    _m("claude-opus-5", "Claude Opus 5"),
    _m("claude-sonnet-5", "Claude Sonnet 5"),
    _m("claude-haiku-4-5", "Claude Haiku 4.5", window=200_000,
       caps={**CAPS, "effort": {"supported": False}}),
]

RESULTS = [
    {"title": "Best AI models, September 2026", "url": "https://www.example.org/best",
     "content": "Claude Opus 5 leads the agentic index, Claude Opus 6 is "
                "new, and Sonnet 5 trails both."},
    {"title": "Price check", "url": "https://prices.example.net/a",
     "content": "Fable 5 is the most expensive model on the market."},
]

SONNET_SEAT = {"slug": "claude", "name": "Claude", "provider": "anthropic",
               "model": "claude-sonnet-5", "base_url": None,
               "api_key_env": None, "reasoning_effort": ""}


@pytest.fixture
def app(tmp_path):
    settings = Settings(data_dir=str(tmp_path / "data"),
                        memory_url="http://127.0.0.1:1", user_name="Alex")
    return create_app(settings)


@pytest.fixture
def world(monkeypatch):
    """The outside world, mocked: a key, a search engine, the model list,
    the search results and the utility model's ranking. Counts every call."""
    calls = {"list": 0, "search": 0, "utility": [], "queries": []}
    state = {"models": list(MODELS), "results": list(RESULTS),
             "ranking": ["claude-opus-5", "claude-fable-5", "claude-sonnet-5"],
             "backends": {"tavily": True, "brave": False}}
    monkeypatch.setenv("ANTHROPIC_API_KEY", "test-key")

    def list_models(provider, api_key_env=None, base_url=None):
        calls["list"] += 1
        if isinstance(state["models"], Exception):
            raise state["models"]
        return state["models"]

    def search_results(query, cfg, *, recency=None):
        calls["search"] += 1
        calls["queries"].append((query, recency))
        if isinstance(state["results"], Exception):
            raise state["results"]
        return state["results"]

    async def utility(chat_id, kind, prompt, cfg, max_tokens=2000):
        calls["utility"].append((kind, prompt))
        if isinstance(state["ranking"], str):
            return state["ranking"]
        return json.dumps({"ranking": state["ranking"]})

    monkeypatch.setattr(model_step.providers, "list_models", list_models)
    monkeypatch.setattr(model_step.tools, "search_results", search_results)
    monkeypatch.setattr(model_step.tools, "available_backends",
                        lambda: state["backends"])
    monkeypatch.setattr(model_step.llm_util, "utility_complete_logged", utility)
    return calls, state


def _cfg(**over):
    return {**Settings().as_cfg(), "user_name": "Alex", **over}


def _find(chat_id, seats, cfg=None):
    return asyncio.run(model_step.find_step_ups(chat_id, seats, cfg or _cfg(),
                                                today=TODAY))


def _chat(c):
    return c.post("/api/chats", json={}).json()["id"]


def _one(decisions):
    assert len(decisions) == 1
    return decisions[0]


# ---------- the pick ----------

def test_steps_up_to_the_strongest_priced_model_the_search_ranks(app, world):
    calls, _ = world
    with TestClient(app, base_url="http://127.0.0.1") as c:
        d = _one(_find(_chat(c), [SONNET_SEAT]))
    assert d["outcome"] == "step"
    assert d["model"] == "claude-opus-5" and d["label"] == "Claude Opus 5"
    assert d["from"] == "claude-sonnet-5" and d["from_label"] == "Claude Sonnet 5"
    assert d["source"] == "example.org"
    assert calls["list"] == 1 and calls["search"] == 1
    kind, prompt = calls["utility"][0]
    assert kind == "model_step_up"
    # the search is the app's own query, for this month, with nothing fetched
    query, recency = calls["queries"][0]
    assert "Anthropic Claude" in query and "September 2026" in query
    assert recency == "month"
    # the utility model sees untrusted evidence and a closed list of ids
    assert "untrusted text from the web" in prompt
    assert "- claude-opus-5: Claude Opus 5" in prompt
    assert "claude-haiku-4-5" in prompt  # depth not raised: it's in the pool


def test_an_unpriced_model_that_ranks_higher_is_named_never_chosen(app, world):
    _, state = world
    state["ranking"] = ["claude-opus-6", "claude-opus-5", "claude-sonnet-5"]
    with TestClient(app, base_url="http://127.0.0.1") as c:
        d = _one(_find(_chat(c), [SONNET_SEAT]))
    assert d["outcome"] == "step" and d["model"] == "claude-opus-5"
    assert d["unpriced"] == ["Claude Opus 6"]
    assert "Claude Opus 6 ranked higher, but the price card has no rate for it." \
        in model_step.step_line(d)


def test_the_unpriced_top_alone_is_a_stay_that_names_it(app, world):
    _, state = world
    state["ranking"] = ["claude-opus-6", "claude-sonnet-5", "claude-opus-5"]
    with TestClient(app, base_url="http://127.0.0.1") as c:
        d = _one(_find(_chat(c), [SONNET_SEAT]))
    assert d["outcome"] == "stay" and d["key"] == "unpriced_top"
    assert model_step.stay_lines([d]) == [
        "Claude stays on Claude Sonnet 5: a web search ranked Claude Opus 6 "
        "higher (example.org), but the price card has no rate for it."]


def test_a_model_no_result_names_is_dropped_from_the_ranking(app, world):
    """The utility model put Opus 4.8 first, but nothing in the results
    mentions it: that order came from somewhere other than the search."""
    _, state = world
    state["models"] = MODELS + [_m("claude-opus-4-8", "Claude Opus 4.8")]
    state["ranking"] = ["claude-opus-4-8", "claude-opus-5", "claude-sonnet-5"]
    with TestClient(app, base_url="http://127.0.0.1") as c:
        d = _one(_find(_chat(c), [SONNET_SEAT]))
    assert d["model"] == "claude-opus-5"


def test_already_the_strongest_is_an_honest_stay(app, world):
    _, state = world
    state["ranking"] = ["claude-sonnet-5", "claude-opus-5"]
    with TestClient(app, base_url="http://127.0.0.1") as c:
        d = _one(_find(_chat(c), [SONNET_SEAT]))
    assert d["outcome"] == "stay" and d["key"] == "strongest"
    assert model_step.stay_lines([d]) == [
        "Claude stays on Claude Sonnet 5: a web search ranked it the strongest "
        "Claude model the app can use here (example.org)."]


def test_a_current_model_the_results_never_mention_ranks_below_them(app, world):
    _, state = world
    state["results"] = [{"title": "Top models", "url": "https://example.org/t",
                         "content": "Opus 5 is the pick for agents."}]
    state["ranking"] = ["claude-opus-5", "claude-sonnet-5"]
    with TestClient(app, base_url="http://127.0.0.1") as c:
        d = _one(_find(_chat(c), [SONNET_SEAT]))
    assert d["outcome"] == "step" and d["model"] == "claude-opus-5"


# ---------- eligibility ----------

def test_the_trusted_channel_is_never_given_up(app, world):
    """A seat on a model with the unforgeable system turn never moves to one
    without it, however the search ranks it."""
    _, state = world
    state["models"] = MODELS + [_m("claude-opus-4-8", "Claude Opus 4.8")]
    state["results"] = RESULTS + [{"title": "x", "url": "https://example.com/y",
                                   "content": "Claude Opus 4.8 is older."}]
    state["ranking"] = ["claude-sonnet-5", "claude-opus-5", "claude-opus-4-8"]
    seat = {**SONNET_SEAT, "model": "claude-opus-4-8"}
    with TestClient(app, base_url="http://127.0.0.1") as c:
        d = _one(_find(_chat(c), [seat]))
    assert d["model"] == "claude-opus-5"


def test_a_raised_depth_needs_a_model_that_takes_an_effort(app, world):
    calls, state = world
    state["ranking"] = ["claude-haiku-4-5", "claude-opus-5"]
    with TestClient(app, base_url="http://127.0.0.1") as c:
        chat_id = _chat(c)
        con = db.connect()
        try:
            db.set_chat_seat_depth(con, chat_id, "claude", "high", set_by="Alex")
        finally:
            con.close()
        d = _one(_find(chat_id, [SONNET_SEAT]))
    assert d["model"] == "claude-opus-5"
    assert "claude-haiku-4-5" not in calls["utility"][0][1]


@pytest.mark.parametrize("image_input,key,reason", [
    (None, "unknown_media",
     "this chat carries images or PDF files, and Anthropic doesn't say which "
     "of its models take them"),
    ({"supported": False}, "none_fit",
     "no other Claude model the app can price fits this chat"),
])
def test_images_need_a_stated_yes(app, world, image_input, key, reason):
    """A model the provider says nothing about is a no for a chat with an
    image, and the line names that (slice 3) apart from a stated no."""
    _, state = world
    caps = {k: v for k, v in CAPS.items() if k != "image_input"}
    if image_input is not None:
        caps["image_input"] = image_input
    state["models"] = [_m("claude-sonnet-5", "Claude Sonnet 5"),
                       _m("claude-opus-5", "Claude Opus 5", caps=caps)]
    with TestClient(app, base_url="http://127.0.0.1") as c:
        chat_id = _chat(c)
        con = db.connect()
        try:
            msg = db.insert_message(con, chat_id, "user", "look at this")
            con.execute(
                "INSERT INTO attachments(message_id, filename, stored_name, mime, "
                "size, created_at) VALUES(?,?,?,?,?,?)",
                (msg["id"], "a.png", "a.png", "image/png", 1000, 0))
            con.commit()
        finally:
            con.close()
        d = _one(_find(chat_id, [SONNET_SEAT]))
    assert d["outcome"] == "stay" and d["key"] == key
    assert model_step.stay_lines([d]) == [
        f"Claude stays on Claude Sonnet 5: {reason}."]


def test_a_window_too_small_for_the_chat_is_out(app, world):
    _, state = world
    state["models"] = [_m("claude-sonnet-5", "Claude Sonnet 5"),
                       _m("claude-opus-5", "Claude Opus 5", window=1000)]
    with TestClient(app, base_url="http://127.0.0.1") as c:
        d = _one(_find(_chat(c), [SONNET_SEAT]))
    assert d["key"] == "none_fit"


def test_unfit_reads_unknown_as_no_only_where_the_chat_needs_it():
    facts = {"prompt_tokens": 5000, "images": False, "pdfs": False,
             "depth": {}, "steps": {}, "usage": {}}
    bare = {"id": "claude-opus-5", "label": "Claude Opus 5",
            "max_input_tokens": None, "capabilities": None}
    assert model_step.unfit(SONNET_SEAT, bare, facts) is None
    assert model_step.unfit(SONNET_SEAT, bare, {**facts, "pdfs": True}) == "pdfs"
    assert model_step.unfit(SONNET_SEAT, {**bare, "id": "claude-sonnet-5"},
                            facts) == "current"


# ---------- failures say why ----------

def test_no_search_engine_means_no_switch_and_no_model_list_call(
        app, world, monkeypatch):
    calls, state = world
    state["backends"] = {"tavily": False, "brave": False}
    gpt = {**SONNET_SEAT, "slug": "gpt", "name": "GPT", "provider": "openai",
           "model": "gpt-5.1"}
    monkeypatch.setenv("OPENAI_API_KEY", "test-key")
    with TestClient(app, base_url="http://127.0.0.1") as c:
        ds = _find(_chat(c), [SONNET_SEAT, gpt])
    assert [d["key"] for d in ds] == ["no_search", "no_search"]
    assert calls["list"] == 0 and calls["search"] == 0
    assert model_step.stay_lines(ds) == [
        "Claude and GPT stay on their models: no web search engine is set up, "
        "and the app only moves a seat to a model a search ranks higher."]


@pytest.mark.parametrize("setup,key", [
    ({"models": RuntimeError("down")}, "no_list"),
    ({"results": []}, "unranked"),
    ({"results": ValueError("engine fell over")}, "search_failed"),
    ({"ranking": "not json at all"}, "unranked"),
    ({"ranking": ["claude-made-up-9"]}, "unranked"),
])
def test_every_failure_is_a_stay_with_its_reason(app, world, setup, key):
    _, state = world
    state.update(setup)
    with TestClient(app, base_url="http://127.0.0.1") as c:
        d = _one(_find(_chat(c), [SONNET_SEAT]))
    assert d["outcome"] == "stay" and d["key"] == key
    (line,) = model_step.stay_lines([d])
    assert line.startswith("Claude stays on ")


def test_an_unpriced_current_model_cannot_be_compared(app, world):
    calls, _ = world
    seat = {**SONNET_SEAT, "model": "claude-opus-6"}
    with TestClient(app, base_url="http://127.0.0.1") as c:
        d = _one(_find(_chat(c), [seat]))
    assert d["key"] == "unpriced_current"
    assert calls["search"] == 0
    assert model_step.stay_lines([d]) == [
        "Claude stays on Claude Opus 6: the price card has no rate for it, "
        "so the app can't say what a stronger model would cost."]


def test_seats_left_alone_without_a_word(app, world, monkeypatch):
    calls, _ = world
    local = {**SONNET_SEAT, "slug": "qwen", "name": "Qwen", "provider": "openai",
             "model": "qwen3", "base_url": "http://127.0.0.1:8080/v1"}
    keyless = {**SONNET_SEAT, "slug": "gpt", "name": "GPT", "provider": "openai",
               "model": "gpt-5.1"}
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    with TestClient(app, base_url="http://127.0.0.1") as c:
        chat_id = _chat(c)
        con = db.connect()
        try:
            db.set_chat_seat_model(con, chat_id, "claude", "claude-opus-5",
                                   model_from="claude-sonnet-5")
        finally:
            con.close()
        ds = _find(chat_id, [SONNET_SEAT, local, keyless])
    assert [(d["slug"], d["outcome"], d["key"]) for d in ds] == [
        ("claude", "skip", "already"), ("qwen", "skip", "endpoint"),
        ("gpt", "skip", "no_key")]
    assert calls["list"] == 0
    assert model_step.stay_lines(ds) == []


def test_discovery_and_research_run_once_per_provider(app, world):
    calls, _ = world
    second = {**SONNET_SEAT, "slug": "claude2", "name": "Mateo"}
    with TestClient(app, base_url="http://127.0.0.1") as c:
        ds = _find(_chat(c), [SONNET_SEAT, second])
    assert [d["model"] for d in ds] == ["claude-opus-5", "claude-opus-5"]
    assert calls["list"] == 1 and calls["search"] == 1
    assert len(calls["utility"]) == 1


# ---------- the lines ----------

def _usage_turn(chat_id, slug, usage):
    con = db.connect()
    try:
        db.insert_message(con, chat_id, slug, "a reply",
                          usage_json=json.dumps(usage))
    finally:
        con.close()


def test_the_step_line_prices_a_turn_like_the_seats_recent_ones(app, world):
    with TestClient(app, base_url="http://127.0.0.1") as c:
        chat_id = _chat(c)
        for _ in range(3):
            _usage_turn(chat_id, "claude", {"model": "claude-sonnet-5",
                                            "input": 6000, "cache_read": 0,
                                            "cache_creation": 0, "output": 1200})
        d = _one(_find(chat_id, [SONNET_SEAT]))
    # Sonnet 5 is $2/$10, Opus 5 is $5/$25: 6000 in and 1200 out per turn
    assert d["cost"]["turns"] == 3
    assert d["cost"]["now"] == pytest.approx(0.024)
    assert d["cost"]["new"] == pytest.approx(0.06)
    assert model_step.step_line(d, "Alex") == (
        "Claude moves from Claude Sonnet 5 to Claude Opus 5 for this chat, set "
        "by Alex. A web search ranked it the strongest Claude model the app "
        "can use here (example.org). A turn like Claude's last few here is "
        "about $0.02 now and about $0.06 on Claude Opus 5, rate-card "
        "estimates. Back to normal returns Claude to Claude Sonnet 5.")


def test_with_no_turns_yet_the_line_gives_the_card_prices(app, world):
    with TestClient(app, base_url="http://127.0.0.1") as c:
        d = _one(_find(_chat(c), [SONNET_SEAT]))
    line = model_step.step_line(d, "")
    assert "for this chat. A web search" in line  # nobody named, no "set by"
    assert ("Claude Opus 5 is $5 in and $25 out per million tokens, against "
            "$2 and $10 for Claude Sonnet 5, rate-card prices.") in line


def test_a_long_chat_adds_the_one_off_recache_both_ways(app, world, monkeypatch):
    monkeypatch.setattr(model_step, "LONG_CHAT_TOKENS", 1000)
    with TestClient(app, base_url="http://127.0.0.1") as c:
        chat_id = _chat(c)
        con = db.connect()
        try:
            db.insert_message(con, chat_id, "user", "x" * 40_000)
        finally:
            con.close()
        d = _one(_find(chat_id, [SONNET_SEAT]))
    assert d["cost"]["recache_up"] > d["cost"]["recache_back"] > 0
    assert "This chat is long, so the first reply there re-caches its " \
           "history" in model_step.step_line(d)


# ---------- the pieces ----------

def test_names_model_keeps_versions_apart():
    opus5 = {"id": "claude-opus-5", "label": "Claude Opus 5"}
    assert model_step.names_model("Claude Opus 5 leads", opus5)
    assert model_step.names_model("Opus 5, then Fable", opus5)
    assert model_step.names_model("the claude-opus-5 id", opus5)
    assert not model_step.names_model("Claude Opus 5.5 is new", opus5)
    assert not model_step.names_model("Claude Opus 50", opus5)
    gpt = {"id": "gpt-5.6-terra", "label": "gpt-5.6-terra"}
    assert model_step.names_model("GPT-5.6 Terra beats the rest", gpt)
    assert not model_step.names_model("GPT-5.6 Sol beats the rest", gpt)


def test_capability_reads_both_shapes():
    assert model_step.capability({"capabilities": CAPS}, "image_input") is True
    assert model_step.capability(
        {"capabilities": {"effort": {"supported": False}}}, "effort") is False
    assert model_step.capability({"capabilities": {}}, "effort") is None
    assert model_step.capability({"capabilities": None}, "pdf_input") is None
    assert model_step.capability({"capabilities": ["vision"]}, "image_input") is True
    assert model_step.capability({"capabilities": ["vision"]}, "pdf_input") is False


def test_parse_ranking_keeps_listed_ids_once_in_order():
    allowed = {"a", "b", "c"}
    assert model_step.parse_ranking('{"ranking": ["b", "x", "a", "b"]}',
                                    allowed) == ["b", "a"]
    assert model_step.parse_ranking("nothing", allowed) == []
    assert model_step.parse_ranking('{"ranking": "a"}', allowed) == []
    assert model_step.parse_ranking(None, allowed) == []


def test_priced_fails_closed():
    assert model_step.priced("claude-opus-5", DEFAULT_PRICING)
    assert not model_step.priced("claude-opus-6", DEFAULT_PRICING)
    # a point release is a new model, never a dated reissue of the old one:
    # unpriced until it has a row of its own, as Opus 5.5 now does
    assert not model_step.priced("claude-sonnet-5-1", DEFAULT_PRICING)
    assert model_step.priced("claude-opus-5-5", DEFAULT_PRICING)
    assert not model_step.priced("gpt-oss:20b", DEFAULT_PRICING)  # self-hosted $0


def test_sends_effort_follows_the_request_path():
    assert providers.sends_effort("anthropic", "claude-opus-5", "high")
    assert not providers.sends_effort("anthropic", "claude-haiku-4-5", "high")
    assert providers.sends_effort("openai", "gpt-5.1", "high")
    assert not providers.sends_effort("openai", "gpt-4o", "high")


# ---------- tools.search_results ----------

def test_search_results_merges_engines_and_skips_a_broken_one(monkeypatch):
    monkeypatch.setattr(tools, "available_backends",
                        lambda: {"tavily": True, "brave": True})
    seen = {}

    def tavily(query, args, cfg):
        seen["args"] = args
        return [{"title": "A", "url": "https://a.example/1", "content": "x"},
                {"title": "B", "url": "https://b.example/2", "content": "y"}]

    def brave(query, args, cfg):
        raise RuntimeError("quota")

    monkeypatch.setattr(tools, "_tavily", tavily)
    monkeypatch.setattr(tools, "_brave", brave)
    out = tools.search_results("q", {}, recency="month")
    assert [x["url"] for x in out] == ["https://a.example/1", "https://b.example/2"]
    assert seen["args"] == {"recency": "month"}

    monkeypatch.setattr(tools, "_brave", lambda q, a, c: [
        {"title": "A again", "url": "https://a.example/1", "content": "z"}])
    assert len(tools.search_results("q", {})) == 2  # de-duplicated by URL


def test_search_results_with_no_engine_raises(monkeypatch):
    monkeypatch.setattr(tools, "available_backends",
                        lambda: {"tavily": False, "brave": False})
    with pytest.raises(RuntimeError):
        tools.search_results("q", {})
