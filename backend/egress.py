"""One vetted egress path for every model-influenced URL (#138, first slice).

Two layers, one policy. vet_url() is the pre-flight guard the research tools
call before any fetch; EgressProxy re-enforces the same policy at connect
time. The proxy resolves a host once, requires every answer to be globally
routable, then connects to the exact address it vetted - a nameserver that
answers differently between check and fetch (DNS rebinding) gains nothing.
Transfer caps bound what one connection can move; per-host spacing keeps the
tools polite to the sites they visit.

The app lifespan starts one proxy per process and publishes it via
set_proxy_url() (module global, same pattern as the events bus). Tools read
proxy_url() and fall back to a direct - still pre-flight-vetted - fetch when
no proxy is running (keyless tests, raw create_app without lifespan). The
proxy is the strengthening layer; the pre-flight guard is the baseline that
never disappears.

First-party credentialled API calls (search engines, GitHub) do NOT come
through here: their destinations are fixed by code, not chosen by a model.
"""

import asyncio
import contextlib
import ipaddress
import logging
import socket
import time
from urllib.parse import urlparse

log = logging.getLogger("crossband.egress")

# Request head (line + headers) bound; also the StreamReader buffer limit.
_HEAD_LIMIT = 32 * 1024
# A tool upload through the proxy is not a feature: request bodies are small
# (Reddit token posts stay direct), so this is a backstop, not a knob.
_MAX_REQUEST_BYTES = 2 * 1024 * 1024

_proxy_url: str | None = None


def set_proxy_url(url: str | None) -> None:
    """Publish (or clear) the running proxy for this process."""
    global _proxy_url
    _proxy_url = url


def proxy_url() -> str | None:
    return _proxy_url


# ---------- policy: one definition of "public" ----------

def address_allowed(ip) -> bool:
    """Globally routable only, after unwrapping IPv4-mapped IPv6
    (::ffff:127.0.0.1 must fail as 127.0.0.1). Both checks are load-bearing:
    is_global adds ranges the flag pile misses (shared address space
    100.64/10, which CGNAT and tailnets use), while the flags keep classes
    is_global follows the IANA special registry on and therefore PASSES -
    multicast 224.0.0.1 is "global" to it."""
    if ip.version == 6 and ip.ipv4_mapped:
        ip = ip.ipv4_mapped
    return ip.is_global and not (
        ip.is_private or ip.is_loopback or ip.is_link_local or ip.is_reserved
        or ip.is_multicast or ip.is_unspecified)


def resolve_public(host, resolver=None, policy=None):
    """Resolve once; every answer must pass the policy. Returns the vetted
    addresses in resolver order, so a caller connects to what was checked
    instead of resolving a second time."""
    try:
        infos = (resolver or socket.getaddrinfo)(host, None)
    except OSError as e:
        raise ValueError(f"Could not resolve host: {e}")
    allowed = policy or address_allowed
    addrs = []
    for info in infos:
        raw = str(info[4][0])
        try:
            ip = ipaddress.ip_address(raw.partition("%")[0])
        except ValueError:
            raise ValueError("URL resolves to a non-public address")
        if not allowed(ip):
            raise ValueError("URL resolves to a non-public address")
        if raw not in addrs:
            addrs.append(raw)
    if not addrs:
        raise ValueError(f"Could not resolve host: no addresses for {host}")
    return addrs


def vet_url(url, resolver=None):
    """Pre-flight guard: http(s) on standard ports, no credentials in the
    URL, host resolves only to public addresses. Returns the URL unchanged."""
    u = urlparse(url)
    if u.scheme not in ("http", "https"):
        raise ValueError("Only http(s) URLs are allowed")
    if not u.hostname:
        raise ValueError("URL has no host")
    if u.username or u.password:
        raise ValueError("Credentials in URLs are not allowed")
    if u.port not in (None, 80, 443):
        raise ValueError("Only standard ports (80/443) are allowed")
    resolve_public(u.hostname, resolver)
    return url


# ---------- the proxy ----------

# End-to-end headers pass through; these do not (RFC 9110 hop-by-hop, plus
# proxy-directed headers a site must never see).
_HOP_HEADERS = {"connection", "proxy-connection", "keep-alive", "te",
                "trailer", "transfer-encoding", "upgrade",
                "proxy-authorization", "proxy-authenticate"}


class _CapExceeded(Exception):
    pass


class _Refused(Exception):
    """Control flow only: the refusal response has already been written."""


