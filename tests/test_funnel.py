"""The Tailscale Funnel guard (#363).

Crossband is meant to be reached over your own tailnet and nowhere
else. Funnel publishes a served port to the public internet, one
command away from the `tailscale serve` command the docs give. Two
guards: the app asks Tailscale whether Funnel is on for its port and
serves nothing but a page that says so while it is; and a request on a
trusted host without the identity header Tailscale adds for tailnet
users is refused before the lock screen.
"""

import asyncio
import json

from fastapi.testclient import TestClient

from backend import db, funnel
from backend.app import create_app
from backend.config import Settings

TAILNET = "my-mac.my-tailnet.ts.net"
PUBLIC = f"{TAILNET}:443"


def _status(proxy="http://127.0.0.1:8902", funnel_on=True, foreground=False):
    cfg = {"TCP": {"443": {"HTTPS": True}},
           "Web": {PUBLIC: {"Handlers": {"/": {"Proxy": proxy}}}},
           "AllowFunnel": {PUBLIC: funnel_on}}
    if foreground:
        cfg = {"Foreground": {"1234": cfg}}
    return json.dumps(cfg)


def _app(tmp_path, **kw):
    return create_app(Settings(data_dir=str(tmp_path / "data"),
                               memory_url="http://127.0.0.1:1", **kw))


# ── reading what Tailscale says ─────────────────────────────────────────────

def test_funnel_on_for_this_port_names_the_public_address():
    assert funnel.funnel_exposes(_status(), 8902) == PUBLIC
    assert funnel.funnel_exposes(_status(proxy="localhost:8902/"), 8902) == PUBLIC
    assert funnel.funnel_exposes(_status(foreground=True), 8902) == PUBLIC


def test_funnel_off_or_for_another_port_is_no_exposure():
    assert funnel.funnel_exposes(_status(funnel_on=False), 8902) is None
    assert funnel.funnel_exposes(_status(proxy="http://127.0.0.1:8903"), 8902) is None
    assert funnel.funnel_exposes(_status(), 8903) is None


def test_anything_unreadable_is_no_exposure():
    for text in ("", None, "No serve config", "[]", "{}", '{"Web": null}'):
        assert funnel.funnel_exposes(text, 8902) is None


def test_no_tailscale_command_means_nothing_to_ask(monkeypatch):
    monkeypatch.setattr(funnel, "find_tailscale", lambda: None)
    assert funnel.check(8902) is None


def test_a_failing_command_is_no_exposure(monkeypatch):
    monkeypatch.setattr(funnel, "serve_status", lambda binary: None)
    assert funnel.check(8902, binary="/nowhere/tailscale") is None


# ── the backstop: identity on a trusted host ────────────────────────────────

def test_a_trusted_host_request_without_tailscale_identity_is_refused(tmp_path):
    app = _app(tmp_path, trusted_hosts=TAILNET)
    with TestClient(app, base_url=f"https://{TAILNET}") as c:
        assert c.get("/api/auth/session").status_code == 200   # a tailnet user
        c.headers.pop(funnel.IDENTITY_HEADER)                   # through Funnel
        r = c.get("/api/auth/session")
        assert r.status_code == 403
        assert "tailnet" in r.json()["detail"]
        assert c.get("/").status_code == 403                    # before any route


def test_loopback_never_needs_the_identity(tmp_path):
    app = _app(tmp_path, trusted_hosts=TAILNET)
    with TestClient(app, base_url="http://127.0.0.1") as c:
        c.headers.pop(funnel.IDENTITY_HEADER)
        assert c.get("/api/auth/session").status_code == 200


def test_the_identity_check_can_be_turned_off(tmp_path):
    app = _app(tmp_path, trusted_hosts=TAILNET,
               tailscale_identity_required=False)
    with TestClient(app, base_url=f"https://{TAILNET}") as c:
        c.headers.pop(funnel.IDENTITY_HEADER)
        assert c.get("/api/auth/session").status_code == 200


