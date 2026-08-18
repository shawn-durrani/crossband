"""view_page containment (#138 slice 3): registration gates, the scrubbed
worker environment, deadline kills, honest protocol errors, and - when a
local Chromium is installed - a real render through a live egress proxy.
The fake-worker tests need no Playwright at all."""

import asyncio
import json
import os
import stat
import sys

import pytest

from backend import browse, egress, tools, url_ledger
from backend.config import Settings


@pytest.fixture(autouse=True)
def _reset(monkeypatch):
    monkeypatch.setattr(browse, "_available", None)
    yield
    egress.set_proxy_url(None)
    egress.set_view_proxy_url(None)


def _cfg(**over):
    c = Settings().as_cfg()
    c.update(over)
    return c


# ---------- registration gates ----------

def test_not_offered_without_playwright(monkeypatch):
    monkeypatch.setattr(browse, "_available", False)
    egress.set_proxy_url("http://127.0.0.1:1")
    assert not browse.offered()
    names = [d["name"] for d in tools.tool_definitions(_cfg())]
    assert "view_page" not in names


def test_not_offered_without_the_proxy(monkeypatch):
    # Fail-closed: a renderer with no vetting proxy is never offered - a
    # rendered page runs attacker-supplied code and must not get a naked
    # network path.
    monkeypatch.setattr(browse, "_available", True)
    assert egress.proxy_url() is None
    assert not browse.offered()
    names = [d["name"] for d in tools.tool_definitions(_cfg())]
    assert "view_page" not in names


def test_offered_with_both(monkeypatch):
    monkeypatch.setattr(browse, "_available", True)
    egress.set_proxy_url("http://127.0.0.1:1")
    names = [d["name"] for d in tools.tool_definitions(_cfg())]
    assert "view_page" in names


def test_view_page_is_ledger_gated_and_a_ledger_source():
    assert "view_page" in tools._URL_LEDGER_GATED
    assert "view_page" in url_ledger.SOURCE_TOOLS


def test_render_refuses_without_proxy():
    with pytest.raises(ValueError, match="egress proxy"):
        browse.render("https://example.com/", _cfg())


# ---------- the worker protocol, with a fake worker ----------

def _fake_worker(tmp_path, monkeypatch, body):
    w = tmp_path / "fake_worker.py"
    w.write_text(body)
    w.chmod(w.stat().st_mode | stat.S_IEXEC)
    monkeypatch.setattr(browse, "_WORKER", w)
    egress.set_proxy_url("http://127.0.0.1:1")
    return w


def test_worker_env_is_built_by_inclusion(tmp_path, monkeypatch):
    """The worker must see only PATH/HOME/TMPDIR (+ optional browsers path):
    a secret added to the parent's environment can never leak by omission."""
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-test-not-real")
    monkeypatch.setenv("MEMORY_AUTH_TOKEN", "tok-not-real")
    _fake_worker(tmp_path, monkeypatch, (
        "import json, os, sys\n"
        "sys.stdin.read()\n"
        "print(json.dumps({'final_url': 'x', 'title': '', "
        "'text': ' '.join(sorted(os.environ)), 'links': []}))\n"))
    out = browse.render("https://example.com/", _cfg())
    seen = set(out["text"].split())
    assert "ANTHROPIC_API_KEY" not in seen
    assert "MEMORY_AUTH_TOKEN" not in seen
    assert {"PATH", "HOME", "TMPDIR"} <= seen


def test_render_routes_through_the_view_listener_with_a_fresh_key(
        tmp_path, monkeypatch):
    """#148: with the budgeted view listener published, the render goes to
    IT with a per-view key - and two renders never share a key, so their
    budgets never pool."""
    _fake_worker(tmp_path, monkeypatch, (
        "import json, sys\n"
        "req = json.loads(sys.stdin.read())\n"
        "print(json.dumps({'final_url': 'x', 'title': '', "
        "'text': json.dumps([req['proxy'], req.get('proxy_user'), "
        "req.get('proxy_pass')]), 'links': []}))\n"))
    egress.set_view_proxy_url("http://127.0.0.1:2")
    one = json.loads(browse.render("https://example.com/", _cfg())["text"])
    two = json.loads(browse.render("https://example.com/", _cfg())["text"])
    assert one[0] == "http://127.0.0.1:2" and one[1] == "view"
    assert len(one[2]) >= 32 and one[2] != two[2]  # a fresh secret per view


