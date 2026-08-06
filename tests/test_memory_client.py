"""Memory client degradation: with the service absent (unroutable port) every
read is a harmless empty value, writes are no-ops, and the app never raises.
Contract-version gating: a major mismatch is treated as absent."""

import asyncio

import httpx

from backend.memory_client import MemoryClient


def run(coro):
    return asyncio.run(coro)


def make_client(url="http://127.0.0.1:1"):
    # port 1 on loopback: connection refused immediately — no real service
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