def _ws(app, host, headers=None):
    from types import SimpleNamespace
    return SimpleNamespace(app=app, url=SimpleNamespace(hostname=host),
                           headers=headers or {}, cookies={})


def test_websockets_hold_the_same_line(tmp_path, monkeypatch):
    """The HTTP middleware never sees a websocket, so the socket guard
    carries both checks itself: no socket while Funnel is on, and none on
    a trusted host without the tailnet identity."""
    from backend.routers.voice import _ws_local
    app = _app(tmp_path, trusted_hosts=TAILNET, funnel_check_s=0)
    with TestClient(app, base_url="http://127.0.0.1"):
        user = {funnel.IDENTITY_HEADER: "owner@example.com"}
        assert _ws_local(_ws(app, "127.0.0.1")) is True
        assert _ws_local(_ws(app, TAILNET)) is False               # via Funnel
        assert _ws_local(_ws(app, TAILNET, user)) is False        # tailnet, no session
        app.state.funnel_exposed = PUBLIC
        assert _ws_local(_ws(app, "127.0.0.1")) is False           # nothing served


# ── the loud guard: refuse to serve while Funnel is on ──────────────────────

def _funnel_is(monkeypatch, exposed):
    monkeypatch.setattr(funnel, "check", lambda port, binary=None: exposed)


def test_while_funnel_is_on_nothing_is_served_but_the_page(tmp_path, monkeypatch):
    _funnel_is(monkeypatch, PUBLIC)
    app = _app(tmp_path, trusted_hosts=TAILNET, funnel_check_s=3600)
    with TestClient(app, base_url="http://127.0.0.1") as c:
        r = c.get("/api/auth/session")
        assert r.status_code == 503 and r.json()["funnel"] == PUBLIC
        page = c.get("/")
        assert page.status_code == 503
        assert page.headers["content-type"].startswith("text/html")
        assert "Funnel" in page.text and PUBLIC in page.text
        assert "tailscale funnel reset" in page.text
        # loopback and the tailnet alike: the port is public either way
        tail = TestClient(app, base_url=f"https://{TAILNET}")
        assert tail.get("/api/auth/session").status_code == 503

        # Funnel turned off: the next check lets the app serve again
        _funnel_is(monkeypatch, None)
        asyncio.run(funnel.run_check(app))
        assert c.get("/api/auth/session").status_code == 200


def test_the_check_off_means_the_app_never_asks(tmp_path, monkeypatch):
    _funnel_is(monkeypatch, PUBLIC)
    app = _app(tmp_path, funnel_check_s=0)
    with TestClient(app, base_url="http://127.0.0.1") as c:
        assert c.get("/api/auth/session").status_code == 200


def test_one_chat_line_per_episode(tmp_path, monkeypatch):
    app = _app(tmp_path, funnel_check_s=0)
    with TestClient(app, base_url="http://127.0.0.1") as c:
        older = c.post("/api/chats", json={"participant_ids": []}).json()
        latest = c.post("/api/chats", json={"participant_ids": []}).json()

        def lines(chat_id):
            con = db.connect()
            try:
                return [r["content"] for r in con.execute(
                    "SELECT content FROM messages WHERE chat_id=? "
                    "AND speaker='system'", (chat_id,))]
            finally:
                con.close()

        _funnel_is(monkeypatch, PUBLIC)
        asyncio.run(funnel.run_check(app))
        asyncio.run(funnel.run_check(app))          # still on: no second line
        assert len(lines(latest["id"])) == 1 and lines(older["id"]) == []
        assert "Funnel" in lines(latest["id"])[0]
        _funnel_is(monkeypatch, None)
        asyncio.run(funnel.run_check(app))          # off again
        _funnel_is(monkeypatch, PUBLIC)
        asyncio.run(funnel.run_check(app))          # a new episode
        assert len(lines(latest["id"])) == 2
