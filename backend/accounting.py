"""Shared cost accounting - the single definition of "what a dollar is" that
every money-showing surface consumes, so they can never diverge. Build the
accounting once; each surface is just a different window/grouping over the same
events. Today that means the cumulative Spend page, the export picker's
per-chat rows, the in-call spend diagnostic, and the chat header, which reads
its total off the chat payload rather than deriving one in the browser.

This docstring has twice named a consumer that was not one. It named a
per-chat ``/cost`` slash command that was proposed and never built, and the
phrase spread into ARCHITECTURE.md as a statement of fact. It then named the
chat header, which really did derive its own total from message rows alone
and so could not see voice or utility spend. Left as a marker: describe what
is built, not what was intended, and check before adding a name here.

PROVENANCE HONESTY. A recorded dollar is not automatically a charged dollar.
Every cost is sorted into exactly one of three buckets:

  metered            - pay-as-you-go API billing we can stand behind. Resident
                       model turns are ALWAYS here: the Messages/Responses API
                       is key-billed by license - a subscription cannot serve
                       it. Voice (ElevenLabs TTS/STT) is metered too. A Claude
                       Code guest turn lands here only when the turn RECORDED
                       that it ran on the metered API key.
  subscription_equiv - Claude Code's own ``total_cost_usd`` for a turn that ran
                       on a subscription or OAuth login instead of the metered
                       API key: an API-list-price ESTIMATE covered by that
                       subscription, not incremental cash. Informational;
                       NEVER summed into billed spend.
  unknown            - a Claude Code guest turn with no recorded auth mode
                       (turns logged before provenance capture landed).
                       We can't prove it either way, so it is shown apart and
                       never silently folded into metered spend.

Guest turns record their auth mode in ``usage_json.auth`` ("api_key" |
"subscription") at turn time (backend/guest.py). Older rows lack it → unknown.

Sources that emit events but carry no cost (e.g. a provider with no pricing
row, or voice logged without a cost) are reported as "not tracked" for that
source rather than a silent $0.00.
"""

import datetime as _dt
import json
from dataclasses import dataclass

from . import provenance as prov
from .config import (ANTHROPIC_CACHE as DEFAULT_CACHE, DEFAULT_PRICING,
                     price_for, provenance_for)
from .llm_util import model_family

# The guest speaker slug. Imported lazily-safe as a literal so this module has
# no import cycle with backend.guest.
CODE_SLUG = "claude-code"

# cost sources
SOURCE_NORMAL = "normal"        # resident chat-participant model turns
SOURCE_CODE = "claude_code"     # summoned Claude Code guest turns
SOURCE_TTS = "tts"              # ElevenLabs text-to-speech (voice out)
SOURCE_STT = "stt"              # ElevenLabs speech-to-text (voice in)
# Cheap utility-model calls (rolling summary / auto-title / project
# distillation, backend/chat_memory.py) - previously invisible entirely: no
# chat_id-attributed insert_message, no usage tracking at all.
# Kept apart from SOURCE_NORMAL (the chat participants) so the Spend page can
# show Claude-chat vs Claude Code vs Haiku/utility separately.
SOURCE_UTILITY = "utility"

SOURCE_LABELS = {
    SOURCE_NORMAL: "Model turns",
    # "Coding agent", not "guest": the Spend page's by_source rows are a
    # workload/category label, and "guest" undersells what the turn actually
    # is. Read-time only - the key (SOURCE_CODE) and every other shape are
    # unchanged, so no migration and no API/frontend change follow from this.
    SOURCE_CODE: "Coding agent",
    SOURCE_TTS: "Voice · TTS",
    SOURCE_STT: "Voice · STT",
    SOURCE_UTILITY: "Utility (background model work)",
}

