"""The ElevenLabs voice model rules (#480): the list, the Automatic rule,
validation, the fallback list and refusals.

The owner asked to choose the voice model in the app, "ideally v3, and v4
when it comes out". These tests pin what makes that safe:

1. The list keeps models that can speak and drops speech-to-speech,
   alpha-only and malformed entries.
2. Automatic picks the newest version, and within it the variant built for
   live talk. A synthetic eleven_v4 appearing on the list becomes the pick,
   with no code change. Deprecated and refused models are never picked.
3. Only "auto" or an id on the current list is accepted, so an arbitrary
   string never reaches ElevenLabs.
4. The pinned list stands in whenever the live one can't be fetched, and a
   failed refresh keeps the last good list.
5. A handshake refusal is told apart from a bad key or a rate limit, and is
   remembered for a day.

Keyless: every fetch is a fake. The fixture is shaped like a real
GET /v1/models response, trimmed to the fields the rules read."""

import pytest
import websockets
from websockets.datastructures import Headers
from websockets.http11 import Response

from backend import tts_models as tm


def _model(mid, name=None, tts=True, alpha=False, langs=3):
    return {"model_id": mid, "name": name or mid, "can_do_text_to_speech": tts,
            "requires_alpha_access": alpha,
            "languages": [{"language_id": f"l{i}"} for i in range(langs)],
            "model_rates": {"character_cost_multiplier": 1.0}}


LIVE = [
    _model("eleven_v3", "Eleven v3"),
    _model("eleven_v3_conversational", "Eleven v3 Conversational"),
    _model("eleven_multilingual_v2", "Eleven Multilingual v2"),
    _model("eleven_flash_v2_5", "Eleven Flash v2.5"),
    _model("eleven_turbo_v2_5", "Eleven Turbo v2.5"),
    _model("eleven_turbo_v2", "Eleven Turbo v2"),
    _model("eleven_flash_v2", "Eleven Flash v2"),
    _model("eleven_english_sts_v2", "Eleven English v2", tts=False),
    _model("eleven_multilingual_sts_v2", "Eleven Multilingual v2", tts=False),
]


def _parsed(extra=()):
    return tm.parse_models(LIVE + list(extra))


# ---------- 1. the list ----------

def test_the_list_keeps_only_models_that_can_speak():
    ids = [m["id"] for m in _parsed()]
    assert "eleven_english_sts_v2" not in ids
    assert "eleven_multilingual_sts_v2" not in ids
    assert ids[:2] == ["eleven_v3", "eleven_v3_conversational"]  # listed order
    assert _parsed()[0] == {"id": "eleven_v3", "name": "Eleven v3", "languages": 3}


def test_alpha_only_malformed_and_repeated_entries_are_dropped():
    raw = [_model("eleven_v9", alpha=True), _model("Eleven V9"),
           _model("eleven_v9/../../x"), _model("x" * 65),
           {"model_id": 7}, "not a model", None,
           _model("eleven_flash_v2_5"), _model("eleven_flash_v2_5", "again")]
    assert [m["id"] for m in tm.parse_models(raw)] == ["eleven_flash_v2_5"]
    assert tm.parse_models({"detail": "bad key"}) == []
    assert tm.parse_models(None) == []


def test_a_missing_name_falls_back_to_the_id():
    m = _model("eleven_v4")
    m["name"] = "  "
    assert tm.parse_models([m])[0]["name"] == "eleven_v4"


def test_the_pinned_list_is_what_the_parser_would_keep():
    for m in tm.PINNED:
        assert tm.parse_models([_model(m["id"], m["name"])])[0]["id"] == m["id"]
    assert tm.DEFAULT_MODEL in {m["id"] for m in tm.PINNED}


# ---------- 2. the Automatic rule ----------

def test_versions_read_from_the_id():
    assert tm.generation("eleven_v3") == (3, 0)
    assert tm.generation("eleven_v3_conversational") == (3, 0)
    assert tm.generation("eleven_flash_v2_5") == (2, 5)
    assert tm.generation("eleven_multilingual_v2") == (2, 0)
    assert tm.generation("eleven_v4") == (4, 0)
    assert tm.generation("eleven_v10_flash") == (10, 0)
    assert tm.generation("something_else") == (0, 0)


def test_automatic_picks_v3_conversational_today():
    assert tm.automatic_pick(_parsed()) == "eleven_v3_conversational"
    assert tm.automatic_order(_parsed()) == [
        "eleven_v3_conversational", "eleven_v3", "eleven_flash_v2_5",
        "eleven_flash_v2", "eleven_multilingual_v2"]


def test_v3_beats_every_v2_model():
    for mid in ("eleven_flash_v2_5", "eleven_multilingual_v2", "eleven_flash_v2"):
        assert tm.automatic_pick(tm.parse_models([_model(mid), _model("eleven_v3")])) \
            == "eleven_v3"


