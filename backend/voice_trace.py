"""Per-turn voice latency instrumentation.

WHAT this is: a small, durable, privacy-safe store of stage-level timings for
live voice turns, plus the aggregation behind the development diagnostics
surface. A "turn" is one human utterance and the spoken replies it triggers;
each turn carries a client-owned correlation id (`turn_id`) so its stages line
up across capture → transcription → dispatch → model generation → TTS →
playback.

WHY it lives server-side: the browser is the only vantage point that sees the
WHOLE end-to-end timeline (it detects end-of-speech, drives the STT/TTS
sockets, and starts audible playback), so it measures the durations. But a
console log vanishes on refresh and can't be aggregated. Persisting the
client's marks here gives a reproducible p50/p95 breakdown segmented by model
and TTS provider, retained long enough to diagnose outliers.

THE PRIVACY FLOOR (non-negotiable, enforced here not just documented): a trace
is CONTENT-FREE. `sanitize()` accepts only allowlisted stage names, numeric
durations, and bounded provider/model/voice/slug identifiers - never a
transcript, a reply, or any spoken words. Anything not on the allowlist is
dropped. So even a buggy or hostile client cannot turn this into a transcript
log; the worst it can do is submit uninteresting numbers.
"""

# The stages we know how to measure, each a DURATION in milliseconds between two
# points on the voice timeline. Kept as an explicit allowlist so the ingest path
# is closed by default - an unknown stage name is dropped, never stored. Every
# stage here is actually emitted by the client (frontend/src/voiceTrace.js); we
# deliberately do NOT list stages we can't measure, so the summary endpoint
# never advertises a permanently-empty column.
ALLOWED_STAGES = {
    # capture: sustained-speech end / mute detection → transcript finalized
    # (the speech-to-text round trip, batch or realtime).
    "end_of_speech_to_final",
    # transcript finalized → first streamed token of the round. Bundles client
    # dispatch (which is synchronous with finalisation, so not separately
    # measurable client-side) + network + group-turn orchestration + model TTFT.
    "final_to_first_token",
    # a speaker's first token → first TTS audio bytes for that speaker
    "first_token_to_first_audio",
    # how long a ready reply waited before the shared audio sink was handed to
    # it (multi-agent serialization: speaker N queues behind speaker N-1).
    # Emitted for 2nd+ speakers only - the lead reply has nobody ahead of it.
    "playback_queue_wait",
    # a speaker's first audio bytes → its audio becoming AUDIBLE (queue + decode)
    "first_audio_to_playback",
    # headline number: end-of-speech → first audible word of the first reply
    "end_to_end_first_audio",
}

# Stages the SERVER itself measures and inserts directly via
# db.insert_voice_trace (backend/engine.py's round loop), correlated by the
# same client-owned turn_id - but never accepted through the public
# POST /api/voice/trace ingest (sanitize_stage below only recognizes
# ALLOWED_STAGES), so a client can never fabricate server-side attribution.
# Together these split final_to_first_token's client-side bundle into: how
# long context assembly (DB reads, tool-definition building, memory reads)
# took before the provider call even started, vs. the provider's own raw
# time-to-first-token (which includes any thinking/deliberation - see
# backend/providers.py's reasoning-policy handling). Emitted only for the
# round's FIRST responder - the one the client's stopwatch is timing.
SERVER_STAGES = {
    # round dispatch (this responder's turn starting) → the provider call
    # actually being issued: DB reads, tool-definition assembly, and (only
    # when this round runs the memory fetch) the memory summary + ambient
    # recall reads.
    "server_context_assembly",
    # the provider call being issued → this responder's first streamed text
    # delta. The provider's raw TTFT, thinking/deliberation included.
    "server_provider_first_token",
    # time specifically spent awaiting the memory service's GET /summary,
    # when this round performed that fetch (first responder only; cached for
    # the rest of the round).
    "server_memory_summary_wait",
    # time specifically spent awaiting the memory service's POST /recall
    # (ambient auto-recall for the latest user message), when it ran.
    "server_memory_recall_wait",
}

# The full recognized set for aggregation (voice_trace.aggregate, below) -
# client-submitted stages plus server-measured ones. sanitize_stage/
# sanitize_turn deliberately check ONLY ALLOWED_STAGES, not this union: the
# public ingest endpoint must never accept a client's claim about server-side
# timing.
ALL_STAGES = ALLOWED_STAGES | SERVER_STAGES

