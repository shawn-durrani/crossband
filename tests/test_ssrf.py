"""SSRF guard characterization: scheme/port/credential checks, private-range
rejection, and per-hop redirect re-validation. All network mocked. Since #138
the policy lives in backend/egress.py; tools._assert_public_url is the
tool-facing door to it, so these tests exercise both."""

import ipaddress

import httpx
import pytest

from backend import egress, tools
from backend.tools import _assert_public_url, _stream_following_redirects


def test_rejects_non_http_schemes():
    for url in ("ftp://example.com/x", "file:///etc/passwd", "gopher://x"):
        with pytest.raises(ValueError, match="http"):
            _assert_public_url(url)


def test_rejects_missing_host():
    with pytest.raises(ValueError):
        _assert_public_url("http:///nohost")


def test_rejects_url_credentials():
    for url in ("http://user:pw@example.com/", "http://admin@example.com/"):
        with pytest.raises(ValueError, match="Credentials"):
            _assert_public_url(url)


def test_rejects_nonstandard_ports():
    with pytest.raises(ValueError, match="ports"):
        _assert_public_url("http://example.com:8080/")
    with pytest.raises(ValueError, match="ports"):
        _assert_public_url("https://example.com:8901/")


@pytest.mark.parametrize("ip", [
    "127.0.0.1",        # loopback
    "10.0.0.5",         # private
    "192.168.1.1",      # private
    "172.16.0.1",       # private
    "169.254.169.254",  # link-local (cloud metadata)
    "100.64.0.1",       # shared address space (CGNAT / tailnets)
    "0.0.0.0",          # unspecified
    "224.0.0.1",        # multicast
])
def test_rejects_private_ip_literals(ip):
    with pytest.raises(ValueError, match="non-public"):
        _assert_public_url(f"http://{ip}/secret")


@pytest.mark.parametrize("ip", [
    "::1",              # IPv6 loopback
    "fe80::1",          # IPv6 link-local
    "fc00::1",          # IPv6 unique-local
    "::ffff:10.0.0.1",  # IPv4-mapped private
    "255.255.255.255",  # broadcast
])
def test_address_policy_rejects_non_global(ip):
    assert not egress.address_allowed(ipaddress.ip_address(ip))


def test_address_policy_accepts_public():
    assert egress.address_allowed(ipaddress.ip_address("93.184.216.34"))
    assert egress.address_allowed(
        ipaddress.ip_address("2606:2800:220:1:248:1893:25c8:1946"))


def test_rejects_hostname_resolving_to_private(monkeypatch):
    monkeypatch.setattr(egress.socket, "getaddrinfo",
                        lambda host, port: [(2, 1, 6, "", ("10.1.2.3", 0))])
    with pytest.raises(ValueError, match="non-public"):
        _assert_public_url("http://internal.corp.example/")


def test_rejects_hostname_with_any_private_answer(monkeypatch):
    # DNS answers with one public and one private record - reject (rebinding)
    monkeypatch.setattr(egress.socket, "getaddrinfo",
                        lambda host, port: [(2, 1, 6, "", ("93.184.216.34", 0)),
                                            (2, 1, 6, "", ("127.0.0.1", 0))])
    with pytest.raises(ValueError, match="non-public"):
        _assert_public_url("http://tricky.example/")


def test_accepts_public_hostname(monkeypatch):
    monkeypatch.setattr(egress.socket, "getaddrinfo",
                        lambda host, port: [(2, 1, 6, "", ("93.184.216.34", 0))])
    assert _assert_public_url("https://example.com/page") == "https://example.com/page"


def test_unresolvable_host_is_rejected(monkeypatch):
    def boom(host, port):
        raise OSError("no such host")
    monkeypatch.setattr(egress.socket, "getaddrinfo", boom)
    with pytest.raises(ValueError, match="resolve"):
        _assert_public_url("http://doesnotexist.example/")


def _realistic_gai(host, port):
    """Like the real resolver: IP literals resolve to themselves, hostnames to
    a public address."""
    try:
        ipaddress.ip_address(host)
        return [(2, 1, 6, "", (host, 0))]
    except ValueError:
        return [(2, 1, 6, "", ("93.184.216.34", 0))]


class _FakeStream:
    """Context manager shaped like httpx.stream(...)."""

    def __init__(self, resp):
        self._resp = resp

    def __enter__(self):
        return self._resp

    def __exit__(self, *exc):
        return False


def test_redirect_to_private_address_is_rejected(monkeypatch, cfg):
    """Every redirect hop is re-validated: a public page 302ing to a private
    address must raise, not be followed."""
    monkeypatch.setattr(egress.socket, "getaddrinfo", _realistic_gai)

    def fake_stream(method, url, **kw):
        assert kw["follow_redirects"] is False  # manual hops only
        return _FakeStream(httpx.Response(
            302, headers={"location": "http://127.0.0.1/admin"},
            request=httpx.Request("GET", url)))

    monkeypatch.setattr(tools.httpx, "stream", fake_stream)
    with pytest.raises(ValueError, match="non-public"):
        _stream_following_redirects("http://example.com/start",
                                    {"User-Agent": "t"}, cfg, 1 << 20)


def test_redirect_chain_capped(monkeypatch, cfg):
    monkeypatch.setattr(egress.socket, "getaddrinfo",
                        lambda host, port: [(2, 1, 6, "", ("93.184.216.34", 0))])

    def fake_stream(method, url, **kw):
        return _FakeStream(httpx.Response(
            301, headers={"location": "http://example.com/again"},
            request=httpx.Request("GET", url)))

    monkeypatch.setattr(tools.httpx, "stream", fake_stream)
    with pytest.raises(ValueError, match="redirects"):
        _stream_following_redirects("http://example.com/loop", {}, cfg, 1 << 20)


def test_page_body_cap_enforced(monkeypatch, cfg):
    """A page bigger than the cap raises instead of ballooning in RAM."""
    monkeypatch.setattr(egress.socket, "getaddrinfo",
                        lambda host, port: [(2, 1, 6, "", ("93.184.216.34", 0))])

    def fake_stream(method, url, **kw):
        return _FakeStream(httpx.Response(
            200, content=b"x" * 2048,
            headers={"content-type": "text/html"},
            request=httpx.Request("GET", url)))

    monkeypatch.setattr(tools.httpx, "stream", fake_stream)
    with pytest.raises(ValueError, match="cap"):
        _stream_following_redirects("http://example.com/big", {}, cfg, 1024)


def test_reddit_redirect_may_not_leave_reddit(monkeypatch, cfg):
    """The Authorization header rides Reddit hops, so a redirect that leaves
    reddit.com is refused instead of followed."""
    monkeypatch.delenv("REDDIT_CLIENT_ID", raising=False)  # public branch
    monkeypatch.delenv("REDDIT_CLIENT_SECRET", raising=False)

    def fake_get(url, **kw):
        assert kw["follow_redirects"] is False
        return httpx.Response(
            302, headers={"location": "https://evil.example/steal"},
            request=httpx.Request("GET", url))

    monkeypatch.setattr(tools.httpx, "get", fake_get)
    with pytest.raises(ValueError, match="reddit"):
        tools._reddit_get("/r/test/comments/abc.json", cfg)
