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
      max_text, max_links}
Out: {final_url, title, text, links: [{t, h}, ...]}  or  {error}
"""

import json
import sys


def render(req: dict) -> dict:
    from playwright.sync_api import sync_playwright

    with sync_playwright() as p:
        browser = p.chromium.launch(
            headless=True,
            proxy={"server": req["proxy"]},
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
            return {
                "final_url": page.url,
                "title": page.title(),
                "text": page.evaluate(
                    "() => document.body ? document.body.innerText : ''"
                )[:req["max_text"]],
                "links": deduped[:req["max_links"]],
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
