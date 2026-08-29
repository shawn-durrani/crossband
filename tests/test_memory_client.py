"""Memory client degradation: with the service absent (unroutable port) every
read is a harmless empty value, writes are no-ops, and the app never raises.
Contract-version gating: a major mismatch is treated as absent.

search() is the one read that must NOT degrade quietly once the service is
reachable (issue #63): a bad/missing owner token, a transport failure, or an
unexpected response shape all raise MemorySearchError rather than looking
like a genuine zero-result search."""

import asyncio

import httpx
import pytest

from backend.memory_client import MemoryClient, MemorySearchError


def run(coro):
    return asyncio.run(coro)


def make_client(url="http://127.0.0.1:1"):
    # port 1 on loopback: connection refused immediately - no real service
    return MemoryClient(url, timeout=0.3)


def test_probe_false_when_unreachable():
    c = make_client()
    assert run(c.probe()) is False
    assert c.status()["available"] is False
    run(c.aclose())


def test_reads_degrade_to_empty():
    c = make_client()

    async def go():
        assert await c.get_summary() == ""
        assert await c.recall("anything") == []
        assert await c.search("anything") == []
        assert await c.save_fact("a fact", origin_agent="claude") is None
        await c.aclose()

    run(go())


def test_handoff_absent_service_records_no_failure():
    c = make_client()

    async def go():
        await c.handoff_chat(7, lambda: [], lambda last: None)
        await c.aclose()

    run(go())
    # absent service is a normal condition, not a failed write
    assert c.write_status() == {"failed": [], "pending": []}
    assert not c.any_write_failed()


def _fake_health(payload, status_code=200):
    async def fake_get(url):
        return httpx.Response(status_code, json=payload,
                              request=httpx.Request("GET", url))
    return fake_get


def test_contract_major_mismatch_treated_as_absent():
    c = make_client()
    c._client.get = _fake_health({"status": "ok", "contract_version": "2.0"})
    assert run(c.probe(force=True)) is False
    run(c.aclose())


def test_matching_contract_is_available():
    c = make_client()
    c._client.get = _fake_health({"status": "ok", "contract_version": "1.0"})
    assert run(c.probe(force=True)) is True
    assert c.status()["contract_version"] == "1.0"
    run(c.aclose())


def test_minor_version_bump_is_compatible():
    c = make_client()
    c._client.get = _fake_health({"status": "ok", "contract_version": "1.7"})
    assert run(c.probe(force=True)) is True
    run(c.aclose())


def test_probe_result_cached_for_ttl():
    c = make_client()
    c._client.get = _fake_health({"status": "ok", "contract_version": "1.0"})
    assert run(c.probe(force=True)) is True

    async def boom(url):
        raise AssertionError("probe should be served from cache")

    c._client.get = boom
    assert run(c.probe()) is True  # cached, no network call
    run(c.aclose())


def test_failed_handoff_is_surfaced():
    c = make_client()
    c._client.get = _fake_health({"status": "ok", "contract_version": "1.0"})

    async def failing_post(url, json=None):
        raise httpx.ConnectError("boom")

    c._client.post = failing_post
    msgs = [{"id": 1, "speaker": "user", "content": "hi", "created_at": 1751600000.0}]
    run(c.handoff_chat(3, lambda: msgs, lambda last: None))
    st = c.write_status()
    assert st["failed"] and st["failed"][0]["chat_id"] == 3
    assert c.any_write_failed()
    run(c.aclose())


def test_recall_sends_origin_label():
    """Ambient recalls tag themselves origin=auto in the request body so the
    service's access log can tell preparation from deliberate lookups."""
    c = make_client()
    c._client.get = _fake_health({"status": "ok", "contract_version": "1.0"})
    sent = {}

    async def fake_post(url, json=None):
        sent["url"], sent["body"] = url, json
        return httpx.Response(200, json={"facts": []},
                              request=httpx.Request("POST", url))

    c._client.post = fake_post
    run(c.recall("oscar follow-up", limit=6, origin="auto"))
    assert sent["url"].endswith("/recall")
    assert sent["body"]["origin"] == "auto"
    run(c.aclose())


def test_ingest_carries_attachments_and_placeholder():
    """Attachments ride the ingest payload; an attachments-only message is
    sent with a placeholder line rather than dropped."""
    c = make_client()
    c._client.get = _fake_health({"status": "ok", "contract_version": "1.0"})
    sent = {}

    async def fake_post(url, json=None):
        sent["body"] = json
        return httpx.Response(200, json={"ingested": 2, "skipped": 0, "attached": 1},
                              request=httpx.Request("POST", url))

    c._client.post = fake_post
    att = {"filename": "pasted.txt", "mime": "text/plain", "data_b64": "aGk="}
    run(c.ingest("7", [
        {"id": 1, "speaker": "user", "content": "with file",
         "created_at": 1751600000.0, "attachments": [att]},
        {"id": 2, "speaker": "user", "content": "",
         "created_at": 1751600001.0, "attachments": [att]},
        {"id": 3, "speaker": "user", "content": "", "created_at": 1751600002.0},
    ]))
    msgs = sent["body"]["messages"]
    assert len(msgs) == 2  # empty no-attachment message still dropped
    assert msgs[0]["attachments"] == [att]
    assert msgs[1]["content"] == "(sent attached file(s))"
    run(c.aclose())


