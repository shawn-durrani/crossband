"""Which ElevenLabs model speaks a seat's reply (#480).

The owner picks a model in the app, per app or per seat, or picks
Automatic, which follows the newest model crossband can stream live. This
module owns every rule behind that choice, so the relay, the settings
routes and the benchmark all resolve a model the same way:

- the model list: ElevenLabs' GET /v1/models, filtered to models that can
  speak, cached for an hour, with a pinned copy for when the API can't be
  reached (or there's no key);
- the route: ElevenLabs serves two streaming sockets. The text-to-speech
  socket refuses every id that starts with `eleven_v3` (HTTP 400
  `unsupported_model` at the handshake, checked live 2026-09-26), and the
  text-to-dialogue socket takes only those ids. The model list has no field
  that says which socket a model works on, so the route comes from that
  documented id rule;
- the Automatic rule (AUTOMATIC_RULE below, shown in the UI word for word);
- validation: only "auto" or an id on the current list is ever accepted, so
  a hand-edited or hostile value never reaches the provider;
- refusals: a model ElevenLabs refuses at the handshake is remembered for a
  day, Automatic skips it, and the relay falls back within the same reply.

Pure apart from the cache and the refusal memory, both module globals that
tests/conftest.py resets between tests.
"""

import logging
import re
import threading
import time

log = logging.getLogger("crossband.tts_models")

AUTO = "auto"
# The model crossband has always spoken with, and the last resort of every
# fallback: ElevenLabs' own low-latency recommendation for the socket.
DEFAULT_MODEL = "eleven_flash_v2_5"

ROUTE_TTS = "tts"            # /v1/text-to-speech/{voice_id}/stream-input
ROUTE_DIALOGUE = "dialogue"  # /v1/text-to-dialogue/stream-input
DIALOGUE_PREFIX = "eleven_v3"

# A model id is a short lowercase token. Anything else is refused before it
# can reach a URL.
_ID = re.compile(r"^[a-z0-9_]{1,64}$")
# The version inside an id: eleven_v3 -> (3, 0), eleven_flash_v2_5 -> (2, 5).
_GEN = re.compile(r"_v(\d{1,3})(?:_(\d{1,3}))?(?=_|$)")

# ElevenLabs' models page lists these as deprecated in favour of Flash. They
# still work and stay pickable, sorted last, and Automatic never picks them.
DEPRECATED = frozenset({"eleven_turbo_v2", "eleven_turbo_v2_5",
                        "eleven_monolingual_v1", "eleven_multilingual_v1"})

# Within one version, the variant built for live talk wins.
LIVE_ORDER = ("conversational", "flash", "turbo")

# What GET /v1/models listed for speech on 2026-09-26, used whenever the
# live list can't be fetched. Newest first.
PINNED = (
    {"id": "eleven_v3_conversational", "name": "Eleven v3 Conversational",
     "languages": 74},
    {"id": "eleven_v3", "name": "Eleven v3", "languages": 74},
    {"id": "eleven_flash_v2_5", "name": "Eleven Flash v2.5", "languages": 32},
    {"id": "eleven_multilingual_v2", "name": "Eleven Multilingual v2",
     "languages": 29},
    {"id": "eleven_flash_v2", "name": "Eleven Flash v2", "languages": 1},
    {"id": "eleven_turbo_v2_5", "name": "Eleven Turbo v2.5", "languages": 32},
    {"id": "eleven_turbo_v2", "name": "Eleven Turbo v2", "languages": 1},
)

# One short plain note per known model, from ElevenLabs' models page, its
# API price list and the first-audio times measured through the relay.
NOTES = {
    "eleven_v3_conversational": "most expressive for live talk, half price",
    "eleven_v3": "most expressive, slowest to start, made for recordings",
    "eleven_flash_v2_5": "fastest to start, 32 languages, half price",
    "eleven_multilingual_v2": "lifelike and steady, slower to start",
    "eleven_flash_v2": "fastest to start, English only, half price",
    "eleven_turbo_v2_5": "older fast model, ElevenLabs suggests Flash",
    "eleven_turbo_v2": "older fast model, ElevenLabs suggests Flash",
}
NEW_MODEL_NOTE = "new from ElevenLabs, not timed here yet"

AUTOMATIC_RULE = (
    "Automatic picks the newest model ElevenLabs lists for speech that "
    "Crossband can stream live. Within a version it prefers the one built "
    "for conversation. It skips older models ElevenLabs has replaced and "
    "any model ElevenLabs refused on live voice in the last day.")

