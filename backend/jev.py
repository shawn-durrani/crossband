"""The client for TypeSafe's Jev, a fast classifier (#642, #646).

Jev reads a `state` (text) and answers typed questions about it, all at
once, each with a choice, its probabilities and a confidence. The key is
TYPESAFE_API_KEY in .env. Every caller treats a failure, or an answer slower
than its timeout, as no answer, and falls back to what it did before.
"""

import os
import time

import httpx

URL = "https://api.typesafe.ai/v1/systemone"
MODEL = "jev-latest"


def api_key() -> str:
    return os.environ.get("TYPESAFE_API_KEY", "").strip()


async def ask(state, questions, timeout_s=1.0, client=None) -> dict:
    """Ask Jev `questions` about `state`. Returns {answers, model, usage,
    ms}, with answers {} and an `error` when there was no usable reply."""
    t0 = time.monotonic()
    out = {"answers": {}, "model": MODEL, "usage": {}, "ms": 0}
    own = client is None
    client = client or httpx.AsyncClient(timeout=timeout_s)
    try:
        r = await client.post(URL, json={"state": state, "model": MODEL,
                                         "questions": questions},
                              headers={"Authorization": f"Bearer {api_key()}"},
                              timeout=timeout_s)
        r.raise_for_status()
        data = r.json()
        out["answers"] = data.get("answers") or {}
        out["model"] = str(data.get("model") or MODEL)
        out["usage"] = data.get("usage") or {}
    except Exception as e:  # every caller falls back on no answer
        out["error"] = type(e).__name__
    finally:
        if own:
            await client.aclose()
        out["ms"] = round((time.monotonic() - t0) * 1000)
    return out
