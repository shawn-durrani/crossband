"""The citation check (#213): a cited source needs a tool row in the same
reply. The 2026-08-23 field shape: "Akai's own docs say ..." with no fetch
anywhere in the reply. Informational only - a chip through the same
audit_flags surface as #211, never a retry or a block - and any tool row in
the reply skips the check entirely."""

import json
import time

import pytest
from fastapi.testclient import TestClient

from backend import citations, engine, rounds
from backend.app import create_app
from backend.config import Settings

CITED = ("Akai's own docs say you need the powered camera adapter for a "
         "Lightning phone.")


def test_the_pure_rules():
    hits = citations.uncited_claims(CITED, [])
    assert len(hits) == 1
    assert hits[0]["kind"] == "citation"
    assert hits[0]["claim"] == CITED
    # the sentence is extracted from surrounding text
    hits = citations.uncited_claims(f"Good news first. {CITED} Easy.", [])
    assert hits[0]["claim"] == CITED
    # other explicit source-says shapes
    assert citations.uncited_claims(
        "According to the manual, hold Shift while powering on.", [])
    assert citations.uncited_claims(
        "The documentation says the input caps at line level.", [])
    # naming a place to look is not citing it
    assert citations.uncited_claims("Check the docs for the pinout.", []) == []
    assert citations.uncited_claims(
        "The documentation covers calibration too.", []) == []
    # a reply that ran ANY tool is never checked
    assert citations.uncited_claims(
        CITED, [{"tool": "web_search", "input": {}, "output": "ok"}]) == []
    # dedupe and cap
    many = " ".join(f"The docs say fact number {i} is true." for i in range(6))
    assert len(citations.uncited_claims(many, [])) == citations.MAX_FINDINGS
    assert citations.uncited_claims("", []) == []


@pytest.fixture
def app(tmp_path):
    return create_app(Settings(data_dir=str(tmp_path / "data"),
                               memory_url="http://127.0.0.1:1"))


def _scripted(calls, with_tool=False):
    async def stream_reply(participant, roster, transcript, names, cfg,
                           project, chat_summary, voice_mode, tools=None,
                           memory=None):
        calls.append(participant["slug"])
        if len(calls) == 1:
            if with_tool:
                yield ("tool", {"tool": "web_search",
                                "input": {"query": "adapter"}, "output": "ok"})
            yield ("text", CITED)
        else:
            yield ("text", "Nothing to add; the adapter question is settled.")
    return stream_reply


def _round(c, chat_id, text):
    with c.stream("POST", f"/api/chats/{chat_id}/send",
                  json={"text": text}) as r:
        for _ in r.iter_lines():
            pass
    deadline = time.time() + 5
    while rounds.active(chat_id) is not None and time.time() < deadline:
        time.sleep(0.05)
    return c.get(f"/api/chats/{chat_id}").json()["messages"]


def test_an_uncited_claim_lands_a_chip_and_a_fetch_clears_it(app, monkeypatch):
    calls = []
    monkeypatch.setattr(engine.providers, "stream_reply", _scripted(calls))
    with TestClient(app, base_url="http://127.0.0.1") as c:
        chat_id = c.post("/api/chats", json={}).json()["id"]
        msgs = _round(c, chat_id, "what adapter does the sampler need?")
        flags = json.loads(msgs[1]["audit_flags"])
        assert flags == [{"kind": "citation", "claim": CITED}]
        assert msgs[2]["audit_flags"] == ""

    calls2 = []
    monkeypatch.setattr(engine.providers, "stream_reply",
                        _scripted(calls2, with_tool=True))
    with TestClient(app, base_url="http://127.0.0.1") as c:
        chat_id = c.post("/api/chats", json={}).json()["id"]
        msgs = _round(c, chat_id, "what adapter does the sampler need?")
        # the same sentence with a tool row behind it is never flagged
        assert msgs[1]["audit_flags"] == ""
