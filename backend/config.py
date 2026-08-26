"""Layered configuration: defaults < config.json < config.local.json < environment.

Personal settings (your name, preferred models) belong in config.local.json so
they never end up in the public repo. Environment overrides use the CROSSBAND_
prefix (e.g. CROSSBAND_PORT=9000, CROSSBAND_USER_NAME=Alex); dict-valued fields
take JSON. The pre-rename MMC_ prefix still applies until v0.3, with a startup
warning naming the exact rename.
"""

import json
import os
import re
from pathlib import Path
from urllib.parse import urlsplit

from pydantic import BaseModel, Field

from . import provenance

ROOT = Path(__file__).resolve().parent.parent
CONFIG_PATH = ROOT / "config.json"
LOCAL_CONFIG_PATH = ROOT / "config.local.json"
ENV_PREFIX = "CROSSBAND_"
# The app was renamed from Sideband (working name mmc) at v0.1; env vars moved
# to the new prefix at v0.2. Old-prefix variables still apply for ONE release
# so an existing .env keeps working, but every use logs a rename warning at
# startup. v0.3 removes this fallback entirely.
DEPRECATED_ENV_PREFIX = "MMC_"

DEFAULT_VOICE_PRICING = {"tts_per_1m_chars": 110.0, "stt_per_hour": 0.40}

# ---- provider-specific cache pricing terms ----
# Cached-token billing is NOT a universal ratio, so each rate card carries its
# OWN cache terms; a card that omits them inherits Anthropic's (the historical
# default), which keeps every existing/hand-written table entry computing
# exactly as before.
#
# CORRECTION. This block used to assert that "OpenAI charges no separate
# cache-write premium (it reports no cache-creation tokens at all)" and set
# write_mult to 0.0. That was wrong on the pricing: OpenAI's published card
# bills gpt-5.6-terra at $2.00/M input and **$2.50/M to write cache** - the same
# 1.25x premium Anthropic charges. Only the READ multiplier matched ($0.20 /
# $2.00 = 0.1). The half that was true is the *measurement*, and it is a
# different defect: our OpenAI adapter reads only
# `input_tokens_details.cached_tokens` (reads), so every GPT row records
# cache_creation=0 and the corrected multiplier currently has nothing to
# multiply. Tracked separately - do not "fix" it by reverting this number.
ANTHROPIC_CACHE = {"read_mult": 0.1, "write_mult": 1.25}
OPENAI_CACHE = {"read_mult": 0.1, "write_mult": 1.25}

# $ per 1M tokens. Matched EXACTLY by model id, then by an entry's explicitly
# declared `aliases` (a differently-named model the operator has attested shares
# this card), then - only - by a narrow date/build-stamped reissue of the SAME
# model (e.g. `gpt-5.5` prices `gpt-5.5-2026-01-15`). There is deliberately no
# broad model-family fallback: a newly configured model with a NEW name (e.g.
# `gpt-5.6-terra`, `gpt-5-mini`) is NOT silently priced as an older family - it
# stays `unknown`/unpriced, is surfaced as such, and is ineligible for trusted
# comparison until an exact entry or an explicit `aliases` declaration is
# supplied. See price_for() for the exact match order.
#
# Each entry also carries its provenance: every figure here is a
# `rate_card_estimate` - a published list price, NOT a billed amount - so it
# records the `as_of` date and the `source` it was transcribed from. Anything
# absent from this table has `unknown` provenance and computes cost=None; its
# seat stays `trial` until an explicit record is supplied. A self-hosted/local
# model is declared by adding an entry with provenance
# `self_hosted_zero_marginal` and input/output rates of 0 (a declared $0
# marginal, distinct from "no data").
def _rate_card(input, output, as_of, source, *, cache=None, aliases=()):
    return {"input": input, "output": output,
            "provenance": provenance.RATE_CARD_ESTIMATE,
            "as_of": as_of, "source": source,
            # Provider cache terms travel WITH the rate (defaults to Anthropic's
            # so untouched entries are unchanged); `aliases` are the explicit,
            # operator-attested compatible model ids that share this card.
            "cache": dict(cache) if cache else dict(ANTHROPIC_CACHE),
            "aliases": tuple(aliases)}


def _openai_rate_card(input, output, as_of, source, *, aliases=()):
    return _rate_card(input, output, as_of, source, cache=OPENAI_CACHE,
                      aliases=aliases)