# ---- producer classes (the like-with-like axis) ----
# A "producer" is the STRUCTURED category of thing that incurred a cost: a chat
# participant, an agent/guest, voice, a utility call. It is the axis the Spend
# page must compare on so a coding agent's spend is NEVER merged into a chat
# participant's model bar. Every cost source maps to exactly one producer; a
# new source that forgets to declare one falls back to being its own producer
# (surfaced, never silently folded into "chat"). Derived, not stored - so any
# newly onboarded producer flows in with a one-line map entry and no migration.
PRODUCER_CHAT = "chat"          # resident chat participants
PRODUCER_AGENT = "agent"        # summoned Claude Code / other agent guests
PRODUCER_VOICE = "voice"        # ElevenLabs TTS/STT
PRODUCER_UTILITY = "utility"    # titles, summaries, distillation,
                                # and the room and voice scans

SOURCE_PRODUCER = {
    SOURCE_NORMAL: PRODUCER_CHAT,
    SOURCE_CODE: PRODUCER_AGENT,
    SOURCE_TTS: PRODUCER_VOICE,
    SOURCE_STT: PRODUCER_VOICE,
    SOURCE_UTILITY: PRODUCER_UTILITY,
}

PRODUCER_LABELS = {
    PRODUCER_CHAT: "Chat participants",
    PRODUCER_AGENT: "Agents / coding guests",
    PRODUCER_VOICE: "Voice",
    PRODUCER_UTILITY: "Utility (titles/summaries)",
}


def producer_for(source: str) -> str:
    """The producer class for a cost source. Unknown sources become their own
    producer rather than being folded into chat - surfaced, never silent."""
    return SOURCE_PRODUCER.get(source, source)

# provenance categories - the CASH axis: is this incremental spend?
CAT_METERED = "metered"
CAT_SUBSCRIPTION = "subscription_equiv"
CAT_UNKNOWN = "unknown"
CATEGORIES = (CAT_METERED, CAT_SUBSCRIPTION, CAT_UNKNOWN)

_AUTH_CATEGORY = {"api_key": CAT_METERED, "subscription": CAT_SUBSCRIPTION}

# provenance sources - the finer HOW-KNOWN axis, carried alongside the
# cash category so a dashboard can group by provenance and treat only
# provider_reported as billed, never folding rate-card estimates or
# subscription-equivalents into billed spend. Vocabulary lives in provenance.py.

# How a guest turn's recorded auth maps to a provenance when the row predates
# per-turn provenance capture: api-key = provider-reported billed cost;
# subscription = an API-equivalent estimate the subscription covers.
_AUTH_PROVENANCE = {
    "api_key": prov.PROVIDER_REPORTED,
    "subscription": prov.SUBSCRIPTION_EQUIVALENT,
}


@dataclass
class CostEvent:
    chat_id: int | None
    ts: float
    source: str
    category: str      # cash axis: metered / subscription_equiv / unknown
    provenance: str    # how-known axis: provider_reported / rate_card_estimate /
                       # self_hosted_zero_marginal / subscription_equivalent / unknown
    provider: str
    model: str
    speaker: str
    cost: float
    tokens: int
    has_cost: bool  # False → this event exists but its cost is not tracked
    # ---- cache health ----
    # The prompt-cache regression that ran unnoticed in plain sight (a prefix
    # re-written on every call instead of re-read) hid there because these two
    # numbers, already sitting in usage_json, were never aggregated anywhere.
    # A cache WRITE is billed at 1.25x input and a READ at 0.1x, so a write
    # costs 12.5x a read, and neither the dollar total nor the token total
    # moves much when a prefix starts being re-written instead of re-read.
    # The RATIO is the signal; defaults keep non-token sources (voice) at zero.
    cache_read: int = 0
    cache_creation: int = 0
    # Metered dollars attributable to those writes. Populated ONLY for
    # rate_card_estimate events, where the formula is ours. A Claude Code guest
    # turn's cost is the SDK's own opaque total_cost_usd and cannot honestly be
    # decomposed - its tokens are reported, its dollars are not split.
    cache_write_cost: float = 0.0

    @property
    def producer(self) -> str:
        """The structured producer class this cost belongs to (chat / agent /
        voice / utility) - the like-with-like axis the Spend page compares on so
        an agent's spend is never merged into a chat participant's bar."""
        return producer_for(self.source)