CACHE_TTL_S = 3600
RETRY_AFTER_FAILURE_S = 300
REFUSAL_MEMORY_S = 24 * 3600

# The live list and when it was fetched. Empty means "never fetched".
_cache: dict = {}
# model id -> when ElevenLabs refused it on a live socket.
_refused: dict = {}
_refresh_lock = threading.Lock()


# ---------- the model list ----------

def parse_models(raw) -> list:
    """The speech models out of a GET /v1/models response: can speak, not
    alpha-only, a well-formed id, each once. Returns [{id, name, languages}]
    in the order ElevenLabs listed them."""
    out, seen = [], set()
    for m in raw if isinstance(raw, list) else []:
        if not isinstance(m, dict):
            continue
        mid = m.get("model_id")
        if not isinstance(mid, str) or not _ID.match(mid) or mid in seen:
            continue
        if m.get("can_do_text_to_speech") is not True:
            continue
        if m.get("requires_alpha_access") is True:
            continue
        name = m.get("name")
        name = name.strip()[:80] if isinstance(name, str) and name.strip() else mid
        langs = m.get("languages")
        seen.add(mid)
        out.append({"id": mid, "name": name,
                    "languages": len(langs) if isinstance(langs, list) else 0})
    return out


def _snapshot() -> dict:
    live = _cache.get("models")
    if live:
        return {"models": [dict(m) for m in live], "source": "live",
                "fetched_at": _cache.get("fetched_at")}
    return {"models": [dict(m) for m in PINNED], "source": "pinned",
            "fetched_at": None}


def needs_refresh(now=None) -> bool:
    now = time.time() if now is None else now
    if _cache.get("models") and now - _cache.get("fetched_at", 0) < CACHE_TTL_S:
        return False
    failed = _cache.get("failed_at")
    return not (failed and now - failed < RETRY_AFTER_FAILURE_S)


def refresh(fetch, now=None) -> bool:
    """Fetch and parse the live list. A failure or an empty answer keeps
    whatever was cached before (the last good live list, or the pinned one)
    and waits RETRY_AFTER_FAILURE_S before trying again."""
    now = time.time() if now is None else now
    try:
        parsed = parse_models(fetch())
    except Exception as e:
        log.warning("could not list ElevenLabs models: %s", str(e)[:200])
        parsed = []
    if not parsed:
        _cache["failed_at"] = now
        return False
    _cache.update(models=parsed, fetched_at=now, failed_at=0)
    return True


def catalogue(fetch=None, *, now=None) -> dict:
    """The model list to offer: {models, source, fetched_at}. Fetches when
    the cache is stale and `fetch` is given (a callable returning the raw
    GET /v1/models JSON). With no `fetch` it never touches the network,
    which is what the relay and the validators want."""
    if fetch is not None and needs_refresh(now):
        with _refresh_lock:
            if needs_refresh(now):
                refresh(fetch, now)
    return _snapshot()


def _spawn_refresh(fetch) -> None:
    threading.Thread(target=catalogue, args=(fetch,), daemon=True,
                     name="tts-models-refresh").start()


def refresh_soon(fetch) -> None:
    """Refresh a stale list in the background, for callers on the live
    path that must not wait on it. tests/conftest.py stubs _spawn_refresh."""
    if fetch is not None and needs_refresh():
        _spawn_refresh(fetch)


# ---------- routes and refusals ----------

def route_for(model_id: str) -> str:
    return ROUTE_DIALOGUE if model_id.startswith(DIALOGUE_PREFIX) else ROUTE_TTS


def is_model_refusal(exc) -> bool:
    """Whether a failed socket handshake is ElevenLabs refusing the model,
    as opposed to a bad key, a rate limit or a network fault. The socket
    answers HTTP 400 with `unsupported_model` and `"param":"model_id"`."""
    resp = getattr(exc, "response", None)
    if getattr(resp, "status_code", None) != 400:
        return False
    body = bytes(getattr(resp, "body", b"") or b"")
    return b"unsupported_model" in body or b'"model_id"' in body


def mark_refused(model_id: str, now=None) -> None:
    _refused[model_id] = time.time() if now is None else now