def _self_hosted(source):
    """A local/self-hosted model: a DECLARED $0 marginal cost (distinct from
    'unknown' - see provenance.py) so the seat is onboardable and its cost
    tracks as a real, verifiable $0 rather than a gap."""
    return {"input": 0.0, "output": 0.0,
            "provenance": provenance.SELF_HOSTED_ZERO_MARGINAL,
            "as_of": _PRICING_AS_OF, "source": source,
            "cache": dict(ANTHROPIC_CACHE), "aliases": ()}


_ANTHROPIC_PRICING_URL = "https://www.anthropic.com/pricing"
_OPENAI_PRICING_URL = "https://openai.com/api/pricing"
_PRICING_AS_OF = "2026-01-01"

# Transcribed from OpenAI's developer pricing table rather than the marketing
# page, because that is where the per-tier cache columns actually live. Stamped
# with its own date: the older entries above were NOT re-verified at the same
# time, and restamping figures nobody checked is how a rate card starts lying.
_OPENAI_DEV_PRICING_URL = "https://developers.openai.com/api/docs/pricing"
_OPENAI_VERIFIED_AS_OF = "2026-07-31"

DEFAULT_PRICING = {
    "claude-fable-5": _rate_card(10.0, 50.0, _PRICING_AS_OF, _ANTHROPIC_PRICING_URL),
    "claude-opus-4-8": _rate_card(5.0, 25.0, _PRICING_AS_OF, _ANTHROPIC_PRICING_URL),
    "claude-opus-4-7": _rate_card(5.0, 25.0, _PRICING_AS_OF, _ANTHROPIC_PRICING_URL),
    "claude-opus-4-6": _rate_card(5.0, 25.0, _PRICING_AS_OF, _ANTHROPIC_PRICING_URL),
    "claude-sonnet-5": _rate_card(3.0, 15.0, _PRICING_AS_OF, _ANTHROPIC_PRICING_URL),
    "claude-sonnet-4-6": _rate_card(3.0, 15.0, _PRICING_AS_OF, _ANTHROPIC_PRICING_URL),
    "claude-haiku-4-5": _rate_card(1.0, 5.0, _PRICING_AS_OF, _ANTHROPIC_PRICING_URL),
    # Standard tier, SHORT context ($2.00 / $0.20 cached / $2.50 write / $12.00).
    # OpenAI also publishes a LONG-context tier at 2x every column
    # ($4.00 / $0.40 / $5.00 / $18.00) and does not state the token threshold
    # that separates them, so a long-context round is under-priced here by up
    # to 2x. Deliberately not guessed: inventing the threshold is exactly the
    # silent mispricing the exact-match rule above exists to prevent. A rate
    # card is a single input/output pair today; expressing tiers needs a schema
    # change, tracked separately.
    "gpt-5.6-terra": _openai_rate_card(2.0, 12.0, _OPENAI_VERIFIED_AS_OF,
                                       _OPENAI_DEV_PRICING_URL),
    "gpt-5.5": _openai_rate_card(1.75, 14.0, _PRICING_AS_OF, _OPENAI_PRICING_URL),
    "gpt-5.1": _openai_rate_card(1.25, 10.0, _PRICING_AS_OF, _OPENAI_PRICING_URL),
    "gpt-5": _openai_rate_card(1.25, 10.0, _PRICING_AS_OF, _OPENAI_PRICING_URL),
    # Local (Ollama) - declared $0 marginal, onboardable, not "unknown".
    "gpt-oss:20b": _self_hosted("local (Ollama, self-hosted)"),
}