def test_render_keeps_the_plain_proxy_without_a_view_listener(
        tmp_path, monkeypatch):
    # budget off (or an older proxy): exactly the pre-#148 request shape
    _fake_worker(tmp_path, monkeypatch, (
        "import json, sys\n"
        "req = json.loads(sys.stdin.read())\n"
        "print(json.dumps({'final_url': 'x', 'title': '', "
        "'text': json.dumps([req['proxy'], req.get('proxy_user')]), "
        "'links': []}))\n"))
    assert egress.view_proxy_url() is None
    out = json.loads(browse.render("https://example.com/", _cfg())["text"])
    assert out == ["http://127.0.0.1:1", None]


def test_deadline_kills_a_hung_worker(tmp_path, monkeypatch):
    _fake_worker(tmp_path, monkeypatch,
                 "import time\ntime.sleep(120)\n")
    with pytest.raises(ValueError, match="stopped"):
        browse.render("https://example.com/", _cfg(browse_timeout_s=0.5))


def test_garbage_output_is_an_honest_error(tmp_path, monkeypatch):
    _fake_worker(tmp_path, monkeypatch,
                 "import sys\nsys.stdin.read()\nprint('not json at all')\n")
    with pytest.raises(ValueError, match="no usable result"):
        browse.render("https://example.com/", _cfg())


def test_worker_error_key_surfaces(tmp_path, monkeypatch):
    _fake_worker(tmp_path, monkeypatch, (
        "import json, sys\nsys.stdin.read()\n"
        "print(json.dumps({'error': 'navigation failed: boom'}))\n"))
    with pytest.raises(ValueError, match="could not render"):
        browse.render("https://example.com/", _cfg())


def test_view_page_output_keeps_links_within_the_cap(tmp_path, monkeypatch):
    """Links are the ledger's navigation feed: a huge page must not push
    them past the output cap."""
    links = [{"t": f"link {i}", "h": f"https://example.com/p{i}"} for i in range(10)]
    _fake_worker(tmp_path, monkeypatch, (
        "import json, sys\nsys.stdin.read()\n"
        "print(json.dumps({'final_url': 'https://example.com/big', "
        "'title': 'Big', 'text': 'word ' * 20000, 'links': " + json.dumps(links) + "}))\n"))
    monkeypatch.setattr(tools, "_assert_public_url", lambda u: u)
    out = tools.view_page({"url": "https://example.com/big"}, _cfg())
    assert len(out) <= _cfg()["max_tool_output"]
    assert "https://example.com/p9" in out  # the last link survived
    assert "…[truncated]" in out


# ---------- real render, when Chromium is present ----------

def _pw():
    try:
        import playwright  # noqa: F401
        return True
    except ImportError:
        return False


@pytest.mark.skipif(not _pw() or not browse._chromium_installed(),
                    reason="playwright + chromium not installed")
