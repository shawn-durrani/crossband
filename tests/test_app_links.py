"""The header's link row to the owner's other apps (workbench#100).

Three things are pinned here. The health route every sibling reads says
where a browser opens crossband. The probe of the siblings leaves out any
that don't answer, only ever talks to this machine, and asks at most once
a minute. The route that hands the list to the page stays behind the gate.
The rule for which link a page shows lives in frontend/src/appLinks.js and
its node suite.
"""

import asyncio
import time

import httpx
from fastapi.testclient import TestClient

from backend import app_links
from backend.app import create_app
from backend.config import DEFAULT_SIBLING_APPS, Settings, load_settings

TAILNET = "my-mac.my-tailnet.ts.net"


def _app(tmp_path, **kw):
    return create_app(Settings(data_dir=str(tmp_path / "data"),
                               memory_url="http://127.0.0.1:1", **kw))


def _session(app, base_url="http://127.0.0.1", **headers):
    c = TestClient(app, base_url=base_url)
    c.headers.update(headers)
    return c.get("/api/auth/session").json()


# ---- crossband's own address, on its health route ----

def test_health_route_says_crossband_answers_on_loopback_by_default(tmp_path):
    s = _session(_app(tmp_path))
    assert s["app"] == "crossband"
    assert s["browser_origin"] == "http://127.0.0.1:8902"


def test_a_trusted_host_makes_the_tailnet_name_the_address(tmp_path):
    app = _app(tmp_path, trusted_hosts=f"{TAILNET}, other.example")
    assert _session(app)["browser_origin"] == f"https://{TAILNET}"
    # The tailnet sees the same answer on its lock screen surface.
    tailnet = _session(app, f"https://{TAILNET}",
                       **{"Tailscale-User-Login": "owner@example.com"})
    assert tailnet["browser_origin"] == f"https://{TAILNET}"


def test_an_explicit_browser_origin_wins_and_a_bad_one_is_ignored(tmp_path):
    app = _app(tmp_path / "a", trusted_hosts=TAILNET,
               browser_origin=f"https://{TAILNET}:10443/")
    assert _session(app)["browser_origin"] == f"https://{TAILNET}:10443"
    app = _app(tmp_path / "b", browser_origin="javascript:alert(1)")
    assert _session(app)["browser_origin"] == "http://127.0.0.1:8902"


# ---- config ----

def test_siblings_default_to_the_fleet_and_env_can_empty_them(tmp_path):
    assert load_settings(tmp_path, environ={}).sibling_apps == DEFAULT_SIBLING_APPS
    off = load_settings(tmp_path, environ={"CROSSBAND_SIBLING_APPS": "{}"})
    assert off.sibling_apps == {}


# ---- the probe ----

def _transport(answers, calls):
    """A fake network: `answers` maps a URL to a response or an exception."""
    async def handle(request):
        url = str(request.url)
        calls.append(url)
        answer = answers[url]
        if isinstance(answer, Exception):
            raise answer
        if callable(answer):
            return await answer()
        return answer
    return httpx.MockTransport(handle)


SIBLINGS = {
    "membro": "http://127.0.0.1:8901/v1/health",
    "spendglass": "http://127.0.0.1:8903/api/session",
    "threadfold": "http://127.0.0.1:8904/health",
}


def test_siblings_that_dont_answer_are_left_out():
    calls = []
    answers = {
        SIBLINGS["membro"]: httpx.Response(200, json={
            "status": "ok", "browser_origin": f"https://{TAILNET}:8443"}),
        SIBLINGS["spendglass"]: httpx.ConnectError("refused"),
        SIBLINGS["threadfold"]: httpx.Response(500, text="broken"),
    }
    probe = app_links.SiblingProbe(SIBLINGS, transport=_transport(answers, calls))
    assert asyncio.run(probe.found()) == [
        {"name": "membro", "origin": f"https://{TAILNET}:8443",
         "local": "http://127.0.0.1:8901"}]