def test_a_v4_on_the_list_becomes_the_pick_with_no_code_change():
    assert tm.automatic_pick(_parsed([_model("eleven_v4", "Eleven v4")])) == "eleven_v4"


def test_within_a_version_the_live_variant_wins():
    both = _parsed([_model("eleven_v4"), _model("eleven_v4_conversational")])
    assert tm.automatic_pick(both) == "eleven_v4_conversational"
    flash_first = tm.parse_models([_model("eleven_multilingual_v2_5"),
                                   _model("eleven_flash_v2_5"),
                                   _model("eleven_turbo_v2_5")])
    assert tm.automatic_pick(flash_first) == "eleven_flash_v2_5"


def test_deprecated_alpha_and_refused_models_are_never_picked():
    only_old = tm.parse_models([_model("eleven_turbo_v2_5"), _model("eleven_turbo_v2")])
    assert tm.automatic_pick(only_old) == tm.DEFAULT_MODEL
    alpha_v4 = _parsed([_model("eleven_v4", alpha=True)])
    assert tm.automatic_pick(alpha_v4) == "eleven_v3_conversational"
    v4 = _parsed([_model("eleven_v4")])
    assert tm.automatic_pick(v4, refused={"eleven_v4"}) == "eleven_v3_conversational"


def test_an_empty_list_falls_back_to_flash():
    assert tm.automatic_pick([]) == tm.DEFAULT_MODEL


# ---------- routes ----------

def test_v3_ids_ride_the_dialogue_socket_and_the_rest_the_speech_socket():
    assert tm.route_for("eleven_v3") == tm.ROUTE_DIALOGUE
    assert tm.route_for("eleven_v3_conversational") == tm.ROUTE_DIALOGUE
    assert tm.route_for("eleven_flash_v2_5") == tm.ROUTE_TTS
    assert tm.route_for("eleven_multilingual_v2") == tm.ROUTE_TTS
    # nothing is known about a v4's socket, so it tries the general one and
    # a refusal falls back (see the relay tests)
    assert tm.route_for("eleven_v4") == tm.ROUTE_TTS


# ---------- 3. validation and resolution ----------

def test_only_auto_or_a_listed_id_is_a_valid_choice():
    models = _parsed()
    assert tm.valid_choice("auto", models)
    assert tm.valid_choice("eleven_v3", models)
    assert not tm.valid_choice("eleven_v4", models)            # not listed
    assert not tm.valid_choice("eleven_english_sts_v2", models)  # can't speak
    assert not tm.valid_choice("eleven_v3&output_format=x", models)
    assert not tm.valid_choice(None, models)
    assert not tm.valid_choice("", models)
    assert tm.valid_choice("", models, allow_blank=True)


def test_a_seat_choice_wins_over_the_app_setting():
    models = _parsed()
    r = tm.resolve("eleven_flash_v2_5", "eleven_v3_conversational", models=models)
    assert r == {"model": "eleven_v3_conversational", "route": tm.ROUTE_DIALOGUE,
                 "choice": "eleven_v3_conversational", "automatic": False}
    assert tm.resolve("auto", "", models=models)["model"] == "eleven_v3_conversational"
    assert tm.resolve("eleven_v3", "auto", models=models)["automatic"] is True


def test_an_unknown_or_refused_choice_speaks_with_flash():
    models = _parsed()
    assert tm.resolve("eleven_v9_typo", models=models)["model"] == tm.DEFAULT_MODEL
    assert tm.resolve("../../etc", models=models)["model"] == tm.DEFAULT_MODEL
    assert tm.resolve(None, models=models)["model"] == tm.DEFAULT_MODEL
    assert tm.resolve("eleven_v3", models=models,
                      refused={"eleven_v3"})["model"] == tm.DEFAULT_MODEL


def test_attempts_fall_down_the_automatic_order_then_to_flash():
    models = _parsed([_model("eleven_v4")])
    auto = tm.resolve("auto", models=models)
    assert tm.attempts(auto, models) == ["eleven_v4", "eleven_v3_conversational",
                                         tm.DEFAULT_MODEL]
    pick = tm.resolve("eleven_multilingual_v2", models=models)
    assert tm.attempts(pick, models) == ["eleven_multilingual_v2", tm.DEFAULT_MODEL]
    flash = tm.resolve("eleven_flash_v2_5", models=models)
    assert tm.attempts(flash, models) == [tm.DEFAULT_MODEL]


# ---------- 4. the cache and the pinned list ----------

class _Fetch:
    def __init__(self, result):
        self.result, self.calls = result, 0

    def __call__(self):
        self.calls += 1
        if isinstance(self.result, Exception):
            raise self.result
        return self.result


def test_no_fetcher_means_the_pinned_list_and_no_network():
    snap = tm.catalogue()
    assert snap["source"] == "pinned" and snap["fetched_at"] is None
    assert [m["id"] for m in snap["models"]] == [m["id"] for m in tm.PINNED]


