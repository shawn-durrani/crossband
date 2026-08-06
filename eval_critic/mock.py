"""A deterministic, keyless stand-in critic — for smoke-running the harness
end to end (CLI --mock) without spending money or needing API keys. It is NOT
a substitute for a real model run; it never affects the reported recall
numbers you'd cite in a decision (those require --model against a live API).

Behavior: mostly returns the fixture's own expected verdict + its
expected_evidence_section/quote (so the full pipeline — prompt build, parse,
grounding check, scoring — exercises real data), but deterministically misses
a small, fixed fraction of unsafe fixtures (by id hash) so a --mock run
demonstrates what a non-zero miss rate looks like in the report rather than
implying a perfect critic is normal."""

import hashlib
import json
import time

from backend.llm_util import UtilityCompletion


def _miss(fixture_id: str, rate: float) -> bool:
    h = int(hashlib.sha256(fixture_id.encode()).hexdigest(), 16)
    return (h % 1000) / 1000.0 < rate


async def mock_call(prompt, cfg, max_tokens=None, model=None, timeout=None,
                     fixture=None, miss_rate: float = 0.1):
    start = time.monotonic()
    if fixture.expected_verdict == "allow" or _miss(fixture.id, miss_rate):
        payload = {"verdict": "allow", "claim_span": "", "evidence_section": "none",
                   "evidence_quote": ""}
    else:
        payload = {
            "verdict": fixture.expected_verdict,
            "claim_span": fixture.expected_evidence_span,
            "evidence_section": fixture.expected_evidence_section or "summary",
            "evidence_quote": fixture.expected_evidence_quote,
        }
    text = json.dumps(payload)
    # A few synthetic input/output tokens so cost/latency reporting has
    # something non-zero to show in a --mock demo run.
    return UtilityCompletion(
        text=text,
        input_tokens=max(50, len(prompt) // 4),
        output_tokens=max(10, len(text) // 4),
        latency_s=time.monotonic() - start + 0.05,
    )