class Settings(BaseModel):
    # server
    host: str = "127.0.0.1"
    port: int = 8902
    data_dir: str = ""  # empty -> <repo root>/data
    # Extra Host headers to accept beyond loopback (comma-separated). For remote
    # access over Tailscale: `tailscale serve https / http://127.0.0.1:8902`
    # proxies to loopback but forwards Host as the tailnet name, which the
    # DNS-rebinding guard would otherwise 403. Set
    # CROSSBAND_TRUSTED_HOSTS=my-mac.my-tailnet.ts.net. Empty = loopback only (default).
    trusted_hosts: str = ""
    # Verbosity for the app's own "crossband.*" loggers - separate from uvicorn's
    # request/access logging, which is unaffected either way. Empty (default):
    # unchanged from before this existed - only WARNING+ reaches
    # data/service.log, so the content-free per-request diagnostics logged at
    # INFO (e.g. providers.py's Claude-chat cache-telemetry line) are
    # silent. Set CROSSBAND_LOG_LEVEL=INFO for a deliberate sampling session (see
    # docs/COST_TELEMETRY.md), then unset it again. This only changes what's
    # written to the log - never what gets cached, priced, or billed.
    log_level: str = ""
    # Recovery secret for the browser gate (#25): gates first-run password
    # enrolment and reset, never the everyday login. Set
    # CROSSBAND_RECOVERY_SECRET in .env for a durable one; empty (default)
    # mints a fresh random secret each start, shown in startup output ONLY
    # while no password is enrolled yet.
    recovery_secret: str = ""
    # Seconds a graceful stop may spend waiting on connections that are still
    # open (a chat round mid-generation, a live voice call) before they are
    # cancelled and the process exits anyway. The live-events watcher streams
    # end immediately on their own, so this ceiling only ever applies to real
    # in-flight work. Uvicorn's own default here is to wait forever, and a stop
    # that never finishes is what this ceiling exists to prevent. Raise it if
    # you would rather a long round always finish; lower it for a deploy loop
    # that values a fast, predictable stop.
    shutdown_timeout_s: int = 15

    # models
    anthropic_model: str = "claude-opus-4-8"
    openai_model: str = "gpt-5.1"
    utility_model: str = "claude-haiku-4-5"  # lab-routable: gpt-* routes to OpenAI

    # identity / display
    user_name: str = "User"
    # display names seed the default roster on FIRST RUN only; after that,
    # names live in the participants table (edit in-app, not here)
    claude_display_name: str = "Claude"
    gpt_display_name: str = "GPT"

    # conversation shaping
    max_response_tokens: int = 16000
    summary_threshold_chars: int = 60000
    keep_recent_messages: int = 12
    max_attachment_mb: int = 20

    # After each completed reply, run a content-free diagnostic that notes when
    # a model's "you said/asked/…" claim isn't found VERBATIM in the raw
    # (speaker=="user") turns in the current window. Prevention-and-observation,
    # never enforcement: it only logs a fingerprinted, text-free line (see
    # providers._check_attribution) and NEVER blocks or edits a reply. A "no
    # verbatim match" can be entirely legitimate (the grounding turn was
    # compressed into the rolling summary, or paraphrased) - it is not a
    # fabrication verdict. Set false (CROSSBAND_ATTRIBUTION_AUDIT=false) to turn the
    # diagnostic off entirely.
    attribution_audit: bool = True

    # After a completed TEXT reply, drop a draft that mostly restates the
    # seat's own previous message or a reply already given this round: one
    # retry with the guard stated, then suppression, mirroring the refused
    # pass (backend/echo.py, engine round loop). Verbatim-leaning by design:
    # a paraphrase passes it, quoted lines are stripped before judging, and
    # short agreements are never judged. Voice rounds only log a warning,
    # since by completion the reply has already been spoken. Set false
    # (CROSSBAND_ECHO_GUARD=false) to turn enforcement off entirely.
    echo_guard: bool = True

    # Flag a citation-shaped claim ("the docs say ...") in a reply that ran
    # no tools: same quiet chip as the attribution audit, one content-free
    # WARNING line, never a retry or a block (backend/citations.py). A model
    # may cite from training or memory; the chip only says nothing was
    # fetched this turn. Any tool row in the reply skips the check. Set
    # false (CROSSBAND_CITATION_CHECK=false) to turn it off.
    citation_check: bool = True

    # voice
    # Provider seam: which engine serves STT/TTS. "auto" (default) =
    # current behaviour exactly: ElevenLabs when ELEVENLABS_API_KEY is
    # present, otherwise voice is cleanly unavailable. "elevenlabs" is the
    # same choice made explicit. "local" is RESERVED for a fully local
    # STT/TTS stack (zero cloud egress) and resolves to "voice unavailable"
    # until such an engine lands: a reserved provider must never leak audio
    # bytes to the cloud.
    voice_provider: str = "auto"
    tts_model: str = "eleven_flash_v2_5"
    tts_speed: float = 1.0  # 0.7-1.2; ElevenLabs speaking speed
    stt_model: str = "scribe_v2"
    voice_pricing: dict = Field(default_factory=lambda: dict(DEFAULT_VOICE_PRICING))
    # Room mode (#28 phase 2): how many people the roster may hold at once
    # (present people; the cap frees as people leave). A product choice, not a
    # technical one - the diarization API clusters up to 32. Override with
    # CROSSBAND_ROOM_ROSTER_MAX.
    room_roster_max: int = 6

    # Local speaker identification (#28): THE identity path. Since PR-B the
    # on-device matcher is the only way a voice ever gets a name - identity
    # is local or honestly uncertain, and no cloud pass ever names a turn.
    # When off (CROSSBAND_VOICE_ID_ENABLED=false), or when sherpa-onnx or
    # the model is absent, turns simply stay unnamed and automatic voice
    # arming does not happen; introductions, spoken commands and the toggle
    # still arm room mode by hand. The ElevenLabs diarize batch call
    # survives only for crosstalk word-splitting when the matcher hears
    # overlapping speech.
    voice_id_enabled: bool = True
    # Cosine match threshold. Calibrated for nemo_en_titanet_small: same-speaker
    # ~0.63-0.73 vs best-impostor ~0.12-0.31 locally, so 0.5 sits in the gap.
    voice_id_threshold: float = 0.5
    # Match margin (#28 PR-B): how far the best enrolled match must beat the
    # runner-up before it is claimed. The hygiene guard widens it further,
    # automatically, for enrolled pairs whose voices sound close.
    voice_id_margin: float = 0.12
    # #81: while anyone on the roster is anchor-pending, the naming bar
    # rises by this much - the person most likely to be speaking has no
    # bank to score against, so borderline matches to remembered people
    # defer instead of confidently stealing a new guest's turns. 0 = off.
    voice_id_pending_extra: float = 0.08
    # #222: how much score, on top of the threshold, a match needs before
    # its audio may be BANKED (accumulation and short-slice harvesting).
    # Naming keeps the plain threshold; this keeps borderline matches from
    # feeding the very bank that produced them. 0 = banking at the naming
    # bar, the pre-#222 behaviour.
    voice_id_banking_extra: float = 0.10
    # The two-part anchor sufficiency bar (#28 PR-B): accepted seconds AND a
    # minimum number of short (~1-2s) clips before a voice counts as
    # identifiable - so second-long interjections have something like
    # themselves to match against.
    voice_id_sufficient_seconds: float = 6.0
    voice_id_min_short_clips: int = 2
    # The pinned model. URL and SHA-256 pin TOGETHER - override both or neither;
    # a URL override checked against the default hash simply fails verification
    # and the matcher stays unavailable. Empty here means "use the built-in
    # pins" (backend/voiceid.py). The model is fetched once to
    # <data_dir>/voice_models/ and never committed.
    voice_id_model_url: str = ""
    voice_id_model_sha256: str = ""

    # memory companion service (Membro)
    memory_url: str = "http://127.0.0.1:8901"

    # summon_claude_code guest. Investigate mode (the default) is read-only;
    # implement mode (code_allow_writes, below) lets it branch, test, push
    # and open a PR, never merge.
    # code_repos maps a short name to a local path; empty = feature dark.
    # code_mcp mounts MCP servers into the guest (e.g. Membro for recall):
    #   {"membro": {"command": "<membro>/.venv/bin/python",
    #               "args": ["-m", "memory_service.mcp_server"],
    #               "env": {"PYTHONPATH": "<membro>"}}}
    # env.PYTHONPATH is required for Membro (it runs from its checkout and is
    # never pip-installed); without it the server dies at spawn and the guest
    # simply arrives with the tool missing.
    code_repos: dict = Field(default_factory=dict)
    code_mcp: dict = Field(default_factory=dict)
    # GitHub issue tools (read + file), same "code" chat toggle. Name → repo
    # slug: {"crossband": "you/crossband"}. Auth: GITHUB_TOKEN env, else the
    # machine's authenticated gh CLI.
    github_repos: dict = Field(default_factory=dict)

    # External MCP servers the MODELS may call (the pull half; ingestion is
    # push). Private by placement: configure in config.local.json so the
    # public repo never learns what your servers are. Name → stdio spec:
    #   {"build-watcher": {"command": ".../python",
    #                      "args": [".../mcp_server.py"],
    #                      "label": "Checking the build"}}
    # "label" is optional and TRUSTED: while a call to that server is
    # in flight, the work-status event shows this exact text instead of a
    # generic "Working on it" fallback. Only the operator who configured the
    # server can honestly say what it does, so Crossband never guesses one:
    # omit it and the generic fallback is used.
    mcp_servers: dict = Field(default_factory=dict)

    # External event ingestion (POST /api/ingest). Loopback is the primary
    # boundary; set a token ONLY if a producer posts from beyond loopback
    # (e.g. another tailnet machine) - then requests need
    # Authorization: Bearer <token>. Empty (default) = loopback-trust only.
    ingest_token: str = ""

    # Slash-command suggestions for the composer. Messages starting with "/"
    # are notes to tooling (no model replies); this list only powers the
    # composer's predictive chips, and Crossband itself assigns no meaning to
    # any command. Entries: {"insert": "/deploy crossband #", "label": "...",
    # "hint": "..."}. Empty (default) = no suggestion UI.
    slash_commands: list = Field(default_factory=list)
    # Dead-man warning for slash commands (#58): if no machine tooling acks a
    # "/" message (via the notice route's ack_command_id) within this many
    # seconds, ONE system line says nothing picked it up - so a stopped
    # watcher stops being indistinguishable from a queued deploy. 0 = off.
    slash_ack_timeout_s: float = 120.0
    code_max_turns: int = 50       # SDK turn cap for one guest visit
    code_timeout_s: float = 600.0  # wall-clock cap for one guest visit
    # Guest auth: false (default) = the machine's own Claude Code login
    # (subscription; run `claude /login` once). true = bill guest turns to
    # ANTHROPIC_API_KEY from .env (metered, per-token; cost is recorded on
    # every guest message).
    code_use_api_key: bool = False
    # Default model alias for summoned Claude Code: "default" (Claude Code's
    # own default), "opus", "sonnet", or "haiku". A per-summon model choice on
    # the summon_claude_code tool overrides this. Model choice is separate from
    # auth (code_use_api_key): a cheaper model is still billed by whichever
    # path authenticated the turn. Unknown values degrade to "default".
    code_model: str = "default"
    # Default effort/thinking level for summoned Claude Code: "default" (Claude
    # Code's own), "think", "think-hard", or "ultrathink" (larger thinking
    # budgets, injected via MAX_THINKING_TOKENS). A per-summon effort choice on
    # the summon_claude_code tool overrides this. Unknown values degrade to
    # "default". The level ACTUALLY applied is reported back on the guest reply.
    code_effort: str = "default"
    # Implement mode (guest ships PRs): off by default - write capability
    # never appears by accident. The guest branches, tests, pushes and opens
    # a PR; it can never merge or push to main.
    code_allow_writes: bool = False
    # New chats start with the code toggle on (harmless without code_repos -
    # the tools are only offered when the harness is actually available).
    code_default_on: bool = False
    code_impl_max_turns: int = 150
    code_impl_timeout_s: float = 1800.0


    # research tool caps (no magic numbers in tools.py)
    search_timeout: float = 20.0
    fetch_timeout: float = 15.0
    max_tool_output: int = 8000
    tool_log_chars: int = 1200  # per tool event when replayed into later transcripts
    max_tool_rounds: int = 6
    max_transcript_chars: int = 100_000  # fetch_youtube_transcript in-chat cap
    max_audio_mb: int = 60  # transcribe_audio_url download cap
    max_search_results: int = 5

    # egress vetting proxy (#138, first slice): every model-influenced URL
    # leaves through one loopback proxy that resolves a host once and connects
    # to the address it vetted, so DNS rebinding gains nothing. The transfer
    # cap is a per-connection machine backstop and must stay >= max_audio_mb
    # (podcast audio rides the same path); fetch_max_page_mb is the tighter
    # decoded-bytes cap fetch_page enforces itself.
    egress_max_transfer_mb: int = 64
    egress_politeness_s: float = 2.0  # per-host spacing between bursts (#153)
    egress_idle_timeout_s: float = 60.0
    egress_tunnel_lifetime_s: float = 300.0
    fetch_max_page_mb: int = 10
    # view_page (#138 slice 3): wall clock for one rendered view, worker
    # process included. The render itself runs in a contained subprocess
    # behind the egress proxy (backend/browse.py).
    browse_timeout_s: float = 20.0
    # #148: total bytes ONE page load may pull across all its connections
    # (subresources included), enforced at the proxy's view listener. 0
    # disables the listener and a render falls back to the per-connection
    # caps alone.
    browse_page_budget_mb: float = 30.0
    # #148: on macOS, wrap the render worker in an OS sandbox profile (no IP
    # traffic except the proxy port, no writes outside its throwaway profile
    # dir, no reads of the data dir / .env / ~/.ssh). Defence in depth, never
    # the boundary: platforms without sandbox-exec, and any OS-refused
    # profile, render exactly as before. False turns the wrap off entirely.
    browse_sandbox: bool = True

    # backups
    backup_keep: int = 14
    backup_interval_hours: float = 6.0
    backup_mirror_dir: str = ""  # optional offsite mirror of completed snapshots
    backup_mirror_keep: int = 7

    # startup behaviour: when true, missing provider keys abort startup instead
    # of degrading (each missing key is always named loudly either way)
    require_keys: bool = False

    pricing: dict = Field(default_factory=lambda: dict(DEFAULT_PRICING))

    def resolved_data_dir(self) -> Path:
        return Path(self.data_dir) if self.data_dir else ROOT / "data"

    def as_cfg(self) -> dict:
        """Plain dict view used by the round loop / prompt assembly."""
        return self.model_dump()


