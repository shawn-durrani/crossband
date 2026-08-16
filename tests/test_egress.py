"""Egress proxy behaviour (#138, first slice): resolve-once + connect-to-the-
vetted-address (DNS rebinding gains nothing), policy refusals, transfer caps,
per-host pacing, CONNECT tunnels, and the fetch_page routing. Everything runs
on loopback; resolvers and address policies are injected, so no real DNS or
network is touched."""

import asyncio
import time

import httpx
import pytest

from backend import egress, tools
from backend.config import Settings


@pytest.fixture(autouse=True)
def _no_leaked_proxy_url():
    yield
    egress.set_proxy_url(None)


def _loop_resolver(calls=None):
    """A resolver that answers 127.0.0.1 for any name and records calls."""
    def resolve(host, port):
        if calls is not None:
            calls.append(host)
        return [(2, 1, 6, "", ("127.0.0.1", 0))]
    return resolve


def _proxy(**kw):
    args = dict(max_transfer_bytes=1 << 20, politeness_s=0.0,
                idle_timeout_s=5.0, lifetime_s=15.0,
                resolver=_loop_resolver(), address_policy=lambda ip: True)
    args.update(kw)
    return egress.EgressProxy(**args)


async def _serve(handler):
    server = await asyncio.start_server(handler, "127.0.0.1", 0)
    return server, server.sockets[0].getsockname()[1]


def _page_handler(seen=None, body=b"hello from vetted", ctype=b"text/html"):
    async def handler(reader, writer):
        head = await reader.readuntil(b"\r\n\r\n")
        if seen is not None:
            seen["line"] = head.split(b"\r\n", 1)[0].decode()
        writer.write(b"HTTP/1.1 200 OK\r\nContent-Type: " + ctype
                     + b"\r\nContent-Length: %d\r\nConnection: close\r\n\r\n"
                     % len(body) + body)
        await writer.drain()
        writer.close()
    return handler


def test_absolute_form_resolves_once_and_connects_to_vetted_address():
    async def main():
        seen, calls = {}, []
        server, sport = await _serve(_page_handler(seen))
        proxy = _proxy(resolver=_loop_resolver(calls), http_ports={sport})
        await proxy.start()
        try:
            async with httpx.AsyncClient(proxy=proxy.url) as client:
                r = await client.get(f"http://h.test:{sport}/x?q=1")
            assert r.status_code == 200
            assert r.content == b"hello from vetted"
            # h.test has no real DNS: reaching the local server at all proves
            # the connect used the resolver's vetted answer. One call proves
            # there is no second resolution for rebinding to poison.
            assert calls == ["h.test"]
            assert seen["line"] == "GET /x?q=1 HTTP/1.1"  # origin-form upstream
        finally:
            await proxy.stop()
            server.close()
    asyncio.run(main())


def test_policy_refusal_is_a_403_with_the_reason():
    async def main():
        server, sport = await _serve(_page_handler())
        # Real policy: the resolver's 127.0.0.1 answer must be refused.
        proxy = _proxy(address_policy=egress.address_allowed,
                       http_ports={sport})
        await proxy.start()
        try:
            async with httpx.AsyncClient(proxy=proxy.url) as client:
                r = await client.get(f"http://h.test:{sport}/")
            assert r.status_code == 403
            assert b"non-public" in r.content
        finally:
            await proxy.stop()
            server.close()
    asyncio.run(main())


def test_nonstandard_port_refused_without_resolving():
    async def main():
        calls = []
        proxy = _proxy(resolver=_loop_resolver(calls))  # default ports only
        await proxy.start()
        try:
            async with httpx.AsyncClient(proxy=proxy.url) as client:
                r = await client.get("http://h.test:8902/api/state")
            assert r.status_code == 403
            assert b"ports" in r.content
            assert calls == []  # refused before any resolution
        finally:
            await proxy.stop()
    asyncio.run(main())


