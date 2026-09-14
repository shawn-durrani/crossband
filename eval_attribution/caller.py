"""One probe, one model call. Keyless callers return text None so a run
without keys reports missing_key rather than crashing, matching llm_util."""

import hashlib
import os
import time

from backend.llm_util import UtilityCompletion, _client, model_family


async def real_call(model: str, system: str, messages, max_tokens: int = 40,
                    timeout: float | None = None) -> UtilityCompletion:
    family = model_family(model)
    start = time.monotonic()
    if family == "openai":
        if not os.environ.get("OPENAI_API_KEY"):
            return UtilityCompletion(text=None)
        resp = await _client("openai").responses.create(
            model=model, max_output_tokens=max_tokens, instructions=system,
            input=messages, timeout=timeout)
        usage = getattr(resp, "usage", None)
        return UtilityCompletion(
            text=(resp.output_text or "").strip(),
            input_tokens=getattr(usage, "input_tokens", 0) if usage else 0,
            output_tokens=getattr(usage, "output_tokens", 0) if usage else 0,
            latency_s=time.monotonic() - start)
    if not os.environ.get("ANTHROPIC_API_KEY"):
        return UtilityCompletion(text=None)
    resp = await _client("anthropic").messages.create(
        model=model, max_tokens=max_tokens, system=system, messages=messages,
        timeout=timeout)
    text = next((b.text for b in resp.content if b.type == "text"), "").strip()
    usage = getattr(resp, "usage", None)
    return UtilityCompletion(
        text=text,
        input_tokens=getattr(usage, "input_tokens", 0) if usage else 0,
        output_tokens=getattr(usage, "output_tokens", 0) if usage else 0,
        latency_s=time.monotonic() - start)


def mock_answer(fixture, probe, variant: str) -> str:
    """A keyless stand-in that acts out the hypothesis under test: with bare
    own turns (`current`) a seat cannot see itself, so a probe whose answer is
    the seat itself comes back naming another member; the labelled shapes
    answer correctly except a fixed small fraction chosen by id hash. Its
    numbers are a demonstration of the report, never evidence."""
    expected = probe.resolve_expected()
    if variant == "current" and probe.kind == "who" and expected == probe.seat:
        other = next((m["name"] for m in fixture.roster if m["slug"] != probe.seat),
                     fixture.user_name)
        return other
    if variant == "current" and probe.kind == "yesno" and expected == "no" \
            and "you" in probe.question.lower():
        return "yes"
    h = int(hashlib.sha256(f"{fixture.id}:{probe.tag}:{variant}".encode()).hexdigest(), 16)
    if (h % 100) < 5:
        return "no" if probe.kind == "yesno" else fixture.user_name
    if probe.kind == "yesno":
        return expected
    return fixture.user_name if expected == "user" else fixture.names[expected]


async def mock_call(fixture, probe, variant, model, system, messages, **_):
    text = mock_answer(fixture, probe, variant)
    return UtilityCompletion(text=text, input_tokens=len(system) // 4 + 200,
                             output_tokens=3, latency_s=0.01)