def test_over_limit_attachments_split_into_continuation_entries():
    """The service caps attachments per message at 20 and 422s the whole
    body past it, which would wedge the chat: the watermark never advances
    and every later handoff retries the same rejected payload. An
    over-limit message is sent as chunks under the same external_id - the
    service's re-ingest top-up (idempotent, content-addressed) attaches
    the rest, and no composed body can exceed the schema."""
    from backend.memory_client import MAX_ATTACH_PER_MESSAGE

    c = make_client()
    c._client.get = _fake_health({"status": "ok", "contract_version": "1.0"})
    sent = {}

    async def fake_post(url, json=None):
        sent["body"] = json
        return httpx.Response(200, json={"ingested": 1, "skipped": 0,
                                         "attached": 45},
                              request=httpx.Request("POST", url))

    c._client.post = fake_post
    atts = [{"filename": f"f{i}.txt", "mime": "text/plain", "data_b64": "aGk="}
            for i in range(45)]
    run(c.ingest("7", [
        {"id": 1, "speaker": "user", "content": "bulk drop",
         "created_at": 1751600000.0, "attachments": atts},
        {"id": 2, "speaker": "user", "content": "plain",
         "created_at": 1751600001.0},
    ]))
    msgs = sent["body"]["messages"]
    bulk = [m for m in msgs if m["external_id"] == "1"]
    assert [len(m["attachments"]) for m in bulk] == [20, 20, 5]
    assert all(len(m.get("attachments", [])) <= MAX_ATTACH_PER_MESSAGE
               for m in msgs)
    carried = [a["filename"] for m in bulk for a in m["attachments"]]
    assert carried == [f"f{i}.txt" for i in range(45)]  # none dropped
    assert all(m["content"] == "bulk drop" for m in bulk)
    assert [m["external_id"] for m in msgs] == ["1", "1", "1", "2"]
    run(c.aclose())


# ---------- /search: current contract is POST /v1/search, owner-token gated,
# {"hits": [...]} on success (verified live against a running Membro) ----------

def test_search_sends_bearer_token_and_preserves_hits(monkeypatch):
    """Success path: with MEMORY_AUTH_TOKEN set, search() sends it as
    Authorization: Bearer <token> and returns the "hits" list untouched."""
    monkeypatch.setenv("MEMORY_AUTH_TOKEN", "s3cr3t-owner-token")
    c = make_client()
    c._client.get = _fake_health({"status": "ok", "contract_version": "1.1"})
    sent = {}

    async def fake_post(url, json=None, headers=None):
        sent["url"], sent["body"], sent["headers"] = url, json, headers
        return httpx.Response(200, json={"hits": [
            {"conversation_id": "c1", "speaker": "user",
             "content": "we discussed espresso",
             "created_at": "2026-05-01T10:00:00+10:00"},
        ]}, request=httpx.Request("POST", url))

    c._client.post = fake_post
    hits = run(c.search("espresso", limit=5))
    assert sent["url"].endswith("/search")
    assert sent["body"] == {"query": "espresso", "limit": 5}
    assert sent["headers"]["Authorization"] == "Bearer s3cr3t-owner-token"
    assert hits == [{"conversation_id": "c1", "speaker": "user",
                      "content": "we discussed espresso",
                      "created_at": "2026-05-01T10:00:00+10:00"}]
    run(c.aclose())


def test_search_without_token_sends_no_auth_header(monkeypatch):
    monkeypatch.delenv("MEMORY_AUTH_TOKEN", raising=False)
    c = make_client()
    c._client.get = _fake_health({"status": "ok", "contract_version": "1.1"})
    sent = {}

    async def fake_post(url, json=None, headers=None):
        sent["headers"] = headers
        return httpx.Response(200, json={"hits": []},
                              request=httpx.Request("POST", url))

    c._client.post = fake_post
    assert run(c.search("anything")) == []
    assert "Authorization" not in sent["headers"]
    run(c.aclose())


def test_search_401_raises_instead_of_empty_list(monkeypatch):
    """A missing/rejected owner token must be observable, not read as
    "no matching messages" (the original #63 failure mode)."""
    monkeypatch.setenv("MEMORY_AUTH_TOKEN", "wrong-token")
    c = make_client()
    c._client.get = _fake_health({"status": "ok", "contract_version": "1.1"})

    async def fake_post(url, json=None, headers=None):
        return httpx.Response(401, json={"error": {"code": "401",
                              "message": "owner token required"}},
                              request=httpx.Request("POST", url))

    c._client.post = fake_post
    with pytest.raises(MemorySearchError):
        run(c.search("anything"))
    run(c.aclose())


def test_search_transport_failure_raises(monkeypatch):
    monkeypatch.setenv("MEMORY_AUTH_TOKEN", "s3cr3t-owner-token")
    c = make_client()
    c._client.get = _fake_health({"status": "ok", "contract_version": "1.1"})

    async def failing_post(url, json=None, headers=None):
        raise httpx.ReadTimeout("boom")

    c._client.post = failing_post
    with pytest.raises(MemorySearchError):
        run(c.search("anything"))
    run(c.aclose())


def test_search_unexpected_shape_raises_not_empty(monkeypatch):
    """A response-shape mismatch (renamed/missing field) must not silently
    read as zero results either."""
    monkeypatch.setenv("MEMORY_AUTH_TOKEN", "s3cr3t-owner-token")
    c = make_client()
    c._client.get = _fake_health({"status": "ok", "contract_version": "1.1"})

    async def fake_post(url, json=None, headers=None):
        return httpx.Response(200, json={"results": []},  # renamed field
                              request=httpx.Request("POST", url))

    c._client.post = fake_post
    with pytest.raises(MemorySearchError):
        run(c.search("anything"))
    run(c.aclose())


def test_search_absent_service_still_degrades_to_empty(monkeypatch):
    """Unlike the reachable-but-broken cases above, a genuinely absent
    service is the documented no-memory posture, not a failure."""
    monkeypatch.setenv("MEMORY_AUTH_TOKEN", "s3cr3t-owner-token")
    c = make_client()  # unroutable port: probe() fails, search() never posts
    assert run(c.search("anything")) == []
    run(c.aclose())