def test_an_older_sibling_without_the_field_still_opens_on_the_mac():
    calls = []
    answers = {SIBLINGS["threadfold"]: httpx.Response(200, json={"ok": True})}
    probe = app_links.SiblingProbe({"threadfold": SIBLINGS["threadfold"]},
                                   transport=_transport(answers, calls))
    assert asyncio.run(probe.found()) == [
        {"name": "threadfold", "origin": "", "local": "http://127.0.0.1:8904"}]


def test_a_reported_address_that_isnt_a_plain_origin_is_dropped():
    calls = []
    answers = {SIBLINGS["membro"]: httpx.Response(200, json={
        "browser_origin": "javascript:alert(1)"})}
    probe = app_links.SiblingProbe({"membro": SIBLINGS["membro"]},
                                   transport=_transport(answers, calls))
    assert asyncio.run(probe.found())[0]["origin"] == ""


def test_a_slow_sibling_is_left_out_within_the_timeout():
    calls = []

    async def slow():
        await asyncio.sleep(5)
        return httpx.Response(200, json={})
    probe = app_links.SiblingProbe(
        {"membro": SIBLINGS["membro"]}, timeout=0.2,
        transport=_transport({SIBLINGS["membro"]: slow}, calls))
    started = time.monotonic()
    assert asyncio.run(probe.found()) == []
    assert time.monotonic() - started < 2


def test_only_loopback_addresses_are_ever_probed():
    calls = []
    probe = app_links.SiblingProbe(
        {"elsewhere": "http://192.0.2.10:8901/v1/health",
         "tailnet": f"https://{TAILNET}:8443/v1/health"},
        transport=_transport({}, calls))
    assert asyncio.run(probe.found()) == []
    assert calls == []


def test_answers_are_kept_for_a_minute():
    calls = []
    now = [1000.0]
    answers = {SIBLINGS["membro"]: httpx.Response(200, json={"browser_origin": ""})}
    probe = app_links.SiblingProbe({"membro": SIBLINGS["membro"]},
                                   transport=_transport(answers, calls),
                                   clock=lambda: now[0])

    async def twice():
        await probe.found()
        now[0] += 59
        await probe.found()
    asyncio.run(twice())
    assert len(calls) == 1
    now[0] += 2
    asyncio.run(probe.found())
    assert len(calls) == 2


# ---- the route the page reads ----

def test_route_hands_the_page_what_answered(tmp_path):
    app = _app(tmp_path)
    calls = []
    answers = {SIBLINGS["membro"]: httpx.Response(200, json={
        "browser_origin": f"https://{TAILNET}:8443"})}
    app.state.sibling_probe = app_links.SiblingProbe(
        {"membro": SIBLINGS["membro"]}, transport=_transport(answers, calls))
    r = TestClient(app, base_url="http://127.0.0.1").get("/api/app-links")
    assert r.status_code == 200
    assert r.json() == {"app": "crossband", "apps": [
        {"name": "membro", "origin": f"https://{TAILNET}:8443",
         "local": "http://127.0.0.1:8901"}]}


def test_route_stays_behind_the_gate(tmp_path):
    app = _app(tmp_path, trusted_hosts=TAILNET, sibling_apps={})
    tailnet = TestClient(app, base_url=f"https://{TAILNET}")
    tailnet.headers["Tailscale-User-Login"] = "owner@example.com"
    assert tailnet.get("/api/app-links").status_code == 401
    owner = TestClient(app, base_url="http://127.0.0.1")
    assert owner.post("/api/auth/setup", json={
        "recovery_secret": app.state.recovery_secret,
        "password": "a-durable-owner-passphrase"}).status_code == 200
    assert owner.get("/api/app-links").status_code == 200
    assert TestClient(app, base_url="http://127.0.0.1"
                      ).get("/api/app-links").status_code == 401