def test_the_live_list_is_fetched_once_an_hour():
    fetch = _Fetch(LIVE)
    assert tm.catalogue(fetch, now=1000)["source"] == "live"
    tm.catalogue(fetch, now=1000 + tm.CACHE_TTL_S - 1)
    assert fetch.calls == 1
    tm.catalogue(fetch, now=1000 + tm.CACHE_TTL_S + 1)
    assert fetch.calls == 2


def test_an_unreachable_api_gives_the_pinned_list_and_waits_to_retry():
    fetch = _Fetch(RuntimeError("connection refused"))
    assert tm.catalogue(fetch, now=1000)["source"] == "pinned"
    tm.catalogue(fetch, now=1000 + tm.RETRY_AFTER_FAILURE_S - 1)
    assert fetch.calls == 1
    tm.catalogue(fetch, now=1000 + tm.RETRY_AFTER_FAILURE_S + 1)
    assert fetch.calls == 2


def test_a_failed_refresh_keeps_the_last_good_live_list():
    tm.catalogue(_Fetch(LIVE + [_model("eleven_v4")]), now=1000)
    later = tm.catalogue(_Fetch(RuntimeError("503")), now=1000 + tm.CACHE_TTL_S + 1)
    assert later["source"] == "live"
    assert "eleven_v4" in {m["id"] for m in later["models"]}
    # an empty answer is a failure too, never an empty picker
    assert tm.refresh(_Fetch([]), now=9e9) is False
    assert tm.catalogue()["models"]


def test_the_live_path_refreshes_in_the_background_only_when_stale(monkeypatch):
    spawned = []
    monkeypatch.setattr(tm, "_spawn_refresh", spawned.append)
    fetch = _Fetch(LIVE)
    tm.refresh_soon(fetch)
    assert spawned == [fetch]
    tm.refresh(fetch)
    tm.refresh_soon(fetch)
    tm.refresh_soon(None)
    assert spawned == [fetch]


# ---------- 5. refusals ----------

def _invalid(status, body):
    return websockets.InvalidStatus(Response(status, "x", Headers(), body))


REFUSED_BODY = (b'{"detail":{"type":"validation_error","code":"unsupported_model",'
                b'"message":"Model \'eleven_v3\' is not supported on the text-to-speech '
                b'websocket endpoint.","status":"unsupported_model","param":"model_id"}}')


def test_a_model_refusal_is_told_apart_from_other_handshake_failures():
    assert tm.is_model_refusal(_invalid(400, REFUSED_BODY))
    assert tm.is_model_refusal(_invalid(400, bytearray(REFUSED_BODY)))
    assert not tm.is_model_refusal(_invalid(401, b'{"detail":"invalid_api_key"}'))
    assert not tm.is_model_refusal(_invalid(429, b"too many"))
    assert not tm.is_model_refusal(_invalid(400, b'{"detail":"voice_not_found"}'))
    assert not tm.is_model_refusal(RuntimeError("network down"))


def test_a_refusal_is_remembered_for_a_day():
    tm.mark_refused("eleven_v4", now=1000)
    assert tm.refused_ids(now=1000 + tm.REFUSAL_MEMORY_S - 1) == {"eleven_v4"}
    assert tm.refused_ids(now=1000 + tm.REFUSAL_MEMORY_S) == set()


# ---------- what the picker is handed ----------

def test_the_picker_lists_newest_first_with_notes_and_old_models_last():
    rows = tm.options(_parsed([_model("eleven_v4", "Eleven v4")]), refused={"eleven_v4"})
    assert [r["value"] for r in rows] == [
        "eleven_v4", "eleven_v3_conversational", "eleven_v3", "eleven_flash_v2_5",
        "eleven_flash_v2", "eleven_multilingual_v2", "eleven_turbo_v2_5",
        "eleven_turbo_v2"]
    assert rows[0]["note"] == tm.NEW_MODEL_NOTE and rows[0]["refused"] is True
    assert rows[1]["route"] == tm.ROUTE_DIALOGUE
    assert rows[-1]["deprecated"] is True
    assert all(r["note"] for r in rows)


def test_describe_names_the_setting_the_pick_and_the_rule():
    out = tm.describe("auto", {"models": _parsed(), "source": "live",
                               "fetched_at": 5.0})
    assert out["setting"] == "auto"
    assert out["speaking"] == "eleven_v3_conversational"
    assert out["automatic_pick_name"] == "Eleven v3 Conversational"
    assert out["automatic_rule"] == tm.AUTOMATIC_RULE
    assert out["source"] == "live" and out["refused"] == []
    assert out["default"] == tm.DEFAULT_MODEL and out["locked_by_env"] is False
    assert tm.describe("", tm.catalogue())["setting"] == tm.DEFAULT_MODEL


@pytest.mark.parametrize("mid", [m["id"] for m in tm.PINNED])
def test_every_pinned_model_has_a_plain_note(mid):
    assert tm.NOTES.get(mid)