def _tokens(u: dict) -> int:
    return int((u.get("input", 0) or 0) + (u.get("cache_read", 0) or 0)
               + (u.get("cache_creation", 0) or 0) + (u.get("output", 0) or 0))


def _cache_write_cost(u: dict, model: str, pricing: dict, provenance: str) -> float:
    """Dollars this turn spent WRITING its prompt cache, or 0.0 when we
    cannot say honestly.

    Only rate_card_estimate rows qualify: that cost was computed by us, from
    this exact formula, so decomposing it back out is arithmetic rather than
    inference. A subscription_equivalent row's figure is Claude Code's own
    total_cost_usd - one opaque number bundling thinking and tool loops - and
    splitting it would be inventing a breakdown the provider never gave us.
    """
    if provenance != prov.RATE_CARD_ESTIMATE:
        return 0.0
    written = int(u.get("cache_creation", 0) or 0)
    if not written:
        return 0.0
    card = price_for(model, pricing)
    if not card:
        return 0.0
    mult = (card.get("cache") or DEFAULT_CACHE)["write_mult"]
    return written * card["input"] * mult / 1_000_000


def _recorded_provenance(u: dict):
    """The provenance SOURCE recorded on a usage row at turn time, if any. A
    recorded value is authoritative and immutable - a later rate-card/config
    edit must never rewrite an already-recorded turn's provenance."""
    rec = u.get("cost_provenance")
    if isinstance(rec, dict) and rec.get("source") in prov.PROVENANCE_STATES:
        return rec["source"]
    return None