def _read_json(path: Path) -> dict:
    try:
        with open(path) as f:
            data = json.load(f)
        return data if isinstance(data, dict) else {}
    except FileNotFoundError:
        return {}
    except Exception:
        return {}  # a malformed config file must never brick startup


def _env_overrides(environ) -> dict:
    out = {}
    fields = Settings.model_fields
    for name, field in fields.items():
        ann = field.annotation
        # v0.2 fallback: try the new name, then the old MMC_ name (removed in
        # v0.3; each use is called out at startup by deprecated_env_vars).
        # Candidate semantics deliberately match db.py's `or` chain and the
        # scripts' ${:-} chains: a new-name line that is present but EMPTY or
        # unparseable falls through to a usable old value instead of silently
        # discarding both - the half-migrated .env with a blank
        # CROSSBAND_DATA_DIR= placeholder would otherwise boot an empty
        # database while the old value sits right there.
        for raw in (environ.get(ENV_PREFIX + name.upper()),
                    environ.get(DEPRECATED_ENV_PREFIX + name.upper())):
            if raw is None or raw == "":
                continue
            try:
                if ann is int:
                    out[name] = int(raw)
                elif ann is float:
                    out[name] = float(raw)
                elif ann is bool:
                    out[name] = raw.strip().lower() in ("1", "true", "yes", "on")
                elif ann is dict:
                    out[name] = json.loads(raw)
                else:
                    out[name] = raw
                break
            except (ValueError, json.JSONDecodeError):
                continue  # unparseable candidate: try the next, never crash
    return out