class EgressProxy:
    """Loopback forward proxy: absolute-form HTTP to port 80, CONNECT tunnels
    to port 443. Per request: vet the target, resolve once, space per-host
    connects, connect to the vetted address, cap bytes moved. Ports are
    injectable so tests can stand servers on ephemeral ports; production uses
    the defaults."""

    def __init__(self, *, max_transfer_bytes, politeness_s, idle_timeout_s,
                 lifetime_s, resolver=None, address_policy=None,
                 http_ports=(80,), connect_ports=(443,)):
        self.max_transfer_bytes = int(max_transfer_bytes)
        self.politeness_s = float(politeness_s)
        self.idle_timeout_s = float(idle_timeout_s)
        self.lifetime_s = float(lifetime_s)
        self._resolver = resolver
        self._policy = address_policy
        self.http_ports = set(http_ports)
        self.connect_ports = set(connect_ports)
        self._server = None
        self._pace_lock = asyncio.Lock()
        self._next_by_host = {}
        self._conns = set()  # live handler tasks, cancelled on stop()
        self.port = None
        self.url = None

    async def start(self, host="127.0.0.1"):
        self._server = await asyncio.start_server(
            self._handle, host, 0, limit=_HEAD_LIMIT)
        self.port = self._server.sockets[0].getsockname()[1]
        self.url = f"http://{host}:{self.port}"
        return self

    async def stop(self):
        """Stop listening and cancel live connections. Cancelling our own
        handler-task registry (rather than Server.close_clients) keeps this
        off 3.13-only API and off wait_closed's wait-for-handlers behaviour,
        which would otherwise let one long tunnel stall shutdown."""
        if self._server is None:
            return
        self._server.close()
        for task in list(self._conns):
            task.cancel()
        if self._conns:
            await asyncio.gather(*self._conns, return_exceptions=True)
        with contextlib.suppress(Exception):
            await self._server.wait_closed()
        self._server = None

    # -- per-connection plumbing --

    async def _handle(self, reader, writer):
        self._conns.add(asyncio.current_task())
        upstream_writer = None
        try:
            head = await asyncio.wait_for(reader.readuntil(b"\r\n\r\n"),
                                          timeout=self.idle_timeout_s)
            request_line, _, raw_headers = head.partition(b"\r\n")
            parts = request_line.decode("latin-1").split()
            if len(parts) != 3:
                return await self._refuse(writer, 400, "malformed request line")
            method, target, _version = parts
            headers = self._parse_headers(raw_headers)
            if method.upper() == "CONNECT":
                upstream_writer = await self._tunnel(reader, writer, target)
            elif "://" in target:
                upstream_writer = await self._forward(
                    reader, writer, method, target, headers)
            else:
                return await self._refuse(
                    writer, 400, "origin-form requests are not proxied")
        except _Refused:
            pass  # _refuse already answered
        except (asyncio.IncompleteReadError, asyncio.LimitOverrunError,
                TimeoutError, ConnectionError, OSError, _CapExceeded) as e:
            log.debug("egress connection ended: %s", e)
        except Exception:
            log.exception("egress handler failed")
        finally:
            self._conns.discard(asyncio.current_task())
            for w in (writer, upstream_writer):
                if w is not None:
                    with contextlib.suppress(Exception):
                        w.close()

    @staticmethod
    def _parse_headers(raw: bytes) -> list[tuple[str, str]]:
        out = []
        for line in raw.decode("latin-1").split("\r\n"):
            name, sep, value = line.partition(":")
            if sep:
                out.append((name.strip(), value.strip()))
        return out

    async def _refuse(self, writer, code, reason):
        """Answer an unproxied request and stop handling this connection.
        403 carries the vet failure so the tool's error is honest."""
        phrase = {400: "Bad Request", 403: "Forbidden",
                  413: "Content Too Large"}.get(code, "Error")
        body = f"egress proxy: {reason}\n".encode()
        writer.write(
            f"HTTP/1.1 {code} {phrase}\r\nContent-Type: text/plain\r\n"
            f"Content-Length: {len(body)}\r\nConnection: close\r\n\r\n".encode()
            + body)
        with contextlib.suppress(Exception):
            await writer.drain()
        raise _Refused()

    async def _vet_and_connect(self, writer, host, port):
        """Vet + resolve once + pace + connect to the vetted address. Any
        policy failure answers 403 and aborts the connection."""
        try:
            addrs = await asyncio.to_thread(
                resolve_public, host, self._resolver, self._policy)
        except ValueError as e:
            await self._refuse(writer, 403, str(e))
        await self._pace(host)
        err = None
        for addr in addrs:
            try:
                return await asyncio.wait_for(
                    asyncio.open_connection(addr, port, limit=_HEAD_LIMIT),
                    timeout=self.idle_timeout_s)
            except OSError as e:
                err = e
        await self._refuse(writer, 403, f"could not connect: {err}")

    async def _pace(self, host):
        """At most one connect per host per politeness window."""
        async with self._pace_lock:
            now = time.monotonic()
            nxt = self._next_by_host.get(host, now)
            wait = max(0.0, nxt - now)
            self._next_by_host[host] = max(now, nxt) + self.politeness_s
            if len(self._next_by_host) > 1024:
                for k in sorted(self._next_by_host,
                                key=self._next_by_host.get)[:512]:
                    del self._next_by_host[k]
        if wait:
            await asyncio.sleep(wait)

    async def _relay(self, src, dst, cap, last=None):
        """Move bytes src -> dst until EOF, idle timeout, or the cap. `last`
        is a shared one-item list of the tunnel's last-activity time: a
        direction with nothing to say stays open while the OTHER direction is
        still moving (a long quiet download must not be killed by the silent
        request side)."""
        moved = 0
        while True:
            try:
                chunk = await asyncio.wait_for(src.read(65536),
                                               timeout=self.idle_timeout_s)
            except TimeoutError:
                if last is not None and \
                        time.monotonic() - last[0] < self.idle_timeout_s:
                    continue
                raise
            if not chunk:
                return
            if last is not None:
                last[0] = time.monotonic()
            moved += len(chunk)
            if moved > cap:
                raise _CapExceeded(f"transfer exceeded {cap} bytes")
            dst.write(chunk)
            await dst.drain()

    # -- CONNECT (https) --

    async def _tunnel(self, reader, writer, target):
        host, _, port_s = target.rpartition(":")
        host = host.strip("[]")
        try:
            port = int(port_s)
        except ValueError:
            port = 0
        if not host or port not in self.connect_ports:
            await self._refuse(
                writer, 403,
                "Only standard ports (80/443) are allowed")
        up_reader, up_writer = await self._vet_and_connect(writer, host, port)
        try:
            writer.write(b"HTTP/1.1 200 Connection Established\r\n\r\n")
            await writer.drain()

            last = [time.monotonic()]

            async def relay_then_close(src, dst, cap):
                # A cap breach or error tears down BOTH ends at once: the
                # tools must see a broken transfer, never a silently
                # truncated body that still parses.
                try:
                    await self._relay(src, dst, cap, last)
                finally:
                    for w in (writer, up_writer):
                        with contextlib.suppress(Exception):
                            w.close()

            await asyncio.wait_for(
                asyncio.gather(
                    relay_then_close(reader, up_writer, _MAX_REQUEST_BYTES),
                    relay_then_close(up_reader, writer, self.max_transfer_bytes),
                    return_exceptions=True),
                timeout=self.lifetime_s)
        except BaseException:
            with contextlib.suppress(Exception):
                up_writer.close()
            raise
        return up_writer

    # -- absolute-form (plain http) --

    async def _forward(self, reader, writer, method, target, headers):
        u = urlparse(target)
        if u.scheme != "http":
            await self._refuse(writer, 400,
                               "absolute-form is http only; https uses CONNECT")
        if not u.hostname:
            await self._refuse(writer, 403, "URL has no host")
        if u.username or u.password:
            await self._refuse(writer, 403, "Credentials in URLs are not allowed")
        port = u.port or 80
        if port not in self.http_ports:
            await self._refuse(writer, 403,
                               "Only standard ports (80/443) are allowed")

        body = b""
        length = 0
        for name, value in headers:
            if name.lower() == "transfer-encoding":
                await self._refuse(writer, 400, "chunked requests are not proxied")
            if name.lower() == "content-length":
                try:
                    length = int(value)
                except ValueError:
                    await self._refuse(writer, 400, "bad content-length")
        if length > _MAX_REQUEST_BYTES:
            await self._refuse(writer, 413, "request body too large")
        if length:
            body = await asyncio.wait_for(reader.readexactly(length),
                                          timeout=self.idle_timeout_s)

        up_reader, up_writer = await self._vet_and_connect(
            writer, u.hostname, port)
        try:
            path = u.path or "/"
            if u.query:
                path += f"?{u.query}"
            lines = [f"{method} {path} HTTP/1.1"]
            if not any(n.lower() == "host" for n, _ in headers):
                lines.append(f"Host: {u.netloc}")
            lines += [f"{n}: {v}" for n, v in headers
                      if n.lower() not in _HOP_HEADERS]
            lines.append("Connection: close")
            up_writer.write(("\r\n".join(lines) + "\r\n\r\n").encode("latin-1")
                            + body)
            await up_writer.drain()
            # Relay the upstream response verbatim until it closes; we asked
            # for Connection: close, so EOF is the terminator and the client
            # socket closes with it (the finally in _handle).
            await asyncio.wait_for(
                self._relay(up_reader, writer, self.max_transfer_bytes),
                timeout=self.lifetime_s)
        except BaseException:
            with contextlib.suppress(Exception):
                up_writer.close()
            raise
        return up_writer