def test_transfer_cap_breaks_the_connection():
    async def main():
        flood = 200_000
        async def handler(reader, writer):
            await reader.readuntil(b"\r\n\r\n")
            writer.write(b"HTTP/1.1 200 OK\r\nContent-Length: %d\r\n"
                         b"Connection: close\r\n\r\n" % flood)
            try:
                for _ in range(flood // 4096):
                    writer.write(b"x" * 4096)
                    await writer.drain()
            except (ConnectionError, OSError):
                pass  # proxy cut us off at the cap - expected
            writer.close()
        server, sport = await _serve(handler)
        proxy = _proxy(max_transfer_bytes=100_000, http_ports={sport})
        await proxy.start()
        try:
            async with httpx.AsyncClient(proxy=proxy.url) as client:
                with pytest.raises(httpx.HTTPError):
                    await client.get(f"http://h.test:{sport}/big")
        finally:
            await proxy.stop()
            server.close()
    asyncio.run(main())


def test_per_host_pacing_spaces_connects():
    async def main():
        server, sport = await _serve(_page_handler())
        proxy = _proxy(politeness_s=0.4, http_ports={sport})
        await proxy.start()
        try:
            async with httpx.AsyncClient(proxy=proxy.url) as client:
                t0 = time.monotonic()
                await client.get(f"http://h.test:{sport}/one")
                await client.get(f"http://h.test:{sport}/two")
                elapsed = time.monotonic() - t0
            assert elapsed >= 0.35
        finally:
            await proxy.stop()
            server.close()
    asyncio.run(main())


async def _open_tunnel(proxy, host, port):
    reader, writer = await asyncio.open_connection("127.0.0.1", proxy.port)
    writer.write(f"CONNECT {host}:{port} HTTP/1.1\r\n"
                 f"Host: {host}:{port}\r\n\r\n".encode())
    await writer.drain()
    assert b"200" in await reader.readline()
    await reader.readline()  # blank line ends the proxy's response
    return reader, writer


def test_connect_tunnel_echoes_through_allowed_port():
    async def main():
        async def echo(reader, writer):
            while True:
                data = await reader.read(4096)
                if not data:
                    break
                writer.write(data)
                await writer.drain()
            writer.close()
        server, eport = await _serve(echo)
        proxy = _proxy(connect_ports={eport})
        await proxy.start()
        try:
            reader, writer = await _open_tunnel(proxy, "e.test", eport)
            writer.write(b"ping")
            await writer.drain()
            assert await reader.readexactly(4) == b"ping"
            writer.close()
        finally:
            await proxy.stop()
            server.close()
    asyncio.run(main())


def test_connect_tunnel_cap_breaks_the_flood():
    """A flood past the transfer cap tears the tunnel down: the client sees
    the stream end long before the flood does. Teardown may surface as EOF or
    a reset depending on buffering, so only the byte count is asserted."""
    flood_total = 100 * 4096
    async def main():
        async def flood(reader, writer):
            await reader.readexactly(1)
            try:
                for _ in range(100):
                    writer.write(b"y" * 4096)
                    await writer.drain()
            except (ConnectionError, OSError):
                pass  # cap tears the tunnel down - expected
            writer.close()
        server, eport = await _serve(flood)
        proxy = _proxy(max_transfer_bytes=50_000, connect_ports={eport})
        await proxy.start()
        try:
            reader, writer = await _open_tunnel(proxy, "e.test", eport)
            writer.write(b"g")
            await writer.drain()
            received = 0
            try:
                while True:
                    chunk = await reader.read(65536)
                    if not chunk:
                        break
                    received += len(chunk)
            except ConnectionError:
                pass
            assert received < flood_total
            writer.close()
        finally:
            await proxy.stop()
            server.close()
    asyncio.run(main())


def test_connect_to_disallowed_port_refused():
    async def main():
        proxy = _proxy()  # default connect ports: 443 only
        await proxy.start()
        try:
            reader, writer = await asyncio.open_connection("127.0.0.1", proxy.port)
            writer.write(b"CONNECT h.test:8902 HTTP/1.1\r\nHost: h.test\r\n\r\n")
            await writer.drain()
            assert b"403" in await reader.readline()
            writer.close()
        finally:
            await proxy.stop()
    asyncio.run(main())


def test_origin_form_request_refused():
    async def main():
        proxy = _proxy()
        await proxy.start()
        try:
            reader, writer = await asyncio.open_connection("127.0.0.1", proxy.port)
            writer.write(b"GET / HTTP/1.1\r\nHost: h.test\r\n\r\n")
            await writer.drain()
            assert b"400" in await reader.readline()
            writer.close()
        finally:
            await proxy.stop()
    asyncio.run(main())


def test_fetch_page_routes_through_the_proxy(monkeypatch):
    """End to end: fetch_page discovers the published proxy and its request
    arrives through it. The pre-flight guard is stubbed (it has its own tests
    above and in test_ssrf.py) because the test server's port is ephemeral."""
    async def main():
        body = b"<html><body><p>proxied page</p></body></html>"
        server, sport = await _serve(_page_handler(body=body))
        proxy = _proxy(http_ports={sport})
        await proxy.start()
        egress.set_proxy_url(proxy.url)
        monkeypatch.setattr(tools, "_assert_public_url", lambda u: u)
        try:
            out = await asyncio.to_thread(
                tools.fetch_page, {"url": f"http://p.test:{sport}/"},
                Settings().as_cfg())
            assert out.startswith("Fetched:")
            assert "proxied page" in out
        finally:
            egress.set_proxy_url(None)
            await proxy.stop()
            server.close()
    asyncio.run(main())


def test_fetch_page_reports_a_dead_proxy_honestly(monkeypatch):
    """A published-but-dead proxy must surface as an explicit tool error,
    never fall back to a silent direct fetch."""
    monkeypatch.setattr(egress.socket, "getaddrinfo",
                        lambda host, port: [(2, 1, 6, "", ("93.184.216.34", 0))])
    egress.set_proxy_url("http://127.0.0.1:9")  # nothing listens there
    out = asyncio.run(tools.run_tool(
        "fetch_page", {"url": "http://example.com/"}, Settings().as_cfg()))
    assert out.startswith("Error running fetch_page:")
