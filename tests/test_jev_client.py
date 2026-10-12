"""The Jev client (backend/jev.py): one request, every question at once.

Pins: the request carries the state, the model and the questions with the
bearer key; an answer is read whole; and any failure is no answer, with the
error named, so every caller falls back to what it did before."""

import asyncio

import httpx

from backend import jev


def _run(handler, monkeypatch):
    monkeypatch.setenv("TYPESAFE_API_KEY", "test-key")

    async def go():
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as c:
            return await jev.ask("the state", {"q": {"type": "choice"}}, client=c)
    return asyncio.run(go())


def test_one_request_carries_the_state_and_every_question(monkeypatch):
    seen = {}

    def handler(request):
        import json
        seen.update(json.loads(request.content), auth=request.headers["authorization"])
        return httpx.Response(200, json={"model": "jev-1.13.0", "usage": {"input_tokens": 9},
                                         "answers": {"q": {"choice": "yes"}}})
    got = _run(handler, monkeypatch)
    assert seen["state"] == "the state" and seen["model"] == jev.MODEL
    assert seen["questions"] == {"q": {"type": "choice"}}
    assert seen["auth"] == "Bearer test-key"
    assert got["answers"] == {"q": {"choice": "yes"}} and got["model"] == "jev-1.13.0"
    assert "error" not in got


def test_any_failure_is_no_answer(monkeypatch):
    got = _run(lambda r: httpx.Response(503), monkeypatch)
    assert got["answers"] == {} and got["error"] == "HTTPStatusError"