# ---- measurement epoch ----
#
# Stage SEMANTICS change over time: a later change backdated speech_end by the
# VAD silence window, so turns measured before it read ~2s faster than the user
# perceived. Averaging across such a change is statistically meaningless, and
# the live consequence is that a blended 24h window makes a fix look like a
# regression: the seats read the blend and report that latency has "gotten
# worse" on the very release that improved it. The epoch is the timestamp when
# the CURRENT semantics first ran on THIS install (stamped once at startup,
# then stable); summaries exclude older rows by default and say how many they
# excluded. Bump SEMANTICS_VERSION whenever a stage's meaning changes again: a
# fresh key stamps a fresh epoch.

SEMANTICS_VERSION = 2  # v1: clocks before speech_end was backdated; v2: honest
_EPOCH_KEY = f"voice_trace_epoch_v{SEMANTICS_VERSION}"


def ensure_epoch(con) -> float:
    """Return (stamping on first call) when the current stage semantics
    first ran on this install."""
    from . import db  # local import: avoid a module-load cycle with db.py
    val = db.get_setting(con, _EPOCH_KEY, "")
    if val:
        try:
            return float(val)
        except ValueError:
            pass  # unreadable stamp: re-stamp below rather than crash
    now = db.now()
    db.set_setting(con, _EPOCH_KEY, str(now))
    con.commit()
    return now


# One-line interpretation per stage, shipped INSIDE the diagnostic payload:
# the numbers travel with their framing, so a model (or human) quoting them
# inherits "by design" where it applies instead of inventing alarm. Static,
# content-free text - the privacy floor is untouched.
STAGE_NOTES = {
    "end_of_speech_to_final":
        "includes the user's configured end-of-turn pause (~2s by default) "
        "plus transcription finalization - not pure system time",
    "final_to_first_token":
        "final transcript to first model token: network transit + "
        "orchestration + the model starting to answer",
    "server_context_assembly":
        "server-side context build before the model call (DB reads, tool "
        "definitions, memory waits)",
    "server_provider_first_token":
        "the model's own time to first visible token, thinking included - "
        "governed by the seat's reasoning-effort setting",
    "server_memory_recall_wait":
        "the round's residual wait on memory recall - near zero when a "
        "speech-end prewarm was adopted",
    "server_memory_summary_wait":
        "fetch of the precomputed memory summary - expected near zero",
    "first_token_to_first_audio":
        "TTS synthesis from first text to first audio bytes",
    "playback_queue_wait":
        "a LATER speaker's finished audio waiting for the current speaker "
        "to stop talking - multi-voice serialization by design, not a "
        "responsiveness defect",
    "first_audio_to_playback":
        "decode + buffering until audible; for later speakers this also "
        "absorbs the by-design queue wait",
    "end_to_end_first_audio":
        "stop-talking to the first audible word of the FIRST reply, "
        "end-of-turn pause included",
}

_MAX_STR = 64          # bound every identifier string we persist
_MAX_MS = 3_600_000    # 1 hour - anything larger is a clock glitch, not a stage
_MAX_STAGES_PER_POST = 200  # a single turn can't plausibly report more than this


def _clean_str(v):
    """Coerce to a short, single-line, plain identifier. Never trusted content -
    just a provider/model/slug label - so we hard-cap length and strip control
    characters rather than trying to be clever."""
    if not isinstance(v, str):
        return ""
    return v.replace("\n", " ").replace("\r", " ").strip()[:_MAX_STR]


def sanitize_stage(raw):
    """Validate ONE incoming stage record. Returns a normalized dict ready for
    db.insert_voice_trace, or None if it fails the allowlist / numeric checks.

    This is the privacy + integrity gate: it is the ONLY way rows enter the
    table, and it accepts no free-text fields at all."""
    if not isinstance(raw, dict):
        return None
    stage = raw.get("stage")
    if stage not in ALLOWED_STAGES:
        return None
    ms = raw.get("ms")
    if isinstance(ms, bool) or not isinstance(ms, (int, float)):
        return None
    ms = float(ms)
    if ms != ms or ms < 0 or ms > _MAX_MS:  # NaN / negative / absurd → drop
        return None
    return {
        "stage": stage,
        "ms": ms,
        "provider": _clean_str(raw.get("provider", "")),
        "model": _clean_str(raw.get("model", "")),
        "tts_provider": _clean_str(raw.get("tts_provider", "")),
        "speaker": _clean_str(raw.get("speaker", "")),
    }