def test_real_render_through_a_live_proxy():
    """End to end: a JS-built page renders through the egress proxy. The
    hostname resolves only through the proxy's injected resolver, so a
    successful render proves the browser's traffic went through it. The
    proxy carries a page budget, so the render goes through the BUDGETED
    view listener - real Chromium answering the 407 challenge with the
    per-view key (#148), not just the fakes above."""
    async def main():
        async def handler(reader, writer):
            await reader.readuntil(b"\r\n\r\n")
            body = (b"<html><head><title>JS Page</title></head><body>"
                    b"<div id='out'>static-only</div>"
                    b"<a href='/next'>next page</a>"
                    b"<script>document.getElementById('out').textContent="
                    b"'rendered-by-script';</script></body></html>")
            writer.write(b"HTTP/1.1 200 OK\r\nContent-Type: text/html\r\n"
                         b"Content-Length: %d\r\nConnection: close\r\n\r\n"
                         % len(body) + body)
            await writer.drain()
            writer.close()

        server = await asyncio.start_server(handler, "127.0.0.1", 0)
        sport = server.sockets[0].getsockname()[1]
        proxy = egress.EgressProxy(
            max_transfer_bytes=1 << 20, politeness_s=0.0, idle_timeout_s=10,
            lifetime_s=30, page_budget_bytes=1 << 20,
            resolver=lambda h, p: [(2, 1, 6, "", ("127.0.0.1", 0))],
            address_policy=lambda ip: True, http_ports={sport})
        await proxy.start()
        egress.set_proxy_url(proxy.url)
        egress.set_view_proxy_url(proxy.view_url)
        try:
            out = await asyncio.to_thread(
                browse.render, f"http://viewtest.invalid:{sport}/page",
                _cfg(browse_timeout_s=30.0))
        finally:
            egress.set_proxy_url(None)
            egress.set_view_proxy_url(None)
            await proxy.stop()
            server.close()
        assert out["title"] == "JS Page"
        assert "rendered-by-script" in out["text"]  # JS actually executed
        assert "static-only" not in out["text"]
        assert any(l["h"].endswith("/next") for l in out["links"])
    asyncio.run(main())


# ---------- challenge detection (#150) ----------

def test_challenge_page_recognises_interstitials():
    # Cloudflare's actual shape from the first field test: tiny page,
    # "Just a moment..." title, challenge-platform resources.
    assert browse.challenge_page({
        "final_url": "https://claude.ai/share/abc?__cf_chl_tk=x",
        "title": "Just a moment...",
        "text": "claude.ai needs to review the security of your connection."})
    # a challenge-platform URL is decisive on its own
    assert browse.challenge_page({
        "final_url": "https://x.test/cdn-cgi/challenge-platform/h/b",
        "title": "", "text": ""})
    # body phrase on a tiny document
    assert browse.challenge_page({
        "final_url": "https://x.test/page", "title": "x",
        "text": "Verify you are human by completing the action below."})


def test_challenge_page_never_trips_on_real_pages():
    # an article ABOUT captchas: big body, ordinary URL and title
    assert not browse.challenge_page({
        "final_url": "https://blog.test/how-captchas-work",
        "title": "How CAPTCHAs work, and why 'verify you are human' fails",
        "text": ("verify you are human appears on many interstitials. "
                 + "analysis " * 900)})
    # an ordinary small page with none of the phrases
    assert not browse.challenge_page({
        "final_url": "https://x.test/about", "title": "About us",
        "text": "We make things."})


def test_view_page_refuses_a_challenge_with_one_clean_line(tmp_path, monkeypatch):
    """The interstitial's text and links never reach the transcript, and the
    Error prefix keeps every URL in the reply out of the seen-URL ledger."""
    _fake_worker(tmp_path, monkeypatch, (
        "import json, sys\nsys.stdin.read()\n"
        "print(json.dumps({"
        "'final_url': 'https://gated.test/x?__cf_chl_tk=abc',"
        "'title': 'Just a moment...',"
        "'text': 'checking your browser before accessing gated.test',"
        "'links': [{'t': 'challenge', 'h': 'https://gated.test/cdn-cgi/x'}]}))\n"))
    monkeypatch.setattr(tools, "_assert_public_url", lambda u: u)
    out = asyncio.run(tools.run_tool(
        "view_page", {"url": "https://gated.test/x"}, _cfg()))
    assert out.startswith("Error running view_page:")
    assert "paste the content" in out
    assert "gated.test" not in out          # no URL laundering, ever
    assert "cdn-cgi" not in out
    assert "checking your browser" not in out  # interstitial text dropped
