"""view_page manager (#138, third slice): rendered pages, contained.

The render itself happens in backend/browse_worker.py, a separate process
that holds nothing worth stealing: it is spawned with a SCRUBBED environment
(no .env, no provider keys, no memory or ingest tokens - the parent process
carries all of those and none are copied), runs the page behind the vetting
egress proxy, and is killed at a hard deadline. This module owns the spawn,
the protocol, the deadline, and the honest errors.

Fail-closed rule: no egress proxy, no browser. fetch_page may fall back to a
direct pre-flight-vetted fetch because it executes nothing; a rendered page
runs attacker-supplied code, so it never gets a network path that skips the
proxy. view_page simply does not register while the proxy is down.

Availability: the playwright package plus an installed Chromium
(`.venv/bin/playwright install chromium`, ~160MB, an operator step). Absent
either, the tool is not offered and models keep fetch_page.
"""

import json
import logging
import os
import secrets
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

from . import egress

log = logging.getLogger("crossband.browse")

_WORKER = Path(__file__).with_name("browse_worker.py")
# Shares of the browse_timeout_s budget: most on navigation, a slice on
# letting app-style pages settle, the rest is extraction + process overhead.
_NAV_SHARE, _SETTLE_SHARE = 0.6, 0.2
_MAX_LINKS = 40
_STDERR_LOG_CAP = 800
_MAX_SHOT_BYTES = 1_500_000  # a viewport PNG is typically well under this

_available: bool | None = None  # cached per process

# ---------- challenge detection (#150) ----------
#
# A human-verification interstitial ("Just a moment...") is not the page: its
# text is junk in the transcript and its links would launder challenge URLs
# into the seen-URL ledger. Signals are held tight so a real article ABOUT
# captchas never trips them: challenge-platform URL paths are decisive on
# their own; title and body phrases only count on the tiny documents these
# interstitials actually are.

_CHALLENGE_URL_BITS = ("/cdn-cgi/challenge-platform", "__cf_chl",
                       "challenge_redirect", "/captcha")
_CHALLENGE_TITLES = ("just a moment", "checking your browser",
                     "attention required", "verify you are human",
                     "verifying you are human", "security check")
_CHALLENGE_PHRASES = ("verify you are human", "checking your browser",
                      "enable javascript and cookies to continue",
                      "review the security of your connection",
                      "complete the action below to continue")
_CHALLENGE_MAX_CHARS = 4000  # real pages dwarf this; interstitials never do


def challenge_page(out: dict) -> bool:
    """Is this render a verification interstitial rather than the page
    itself? Pure; drives the one-line refusal in tools.view_page."""
    url = (out.get("final_url") or "").casefold()
    if any(b in url for b in _CHALLENGE_URL_BITS):
        return True
    text = (out.get("text") or "").strip().casefold()
    if len(text) > _CHALLENGE_MAX_CHARS:
        return False
    title = (out.get("title") or "").strip().casefold()
    return (any(t in title for t in _CHALLENGE_TITLES)
            or any(p in text for p in _CHALLENGE_PHRASES))


def _chromium_installed() -> bool:
    """True when Playwright's Chromium build exists on disk. Checked without
    starting a Playwright driver: the registry directory is enough to gate
    REGISTRATION - a stale hit still fails honestly at render time."""
    root = os.environ.get("PLAYWRIGHT_BROWSERS_PATH")
    if not root:
        home = Path.home()
        root = (home / "Library" / "Caches" / "ms-playwright" if sys.platform == "darwin"
                else home / ".cache" / "ms-playwright")
    try:
        return any(Path(root).glob("chromium*/"))
    except OSError:
        return False


def available() -> bool:
    global _available
    if _available is None:
        try:
            import playwright  # noqa: F401
        except ImportError:
            _available = False
        else:
            _available = _chromium_installed()
    return _available


def offered() -> bool:
    """Registration gate: renderer installed AND the egress proxy is up."""
    return available() and egress.proxy_url() is not None


def _worker_env(profile_dir: str) -> dict:
    """The whole environment the worker gets. Built by inclusion, never by
    filtering, so a new secret in the parent's env can never leak by being
    missed on a denylist."""
    env = {
        "PATH": "/usr/bin:/bin",
        "HOME": os.environ.get("HOME", ""),
        "TMPDIR": profile_dir,
    }
    if os.environ.get("PLAYWRIGHT_BROWSERS_PATH"):
        env["PLAYWRIGHT_BROWSERS_PATH"] = os.environ["PLAYWRIGHT_BROWSERS_PATH"]
    return env


def render(url: str, cfg) -> dict:
    """Render one page in the contained worker. Returns the worker's result
    dict; raises ValueError with an honest message on any failure."""
    proxy = egress.proxy_url()
    if not proxy:
        raise ValueError(
            "the egress proxy is not running, so rendered viewing is "
            "unavailable (fetch_page still works)")
    from .tools import USER_AGENT  # lazy: tools imports browse lazily too
    timeout = float(cfg["browse_timeout_s"])
    req = {
        "url": url,
        "proxy": proxy,
        "user_agent": USER_AGENT,
        "nav_timeout_ms": int(timeout * _NAV_SHARE * 1000),
        "settle_timeout_ms": int(timeout * _SETTLE_SHARE * 1000),
        "max_text": int(cfg["max_tool_output"]) * 4,  # pre-format slack; the tool caps the final text
        "max_links": _MAX_LINKS,
        # #149: viewport screenshot budget. A capture bigger than this is
        # dropped by the worker; the render itself is never affected.
        "max_shot_bytes": _MAX_SHOT_BYTES,
    }
    # #148: when the proxy's budgeted view listener is up, the render goes
    # through IT with a fresh per-view key, so this page load's many
    # connections share one transfer budget. No listener (budget off, or an
    # older proxy) keeps the plain per-connection path.
    view_proxy = egress.view_proxy_url()
    if view_proxy:
        req["proxy"] = view_proxy
        req["proxy_user"] = "view"
        req["proxy_pass"] = secrets.token_hex(16)
    profile_dir = tempfile.mkdtemp(prefix="crossband-browse-")
    try:
        proc = subprocess.run(
            [sys.executable, str(_WORKER)],
            input=json.dumps(req).encode(),
            capture_output=True,
            env=_worker_env(profile_dir),
            timeout=timeout + 5,  # the worker's own budgets end first
        )
    except subprocess.TimeoutExpired:
        raise ValueError(
            f"page render exceeded {timeout:.0f}s and was stopped")
    finally:
        shutil.rmtree(profile_dir, ignore_errors=True)
    if proc.stderr:
        log.debug("browse worker stderr: %s",
                  proc.stderr[:_STDERR_LOG_CAP].decode("utf-8", "replace"))
    try:
        out = json.loads(proc.stdout.decode("utf-8", "replace").strip() or "{}")
    except ValueError:
        raise ValueError("page renderer returned no usable result")
    if not isinstance(out, dict) or (not out.get("error") and "text" not in out):
        raise ValueError("page renderer returned no usable result")
    if out.get("error"):
        raise ValueError(f"could not render the page: {out['error']}")
    return out