def iter_cost_events(con, *, code_slug=CODE_SLUG, chat_id=None, pricing=None):
    """Yield one CostEvent per priced action across messages + voice_usage.

    Single scan used by both the per-chat and cumulative surfaces. Pass
    ``chat_id`` to scope to one chat; omit it for all chats.

    ``pricing`` (the live price table) does two jobs. It classifies the
    provenance of historical rows that predate per-turn provenance capture,
    where a row that recorded its own provenance keeps it regardless of the
    table. It also prices cache writes, which are computed on read rather than
    stamped, so a caller that omits it prices those against DEFAULT_PRICING and
    reads $0.00 for any model the operator priced by override."""
    pricing = DEFAULT_PRICING if pricing is None else pricing
    # base_url/api_key_env ride along because they are part of resolving a
    # historical row's provenance: a keyless loopback seat is self-hosted,
    # so its untagged rows read as a declared $0 rather than "unknown".
    parts = {r["slug"]: dict(r) for r in con.execute(
        "SELECT slug, name, provider, model, base_url, api_key_env "
        "FROM participants")}

    mq = "SELECT chat_id, speaker, usage_json, created_at FROM messages " \
         "WHERE usage_json IS NOT NULL"
    margs: tuple = ()
    if chat_id is not None:
        mq += " AND chat_id=?"
        margs = (chat_id,)
    for r in con.execute(mq, margs):
        try:
            u = json.loads(r["usage_json"])
        except (ValueError, TypeError):
            continue
        cost = u.get("cost")
        has_cost = cost is not None
        tokens = _tokens(u)
        if r["speaker"] == code_slug:
            # verified_model is preferred when present, else the alias/model
            # the turn recorded, else the slug. Read defensively, because rows
            # written before that field existed do not carry it at all.
            model = u.get("verified_model") or u.get("model") or code_slug
            auth = (u.get("auth") or "").strip().lower()
            category = _AUTH_CATEGORY.get(auth, CAT_UNKNOWN)
            provenance = (_recorded_provenance(u)
                          or _AUTH_PROVENANCE.get(auth, prov.UNKNOWN))
            yield CostEvent(
                chat_id=r["chat_id"], ts=r["created_at"], source=SOURCE_CODE,
                category=category, provenance=provenance, provider="anthropic",
                model=model, speaker=code_slug, cost=cost or 0.0, tokens=tokens,
                has_cost=has_cost,
                cache_read=int(u.get("cache_read", 0) or 0),
                cache_creation=int(u.get("cache_creation", 0) or 0),
                cache_write_cost=_cache_write_cost(u, model, pricing, provenance))
        else:
            p = parts.get(r["speaker"], {})
            model = u.get("model") or p.get("model") or r["speaker"]
            provider = p.get("provider") or "unknown"
            # Cash axis is unchanged (resident API turns = metered). Provenance
            # is the honest refinement: a figure computed from the local price
            # table is a rate_card_estimate, never a billed amount - unless the
            # row recorded a different provenance (e.g. a self_hosted seat), in
            # which case that recorded value wins and stays immutable.
            provenance = (_recorded_provenance(u)
                          or provenance_for(model, pricing,
                                            base_url=p.get("base_url"),
                                            api_key_env=p.get("api_key_env"))["source"])
            yield CostEvent(
                chat_id=r["chat_id"], ts=r["created_at"], source=SOURCE_NORMAL,
                category=CAT_METERED, provenance=provenance, provider=provider,
                model=model, speaker=r["speaker"], cost=cost or 0.0,
                tokens=tokens, has_cost=has_cost,
                cache_read=int(u.get("cache_read", 0) or 0),
                cache_creation=int(u.get("cache_creation", 0) or 0),
                cache_write_cost=_cache_write_cost(u, model, pricing, provenance))

    vq = "SELECT chat_id, kind, cost, created_at FROM voice_usage"
    vargs: tuple = ()
    if chat_id is not None:
        vq += " WHERE chat_id=?"
        vargs = (chat_id,)
    for r in con.execute(vq, vargs):
        source = SOURCE_TTS if r["kind"] == "tts" else SOURCE_STT
        cost = r["cost"]
        # Voice cost is computed from the ElevenLabs rate card, not a
        # provider-returned invoice → a rate_card_estimate (metered cash).
        yield CostEvent(
            chat_id=r["chat_id"], ts=r["created_at"], source=source,
            category=CAT_METERED, provenance=prov.RATE_CARD_ESTIMATE,
            provider="elevenlabs", model=r["kind"], speaker="voice",
            cost=cost or 0.0, tokens=0, has_cost=cost is not None)

    uq = ("SELECT chat_id, model, input_tokens, output_tokens, cost, provenance, "
          "created_at FROM utility_usage")
    uargs: tuple = ()
    if chat_id is not None:
        uq += " WHERE chat_id=?"
        uargs = (chat_id,)
    for r in con.execute(uq, uargs):
        # Provenance recorded ON THE ROW at write time is authoritative and
        # immutable, the same discipline as every other cost source, so a
        # later rate-card edit can't rewrite history. Only a row written
        # before that column existed (NULL) falls back to resolving against
        # the live table, same rate-card-estimate honesty this path always
        # had.
        model = r["model"] or "unknown"
        recorded = r["provenance"] if r["provenance"] in prov.PROVENANCE_STATES else None
        provenance = recorded or provenance_for(model, pricing)["source"]
        cost = r["cost"]
        yield CostEvent(
            chat_id=r["chat_id"], ts=r["created_at"], source=SOURCE_UTILITY,
            category=CAT_METERED, provenance=provenance,
            provider=model_family(model), model=model, speaker="utility",
            cost=cost or 0.0,
            tokens=(r["input_tokens"] or 0) + (r["output_tokens"] or 0),
            has_cost=cost is not None)


def _blank_totals():
    return {c: 0.0 for c in CATEGORIES}