def refused_ids(now=None) -> set:
    now = time.time() if now is None else now
    for mid, at in list(_refused.items()):
        if now - at >= REFUSAL_MEMORY_S:
            _refused.pop(mid, None)
    return set(_refused)


# ---------- the Automatic rule ----------

def generation(model_id: str) -> tuple:
    m = _GEN.search(model_id)
    if not m:
        return (0, 0)
    return (int(m.group(1)), int(m.group(2) or 0))


def _live_rank(model_id: str) -> int:
    for i, word in enumerate(LIVE_ORDER):
        if word in model_id:
            return i
    return len(LIVE_ORDER)


def _newest_first(model_id: str):
    major, minor = generation(model_id)
    return (-major, -minor, _live_rank(model_id), model_id)


def automatic_order(models, refused=frozenset()) -> list:
    """Every model Automatic may pick, best first: newest version, then the
    variant built for live talk. Deprecated and refused models are out."""
    ids = [m["id"] for m in models
           if m["id"] not in DEPRECATED and m["id"] not in refused]
    return sorted(ids, key=_newest_first)


def automatic_pick(models, refused=frozenset()) -> str:
    order = automatic_order(models, refused)
    return order[0] if order else DEFAULT_MODEL


# ---------- validation and resolution ----------

def valid_choice(value, models, *, allow_blank=False) -> bool:
    """A setting may be "auto" or an id on the current list. A seat may also
    be blank, meaning "same as the app setting"."""
    if value == "":
        return allow_blank
    if value == AUTO:
        return True
    return (isinstance(value, str) and bool(_ID.match(value))
            and value in {m["id"] for m in models})


def resolve(setting, seat_choice="", *, models, refused=frozenset()) -> dict:
    """Which model speaks. {model, route, choice, automatic}. A seat's own
    choice wins over the app setting. An id that isn't on the list (a
    hand-edited config, a model ElevenLabs withdrew) speaks with
    DEFAULT_MODEL, so an arbitrary string never reaches the provider."""
    choice = seat_choice if seat_choice else setting
    if not isinstance(choice, str) or not choice:
        choice = DEFAULT_MODEL
    automatic = choice == AUTO
    if automatic:
        model = automatic_pick(models, refused)
    elif valid_choice(choice, models) and choice not in refused:
        model = choice
    else:
        model = DEFAULT_MODEL
    return {"model": model, "route": route_for(model), "choice": choice,
            "automatic": automatic}


def attempts(resolved, models, refused=frozenset()) -> list:
    """The models the relay tries in order when ElevenLabs refuses one at
    the handshake: the resolved model, for Automatic the next one down, and
    DEFAULT_MODEL last. At most three, so a refusal costs one extra
    handshake, rarely two."""
    out = [resolved["model"]]
    if resolved["automatic"]:
        out += automatic_order(models, refused)[:2]
    out.append(DEFAULT_MODEL)
    seen, uniq = set(), []
    for mid in out:
        if mid not in seen:
            seen.add(mid)
            uniq.append(mid)
    return uniq[:3]


# ---------- what the settings UI shows ----------

def note_for(model_id: str) -> str:
    return NOTES.get(model_id, NEW_MODEL_NOTE)


def options(models, refused=frozenset()) -> list:
    """The picker's rows, newest first and deprecated last."""
    rows = sorted(models, key=lambda m: (m["id"] in DEPRECATED,
                                         _newest_first(m["id"])))
    return [{"value": m["id"], "label": m["name"], "note": note_for(m["id"]),
             "route": route_for(m["id"]),
             "deprecated": m["id"] in DEPRECATED,
             "refused": m["id"] in refused} for m in rows]


def describe(setting, snap, *, locked_by_env=False, now=None) -> dict:
    """The GET /api/voice/models payload."""
    models = snap["models"]
    refused = refused_ids(now)
    pick = automatic_pick(models, refused)
    names = {m["id"]: m["name"] for m in models}
    current = resolve(setting, models=models, refused=refused)
    return {
        "setting": setting if isinstance(setting, str) and setting else DEFAULT_MODEL,
        "speaking": current["model"],
        "automatic_pick": pick,
        "automatic_pick_name": names.get(pick, pick),
        "automatic_rule": AUTOMATIC_RULE,
        "options": options(models, refused),
        "refused": sorted(refused),
        "source": snap["source"],
        "fetched_at": snap["fetched_at"],
        "default": DEFAULT_MODEL,
        "locked_by_env": bool(locked_by_env),
    }
