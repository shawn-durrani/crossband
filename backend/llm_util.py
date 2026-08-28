"""Small non-streamed utility completions (rolling summaries, auto-titles,
project distillation). Chat-side and lab-routable: the utility model is picked
by name - claude-* routes to Anthropic, gpt-*/o* to OpenAI's Responses API.
Returns None when the needed key is missing so callers degrade gracefully."""

import asyncio
import logging
import os
import time
from dataclasses import dataclass


def _is_openai_model(model: str) -> bool:
    m = (model or "").lower()
    return m.startswith(("gpt-", "o1", "o3", "o4", "chatgpt"))


def model_family(model: str) -> str:
    """Coarse routing family for a model name ("openai" or "anthropic") - the
    same rule the completion helpers route on, exposed so callers that need to
    reason about model families (e.g. the offline critic eval harness, which
    reports recall by author/critic family pairing) don't duplicate it."""
    return "openai" if _is_openai_model(model) else "anthropic"


log = logging.getLogger("crossband.llm_util")

@dataclass
class UtilityCompletion:
    """A utility completion plus the telemetry the offline critic eval harness
    needs (token counts, wall-clock latency, timeout signal). `text` is None
    when the needed key is missing OR the call exceeded `timeout`; check
    `timed_out` to tell those apart. A None text means no call went out, so
    there is nothing to price and nothing to log."""
    text: str | None
    input_tokens: int = 0
    output_tokens: int = 0
    latency_s: float = 0.0
    timed_out: bool = False


# One async client per provider per process (mirroring providers.py's
# per-key cache): a fresh AsyncOpenAI/AsyncAnthropic per utility call paid a
# new TLS handshake for every rolling summary, auto-title and distillation.
# Key checks still run FIRST, so keyless callers degrade to None before any
# client is ever built - and tests stay keyless.
_clients: dict = {}


def _client(family: str):
    if family not in _clients:
        if family == "openai":
            from openai import AsyncOpenAI
            _clients[family] = AsyncOpenAI()
        else:
            from anthropic import AsyncAnthropic
            _clients[family] = AsyncAnthropic()
    return _clients[family]


async def _call_model(prompt: str, model: str, max_tokens: int):
    if _is_openai_model(model):
        if not os.environ.get("OPENAI_API_KEY"):
            return None, 0, 0
        resp = await _client("openai").responses.create(
            model=model,
            max_output_tokens=max_tokens,
            input=[{"role": "user", "content": prompt}],
        )
        usage = getattr(resp, "usage", None)
        in_tok = getattr(usage, "input_tokens", 0) if usage else 0
        out_tok = getattr(usage, "output_tokens", 0) if usage else 0
        return (resp.output_text or "").strip(), in_tok, out_tok
    if not os.environ.get("ANTHROPIC_API_KEY"):
        return None, 0, 0
    resp = await _client("anthropic").messages.create(
        model=model,
        max_tokens=max_tokens,
        messages=[{"role": "user", "content": prompt}],
    )
    text = next((b.text for b in resp.content if b.type == "text"), "").strip()
    usage = getattr(resp, "usage", None)
    in_tok = getattr(usage, "input_tokens", 0) if usage else 0
    out_tok = getattr(usage, "output_tokens", 0) if usage else 0
    return text, in_tok, out_tok


async def utility_complete_with_usage(prompt: str, cfg: dict, max_tokens: int = 2000,
                                       model: str | None = None,
                                       timeout: float | None = None) -> UtilityCompletion:
    """The routing helper every utility call goes through, with usage,
    latency and an optional per-call timeout (seconds). `model` overrides
    cfg["utility_model"] so a caller (the critic eval harness) can hold cfg
    fixed while sweeping models.

    Callers that spend the owner's money should use utility_complete_logged
    below, which records the cost. This one is for the offline eval harness,
    which spends nothing the Spend page should show."""
    model = model or cfg.get("utility_model") or "claude-haiku-4-5"
    start = time.monotonic()
    try:
        if timeout is not None:
            text, in_tok, out_tok = await asyncio.wait_for(
                _call_model(prompt, model, max_tokens), timeout=timeout)
        else:
            text, in_tok, out_tok = await _call_model(prompt, model, max_tokens)
    except asyncio.TimeoutError:
        return UtilityCompletion(text=None, latency_s=time.monotonic() - start,
                                  timed_out=True)
    return UtilityCompletion(text=text, input_tokens=in_tok, output_tokens=out_tok,
                              latency_s=time.monotonic() - start)


def price_utility_call(model: str, result: "UtilityCompletion", cfg: dict):
    """(cost, provenance) for one utility call, resolved AT CALL TIME.

    Shared by chat_memory._run_utility and utility_complete_logged so the two
    logging paths cannot price the same call differently. A later rate-card
    edit must never rewrite what a recorded call's cost meant when it was
    made, which is why both stamp rather than recompute."""
    from . import config as config_mod
    pricing = cfg.get("pricing") or config_mod.DEFAULT_PRICING
    usage = {"input": result.input_tokens, "output": result.output_tokens}
    cost = config_mod.compute_cost(model, usage, pricing)
    return cost, config_mod.provenance_for(model, pricing)["source"]


def _write_utility_row(chat_id, kind, model, result, cost, provenance):
    """Own connection, own commit, own thread. Called through asyncio.to_thread
    because sqlite connections are not shareable across threads and
    db.connect() sets busy_timeout = 5000, so a synchronous insert under
    contention could stall the event loop for five seconds."""
    from . import db
    con = db.connect()
    try:
        db.log_utility_usage(con, chat_id, kind, model, result.input_tokens,
                             result.output_tokens, cost, provenance=provenance)
        con.commit()
    finally:
        con.close()


async def utility_complete_logged(chat_id, kind: str, prompt: str, cfg: dict,
                                  max_tokens: int = 2000) -> str | None:
    """A utility call whose cost reaches the Spend page.

    The scan paths in introductions.py and mismatch.py fire real metered
    calls and had no cost record, so half the utility bucket was invisible.
    They have a chat_id but no open connection, so this opens its own.

    A logging failure is swallowed on purpose. Both callers wrap their whole
    body in a try/except that abandons the rest of the turn: in
    introductions.scan_user_turn a raise here would skip the remaining scans
    and emit a false `scan_error` verdict, and in mismatch.check_turn it
    would lose a real mismatch flag. A spend-telemetry fault must not become
    a room-mode regression. The call already cost money either way, so the
    reply is returned regardless."""
    model = cfg.get("utility_model") or "claude-haiku-4-5"
    result = await utility_complete_with_usage(prompt, cfg, max_tokens=max_tokens,
                                               model=model)
    if result.text is None:
        return None                      # no call went out; nothing to log
    try:
        cost, provenance = price_utility_call(model, result, cfg)
        await asyncio.to_thread(_write_utility_row, chat_id, kind, model,
                                result, cost, provenance)
    except Exception:
        log.debug("utility spend row failed for kind=%s", kind, exc_info=True)
    return result.text
