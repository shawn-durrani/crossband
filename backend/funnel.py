"""The Tailscale Funnel guard (#363).

Crossband is meant to be reached over your own tailnet and nowhere else.
Funnel is the Tailscale feature that publishes a served port to the
public internet, one command away from the `tailscale serve` command
the remote-access page tells you to run. Until now the only guard was
a sentence in the docs. Two guards here:

- `run_check` asks Tailscale whether Funnel is on for this app's port
  (`tailscale serve status --json`), at startup and every few minutes.
  While it is, the app serves nothing but a page that says why, and
  posts one line in chat. Turn Funnel off and the app resumes on the
  next check.
- `outside_the_tailnet` is the backstop for the minute between Funnel
  going on and the next check. Tailscale adds identity headers to every
  request it proxies for a tailnet user; a request on a trusted host
  without them came from the public internet, and is refused before the
  lock screen and any route. Loopback carries no proxy headers and is
  never asked.
"""

import asyncio
import html
import json
import logging
import os
import shutil
import subprocess

from fastapi.responses import HTMLResponse, JSONResponse

from . import db

log = logging.getLogger("crossband.funnel")

# The Mac App Store build keeps the command inside the app bundle, off PATH.
TAILSCALE_BIN_MAC = "/Applications/Tailscale.app/Contents/MacOS/Tailscale"
# The header Tailscale serve adds for a tailnet user. Its siblings are
# Tailscale-User-Name and Tailscale-User-Profile-Pic; one is enough.
IDENTITY_HEADER = "tailscale-user-login"
STATUS_TIMEOUT_S = 10.0


def find_tailscale() -> str | None:
    found = shutil.which("tailscale")
    if found:
        return found
    return TAILSCALE_BIN_MAC if os.path.exists(TAILSCALE_BIN_MAC) else None


def serve_status(binary: str) -> str | None:
    """What `tailscale serve status --json` prints, or None when the command
    fails or hangs. A missing daemon is a failure, never an exposure."""
    try:
        r = subprocess.run([binary, "serve", "status", "--json"],
                           capture_output=True, text=True,
                           timeout=STATUS_TIMEOUT_S)
    except (OSError, subprocess.SubprocessError):
        return None
    return r.stdout if r.returncode == 0 else None


def _targets_port(proxy: str, port: int) -> bool:
    """Does a serve handler's Proxy target ("http://127.0.0.1:8902",
    "localhost:8902/") name this port?"""
    hostport = proxy.split("://", 1)[-1].split("/", 1)[0]
    return hostport.rsplit(":", 1)[-1] == str(port)


def funnel_exposes(status_text, port: int) -> str | None:
    """The public address Funnel serves this app's port on, or None.

    Reads the serve config Tailscale prints: `AllowFunnel` names the
    host:port pairs Funnel is on for, and `Web` says what each one proxies
    to. A foreground serve keeps its own copy under `Foreground`. Anything
    unreadable reads as no exposure: the guard never blocks on a parse."""
    try:
        cfg = json.loads(status_text or "")
    except ValueError:
        return None
    if not isinstance(cfg, dict):
        return None
    configs = [cfg] + [c for c in (cfg.get("Foreground") or {}).values()
                       if isinstance(c, dict)]
    for c in configs:
        allow = c.get("AllowFunnel") or {}
        web = c.get("Web") or {}
        for hostport, on in allow.items():
            if not on:
                continue
            handlers = (web.get(hostport) or {}).get("Handlers") or {}
            for h in handlers.values():
                if _targets_port(str((h or {}).get("Proxy") or ""), port):
                    return hostport
    return None


def check(port: int, binary: str | None = None) -> str | None:
    """One check: the address Funnel exposes this port on, or None. With
    no `tailscale` command on the machine there is nothing to ask."""
    binary = binary or find_tailscale()
    if not binary:
        return None
    return funnel_exposes(serve_status(binary), port)


def refusal_text(exposed: str) -> str:
    return (f"Crossband has stopped serving because Tailscale Funnel is on "
            f"for {exposed}, which puts this app on the public internet. "
            f"Turn Funnel off (tailscale funnel reset), set serve up again "
            f"as docs/REMOTE_ACCESS.md says, and the app resumes on its own "
            f"within a few minutes.")


def refusal(request, exposed: str):
    """The one response served while Funnel is on: JSON on the API, a
    plain page everywhere else."""
    text = refusal_text(exposed)
    if request.url.path.startswith("/api/"):
        return JSONResponse(status_code=503,
                            content={"detail": text, "funnel": exposed})
    body = ("<!doctype html><title>Crossband is not serving</title>"
            "<main style=\"font:16px/1.5 system-ui,sans-serif;max-width:40rem;"
            "margin:4rem auto;padding:0 1rem\">"
            "<h1>Crossband has stopped serving</h1>"
            f"<p>{html.escape(text)}</p></main>")
    return HTMLResponse(body, status_code=503)


def outside_the_tailnet(request, loopback_hosts) -> bool:
    """A request on a trusted (non-loopback) host that lacks the identity
    header Tailscale adds for tailnet users. Through Funnel, Tailscale
    proxies the request the same way but adds no identity."""
    host = (request.url.hostname or "").lower()
    if host in loopback_hosts:
        return False
    return not request.headers.get(IDENTITY_HEADER)


def post_chat_line(exposed: str) -> None:
    """One system line in the chat the owner used last, so the episode is
    on the record once the app is back."""
    con = db.connect()
    try:
        row = con.execute(
            "SELECT id FROM chats WHERE archived_at IS NULL "
            "ORDER BY updated_at DESC LIMIT 1").fetchone()
        if row:
            db.insert_message(con, row["id"], "system", refusal_text(exposed))
            con.commit()
    finally:
        con.close()


async def run_check(app) -> None:
    """One check against the running app: records the answer on
    `app.state.funnel_exposed`, and on the way in posts the chat line."""
    port = app.state.settings.port
    exposed = await asyncio.to_thread(check, port)
    was = getattr(app.state, "funnel_exposed", None)
    app.state.funnel_exposed = exposed
    if exposed and not was:
        log.warning("Tailscale Funnel is on for %s - refusing to serve "
                    "until it is off", exposed)
        try:
            await asyncio.to_thread(post_chat_line, exposed)
        except Exception:
            log.exception("could not post the Funnel line in chat")
    elif was and not exposed:
        log.warning("Tailscale Funnel is off again - serving resumes")


async def loop(app, interval_s: float) -> None:
    while True:
        await asyncio.sleep(interval_s)
        try:
            await run_check(app)
        except Exception:
            log.exception("Funnel check failed")
