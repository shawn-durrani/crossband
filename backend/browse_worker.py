"""Isolated page renderer for view_page (#138, third slice).

Runs as a SEPARATE PROCESS with a scrubbed environment: the parent
(backend/browse.py) passes exactly what a render needs on stdin and nothing
else - no provider keys, no memory or ingest tokens, no .env. To keep it that
way this file imports only the standard library and Playwright: importing the
backend package would run dotenv and pull the parent's secrets straight into
this process. Never add a backend import here.

Containment, per the issue:
- every request egresses through the vetting proxy passed in (the proxy
  resolves once and connects to the address it vetted, so subresources,
  iframes, scripts and redirects all face the same SSRF policy);
- WebRTC's non-proxied UDP is disabled (it would ignore the proxy);
- downloads are refused; Chromium's own sandbox stays on (no --no-sandbox);
- the profile is fresh per run (TMPDIR set by the parent, deleted after), so
  no cookies or state survive between views;
- the parent enforces the wall clock and kills this process at the deadline.

Protocol: one JSON object on stdin ->  one JSON object on stdout.
In:  {url, proxy, user_agent, nav_timeout_ms, settle_timeout_ms,
      max_text, max_links, max_shot_bytes?, proxy_user?, proxy_pass?}
Out: {final_url, title, text, links: [{t, h}, ...], shot_b64?}  or  {error}

shot_b64 (#149): a viewport PNG of what was rendered, base64, so the user
can SEE exactly what was viewed. Best-effort and bounded by
max_shot_bytes - a failed or oversized capture drops the field, never the
render.

proxy_user/proxy_pass (#148): the per-view key for the proxy's budgeted
view listener, so every connection this page load opens shares one
transfer budget. Chromium answers the listener's 407 challenge with them;
the header is hop-by-hop, so the site never sees it.
"""

import base64
import json
import sys


def render(req: dict) -> dict:
    from playwright.sync_api import sync_playwright

    proxy = {"server": req["proxy"]}
    if req.get("proxy_user"):
        proxy["username"] = req["proxy_user"]
        proxy["password"] = req.get("proxy_pass", "")
    with sync_playwright() as p:
        browser = p.chromium.launch(
            headless=True,
            proxy=proxy,
            args=[
                "--webrtc-ip-handling-policy=disable_non_proxied_udp",
                "--force-webrtc-ip-handling-policy=disable_non_proxied_udp",
                "--dns-prefetch-disable",
                "--disable-background-networking",
                "--no-first-run",
                "--no-default-browser-check",
            ])
        try:
            context = browser.new_context(
                user_agent=req["user_agent"],
                accept_downloads=False,
                viewport={"width": 1280, "height": 1600})
            page = context.new_page()
            page.set_default_timeout(req["nav_timeout_ms"])
            try:
                page.goto(req["url"], wait_until="domcontentloaded")
            except Exception as e:
                if page.url in ("about:blank", ""):
                    return {"error": f"navigation failed: {e.__class__.__name__}: {e}"}
                # A partial navigation still has a DOM worth reading.
            try:
                # Best-effort settle for app-style pages; a page that streams
                # forever is read as it stands when the budget runs out.
                page.wait_for_load_state("networkidle",
                                         timeout=req["settle_timeout_ms"])
            except Exception:
                pass
            links = page.evaluate(
                "() => Array.from(document.querySelectorAll('a[href]'))"
                ".map(a => ({t: (a.innerText || '').trim().slice(0, 80),"
                " h: a.href}))")
            seen, deduped = set(), []
            for l in links:
                h = l.get("h") or ""
                if h.startswith(("http://", "https://")) and h not in seen:
                    seen.add(h)
                    deduped.append(l)
            shot_b64 = None
            try:
                raw = page.screenshot(type="png")  # viewport, not full page
                if len(raw) <= int(req.get("max_shot_bytes") or 0):
                    shot_b64 = base64.b64encode(raw).decode()
            except Exception:
                pass  # a failed capture never costs the render
            return {
                "final_url": page.url,
                "title": page.title(),
                "text": page.evaluate(
                    "() => document.body ? document.body.innerText : ''"
                )[:req["max_text"]],
                "links": deduped[:req["max_links"]],
                "shot_b64": shot_b64,
            }
        finally:
            browser.close()


def main():
    try:
        req = json.loads(sys.stdin.read())
        out = render(req)
    except Exception as e:  # any escape becomes an honest, parseable error
        out = {"error": f"{e.__class__.__name__}: {e}"}
    print(json.dumps(out))


if __name__ == "__main__":
    main()