def _group_add(bucket: dict, key, label, e: CostEvent):
    g = bucket.setdefault(key, {
        "key": str(key), "label": label,
        **{c: 0.0 for c in CATEGORIES},
        "tokens": 0, "not_tracked": False,
        # Cache health rides EVERY axis, because this is the single
        # accumulator all of them go through. Cheap here, and the comparison
        # that mattered in the prompt-cache regression (one seat's read:write
        # ratio against a neighbouring seat's on the same API) is a
        # by_producer_model row against its neighbour.
        "cache_read": 0, "cache_creation": 0, "cache_write_cost": 0.0})
    if e.has_cost:
        g[e.category] += e.cost
    else:
        g["not_tracked"] = True
    g["tokens"] += e.tokens
    g["cache_read"] += e.cache_read
    g["cache_creation"] += e.cache_creation
    g["cache_write_cost"] += e.cache_write_cost


def cache_health(read: int, written: int) -> dict:
    """Read:write ratio and a plain word for it.

    A prompt cache only pays off when a prefix is written ONCE and read MANY
    times: a write costs 1.25x input, a read 0.1x, so a write is 12.5x a read.
    Writing about as often as you read means the prefix is being invalidated
    and re-sent. That is what the live regression looked like: one seat sat
    below 1:1 while a neighbouring seat on the same API read many times per
    write, and nothing surfaced the difference.

    ``ratio`` is None when nothing was written - no activity is not a 0:0
    problem, and must not render as one.
    """
    if not written:
        return {"ratio": None, "verdict": "none" if not read else "no_writes"}
    ratio = read / written
    if ratio >= 3.0:
        verdict = "good"
    elif ratio >= 1.0:
        verdict = "watch"
    else:
        verdict = "poor"     # re-writing more than it reads back
    return {"ratio": ratio, "verdict": verdict}


def _grouped_list(bucket: dict):
    out = list(bucket.values())
    for g in out:
        g["cache"] = cache_health(g["cache_read"], g["cache_creation"])
    out.sort(key=lambda g: (g[CAT_METERED] + g[CAT_SUBSCRIPTION] + g[CAT_UNKNOWN]),
             reverse=True)
    return out