def deprecated_env_vars(environ=None) -> list[tuple[str, str]]:
    """Every MMC_-prefixed variable in the environment that maps to a Settings
    field, as (old_name, new_name) pairs. Startup logs one warning per entry;
    v0.3 turns the fallback off, so the warning names the exact rename and the
    deadline. A variable set under BOTH prefixes is still listed: the new name
    won, and the operator should delete the stale line rather than trust it."""
    environ = environ if environ is not None else os.environ
    pairs = []
    for name in Settings.model_fields:
        old = DEPRECATED_ENV_PREFIX + name.upper()
        if old in environ:
            pairs.append((old, ENV_PREFIX + name.upper()))
    return pairs


def load_settings(root: Path | None = None, environ=None) -> Settings:
    """defaults <- config.json (committed) <- config.local.json (gitignored) <- env."""
    root = root or ROOT
    environ = environ if environ is not None else os.environ
    merged: dict = {}
    for path in (root / "config.json", root / "config.local.json"):
        merged.update(_read_json(path))
    merged.update(_env_overrides(environ))
    known = {k: v for k, v in merged.items() if k in Settings.model_fields}
    # The table-valued fields LAYER over their built-in defaults instead of
    # replacing them. Pydantic's default_factory only runs when the key is
    # absent, so before this a config.local.json carrying `pricing` with one
    # model wiped every OTHER model's card - silently unpricing the whole
    # roster, dropping every seat to `trial`, and recording cost=None across
    # the board. docs/CONFIG.md actively told operators to edit `pricing` to
    # add a model, so following the documentation was the way to trigger it.
    # Merge is per ENTRY (a card is overridden whole, never field-by-field),
    # which is the granularity a rate card is published at.
    for field, base in (("pricing", DEFAULT_PRICING),
                        ("voice_pricing", DEFAULT_VOICE_PRICING)):
        if isinstance(known.get(field), dict):
            known[field] = {**base, **known[field]}
    return Settings(**known)


