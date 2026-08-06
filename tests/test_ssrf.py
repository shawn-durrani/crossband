"""SSRF guard characterization: scheme/port checks, private-range rejection,
and per-hop redirect re-validation. All network mocked."""

import httpx
import pytest

from backend import tools
from backend.tools import _assert_public_url, _get_following_redirects


def test_rejects_non_http_schemes():
    for url in ("ftp://example.com/x", "file:///etc/passwd", "gopher://x"):
        with pytest.raises(ValueError, match="http"):
            _assert_public_url(url)


def test_rejects_missing_host():
    with pytest.raises(ValueError):
        _assert_public_url("http:///nohost")


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
    "0.0.0.0",          # unspecified
    "224.0.0.1",        # multicast
])
def test_rejects_private_ip_literals(ip):
    with pytest.raises(ValueError, match="non-public"):
        _assert_public_url(f"http://{ip}/secret")


def test_rejects_hostname_resolving_to_private(monkeypatch):
    monkeypatch.setattr(tools.socket, "getaddrinfo",
                        lambda host, port: [(2, 1, 6, "", ("10.1.2.3", 0))])
    with pytest.raises(ValueError, match="non-public"):
        _assert_public_url("http://internal.corp.example/")


def test_rejects_hostname_with_any_private_answer(monkeypatch):
    # DNS answers with one public and one private record — reject (rebinding)
    monkeypatch.setattr(tools.socket, "getaddrinfo",
                        lambda host, port: [(2, 1, 6, "", ("93.184.216.34", 0)),
                                            (2, 1, 6, "", ("127.0.0.1", 0))])
    with pytest.raises(ValueError, match="non-public"):
        _assert_public_url("http://tricky.example/")


def test_accepts_public_hostname(monkeypatch):
    monkeypatch.setattr(tools.socket, "getaddrinfo",
                        lambda host, port: [(2, 1, 6, "", ("93.184.216.34", 0))])
    assert _assert_public_url("https://example.com/page") == "https://example.com/page"


def test_unresolvable_host_is_rejected(monkeypatch):
    def boom(host, port):
        raise OSError("no such host")
    monkeypatch.setattr(tools.socket, "getaddrinfo", boom)
    with pytest.raises(ValueError, match="resolve"):
        _assert_public_url("http://doesnotexist.example/")


def _realistic_gai(host, port):
    """Like the real resolver: IP literals resolve to themselves, hostnames to
    a public address."""
    import ipaddress
    try:
        ipaddress.ip_address(host)
        return [(2, 1, 6, "", (host, 0))]
    except ValueError:
        return [(2, 1, 6, "", ("93.184.216.34", 0))]


def test_redirect_to_private_address_is_rejected(monkeypatch, cfg):
    """Every redirect hop is re-validated: a public page 302ing to a private
    address must raise, not be followed."""
    monkeypatch.setattr(tools.socket, "getaddrinfo", _realistic_gai)

    def fake_get(url, timeout, follow_redirects, headers):
        assert follow_redirects is False  # manual hops only
        return httpx.Response(
            302, headers={"location": "http://127.0.0.1/admin"},
            request=httpx.Request("GET", url),
        )

    monkeypatch.setattr(tools.httpx, "get", fake_get)
    with pytest.raises(ValueError, match="non-public"):
        _get_following_redirects("http://example.com/start",
                                 {"User-Agent": "t"}, cfg)


def test_redirect_chain_capped(monkeypatch, cfg):
    monkeypatch.setattr(tools.socket, "getaddrinfo",
                        lambda host, port: [(2, 1, 6, "", ("93.184.216.34", 0))])

    def fake_get(url, timeout, follow_redirects, headers):
        return httpx.Response(
            301, headers={"location": "http://example.com/again"},
            request=httpx.Request("GET", url),
        )

    monkeypatch.setattr(tools.httpx, "get", fake_get)
    with pytest.raises(ValueError, match="redirects"):
        _get_following_redirects("http://example.com/loop", {}, cfg)