def summarize(events, *, since=None, until=None, chat_titles=None):
    """Aggregate CostEvents into totals + breakdowns for one time window.

    ``since``/``until`` are epoch seconds (``since`` inclusive, ``until``
    exclusive); either may be None for open-ended. Returns a JSON-able dict.
    """
    chat_titles = chat_titles or {}
    events = [e for e in events
              if (since is None or e.ts >= since)
              and (until is None or e.ts < until)]

    totals = _blank_totals()
    tokens = 0
    cache_read = cache_written = 0
    cache_write_cost = 0.0
    by_source, by_provider, by_model, by_chat = {}, {}, {}, {}
    # by_party groups on the SPEAKER - the conversation party or usage producer
    # that incurred the cost (a participant slug, a summoned guest's slug,
    # "voice", "utility", …). It is keyed on the event's own speaker field, so a
    # brand-new participant, guest or integration flows into the split with no
    # code change here - nothing is hardcoded to a fixed roster.
    by_party = {}
    # by_producer is the like-with-like landing axis: chat vs
    # agents/guests vs voice vs utility, so a coding agent's spend is a bar of
    # its own and never inflates a chat participant's. by_producer_model keeps
    # the useful model drilldown but scopes each model to its producer, so the
    # SAME concrete model (e.g. claude-sonnet-5) used by the Claude chat seat and
    # by a Claude Code guest lands in two separate rows, never one merged bucket.
    by_producer = {}
    by_producer_model = {}
    not_tracked = set()

    for e in events:
        if e.has_cost:
            totals[e.category] += e.cost
        else:
            not_tracked.add(e.source)
        tokens += e.tokens
        cache_read += e.cache_read
        cache_written += e.cache_creation
        cache_write_cost += e.cache_write_cost
        _group_add(by_source, e.source, SOURCE_LABELS.get(e.source, e.source), e)
        _group_add(by_party, e.speaker, e.speaker, e)
        _group_add(by_provider, e.provider, e.provider, e)
        _group_add(by_model, e.model, e.model, e)
        producer = e.producer
        _group_add(by_producer, producer,
                   PRODUCER_LABELS.get(producer, producer), e)
        # Key on producer+model so like compares with like; label names both so
        # "claude-sonnet-5 · Agents" reads distinctly from the chat row.
        _group_add(by_producer_model, (producer, e.model),
                   f"{e.model} · {PRODUCER_LABELS.get(producer, producer)}", e)
        if e.chat_id is not None:
            _group_add(by_chat, e.chat_id,
                       chat_titles.get(e.chat_id, f"Chat {e.chat_id}"), e)

    return {
        "totals": totals,
        "tokens": tokens,
        "events": len(events),
        # The window's prompt-cache health. `write_cost` is the metered
        # spend attributable to WRITING the cache, and `write_cost_share` its
        # fraction of metered spend. A churning prefix can push that share to
        # most of a seat's metered bill, and nothing on this page said so
        # before. Both cover rate-card-priced events only; a
        # subscription-equivalent figure is the SDK's own opaque total and is
        # never split (see _cache_write_cost).
        "cache": {
            "read": cache_read,
            "written": cache_written,
            **cache_health(cache_read, cache_written),
            "write_cost": cache_write_cost,
            "write_cost_share": (cache_write_cost / totals[CAT_METERED]
                                 if totals[CAT_METERED] else 0.0),
        },
        "by_source": _grouped_list(by_source),
        # Per conversation party / usage producer (speaker-keyed, fully
        # dynamic - see the definition above): the split the in-call
        # diagnostic reports after the metered total.
        "by_party": _grouped_list(by_party),
        "by_provider": _grouped_list(by_provider),
        "by_model": _grouped_list(by_model),
        # Producer-aware axes: the landing "mostly spent on" draws
        # by_producer_model so a guest's spend is never merged into a chat
        # participant's model bar; by_producer is the coarse chat-vs-agents
        # split. Both reconcile to the same totals as by_model - same events,
        # grouped differently - so there is no double count.
        "by_producer": _grouped_list(by_producer),
        "by_producer_model": _grouped_list(by_producer_model),
        "by_chat": _grouped_list(by_chat),
        "not_tracked": sorted(not_tracked),
    }


def daily_series(events, *, since=None, until=None):
    """Per-day category totals (ascending by date) for the trend chart."""
    days: dict[str, dict] = {}
    for e in events:
        if since is not None and e.ts < since:
            continue
        if until is not None and e.ts >= until:
            continue
        if not e.has_cost:
            continue
        day = _dt.date.fromtimestamp(e.ts).isoformat()
        d = days.setdefault(day, {"day": day, **_blank_totals()})
        d[e.category] += e.cost
    return [days[k] for k in sorted(days)]


def auth_provenance(cfg: dict) -> dict:
    """How NEW guest turns will be billed right now, for the UI banner. This is
    the CURRENT config, not a claim about historical rows (those carry their
    own recorded auth, or count as unknown)."""
    api_key = bool(cfg.get("code_use_api_key"))
    if api_key:
        return {
            "code_use_api_key": True,
            "mode": "api_key",
            "label": "Claude Code guest → metered API key",
            "note": "New guest turns bill pay-as-you-go to ANTHROPIC_API_KEY "
                    "(real cash). Historical turns are classified by the auth "
                    "each recorded - turns from before provenance capture show "
                    "as unknown.",
        }
    return {
        "code_use_api_key": False,
        "mode": "subscription",
        "label": "Claude Code guest → subscription or OAuth login",
        "note": "New guest turns ride the user's subscription; Claude Code's "
                "reported cost is an API-equivalent estimate, not incremental "
                "cash. Shown as subscription-equivalent, never billed spend.",
    }