# A date/build-stamped reissue of the SAME model: the key, then a boundary
# separator, then a DIGIT (a date or build number, e.g. `-2026-01-15`,
# `-20260101`). This is the ONLY implicit prefix inheritance left - deliberately
# narrow, so a differently-NAMED model (`gpt-5.6-terra`, `gpt-5-mini`, whose
# suffix begins with `.` or a letter) never matches a shorter family key and is
# never silently priced as an older family.
_DATED_VARIANT = re.compile(r"^[-:@/_ ]\d")


def _is_dated_variant(model, key) -> bool:
    if model == key or not model.startswith(key):
        return False
    return bool(_DATED_VARIANT.match(model[len(key):]))


def price_for(model, pricing):
    """Resolve a model id to its rate card, failing CLOSED.

    Match order, most-trusted first:
      1. exact model-id match;
      2. an entry that explicitly declares this id in its ``aliases`` - an
         operator-attested "this model is priced like that one";
      3. a narrow date/build-stamped reissue of the same model (see
         _is_dated_variant): ``gpt-5.5`` prices ``gpt-5.5-2026-01-15``.

    There is no broad model-family fallback. A newly configured model with a new
    NAME resolves to ``None`` (→ cost unknown, provenance unknown, seat stays
    trial) rather than inheriting an unrelated older family's price."""
    exact = pricing.get(model)
    if exact is not None:
        return exact
    for key, p in pricing.items():
        if model in (p.get("aliases") or ()):
            return p
    best = None
    for key, p in pricing.items():
        if _is_dated_variant(model, key) and (best is None or len(key) > best[0]):
            best = (len(key), p)
    return best[1] if best else None