def sanitize_turn(payload):
    """Validate a whole turn's trace POST. Returns (turn_id, chat_id, [stages]).
    turn_id is required and bounded; chat_id is optional (may be null for a turn
    that never reached a chat). Silently drops any stage that doesn't sanitize -
    partial telemetry is fine, a rejected turn would just lose the good rows too."""
    if not isinstance(payload, dict):
        return None, None, []
    turn_id = _clean_str(payload.get("turn_id", ""))
    if not turn_id:
        return None, None, []
    chat_id = payload.get("chat_id")
    if not isinstance(chat_id, int) or isinstance(chat_id, bool):
        chat_id = None
    raw_stages = payload.get("stages")
    if not isinstance(raw_stages, list):
        return turn_id, chat_id, []
    stages = []
    for raw in raw_stages[:_MAX_STAGES_PER_POST]:
        s = sanitize_stage(raw)
        if s is not None:
            stages.append(s)
    return turn_id, chat_id, stages


# ---------- server-side stage recording ----------

def record_server_stage(con, turn_id, chat_id, stage, ms, *, provider="",
                        model="", speaker=""):
    """Persist ONE server-measured stage (backend/engine.py's round loop) - the
    trusted counterpart to sanitize_stage for numbers the server itself timed
    with a monotonic clock, never client input. Still defensive: `stage` must
    be one we know about (SERVER_STAGES) and `ms` is clamped into the same
    sane bounds as client-submitted stages, so a clock glitch or a future
    refactor can't silently write nonsense into the diagnostics table.
    Best-effort by design - a caller wraps this and never lets a tracing
    failure break the actual voice round; see engine.py's usage."""
    if not turn_id or stage not in SERVER_STAGES:
        return
    if not isinstance(ms, (int, float)) or isinstance(ms, bool):
        return
    ms = float(ms)
    if ms != ms or ms < 0:  # NaN / negative → drop
        return
    ms = min(ms, _MAX_MS)
    from . import db  # local import: avoid a module-load cycle with db.py
    db.insert_voice_trace(con, turn_id, chat_id, stage, ms,
                          provider=_clean_str(provider), model=_clean_str(model),
                          speaker=_clean_str(speaker))


# ---------- aggregation (the diagnostics surface) ----------

def _percentile(sorted_vals, pct):
    """Nearest-rank percentile of an already-sorted list. Small-sample honest:
    p95 of 3 points is just the top point, not an interpolation pretending to
    precision we don't have."""
    if not sorted_vals:
        return None
    if len(sorted_vals) == 1:
        return sorted_vals[0]
    rank = pct / 100 * (len(sorted_vals) - 1)
    lo = int(rank)
    hi = min(lo + 1, len(sorted_vals) - 1)
    frac = rank - lo
    return sorted_vals[lo] * (1 - frac) + sorted_vals[hi] * frac


def _summarize(values):
    vals = sorted(values)
    n = len(vals)
    return {
        "count": n,
        "p50": round(_percentile(vals, 50), 1) if n else None,
        "p95": round(_percentile(vals, 95), 1) if n else None,
        "max": round(vals[-1], 1) if n else None,
    }


def aggregate(rows):
    """Turn raw trace rows into a stage-level p50/p95 breakdown, plus segments
    by model (for the generation stages) and by TTS provider (for the voice
    stages). `rows` is a list of dicts as returned by db.get_voice_traces.

    Shape:
      {
        "turns": <distinct turn_id count>,
        "stages": { <stage>: {count, p50, p95, max,
                              "by_model": {<model>: {...}},
                              "by_tts_provider": {<provider>: {...}}} },
      }
    """
    by_stage = {}
    turn_ids = set()
    for r in rows:
        turn_ids.add(r.get("turn_id"))
        stage = r.get("stage")
        if stage not in ALL_STAGES:
            continue
        ms = r.get("ms")
        if not isinstance(ms, (int, float)):
            continue
        b = by_stage.setdefault(stage, {"all": [], "by_model": {}, "by_tts": {}})
        b["all"].append(float(ms))
        model = r.get("model") or ""
        if model:
            b["by_model"].setdefault(model, []).append(float(ms))
        tts = r.get("tts_provider") or ""
        if tts:
            b["by_tts"].setdefault(tts, []).append(float(ms))
    stages = {}
    for stage, b in by_stage.items():
        entry = _summarize(b["all"])
        entry["by_model"] = {m: _summarize(v) for m, v in sorted(b["by_model"].items())}
        entry["by_tts_provider"] = {t: _summarize(v) for t, v in sorted(b["by_tts"].items())}
        stages[stage] = entry
    return {"turns": len(turn_ids), "stages": stages}
