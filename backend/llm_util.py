"""Small non-streamed utility completions (rolling summaries, auto-titles,
project distillation). Chat-side and lab-routable: the utility model is picked
by name - claude-* routes to Anthropic, gpt-*/o* to OpenAI's Responses API.
Returns None when the needed key is missing so callers degrade gracefully."""

import asyncio
import os
import time
from dataclasses import dataclass


def _is_openai_model(model: str) -> bool:
    m = (model or "").lower()
    return m.startswith(("gpt-", "o1", "o3", "o4", "chatgpt"))


def model_family(model: str) -> str:
    """Coarse routing family for a model name ("openai" or "anthropic") - the
    same rule utility_complete routes on, exposed so callers that need to
    reason about model families (e.g. the offline critic eval harness, which
    reports recall by author/critic family pairing) don't duplicate it."""
    return "openai" if _is_openai_model(model) else "anthropic"


@dataclass
class UtilityCompletion:
    """A utility completion plus the telemetry the offline critic eval harness
    needs (token counts, wall-clock latency, timeout signal). `text` is None
    when the needed key is missing OR the call exceeded `timeout`; check
    `timed_out` to tell those apart. utility_complete() below is unchanged -
    it just discards everything but `.text`."""
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
    """Same routing as utility_complete, plus usage/latency and an optional
    per-call timeout (seconds). `model` overrides cfg["utility_model"] so a
    caller (the critic eval harness) can hold cfg fixed while sweeping models.
    Existing callers are unaffected - utility_complete() still takes neither
    argument and behaves exactly as before."""
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


async def utility_complete(prompt: str, cfg: dict, max_tokens: int = 2000) -> str | None:
    result = await utility_complete_with_usage(prompt, cfg, max_tokens=max_tokens)
    return result.text