# Hosts that mean "this machine". A seat pointed at one of these, with no
# credential, is self-hosted by definition.
_LOOPBACK_HOSTS = frozenset({"localhost", "127.0.0.1", "::1", "0.0.0.0"})


def is_self_hosted_endpoint(base_url, api_key_env=None) -> bool:
    """Is this seat served from THIS machine, keylessly?

    A model the price table has never heard of normally resolves to `unknown`
    provenance, which blocks onboarding - correct for a hosted model whose rate
    nobody has recorded, but a dead end for the documented zero-key path (pull a
    model in Ollama, add it, it never speaks and cannot be promoted). A seat
    served from loopback with no API key cannot be metering anyone: nothing
    leaves the machine. That is a declared $0 marginal cost, not a gap.

    Deliberately narrow, because a wrong $0 is worse than an honest "unknown":

    · **Loopback only.** A LAN address or a public host may be someone else's
      metered service, so it keeps `unknown` and the existing gate.
    · **Keyless only.** A seat carrying an `api_key_env` is authenticating to
      something. This is what rules out the one case that would otherwise
      mis-price silently - a paid provider reached through a localhost tunnel -
      since that always needs a credential.

    Everything outside those two conditions is unchanged: still `unknown`, still
    gated, still requiring an explicit rate-card entry to onboard.
    """
    if not base_url or (api_key_env or "").strip():
        return False
    raw = base_url.strip()
    if "://" not in raw:  # a bare "localhost:11434/v1" has no scheme to split on
        raw = "http://" + raw
    try:
        host = (urlsplit(raw).hostname or "").lower()
    except ValueError:  # malformed authority (e.g. a bad IPv6 literal)
        return False
    return host in _LOOPBACK_HOSTS or host.startswith("127.")


def compute_cost(model, usage, pricing, *, base_url=None, api_key_env=None):
    p = price_for(model, pricing)
    if p is None and is_self_hosted_endpoint(base_url, api_key_env):
        # A declared $0 (see is_self_hosted_endpoint), not "no data" - this must
        # stay in lockstep with provenance_for below, or the seat would report a
        # self-hosted provenance while its cost read as untracked.
        return 0.0
    if not p:
        return None
    # Per-provider cache terms ride on the rate card; a card that
    # predates them (or a hand-written test table) inherits Anthropic's ratios,
    # so nothing that computed before changes value.
    cache = p.get("cache") or ANTHROPIC_CACHE
    return (
        usage.get("input", 0) * p["input"]
        + usage.get("cache_creation", 0) * p["input"] * cache["write_mult"]
        + usage.get("cache_read", 0) * p["input"] * cache["read_mult"]
        + usage.get("output", 0) * p["output"]
    ) / 1_000_000


