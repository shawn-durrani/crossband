"""Keyless stand-ins for --mock and the tests. The merged stand-in answers
with the graded verdict, wrong on a fixed few by id hash, so a report has
misses to show. The today stand-in answers each live prompt with the graded
verdict for that axis alone. Their numbers say nothing about any model."""

import hashlib
import json

from backend.llm_util import UtilityCompletion


def _flip(fx) -> bool:
    h = int(hashlib.sha256(fx.id.encode()).hexdigest(), 16)
    return (h % 100) < 8


def merged_reply(fx) -> str:
    e = dict(fx.expected)
    if _flip(fx):
        e = {**e, "research": "none" if e["research"] == "more" else "more"}
    return json.dumps(e)


def today_reply(fx, axis: str) -> str:
    e = fx.expected
    if axis == "mode_command":
        return json.dumps({"mode_command": e["mode_command"]})
    if axis == "introductions":
        return json.dumps({"introductions": e["introductions"],
                           "departures": e["departures"], "aliases": e["aliases"]})
    if axis == "corrections":
        return json.dumps({"corrections": e["corrections"]})
    return json.dumps({"changes": e["depth"]})


class MockCaller:
    """Answers a prompt by recognising the fixture it was built from."""

    def __init__(self, fixtures):
        self._by_text = {fx.text[:1200]: fx for fx in fixtures}

    def _fixture(self, prompt: str):
        tail = prompt.rsplit("Message: ", 1)[-1]
        return self._by_text.get(tail[:1200])

    async def __call__(self, prompt: str, axis: str = "merged") -> UtilityCompletion:
        fx = self._fixture(prompt)
        if fx is None:
            return UtilityCompletion(text="{}")
        text = merged_reply(fx) if axis == "merged" else today_reply(fx, axis)
        return UtilityCompletion(text=text, input_tokens=400, output_tokens=60,
                                 latency_s=0.01)