def provenance_for(model, pricing, *, base_url=None, api_key_env=None):
    """Provenance snapshot for a model's cost, derived from the pricing table.

    A model in the table inherits its entry's declared provenance (rate-card
    estimate by default, or a self-hosted declaration); a model absent from the
    table is `unknown`. Returns a JSON-able record (see provenance.record) safe
    to persist onto a per-turn cost so later table edits don't rewrite it.

    The one exception: a model absent from the table but served
    from a keyless loopback endpoint resolves to `self_hosted_zero_marginal`
    instead of `unknown`, so ANY local model a user pulls is onboardable - not
    just the one (`gpt-oss:20b`) that happens to be named in the table. The
    conditions are deliberately narrow; see is_self_hosted_endpoint. The record
    carries no `as_of` because nothing was transcribed from a dated price list -
    the $0 follows from where the endpoint is, and that is true whenever it's
    read."""
    p = price_for(model, pricing)
    if p is None and is_self_hosted_endpoint(base_url, api_key_env):
        return provenance.record(
            provenance.SELF_HOSTED_ZERO_MARGINAL,
            source_ref="local endpoint on this machine (keyless, self-hosted)",
        )
    p = p or {}
    return provenance.record(
        p.get("provenance", provenance.UNKNOWN),
        as_of=p.get("as_of"),
        source_ref=p.get("source"),
    )


def key_status():
    return {
        "anthropic": bool(os.environ.get("ANTHROPIC_API_KEY")),
        "openai": bool(os.environ.get("OPENAI_API_KEY")),
    }


# What each key unlocks - used for the loud startup report.
KEY_ROLES = {
    "ANTHROPIC_API_KEY": "Claude participants cannot reply; rolling summaries, "
                         "auto-titles and project distillation are disabled",
    "OPENAI_API_KEY": "GPT participants cannot reply",
    "ELEVENLABS_API_KEY": "voice mode (TTS/STT) is disabled",
    "TAVILY_API_KEY": "web_search loses the Tavily engine",
    "BRAVE_API_KEY": "web_search loses the Brave engine",
}


def report_missing_keys(settings: Settings, log) -> None:
    """Name every missing key and exactly what breaks. With require_keys=true,
    a missing provider key aborts startup instead of silently degrading."""
    missing = [k for k in KEY_ROLES if not os.environ.get(k)]
    for k in missing:
        log.error("MISSING KEY %s - %s. Add it to .env and restart.", k, KEY_ROLES[k])
    hard = [k for k in ("ANTHROPIC_API_KEY", "OPENAI_API_KEY") if k in missing]
    if settings.require_keys and hard:
        raise RuntimeError(
            f"Required API key(s) not set: {', '.join(hard)} - "
            f"set them in .env or start with CROSSBAND_REQUIRE_KEYS=false"
        )


def write_local_key(key: str, value) -> None:
    """Atomically set/remove one top-level key in config.local.json, preserving
    every other key. Extracted from routers/pricing.py's _write_overrides so
    every config-backed UI shares one write path: temp file + os.replace, with
    a single .bak of the previous contents."""
    import contextlib as _ctx
    import json as _json
    import os as _os
    import shutil as _shutil
    import tempfile as _tempfile
    local = _read_json(LOCAL_CONFIG_PATH)
    if value:
        local[key] = value
    else:
        local.pop(key, None)
    LOCAL_CONFIG_PATH.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = _tempfile.mkstemp(dir=str(LOCAL_CONFIG_PATH.parent),
                                prefix=".config.local.", suffix=".json")
    try:
        with _os.fdopen(fd, "w") as f:
            _json.dump(local, f, indent=2, sort_keys=True)
            f.write("\n")
        if LOCAL_CONFIG_PATH.exists():
            with _ctx.suppress(OSError):
                _shutil.copy2(LOCAL_CONFIG_PATH,
                              LOCAL_CONFIG_PATH.with_suffix(".json.bak"))
        _os.replace(tmp, LOCAL_CONFIG_PATH)
    except Exception:
        with _ctx.suppress(OSError):
            _os.unlink(tmp)
        raise
